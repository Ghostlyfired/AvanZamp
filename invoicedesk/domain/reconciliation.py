from decimal import Decimal
from invoicedesk.domain.models import ExtractedInvoice


def reconcile(inv: ExtractedInvoice, tolerance: Decimal) -> list[dict]:
    from .adjustments import effective_adjustments
    charge_rows = effective_adjustments(inv)
    findings: list[dict] = []

    def compare(name: str, calculated: Decimal, printed: Decimal, scope: str, *, row_index: int | None = None) -> None:
        findings.append(dict(name=name, calculated=str(calculated), printed=str(printed),
                             scope=scope, passed=abs(calculated-printed) <= tolerance, row_index=row_index))

    for index, item in enumerate(inv.line_items, 1):
        if item.quantity is not None and item.unit_price is not None and item.amount is not None:
            compare(f'Line {index}: quantity times unit price', item.quantity*item.unit_price, item.amount, 'line', row_index=index-1)
    amounts = [item.amount for item in inv.line_items]
    items_complete = bool(amounts) and all(amount is not None for amount in amounts)
    if inv.subtotal is not None and items_complete:
        compare('Line items to subtotal', sum(amounts, Decimal(0)), inv.subtotal, 'subtotal')
    adjustments_complete = all(a.amount is not None for a in charge_rows)
    adjustments = sum((a.amount for a in charge_rows if a.amount is not None), Decimal(0))
    if inv.subtotal is not None and inv.tax_amount is not None and inv.total is not None and adjustments_complete:
        compare('Subtotal plus tax and printed adjustments to total', inv.subtotal+inv.tax_amount+adjustments, inv.total, 'total')
    elif inv.prices_include_tax and inv.total is not None and inv.tax_amount is not None and items_complete and adjustments_complete:
        compare('Net line items plus included tax', sum(amounts, Decimal(0))+inv.tax_amount+adjustments, inv.total, 'total')
    elif inv.subtotal is not None and inv.total is not None and inv.adjustments and adjustments_complete:
        compare('Printed subtotal plus adjustments to grand total', inv.subtotal+adjustments, inv.total, 'printed_total')
    return findings
