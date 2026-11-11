import re
from decimal import Decimal
from invoicedesk.domain.models import ExtractedInvoice, NormalizedInvoice

def key(value: str | None) -> str | None:
    return re.sub(r'[^A-Z0-9]','',value.upper()) if value else None


def matches_explicit_po(reference: str | None, po_number: str) -> bool:
    """A labelled numeric PO reference may omit the conventional PO prefix."""
    reference_key, order_key = key(reference), key(po_number)
    return bool(reference_key and (reference_key == order_key or
                (reference_key.isascii() and reference_key.isdigit() and order_key == 'PO'+reference_key)))

CURRENCY_ALIASES = {
    'USD': ('USD', 'US$', '$US', 'USDOLLAR', 'USDOLLARS', 'UNITEDSTATESDOLLAR', 'UNITEDSTATESDOLLARS'),
    'CAD': ('CAD', 'CA$', 'C$', 'CANADIANDOLLAR', 'CANADIANDOLLARS'),
    'AUD': ('AUD', 'AU$', 'A$', 'AUSTRALIANDOLLAR', 'AUSTRALIANDOLLARS'),
    'NZD': ('NZD', 'NZ$', 'NEWZEALANDDOLLAR', 'NEWZEALANDDOLLARS'),
    'SGD': ('SGD', 'SG$', 'S$', 'SINGAPOREDOLLAR', 'SINGAPOREDOLLARS'),
    'HKD': ('HKD', 'HK$', 'HONGKONGDOLLAR', 'HONGKONGDOLLARS'),
    'EUR': ('EUR', '€', 'EURO', 'EUROS'),
    'GBP': ('GBP', '£', 'BRITISHPOUND', 'BRITISHPOUNDS', 'POUNDSTERLING'),
    'INR': ('INR', '₹', 'INDIANRUPEE', 'INDIANRUPEES'),
    'JPY': ('JPY', 'JP¥', 'JAPANESEYEN'),
    'CNY': ('CNY', 'CN¥', 'RMB', 'CHINESEYUAN'),
}


def currency_code(value: str | None) -> str | None:
    if value is None:
        return None
    compact = re.sub(r'[\s.]', '', value).upper()
    for code, aliases in CURRENCY_ALIASES.items():
        if compact in aliases:
            return code
    # A bare dollar symbol needs an explicit currency anchor; preserve it as printed.
    return value.strip().upper() or None


def currency_supported(value: str, evidence: str) -> bool:
    target = currency_code(value)
    if target in {'$', '¥'}:
        return value.strip() in evidence
    text = evidence.replace('.', '').upper()
    for alias in CURRENCY_ALIASES.get(target, (target,)):
        if alias and re.search(r'(?<![A-Z])'+r'\s*'.join(re.escape(char) for char in alias)+r'(?![A-Z])', text):
            # C$ is not evidence for an unqualified $ / a different dollar currency.
            return True
    return False


def normalize(inv: ExtractedInvoice) -> NormalizedInvoice:
    from .adjustments import effective_adjustments
    adjustments = effective_adjustments(inv)
    invoice_workflow = inv.document_type in {'invoice', 'unknown'}
    net=inv.subtotal if invoice_workflow else None
    if invoice_workflow and net is not None and adjustments:
        net = net + sum((a.amount for a in adjustments if a.amount is not None), Decimal(0)) if all(a.amount is not None for a in adjustments) else None
    if invoice_workflow and net is None and inv.total is not None and inv.tax_amount is not None and all(a.amount is not None for a in inv.adjustments): net=inv.total-inv.tax_amount
    currency = currency_code(inv.currency)
    if currency == '$':
        anchors = [code for code in CURRENCY_ALIASES if currency_supported(code,inv.evidence.get('currency',''))]
        currency = anchors[0] if len(anchors)==1 else 'CONFLICT' if len(anchors)>1 else currency
    return NormalizedInvoice(vendor_name=inv.vendor_name,invoice_number=inv.invoice_number,invoice_number_key=key(inv.invoice_number),
        invoice_date=inv.invoice_date,po_reference_key=key(inv.po_reference),currency=currency or '',
        net_amount=net,tax_amount=inv.tax_amount,gross_amount=inv.total,
        description_text=' '.join(filter(None,[inv.reference_text,*[i.description for i in inv.line_items]])))


def normalize_po(norm: NormalizedInvoice, po: dict | None) -> NormalizedInvoice:
    """Derive an explicitly marked net interpretation without mutating the input."""
    from decimal import Decimal
    if (po and norm.currency == '$' and matches_explicit_po(norm.po_reference_key,po['po_number'])
            and po['currency'] in {'USD','CAD','AUD','NZD','SGD','HKD'}):
        norm = norm.model_copy(update={'currency': po['currency']})
    if po and norm.net_amount is None and norm.gross_amount is not None:
        return norm.model_copy(update={'net_amount': (norm.gross_amount/(1+Decimal(po['tax_rate']))).quantize(Decimal('.01')),
                                       'net_is_inferred': True})
    return norm.model_copy(deep=True)
