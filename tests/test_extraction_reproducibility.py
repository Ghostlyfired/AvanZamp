"""Offline schema and artifact reproducibility checks, with explicitly synthetic providers."""
import json
import pytest
from invoicedesk.extract.llm import LlmClient, extraction_schema
from invoicedesk.extract.providers import ProviderResponse
from invoicedesk.extract.providers.openai import response_schema
from invoicedesk.extract.validation import parse_response
from invoicedesk.policy import ROOT


class Provider:
    name = 'synthetic-reproducibility-test'
    def __init__(self): self.calls = 0
    def extract(self,*args,**kwargs):
        self.calls += 1
        return ProviderResponse({'vendor_name':'Example Vendor','invoice_number':None,'invoice_date':None,
            'total':'100.00','evidence':{'total':None}}, {'input_tokens':10,'output_tokens':5})


def test_explicit_results_are_independent_and_preserve_partial_cache_uncertainty(tmp_path):
    provider = Provider()
    client = LlmClient('live',tmp_path,provider=provider,model='synthetic-model-1')
    path = ROOT/'data/examples/office_furniture.png'
    first = client.extract_result(path)
    second = client.extract_result(path)
    assert provider.calls == 2
    assert first.api_calls == second.api_calls == 1 and not second.cache_hit
    assert first.issues_json == second.issues_json
    assert first.provenance == second.provenance == 'api_recording_partial'
    assert first.facts['total'] == '100.00'
    assert json.loads(first.raw_response_json)['evidence']['total'] is None
    assert client.calls == 0  # the explicit-result API does not mutate the caller's last-result facade


def test_prompt_or_schema_changes_invalidate_extraction_cache(tmp_path,monkeypatch):
    import invoicedesk.infrastructure.extraction.llm as module
    provider = Provider()
    path = ROOT/'data/examples/office_furniture.png'
    client = LlmClient('live',tmp_path,provider=provider,model='synthetic-model-1')
    first = client.extract_result(path)
    monkeypatch.setattr(module,'EXTRACTION_PROMPT',module.EXTRACTION_PROMPT+' Changed extraction rule.')
    second = client.extract_result(path)
    assert first.fingerprint != second.fingerprint and provider.calls == 2


def test_provider_wire_schema_accepts_local_decimal_and_evidence_contract():
    jsonschema = pytest.importorskip('jsonschema')
    schema = response_schema(extraction_schema())
    # Build every required field from the nullable canonical contract; evidence remains string-only.
    raw = {name:None for name in schema['properties']}
    raw.update(document_type='invoice',line_items=[],adjustments=[],field_evidence=[],
               total='100.00',evidence={name:'' for name in schema['properties']['evidence']['properties']})
    jsonschema.validate(raw,schema)
    parsed = parse_response(raw)
    assert parsed.invoice.total == 100
    assert not parsed.issues
    raw['evidence']['total'] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw,schema)
    # Semantic parser retains the amount even when a provider violates the evidence schema.
    parsed = parse_response(raw)
    assert parsed.invoice.total == 100 and parsed.issues[0]['field'] == 'evidence.total'
