"""Distinguish a repeated tax presentation from additional payable charges."""
from decimal import Decimal
import re


def effective_adjustments(invoice):
    rows = invoice.adjustments
    if any(value is None for value in (invoice.subtotal, invoice.tax_amount, invoice.total)):
        return rows
    # Only collapse an exact, independently printed tax duplication when the
    # printed total proves the interpretation. Ambiguous taxes stay reviewable.
    tax_rows = [a for a in rows if a.amount == invoice.tax_amount and
                re.fullmatch(r'(?:tax|vat|gst|sales tax)(?:\s*\(?\d+(?:\.\d+)?\s*%\)?)?', a.description.strip(), re.I)]
    if len(tax_rows) != 1 or invoice.tax_amount <= 0:
        return rows
    candidate = [a for a in rows if a is not tax_rows[0]]
    if any(a.amount is None for a in candidate):
        return rows
    calculated = invoice.subtotal + invoice.tax_amount + sum((a.amount for a in candidate), Decimal(0))
    return candidate if calculated == invoice.total else rows
