"""Production extraction contracts; provider responses here are synthetic."""
from decimal import Decimal
import pytest
from invoicedesk.infrastructure.extraction.llm import LlmClient
from invoicedesk.infrastructure.extraction.providers import ProviderResponse
from invoicedesk.infrastructure.extraction.router import extract, merge_extraction
from invoicedesk.infrastructure.extraction.text import DocumentText
from invoicedesk.domain.models import ExtractedInvoice, Quality
from invoicedesk.domain.validation import blocking_warnings
from invoicedesk.policy import ROOT, Policy


def facts():
    return dict(document_type='invoice', vendor_name='Northwind Office Supplies Ltd',
                invoice_number='NEW-1', invoice_date='2026-11-09', po_reference='PO-1001',
                currency='USD', subtotal='100.00', tax_amount='8.00', total='108.00',
                line_items=[], adjustments=[], evidence={})


class Provider:
    name = 'synthetic-mandatory-extraction'
    def __init__(self): self.calls = 0
    def extract(self, *args, **kwargs):
        self.calls += 1
        return ProviderResponse(facts())


def test_complete_reconciling_pdf_always_calls_live_provider(tmp_path, monkeypatch):
    from invoicedesk.infrastructure.extraction import router
    text = ('Vendor: Northwind Office Supplies Ltd\nInvoice No: OLD-1\n'
            'Invoice date: 2026-11-09\nSubtotal: 100.00\nTax: 8.00\nTotal: 108.00\n')
    monkeypatch.setattr(router, 'read_document_text', lambda _: DocumentText([text]))
    provider = Provider()
    client = LlmClient('live', tmp_path/'cache', provider=provider, model='test')
    path = ROOT/'data/invoices/01_clean_exact.pdf'
    for _ in range(2):
        invoice, findings = extract(path, Policy.load(), client)
        assert invoice.invoice_number == 'NEW-1'
        assert invoice.extraction_path == 'llm_text'
        assert client.calls == 1 and not client.cache_hit
        assert all(f['passed'] for f in findings)
    assert provider.calls == 2


def test_model_failure_never_auto_accepts_parser_fields(tmp_path, monkeypatch):
    from invoicedesk.infrastructure.extraction import router
    from invoicedesk.infrastructure.extraction.providers import ProviderUnavailable
    from invoicedesk.domain.checks import integrity
    monkeypatch.setattr(router, 'read_document_text', lambda _: DocumentText([
        'Vendor: Seller\nInvoice No: 1\nInvoice date: 2026-11-09\nSubtotal: 100.00\nTax: 8.00\nTotal: 108.00']))
    class FailedProvider:
        name = 'synthetic-failure'
        def extract(self, *args, **kwargs):
            raise ProviderUnavailable('Provider unavailable.', api_calls=1)
    client = LlmClient('live', tmp_path, provider=FailedProvider(), model='test')
    invoice, findings = extract(ROOT/'data/invoices/01_clean_exact.pdf', Policy.load(), client)
    assert invoice.total == Decimal('108')
    assert invoice.extraction_path == 'failed'
    assert integrity(invoice, findings, Policy.load())[0].proposed.value == 'NEEDS_REVIEW'
    assert client.calls == 1


def test_grounded_model_resolves_narrow_parser_disagreement():
    parsed = ExtractedInvoice(total='1100')
    model = ExtractedInvoice(total='100', evidence={'total':'Total 100.00'})
    result = merge_extraction(parsed, model, provided_fields={'total'}, invalid_fields=set(),
                              text='Total 100.00', pages=['Total 100.00'])
    assert result.total == Decimal('100')
    assert result.field_quality['total'] == Quality.GROUNDED
    assert not result.warnings


@pytest.mark.parametrize('field', ['due_date', 'sender', 'recipient', 'document_type'])
def test_optional_metadata_grounding_does_not_block_payment(field):
    invoice = ExtractedInvoice(warnings=[f'Grounding failed: {field}'])
    assert blocking_warnings(invoice) == []


def test_financial_grounding_still_blocks_payment():
    invoice = ExtractedInvoice(warnings=['Grounding failed: total', 'Grounding failed: line_items'])
    assert blocking_warnings(invoice) == invoice.warnings


def test_ocr_absence_keeps_image_evidence_unverified(tmp_path, monkeypatch):
    from PIL import Image
    from invoicedesk.infrastructure.extraction import text
    from invoicedesk.infrastructure.extraction.evidence import verify_references
    path = tmp_path/'invoice.png'
    Image.new('RGB', (100, 100), 'white').save(path)
    monkeypatch.setattr(text.shutil, 'which', lambda _: None)
    source = text.read_document_text(path)
    invoice = ExtractedInvoice(total='100', evidence={'total':'Total 100.00'})
    assert source.needs_vision
    assert verify_references(invoice, source.pages)[0].verification == 'UNVERIFIED'


def test_independent_image_ocr_can_ground_printed_facts(tmp_path, monkeypatch):
    from PIL import Image
    from invoicedesk.infrastructure.extraction import text
    from invoicedesk.infrastructure.extraction.evidence import verify_references
    path = tmp_path/'invoice.png'
    Image.new('RGB', (100, 100), 'white').save(path)
    monkeypatch.setattr(text, '_ocr_image', lambda _: 'Invoice number X-1\nTotal 100.00')
    source = text.read_document_text(path)
    invoice = ExtractedInvoice(total='100', evidence={'total':'Total 100.00'})
    assert verify_references(invoice, source.pages)[0].verification == 'GROUNDED'
