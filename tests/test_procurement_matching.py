"""Procurement safety rules use synthetic facts, never document-specific branches."""
from decimal import Decimal
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.domain.normalize import normalize, normalize_po
from invoicedesk.domain.checks import amounts, resolve_po
from invoicedesk.domain.master import PurchaseOrder
from invoicedesk.policy import Policy


def order(**changes):
    fields = dict(po_number='PO-A', vendor_id='V-A', description='Portable computers',
                  net_amount='2000', tax_rate='.08', currency='USD', status='open', issue_date='2026-10-01')
    fields.update(changes)
    return fields


def invoice(**changes):
    fields = dict(document_type='invoice', vendor_name='Supplier', invoice_number='NEW-1',
        invoice_date='2026-11-01', po_reference='PO-A', currency='USD', subtotal='1000',
        tax_amount='80', total='1080', reference_text='Portable computers',
        line_items=[dict(description='Portable computers',quantity='1',unit_price='1000',amount='1000')])
    fields.update(changes)
    return ExtractedInvoice(**fields)


class Snapshot:
    def __init__(self, orders, billed='0'):
        self.orders = orders
        self.billed = Decimal(billed)

    def pos(self):
        return self.orders

    def remaining(self, po):
        return Decimal(po['net_amount'])-self.billed

    def runs(self):
        return []


def checks(inv, po=None, billed='0'):
    po = po or order()
    return {c.check_id:c for c in amounts(inv,normalize(inv),po,'explicit',Snapshot([po],billed),Policy.load())}


def test_additional_delivery_charge_never_hides_in_unused_balance():
    inv = invoice(adjustments=[dict(description='Delivery',amount='20')],tax_amount='81.60',total='1101.60')
    result = checks(inv)
    assert result['amount_vs_balance'].proposed is None
    assert result['po_charge_consistency'].proposed == 'NEEDS_REVIEW'
    allowed = checks(inv,order(charge_allowances=[dict(description='Delivery',amount='20')]))
    assert allowed['po_charge_consistency'].proposed is None


def test_allowance_limits_aggregate_repeated_charge_rows():
    inv = invoice(adjustments=[dict(description='Delivery',amount='15'),dict(description='Delivery',amount='15')])
    result = checks(inv,order(charge_allowances=[dict(description='Delivery',amount='20')]))
    assert result['po_charge_consistency'].proposed == 'NEEDS_REVIEW'


def test_zero_charges_and_optional_metadata_do_not_raise_scope_issues():
    inv = invoice(adjustments=[dict(description='Shipping',amount='0')])
    assert checks(inv)['po_charge_consistency'].proposed is None


def test_item_prices_quantities_and_scope_are_independent_of_total_budget():
    po = order(line_items=[dict(description='Portable computers',quantity='1',unit_price='900',amount='900')])
    assert checks(invoice(),po)['po_scope_consistency'].proposed == 'NEEDS_REVIEW'
    po['line_items'][0].update(quantity='2',unit_price='1000',amount='2000')
    assert checks(invoice(),po)['po_scope_consistency'].proposed is None


def test_tax_variance_and_missing_currency_require_review():
    assert checks(invoice(tax_amount='90',total='1090'))['po_tax_consistency'].proposed == 'NEEDS_REVIEW'
    assert checks(invoice(currency=None))['currency_consistency'].proposed == 'NEEDS_REVIEW'


def test_implied_po_requires_content_not_just_affordable_amount():
    inv = invoice(po_reference=None)
    po, _, result = resolve_po(inv,normalize(inv),dict(vendor_id='V-A'),Snapshot([order(description='Cleaning and janitorial services')]),Policy.load())
    assert po is None and result[0].proposed == 'NEEDS_REVIEW'
    po, mode, result = resolve_po(inv,normalize(inv),dict(vendor_id='V-A'),Snapshot([order()]),Policy.load())
    assert po['po_number'] == 'PO-A' and mode == 'implied'


def test_explicit_unknown_reference_never_falls_back_to_other_po():
    inv = invoice(po_reference='UNKNOWN')
    po, _, result = resolve_po(inv,normalize(inv),dict(vendor_id='V-A'),Snapshot([order()]),Policy.load())
    assert po is None and result[0].proposed == 'NEEDS_REVIEW'


def test_numeric_labelled_po_reference_resolves_unique_prefix_alias_and_currency():
    inv = invoice(po_reference='1241',currency='$')
    po,mode,result = resolve_po(inv,normalize(inv),dict(vendor_id='V-A'),Snapshot([order(po_number='PO-1241')]),Policy.load())
    assert po['po_number']=='PO-1241' and mode=='explicit'
    assert normalize_po(normalize(inv),po).currency=='USD'
    assert all(check.proposed is None for check in result)


def test_exact_numeric_order_id_wins_and_alias_collisions_require_review():
    inv = invoice(po_reference='1241')
    vendor = dict(vendor_id='V-A')
    po,_,_ = resolve_po(inv,normalize(inv),vendor,Snapshot([order(po_number='PO-1241'),order(po_number='1241')]),Policy.load())
    assert po['po_number']=='1241'
    po,_,result = resolve_po(inv,normalize(inv),vendor,Snapshot([order(po_number='PO-1241'),order(po_number='PO1241')]),Policy.load())
    assert po is None and result[0].proposed=='NEEDS_REVIEW'


def test_numeric_alias_never_bypasses_vendor_consistency():
    inv = invoice(po_reference='1241')
    po,_,result = resolve_po(inv,normalize(inv),dict(vendor_id='V-A'),Snapshot([order(po_number='PO-1241',vendor_id='OTHER')]),Policy.load())
    assert po['po_number']=='PO-1241'
    assert any(c.check_id=='po_vendor_consistency' and c.proposed=='NEEDS_REVIEW' for c in result)


def test_current_remaining_balance_enforces_cumulative_partial_billing():
    assert checks(invoice(),billed='1000')['amount_vs_balance'].proposed is None
    assert checks(invoice(),billed='1500')['amount_vs_balance'].proposed == 'REJECT'


def test_ordered_quantity_tracks_only_approved_prior_invoices():
    po = order(net_amount='10000',line_items=[dict(description='Portable computers',quantity='1',unit_price='1000')])
    prior = dict(matched_po='PO-A',review_status='approved',extraction_json=invoice(invoice_number='OLD-1').model_dump_json())
    snapshot = Snapshot([po])
    snapshot.runs = lambda: [prior]
    inv = invoice()
    result = {c.check_id:c for c in amounts(inv,normalize(inv),po,'explicit',snapshot,Policy.load())}
    assert result['amount_vs_balance'].proposed is None
    assert result['po_scope_consistency'].proposed == 'NEEDS_REVIEW'
    prior['review_status'] = 'pending'
    result = {c.check_id:c for c in amounts(inv,normalize(inv),po,'explicit',snapshot,Policy.load())}
    assert result['po_scope_consistency'].proposed is None


def test_missing_line_quantity_cannot_silently_pass_structured_order():
    po = order(line_items=[dict(description='Portable computers',quantity='2',unit_price='1000')])
    inv = invoice(line_items=[dict(description='Portable computers',unit_price='1000',amount='1000')])
    assert checks(inv,po)['po_scope_consistency'].proposed == 'NEEDS_REVIEW'


def test_total_only_invoice_cannot_verify_a_structured_order():
    po = order(line_items=[dict(description='Portable computers',quantity='2',unit_price='1000')])
    assert checks(invoice(line_items=[]),po)['po_scope_consistency'].proposed == 'NEEDS_REVIEW'


def test_different_model_number_is_not_tolerated_as_minor_wording():
    po = order(line_items=[dict(description='Laptop M3 16GB',quantity='2',unit_price='1000')])
    inv = invoice(line_items=[dict(description='Laptop M4 16GB',quantity='1',unit_price='1000',amount='1000')])
    assert checks(inv,po)['po_scope_consistency'].proposed == 'NEEDS_REVIEW'


def test_authorized_charge_may_be_printed_as_a_line_item():
    po = order(line_items=[dict(description='Portable computers',quantity='2',unit_price='1000')],
        charge_allowances=[dict(description='Installation fee',amount='20')])
    inv = invoice(line_items=[dict(description='Portable computers',quantity='1',unit_price='1000',amount='1000'),
                              dict(description='Installation fee',quantity='1',unit_price='20',amount='20')],
                  subtotal='1020',tax_amount='81.60',total='1101.60')
    result = checks(inv,po)
    assert result['po_scope_consistency'].proposed is None
    assert result['po_charge_consistency'].proposed is None


def test_po_optional_detail_contract_preserves_decimal_money():
    po = PurchaseOrder.model_validate(order(line_items=[dict(description='Portable computers',quantity='2',unit_price='1000')],
        charge_allowances=[dict(description='Delivery',amount='20')]))
    assert po.line_items[0].unit_price == Decimal('1000')
    assert po.charge_allowances[0].amount == Decimal('20')
