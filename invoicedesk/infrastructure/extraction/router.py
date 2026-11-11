"""Extract every live document with a model; independently verify printed facts."""
from pathlib import Path
from dataclasses import dataclass
import json
from invoicedesk.domain.contracts import canonical
from invoicedesk.infrastructure.documents import inspect_document
from invoicedesk.domain.models import ExtractedInvoice, Quality
from invoicedesk.policy import Policy
from .text import read_document_text
from .tier1 import parse
from invoicedesk.domain.reconciliation import reconcile
from invoicedesk.domain.normalize import currency_code
from .grounding import ground
from .llm import LlmClient, LlmUnavailable

META_FIELDS = {'field_evidence', 'field_quality', 'evidence', 'extraction_path', 'warnings'}


def merge_extraction(inv: ExtractedInvoice, other: ExtractedInvoice, *, provided_fields: set[str], invalid_fields: set[str], text: str, pages: list[str]) -> ExtractedInvoice:
    # The model reads the complete document and owns facts. The label parser is
    # only an independent cross-check; it must never overwrite model output.
    ground(other, text, pages=pages)
    for key in set(ExtractedInvoice.model_fields) - provided_fields - META_FIELDS:
        other.field_quality.pop(key, None)
    for key in invalid_fields:
        if key in ExtractedInvoice.model_fields:
            other.field_quality[key] = Quality.UNVERIFIED
    scalar_checks = {'vendor_name', 'invoice_number', 'invoice_date', 'po_reference',
                     'currency', 'subtotal', 'tax_amount', 'total'}
    for key in scalar_checks:
        old, new = getattr(inv, key), getattr(other, key)
        if old is None or new is None or old == new:
            continue
        if key == 'currency' and currency_code(old) == currency_code(new):
            continue
        if isinstance(old, str) and isinstance(new, str):
            from invoicedesk.domain.normalize import key as identifier_key
            if identifier_key(old) == identifier_key(new):
                continue
        # Grounded monetary facts can resolve the narrow parser. Conflicting
        # labelled identifiers still require review even if both occur in text.
        if key in {'invoice_number', 'po_reference'} or other.field_quality.get(key) != Quality.GROUNDED:
            other.warnings.append(f'Tiers disagree: {key}')
            other.field_quality[key] = Quality.UNVERIFIED
    other.warnings.extend(inv.warnings)
    return other



def extract(path: Path, policy: Policy, llm: LlmClient) -> tuple[ExtractedInvoice, list[dict]]:
    document = inspect_document(path)
    text = ''
    pages = []
    needs_vision = not document.is_pdf
    read_warning = None
    if document.is_pdf or llm.mode == 'live':
        try:
            source = read_document_text(path)
            text = source.text
            pages = source.pages
            needs_vision = not document.is_pdf or source.needs_vision
        except Exception:
            # A text reader failure can still be recovered by the PDF-capable model.
            needs_vision = True
            read_warning = 'PDF text reader could not read the document; vision extraction required.'
    inv = parse(text) if text.strip() else ExtractedInvoice()
    findings = reconcile(inv, policy.extraction.reconcile_abs_tolerance)
    ready = (
        not needs_vision
        and all(getattr(inv, key) is not None for key in policy.critical_fields)
        and any(finding['scope'] == 'total' for finding in findings)
        and all(finding['passed'] for finding in findings)
    )
    # Explicit offline/demo profiles preserve reproducible parser fixtures.
    # Production live extraction always invokes the provider, including clean PDFs.
    if ready and llm.mode != 'live':
        for key in ExtractedInvoice.model_fields:
            if key not in META_FIELDS:
                if getattr(inv, key) is not None and getattr(inv, key) != []:
                    inv.field_quality[key] = Quality.VERIFIED
                    llm.provided_fields.add(key)
        def record_paths(value, prefix=''):
            if isinstance(value, dict):
                for key, part in value.items():
                    if part is not None:
                        path = f'{prefix}.{key}' if prefix else key
                        llm.provided_paths.add(path)
                        record_paths(part, path)
            elif isinstance(value, list):
                for index, row in enumerate(value):
                    record_paths(row, f'{prefix}.{index}')
        record_paths(inv.model_dump(mode='json'))
        if not inv.currency:
            inv.warnings.append('Currency was not explicitly identified; confirm it before payment.')
        return inv, findings
    try:
        result = llm.extract_result(path)
        # Only this legacy adapter copies result data onto the historical client report interface.
        llm.validation_issues = json.loads(result.issues_json)
        llm.provided_fields = set(result.provided_fields)
        llm.provided_paths = set(result.provided_paths)
        llm.provenance, llm.cache_hit = result.provenance, result.cache_hit
        llm.calls, llm.response_ms = result.api_calls, result.response_ms
        llm.usage = json.loads(result.usage_json)
        llm.raw_response_json, llm.fingerprint = result.raw_response_json, result.fingerprint
        llm.resolved_model = result.resolved_model
        other = ExtractedInvoice.model_validate(result.facts)
        inv = merge_extraction(inv,other,provided_fields=llm.provided_fields,invalid_fields={issue['field'].split('.')[0] for issue in llm.validation_issues},text=text,pages=pages)
        inv.extraction_path = 'llm_vision' if needs_vision else 'llm_text'
        findings = reconcile(inv, policy.extraction.reconcile_abs_tolerance)
        if not inv.currency:
            inv.warnings.append('Currency was not explicitly identified; confirm it before payment.')
    except (LlmUnavailable, ValueError) as exc:
        inv.extraction_path = 'failed'
        if read_warning:
            inv.warnings.append(read_warning)
        inv.warnings.append(str(exc))
        for key in policy.critical_fields:
            inv.field_quality[key] = Quality.UNVERIFIED
    return inv, findings


# Primary application contract. The tuple-returning extract above is retained for Section 5 callers.


@dataclass(frozen=True)
class DocumentExtraction:
    invoice_json: str
    findings_json: str
    sections_json: str
    raw_response_json: str
    issues_json: str
    usage_json: str
    token_usage_json: str
    provenance: str
    fingerprint: str
    response_ms: float
    calls: int
    cache_hit: bool
    failure_kind: str | None
    model: str
    provider: str

    @property
    def invoice(self):
        return ExtractedInvoice.model_validate_json(self.invoice_json)

    @property
    def findings(self):
        return json.loads(self.findings_json)

    @property
    def sections(self):
        return json.loads(self.sections_json)

    @property
    def usage(self):
        return json.loads(self.usage_json)

    @property
    def validation_issues(self):
        return json.loads(self.issues_json)

    @property
    def token_usage(self):
        return json.loads(self.token_usage_json)


def extract_document(path: Path, policy: Policy, client: LlmClient) -> DocumentExtraction:
    from .sections import section_report
    scoped = LlmClient(client.mode, client.cache_dir, provider=client.provider, model=client.model,
                       refresh=client.refresh, on_provider_call=client.on_provider_call)
    inv, findings = extract(path, policy, scoped)
    sections = section_report(inv, scoped.validation_issues, scoped.provided_fields, findings, scoped.provided_paths)
    return DocumentExtraction(inv.model_dump_json(), canonical(findings), canonical(sections),
        scoped.raw_response_json if scoped.provenance else inv.model_dump_json(), canonical(scoped.validation_issues),
        canonical(scoped.usage), canonical(scoped.token_usage), scoped.provenance or 'deterministic_parser',
        scoped.fingerprint, scoped.response_ms, scoped.calls, scoped.cache_hit, scoped.failure_kind, scoped.resolved_model, scoped.provider_name)
