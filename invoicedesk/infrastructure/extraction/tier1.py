"""Conservative label parser. Unsupported layouts escalate rather than invent values."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from invoicedesk.domain.models import ExtractedInvoice, LineItem, Party, FieldEvidence

MONEY = r'(?:USD\s*|\$\s*)?(-?\d[\d,]*\.\d{2})(?!\d)'


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d %b %Y', '%d %B %Y', '%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', value)
    if match:
        first, second, year = map(int, match.groups())
        if first > 12 or first == second:
            day, month = first, second
        elif second > 12:
            month, day = first, second
        else:
            return None  # 03/04/2026 has two valid readings.
        try:
            return date(year, month, day)
        except ValueError:
            pass
    return None


COLUMN_HEADINGS = {
    'dateofissue','duedate','invoicedate','invoicenumber','invoiceno','date',
    'unitcost','unitprice','qty','quantity','rate','amount','description',
    'billedto','from','purchaseorder','subtotal','total',
}


def is_column_heading(value: str) -> bool:
    # PDF text readers may collapse table column spacing to single spaces.
    # Recognize a sequence of known headings without accepting arbitrary values.
    normalized = re.sub(r'[^a-z0-9]', '', value.casefold())
    headings = '|'.join(sorted(COLUMN_HEADINGS, key=len, reverse=True))
    return bool(normalized) and bool(re.fullmatch('(?:'+headings+')+', normalized))


def parse(text: str) -> ExtractedInvoice:
    def label(pattern: str) -> str | None:
        # Do not let whitespace consume a newline and steal the following field.
        matches = re.findall(
            r'(?im)^[ \t]*(?:' + pattern + r')(?:[ \t]*:[ \t]*|[ \t]+)([^\r\n]+)$', text,
        )
        values = list(dict.fromkeys(value.strip() for value in matches if value.strip() and not is_column_heading(value)))
        return values[0] if len(values) == 1 else None

    def money(pattern: str) -> Decimal | None:
        value = label(pattern)
        if value is None:
            return None
        amounts = re.findall(MONEY, value)
        if len(amounts) != 1:
            return None
        try:
            return Decimal(amounts[0].replace(',', ''))
        except InvalidOperation:
            return None

    po_refs = list(dict.fromkeys(re.findall(r'\bPO[-\s]?\d{3,}\b', text, re.I)))
    items = []
    for desc, qty, unit, amount in re.findall(
        r'(?m)^Item:[ \t]*(.+?)[ \t]*\|[ \t]*(\d+(?:\.\d+)?)[ \t]*\|[ \t]*'
        + MONEY + r'[ \t]*\|[ \t]*' + MONEY + r'[ \t]*$', text,
    ):
        items.append(LineItem(description=desc, quantity=Decimal(qty),
                              unit_price=Decimal(unit.replace(',', '')),
                              amount=Decimal(amount.replace(',', ''))))
    inv = ExtractedInvoice(
        vendor_name=label(r'Vendor|Supplier|Bill from|From'),
        invoice_number=label(r'Invoice[ \t]*(?:No\.?|Number|#)|Inv\.?|Document ref'),
        invoice_date=parse_date(label(r'Invoice date|Date issued|Issue date|Date')),
        due_date=parse_date(label(r'Due date|Payment due')),
        po_reference=po_refs[0] if len(po_refs) == 1 else None,
        currency=label(r'Currency'), subtotal=money(r'Subtotal|Sub-total|Net amount'),
        tax_amount=money(r'Tax|VAT(?: incl\.)?|GST'),
        total=money(r'Total Due|Amount due|Grand total|Invoice total|Total'),
        prices_include_tax=True if re.search(r'incl\.|includes|inclusive of', text, re.I) else None,
        line_items=items, reference_text=label('Re|Description|Subject'),
    )
    headings = {'invoice': r'(?im)^\s*(?:tax )?invoice\s*$',
                'bill_of_materials': r'(?im)^\s*bill of materials\s*$',
                'advance_request': r'(?im)^\s*advance request\s*$',
                'quotation': r'(?im)^\s*(?:quotation|quote)\s*$',
                'purchase_order': r'(?im)^\s*purchase order\s*$'}
    kinds = [kind for kind, pattern in headings.items() if re.search(pattern, text)]
    if len(re.findall(headings['invoice'], text)) > 1:
        inv.warnings.append('Multiple invoice headings; confirm document interpretation before approval.')
    if len(kinds) == 1:
        inv.document_type = kinds[0]
    elif len(kinds) > 1:
        inv.warnings.append('Conflicting document headings require interpretation.')
    # Source snippets originate in the parser's input, independently of arithmetic.
    for name in ('vendor_name', 'invoice_number', 'invoice_date', 'currency', 'po_reference', 'subtotal', 'tax_amount', 'total'):
        value = getattr(inv, name)
        if value is None:
            continue
        for line in text.splitlines():
            supported = str(value) in line
            if name == 'invoice_date':
                supported = parse_date(line.split(':', 1)[-1].strip()) == value or str(value) in line
            if isinstance(value, Decimal):
                supported = any(Decimal(t.replace(',', '')) == value for t in re.findall(MONEY, line))
            if supported:
                inv.evidence[name] = line
                inv.field_evidence.append(FieldEvidence(path=name, snippet=line))
                break
    for index, row in enumerate(inv.line_items):
        source = next((line for line in text.splitlines() if row.description in line), None)
        if source:
            for name, value in row.model_dump().items():
                if value is not None:
                    inv.field_evidence.append(FieldEvidence(path=f'line_items.{index}.{name}', snippet=source))
    if inv.vendor_name:
        inv.sender = Party(name=inv.vendor_name)
    recipient = label(r'Bill to|Recipient')
    if recipient:
        inv.recipient = Party(name=recipient)
    if inv.currency:
        inv.currency = inv.currency.upper()
    if not inv.currency and re.search(r'\bUSD\b', text):
        inv.currency = 'USD'
    if len(po_refs) > 1:
        inv.warnings.append('Multiple PO references found; manual allocation is required.')
    return inv
