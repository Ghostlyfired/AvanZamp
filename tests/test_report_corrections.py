"""Regression cases shaped like the observed report, with synthetic document facts."""
from decimal import Decimal
from invoicedesk.extract.validation import parse_response
from invoicedesk.extract.reconcile import reconcile
from invoicedesk.extract.sections import section_report
from invoicedesk.checks.engine import integrity
from invoicedesk.normalize import normalize, currency_code
from invoicedesk.policy import Policy, ROOT
from invoicedesk.extract.llm import LlmClient
from invoicedesk.extract.providers import ProviderResponse


def recorded_shape():
    # Synthetic example shaped like the observed legacy response; not an API recording.
    return {'document_type':'bill_of_materials','sender':{'name':'Example Project'},
            'vendor_name':'Example Project','invoice_date':'2026-04-19','invoice_number':None,
            'due_date':None,'currency':'₹','subtotal':'120.00','tax_amount':None,'total':'100.00',
            'prices_include_tax':True,'adjustments':[{'description':'Round-off','amount':'-20.00'}],
            'line_items':[{'description':'Component','quantity':'2','unit_price':'10.00','amount':'20.00'},
                          {'description':'Contingency','quantity':None,'unit_price':'100.00','amount':'100.00'}],
            'evidence':{'invoice_date':'19 April 2026','sender':'Example Project'}}


def parts(raw):
    parsed=parse_response(raw)
    findings=reconcile(parsed.invoice,Decimal('.02'))
    result=section_report(parsed.invoice,parsed.issues,parsed.provided_fields,findings,parsed.provided_paths)
    return parsed,findings,{s['section']:s for s in result}


def test_non_invoice_roles_and_date_are_corrected_without_losing_date():
    parsed,_,sections=parts(recorded_shape())
    inv=parsed.invoice
    assert inv.sender.name=='Example Project' and inv.vendor_name is None
    assert str(inv.document_date)=='2026-04-19' and inv.invoice_date is None
    assert inv.evidence['document_date']=='19 April 2026'
    assert 'invoice_date' not in inv.evidence
    assert sections['metadata']['fields']['invoice_date']['status']=='NOT_APPLICABLE'
    assert sections['sender']['fields']['vendor_name']['status']=='NOT_APPLICABLE'
    assert sections['metadata']['fields']['document_date']['status']=='PARSED'
    assert normalize(inv).currency=='INR'
    assert normalize(inv).net_amount is None
    assert currency_code('$')=='$'  # a bare dollar sign is not assumed to be USD


def test_missing_quantity_is_not_silently_counted_as_checked():
    _,_,sections=parts(recorded_shape())
    table=sections['line_items']
    assert table['status']=='PARSED'
    assert table['subtotal_arithmetic_status']=='PASS'
    assert table['arithmetic_status']=='PARTIAL'
    assert table['row_arithmetic_counts']=={'PASS':1,'FAIL':0,'NOT_CHECKED':1}
    row=table['row_validation'][1]
    assert row['row_number']==2 and row['arithmetic_status']=='NOT_CHECKED'
    assert 'quantity' in row['arithmetic_note']


def test_inclusive_printed_total_success_is_not_reported_as_failure():
    parsed,checks,sections=parts(recorded_shape())
    totals=sections['totals']
    assert totals['arithmetic_status']=='PASS'
    assert totals['tax_status']=='INCLUDED_NOT_ITEMIZED'
    assert totals['fields']['currency']['value']=='₹'
    assert totals['fields']['currency']['normalized_value']=='INR'
    parsed.invoice.extraction_path='llm_vision'
    parsed.invoice.field_quality['total']='UNVERIFIED'
    result=integrity(parsed.invoice,checks,Policy.load())[0]
    assert 'reconcile with the grand total' in result.detail
    assert 'could not be independently' not in result.detail
    assert 'invoice_date' not in result.detail and 'vendor_name' not in result.detail
    assert parsed.invoice.tax_amount is None


def test_new_columns_are_unavailable_in_old_responses_and_retained_in_new_ones():
    raw=recorded_shape()
    _,_,sections=parts(raw)
    old=sections['line_items']['row_validation'][0]['fields']
    assert old['supplier_name']['status']=='UNAVAILABLE'
    assert old['part_number']['status']=='UNAVAILABLE'
    assert old['unit']['status']=='UNAVAILABLE'
    raw['line_items'][0].update(supplier_name='Example Supplier',part_number='PART-42',unit='nos.')
    _,_,sections=parts(raw)
    new=sections['line_items']['row_validation'][0]['fields']
    assert new['supplier_name']['value']=='Example Supplier'
    assert new['part_number']['status']=='PARSED' and new['unit']['status']=='PARSED'


def test_explicit_null_is_different_from_an_omitted_party_field():
    raw=recorded_shape()
    raw['sender']['address']=None
    _,_,sections=parts(raw)
    party=sections['sender']['party_validation']['sender']
    assert party['address']['status']=='MISSING'
    assert party['email']['status']=='UNAVAILABLE'


def test_image_evidence_is_not_reported_as_independently_verified():
    parsed,findings,_=parts(recorded_shape())
    parsed.invoice.extraction_path='llm_vision'
    reports=section_report(parsed.invoice,parsed.issues,parsed.provided_fields,findings,parsed.provided_paths)
    evidence=next(s for s in reports if s['section']=='evidence')
    assert evidence['status']=='PARSED' and evidence['verification_status']=='UNVERIFIED'
    assert 'no independent' in evidence['verification_note']


def test_live_requests_are_always_fresh_with_or_without_refresh(tmp_path):
    calls=[]
    class Provider:
        name='test'
        def extract(self,*args,**kwargs):
            calls.append(True)
            return ProviderResponse(recorded_shape())
    path=ROOT/'data/examples/office_furniture.png'
    client=LlmClient('live',tmp_path/'cache',provider=Provider(),model='test')
    client.extract_invoice(path)
    client.extract_invoice(path)
    assert len(calls)==2 and not client.cache_hit
    fresh=LlmClient('live',tmp_path/'cache',provider=Provider(),model='test',refresh=True)
    fresh.extract_invoice(path)
    assert len(calls)==3 and not fresh.cache_hit
