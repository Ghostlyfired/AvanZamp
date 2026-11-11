"""Printed-value and cache-identity checks; no provider is invoked."""
from decimal import Decimal
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.domain.reconciliation import reconcile
from invoicedesk.infrastructure.extraction.evidence import verify_references
from invoicedesk.infrastructure.extraction.llm import recording_matches_model
from invoicedesk.infrastructure.extraction.sections import section_report


def test_alias_snapshot_metadata_is_separate_from_document_fields():
    assert recording_matches_model({'model':'gpt-5-mini-2025-08-07'},'gpt-5-mini')
    assert recording_matches_model({'model':'gpt-5-mini-2025-08-07','requested_model':'gpt-5-mini'},'gpt-5-mini')
    assert recording_matches_model({'model':'gpt-5-mini-2025-08-07','requested_model':'gpt-5-mini'},'gpt-5-mini-2025-08-07')
    assert not recording_matches_model({'model':'gpt-5-mini-2025-08-07'},'gpt-5')
    assert not recording_matches_model({'model':'gpt-5-mini-2025-08-07','requested_model':'different'},'gpt-5-mini')


def test_row_arithmetic_source_verification_and_absent_rate_remain_separate():
    inv=ExtractedInvoice(line_items=[dict(description='Desks',quantity='2',unit_price='50',amount='100'),
        dict(description='Contingency',quantity=None,unit_price=None,amount='5')],
        field_evidence=[dict(path='line_items.0.description',snippet='Desks'),dict(path='line_items.0.quantity',snippet='2'),
            dict(path='line_items.0.unit_price',snippet='50'),dict(path='line_items.0.amount',snippet='100')])
    refs=verify_references(inv,['Desks 2 50 100\nContingency 5'])
    rows=next(s for s in section_report(inv,findings=reconcile(inv,Decimal('.02')),source_evidence=refs) if s['section']=='line_items')['row_validation']
    assert rows[0]['arithmetic_status']=='PASS' and rows[0]['verification_status']=='GROUNDED'
    assert rows[1]['arithmetic_status']=='NOT_CHECKED' and rows[1]['verification_status']=='UNVERIFIED'
    assert rows[1]['fields']['quantity']['value'] is None and rows[1]['fields']['unit_price']['value'] is None
    unverified=next(s for s in section_report(inv,findings=reconcile(inv,Decimal('.02'))) if s['section']=='line_items')['row_validation'][0]
    assert unverified['arithmetic_status']=='PASS' and unverified['verification_status']=='UNVERIFIED'


def test_column_headings_are_not_invoice_or_reference_values():
    from invoicedesk.infrastructure.extraction.tier1 import parse
    from invoicedesk.infrastructure.extraction.router import merge_extraction
    source='Invoice\nINVOICE NUMBER          DATE OF ISSUE             DUE DATE\n\nINV-004                 20/11/2026                26/11/2026\n\nDescription          Unit cost      QTY      Amount\nDesk                 100            1        100'
    parsed=parse(source)
    assert parsed.invoice_number is None and parsed.reference_text is None
    model=ExtractedInvoice(document_type='invoice',invoice_number='INV-004',invoice_date='2026-11-20',due_date='2026-11-26',
        evidence={'document_type':'Invoice','invoice_number':'INV-004','invoice_date':'20/11/2026','due_date':'26/11/2026'},reference_text=None)
    merged=merge_extraction(parsed,model,provided_fields=set(ExtractedInvoice.model_fields),invalid_fields=set(),text=source,pages=[source])
    assert merged.invoice_number=='INV-004' and merged.reference_text is None
    assert not merged.warnings


def test_a_real_identifier_disagreement_is_still_reviewed():
    from invoicedesk.infrastructure.extraction.tier1 import parse
    from invoicedesk.infrastructure.extraction.router import merge_extraction
    source='Invoice\nInvoice No: FIRST-12\nOther reference SECOND-14'
    model=ExtractedInvoice(document_type='invoice',invoice_number='SECOND-14',evidence={'invoice_number':'SECOND-14'})
    merged=merge_extraction(parse(source),model,provided_fields=set(ExtractedInvoice.model_fields),invalid_fields=set(),text=source,pages=[source])
    assert merged.invoice_number=='SECOND-14'
    assert 'Tiers disagree: invoice_number' in merged.warnings


def test_top_level_scalar_evidence_is_verified_without_a_duplicate_path_entry():
    inv=ExtractedInvoice(vendor_name='Example Supplier',evidence={'vendor_name':'Example Supplier'})
    references=verify_references(inv,['Seller: Example Supplier'])
    assert len(references)==1 and references[0].path=='vendor_name' and references[0].verification=='GROUNDED'
    assert verify_references(inv,['A different seller'])[0].verification=='UNVERIFIED'
