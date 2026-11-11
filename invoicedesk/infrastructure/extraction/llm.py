"""Optional real extraction; fixtures are available only in explicit cache mode."""
import json
import os
import re
from time import perf_counter
from pathlib import Path
from typing import Any
from dataclasses import dataclass
from invoicedesk.domain.contracts import canonical, fingerprint
from .prompt import EXTRACTION_PROMPT, PROMPT_VERSION, PREPROCESSING_VERSION
from pydantic import ValidationError
from invoicedesk.infrastructure.documents import inspect_document
from invoicedesk.infrastructure.telemetry.logger import get_logger
from invoicedesk.infrastructure.telemetry.metrics import token_usage
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.policy import ROOT
from invoicedesk.domain.validation import parse_response
from .providers import ExtractionProvider, ProviderUnavailable, load_provider

logger = get_logger(__name__)
OUTPUT_FIELDS = set(ExtractedInvoice.model_fields) - {'field_quality', 'extraction_path', 'warnings'}

class LlmUnavailable(Exception):
    """Extraction could not complete; this is not confirmation of missing fields."""


def validate_response(result: dict[str, Any]) -> dict[str, Any]:
    # An omitted field means incomplete tool output, not a confirmed null.
    if not isinstance(result, dict):
        raise LlmUnavailable('Extraction response must be a JSON object.')
    required = {'vendor_name', 'invoice_number', 'invoice_date', 'total'}
    if not required <= result.keys():
        raise LlmUnavailable('Extraction response omitted required keys.')
    for key in ('subtotal', 'tax_amount', 'total'):
        if isinstance(result.get(key), float):
            raise LlmUnavailable('Model amounts must be decimal strings, not floating-point numbers.')
    items = result.get('line_items', [])
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and any(isinstance(item.get(key), float) for key in ('quantity', 'unit_price', 'amount')):
                raise LlmUnavailable('Model line-item amounts must be decimal strings.')
    clean = {key: value for key, value in result.items() if key in OUTPUT_FIELDS}
    return ExtractedInvoice.model_validate(clean).model_dump(mode='json', include=OUTPUT_FIELDS)



@dataclass(frozen=True)
class ExtractionResult:
    facts_json: str
    raw_response_json: str
    issues_json: str
    provided_fields: frozenset[str]
    provided_paths: frozenset[str]
    evidence_json: str
    usage_json: str
    response_ms: float
    api_calls: int
    cache_hit: bool
    provenance: str
    fingerprint: str
    resolved_model: str = ''
    provider: str = ''

    @property
    def facts(self) -> dict:
        return json.loads(self.facts_json)


def recording_matches_model(stored: dict, requested: str) -> bool:
    """Legacy recordings predate requested_model; retain their exact snapshot identity."""
    resolved = stored.get('model', '')
    if not isinstance(resolved, str):
        return False
    if stored.get('requested_model', resolved) == requested or resolved == requested:
        return True
    return ('requested_model' not in stored and resolved.startswith(requested+'-')
            and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', resolved[len(requested)+1:])))


class LlmClient:
    def __init__(self, mode: str | None = None, cache_dir: Path | None = None, *,
                 provider: str | ExtractionProvider | None = None, model: str | None = None, refresh: bool = False, on_provider_call=None):
        self.mode = mode or os.getenv('LLM_MODE', 'off')
        self.provider = provider if provider is not None else os.getenv('LLM_PROVIDER', 'anthropic')
        self.provider_name = self.provider if isinstance(self.provider, str) else self.provider.name
        self.model = model if model is not None else os.getenv('LLM_MODEL', '')
        self.resolved_model = self.model
        self.refresh = refresh
        self.on_provider_call = on_provider_call
        if refresh and self.mode != 'live':
            raise ValueError('Refreshing extraction requires live mode.')
        if self.mode not in {'live', 'cache', 'off'}:
            raise ValueError('LLM_MODE must be live, cache, or off')
        # Customer responses belong outside the committed demo fixture directory.
        self.cache_dir = cache_dir or (Path(os.environ['LLM_CACHE_DIR']) if os.getenv('LLM_CACHE_DIR') and self.mode != 'cache' else None) or (ROOT/'.cache/llm' if self.mode == 'cache' else ROOT/'data/extraction-cache')
        self.failure_kind: str | None = None
        self.raw_response_json = '{}'
        self.fingerprint = ''
        self.calls = 0
        self.cache_hit = False
        self.provenance: str | None = None
        self.usage: dict[str, Any] = {}
        self.response_ms = 0.0
        self.validation_issues: list[dict[str, str]] = []
        self.provided_fields: set[str] = set()
        self.provided_paths: set[str] = set()

    @property
    def token_usage(self) -> dict[str, int | None]:
        return token_usage(self.usage, self.calls)

    def _accept_response(self, raw: dict[str, Any]) -> dict[str, Any]:
        self.raw_response_json = canonical(raw)
        parsed = parse_response(raw)
        self.validation_issues = parsed.issues
        self.provided_fields = parsed.provided_fields
        self.provided_paths = parsed.provided_paths
        for issue in parsed.issues:
            logger.warning('Partial extraction: %s (%s); other fields retained.', issue['field'], issue['type'])
        return parsed.invoice.model_dump(mode='json')

    def extract_invoice(self, path: Path) -> dict[str, Any]:
        self.calls = 0
        self.cache_hit = False
        self.provenance = None
        self.usage = {}
        self.response_ms = 0.0
        self.validation_issues = []
        self.provided_fields = set()
        self.provided_paths = set()
        self.failure_kind = None
        if self.mode == 'off':
            self.failure_kind = 'configuration'
            raise LlmUnavailable('LLM is off. Configure live extraction for unfamiliar layouts and images.')
        document = inspect_document(path)
        schema = extraction_schema()
        self.fingerprint = fingerprint(dict(document=document.sha256, preprocessing=PREPROCESSING_VERSION,
            provider=self.provider_name, model=self.model, prompt=EXTRACTION_PROMPT, schema=schema,
            settings=provider_settings(self.provider_name)))
        cache = self.cache_dir/((document.sha256 if self.mode == 'cache' else self.fingerprint)+'.json')
        # Live uploads are fresh extraction attempts. Recordings are retained for
        # audit, but only explicit offline replay may read them.
        if self.mode == 'cache' and cache.exists() and not self.refresh:
            try:
                stored = json.loads(cache.read_text())
                if not isinstance(stored, dict):
                    raise ValueError('Cache envelope must be a JSON object.')
                result = self._accept_response(stored['extraction'])
                for issue in stored.get('validation_issues', []):
                    if isinstance(issue, dict) and issue.get('type') == 'model_snapshot_mismatch':
                        continue  # Historical transport metadata is not a document-field error.
                    if isinstance(issue, dict) and {'field','type','message'} <= issue.keys() and issue not in self.validation_issues:
                        self.validation_issues.append(issue)
                        result['warnings'].append('Partial extraction: '+str(issue['field'])+' ('+str(issue['type'])+')')
                self.fingerprint = stored.get('fingerprint', self.fingerprint)
                self.resolved_model = stored.get('model', self.model)
                self.cache_hit = True
                self.provenance = stored.get('provenance', 'unknown')
                logger.debug('Extraction cache hit; provenance=%s', self.provenance)
                return result
            except (OSError, ValueError, KeyError, TypeError, LlmUnavailable):
                logger.warning('Ignoring invalid extraction cache entry.')
        if self.mode == 'cache':
            raise LlmUnavailable('No valid cached extraction for this document.')
        if not self.model:
            raise LlmUnavailable('Set LLM_MODEL or pass --model for live extraction.')
        started = perf_counter()
        result = None
        try:
            adapter = load_provider(self.provider) if isinstance(self.provider, str) else self.provider
            logger.debug('Requesting extraction from provider=%s model=%s', self.provider_name, self.model)
            started = perf_counter()
            if self.on_provider_call is not None:
                self.on_provider_call()
            response = adapter.extract(path, model=self.model, schema=schema)
            self.resolved_model = response.resolved_model or self.model
            self.calls = response.api_calls
            self.response_ms = (response.response_ms if response.response_ms is not None else (perf_counter()-started)*1000) if self.calls else 0.0
            self.usage = response.usage
            result = response.extraction
            validated = self._accept_response(result)
            self.provenance = 'api_recording_partial' if self.validation_issues else 'api_recording'
        except ValidationError as exc:
            errors = [
                {'field': '.'.join(str(part) for part in error['loc']),
                 'type': error['type'], 'message': error['msg']}
                for error in exc.errors(include_input=False, include_context=False, include_url=False)
            ]
            detail = '; '.join(f"{error['field']} ({error['type']})" for error in errors)
            logger.warning('Extraction response failed validation: %s', detail)
            self.provenance = 'api_recording_invalid'
            diagnostic = self.cache_dir/'failed'/(document.sha256+'.json')
            try:
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(json.dumps({
                    'provenance': self.provenance, 'provider': self.provider_name, 'model': self.resolved_model,
                    'extractor_version': 6, 'usage': self.usage, 'api_response_ms': self.response_ms,
                    'extraction': result, 'validation_errors': errors,
                }, indent=2))
                diagnostic.chmod(0o600)
                logger.info('Invalid extraction saved for local diagnosis: %s', diagnostic)
            except OSError:
                logger.warning('Could not save the invalid extraction diagnostic.')
            raise LlmUnavailable('Extracted data failed validation: '+detail) from exc
        except ProviderUnavailable as exc:
            self.failure_kind = exc.category
            self.calls = exc.api_calls
            self.usage = exc.usage
            self.response_ms = (exc.response_ms if exc.response_ms is not None else (perf_counter()-started)*1000) if self.calls else 0.0
            raise LlmUnavailable(str(exc)) from exc
        except LlmUnavailable:
            raise
        except Exception as exc:
            # Do not expose credentials, raw responses or private document contents in logs.
            raise LlmUnavailable(f'Live extraction unavailable ({type(exc).__name__}).') from exc
        finally:
            if self.calls:
                counts = self.token_usage
                logger.info(
                    'API metrics: provider=%s model=%s response_time_ms=%.2f %s',
                    self.provider_name, self.model, self.response_ms,
                    ' '.join(f'{key}={value if value is not None else "unknown"}' for key, value in counts.items()),
                )
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({
                'provenance': self.provenance, 'provider': self.provider_name, 'model': self.resolved_model,
                'document_sha256': document.sha256, 'requested_model': self.model, 'extractor_version': PROMPT_VERSION, 'fingerprint': self.fingerprint,
                'preprocessing_version': PREPROCESSING_VERSION, 'settings': provider_settings(self.provider_name),
                'usage': self.usage, 'extraction': result,
                'validation_issues': self.validation_issues,
            }, indent=2))
            cache.chmod(0o600)
        except OSError:
            logger.warning('Extraction succeeded but its cache could not be saved.')
        return validated

    def extract_result(self, path: Path) -> ExtractionResult:
        """Explicit per-call result. Compatibility properties remain for old reports only."""
        scoped = LlmClient(self.mode, self.cache_dir, provider=self.provider, model=self.model, refresh=self.refresh, on_provider_call=self.on_provider_call)
        try:
            facts = scoped.extract_invoice(path)
        except LlmUnavailable:
            # Preserve failure accounting in the legacy report facade as well.
            for name in ('failure_kind', 'calls', 'response_ms', 'usage', 'provenance', 'validation_issues', 'provided_fields', 'provided_paths', 'raw_response_json', 'fingerprint', 'resolved_model'):
                setattr(self, name, getattr(scoped, name))
            raise
        return ExtractionResult(canonical(facts), scoped.raw_response_json, canonical(scoped.validation_issues),
            frozenset(scoped.provided_fields), frozenset(scoped.provided_paths),
            canonical(facts.get('field_evidence', [])), canonical(scoped.usage), scoped.response_ms,
            scoped.calls, scoped.cache_hit, scoped.provenance or 'unknown', scoped.fingerprint, scoped.resolved_model, scoped.provider_name)


def provider_settings(provider: str) -> dict:
    return {'max_output_tokens': 8192, 'temperature': 0} if provider == 'anthropic' else {'max_output_tokens': 8192}


def extraction_schema() -> dict:
    schema = ExtractedInvoice.model_json_schema()
    schema['properties'] = {k: v for k, v in schema['properties'].items() if k in OUTPUT_FIELDS}
    schema['required'] = sorted(OUTPUT_FIELDS)
    schema['additionalProperties'] = False
    return schema
