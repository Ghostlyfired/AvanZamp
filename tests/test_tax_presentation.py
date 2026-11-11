from decimal import Decimal
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.domain.adjustments import effective_adjustments
from invoicedesk.domain.normalize import normalize
from invoicedesk.domain.reconciliation import reconcile


def test_repeated_printed_tax_is_counted_once_without_changing_source():
    inv = ExtractedInvoice(subtotal='100',tax_amount='12',total='112',
        adjustments=[dict(description='Tax (12%)',amount='12')])
    assert effective_adjustments(inv)==[]
    assert normalize(inv).net_amount==Decimal('100')
    assert all(f['passed'] for f in reconcile(inv,Decimal('.02')))
    assert len(inv.adjustments)==1


def test_real_extra_charge_and_ambiguous_tax_are_not_dropped():
    inv = ExtractedInvoice(subtotal='100',tax_amount='12',total='124',
        adjustments=[dict(description='Tax (12%)',amount='12')])
    assert len(effective_adjustments(inv))==1
    inv.adjustments[0].description='Delivery'
    inv.total=Decimal('112')
    assert len(effective_adjustments(inv))==1
    assert not all(f['passed'] for f in reconcile(inv,Decimal('.02')))
