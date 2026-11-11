"""Regression coverage for the schema/backend mismatch observed on a BOM image."""
import json
import logging
import re
import pytest
from pydantic import ValidationError
from invoicedesk.extract.llm import LlmClient, LlmUnavailable, OUTPUT_FIELDS, validate_response
from invoicedesk.extract.providers import ProviderResponse
from invoicedesk.extract.providers.openai import response_schema
from invoicedesk.models import ExtractedInvoice
from invoicedesk.policy import ROOT


def wire_schema():
    schema=ExtractedInvoice.model_json_schema()
    schema['properties']={k:v for k,v in schema['properties'].items() if k in OUTPUT_FIELDS}
    return response_schema(schema)


def bom_fields():
    # Synthetic regression facts, not a recording of the user's discarded API response.
    return {'vendor_name':None,'invoice_number':None,'invoice_date':None,
            'currency':'INR','subtotal':'10282.00','tax_amount':None,'total':'10000.00',
            'evidence':{'subtotal':'Subtotal ₹10,282.00','tax_amount':'','total':'Grand Total ₹10,000.00'}}


def test_evidence_names_do_not_inherit_decimal_nullability():
    schema=wire_schema()
    for name in ['subtotal','tax_amount','total']:
        assert schema['properties']['evidence']['properties'][name]=={'type':'string'}
        assert {'type':'null'} in schema['properties'][name]['anyOf']
    assert validate_response(bom_fields())['evidence']['tax_amount']==''


def test_plain_decimal_pattern_accepts_negative_amount_and_rejects_formatted_currency():
    pattern=wire_schema()['properties']['total']['anyOf'][0]['pattern']
    assert re.fullmatch(pattern,'10000.00')
    assert re.fullmatch(pattern,'-282.00')
    assert not re.fullmatch(pattern,'₹10,000.00')
    assert not re.fullmatch(pattern,'')


def test_bad_evidence_retains_facts_and_caches_partial_response(tmp_path,caplog):
    invalid=bom_fields();invalid['evidence']['tax_amount']=None
    class Provider:
        name='regression-provider'
        def extract(self,*args,**kwargs):
            return ProviderResponse(invalid,{'input_tokens':100,'output_tokens':50},response_ms=120)
    client=LlmClient('live',tmp_path/'cache',provider=Provider(),model='regression-model')
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        result=client.extract_invoice(ROOT/'data/examples/office_furniture.png')
    assert result['total']=='10000.00'
    assert 'tax_amount' not in result['evidence']
    assert 'evidence.tax_amount (string_required)' in caplog.text
    assert 'Grand Total' not in caplog.text
    assert client.validation_issues[0]['field']=='evidence.tax_amount'
    cached=list((tmp_path/'cache').glob('*.json'))
    assert len(cached)==1
    stored=json.loads(cached[0].read_text())
    assert stored['provenance']=='api_recording_partial'
    assert stored['extraction']==invalid
    assert stored['usage']['input_tokens']==100
    assert 'total_tokens=150' in caplog.text
    result=client.extract_invoice(ROOT/'data/examples/office_furniture.png')
    assert not client.cache_hit and client.calls==1
    assert result['total']=='10000.00'
    assert client.validation_issues[0]['field']=='evidence.tax_amount'


@pytest.mark.parametrize('items',[None,['invalid-item']])
def test_malformed_line_items_raise_field_validation_errors(items):
    result=bom_fields();result['line_items']=items
    with pytest.raises(ValidationError) as error:validate_response(result)
    assert error.value.errors()[0]['loc'][0]=='line_items'
