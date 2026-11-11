"""Evidence must occur in PDF text and support the claimed normalized value."""
import re
from decimal import Decimal, InvalidOperation
from invoicedesk.domain.models import ExtractedInvoice, Quality
from .tier1 import parse_date
from invoicedesk.domain.normalize import currency_supported


def compact(value: str) -> str:
    text = re.sub(r'\s+', ' ', value.casefold()).strip()
    text = re.sub(r'\b(us|ca|au|nz|hk|sg)\s+\$', r'\1$', text)
    return re.sub(r'([€£₹$¥])\s+(?=[+-]?\d)', r'\1', text)


def amount_supported(value: Decimal, evidence: str) -> bool:
    # Numeric boundaries prevent a claim of 100.00 being supported by 1100.00.
    for token in re.findall(r'(?<![\d.])-?\d[\d,]*(?:\.\d+)?(?![\d.])', evidence):
        try:
            if Decimal(token.replace(',', '')) == value:
                return True
        except InvalidOperation:
            pass
    return False


def date_supported(value, evidence: str, *, source: str = '') -> bool:
    candidates = [evidence.strip(), evidence.split(':', 1)[-1].strip()]
    candidates.extend(re.findall(r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b', evidence))
    if any(parse_date(candidate) == value for candidate in candidates):
        return True
    order = re.sub(r'\s+', '', source.upper())
    day_first = 'DD/MM/YYYY' in order or 'DD-MM-YYYY' in order
    month_first = 'MM/DD/YYYY' in order or 'MM-DD-YYYY' in order
    if day_first == month_first:
        return False
    from datetime import datetime
    for token in candidates:
        if not re.fullmatch(r'\d{1,2}[/-]\d{1,2}[/-]\d{4}', token):
            continue
        try:
            parsed = datetime.strptime(token.replace('-', '/'), '%d/%m/%Y' if day_first else '%m/%d/%Y').date()
            if parsed == value:
                return True
        except ValueError:
            pass
    return False


def ground(inv: ExtractedInvoice, text: str, *, pages: list[str] | None = None) -> None:
    # Per-cell references ground structured sections without requiring a model-written
    # concatenation of separate table rows to appear verbatim in the source.
    from .evidence import verify_references
    references = verify_references(inv, pages if pages is not None else ([text] if text else []))
    supplied = {r.path for r in references}
    verified = {r.path for r in references if r.verification in {'VERIFIED', 'GROUNDED'}}

    def leaves(value, prefix):
        if isinstance(value, dict):
            return [path for key, part in value.items() for path in leaves(part, prefix+'.'+key)]
        if isinstance(value, list):
            return [path for i, part in enumerate(value) for path in leaves(part, prefix+'.'+str(i))]
        return [prefix] if value is not None else []

    for name in ExtractedInvoice.model_fields:
        if name in {'field_evidence', 'field_quality', 'evidence', 'extraction_path', 'warnings'}:
            continue
        value = getattr(inv, name)
        if (name == 'document_type' and value == 'unknown') or value is None or value == []:
            inv.field_quality[name] = Quality.MISSING
            continue
        evidence = inv.evidence.get(name, '')
        found = bool(evidence) and compact(evidence) in compact(text)
        supported = True
        if name in {'subtotal', 'tax_amount', 'total'}:
            supported = amount_supported(value, evidence)
        elif name in {'invoice_date', 'due_date', 'document_date'}:
            supported = date_supported(value, evidence, source=text)
        elif name == 'currency':
            supported = currency_supported(value, evidence)
        elif name in {'vendor_name', 'invoice_number', 'po_reference', 'reference_text'}:
            supported = compact(str(value)) in compact(evidence)
        elif name in {'sender', 'recipient', 'adjustments', 'line_items'}:
            root = value.model_dump() if hasattr(value, 'model_dump') else [item.model_dump() for item in value]
            paths = leaves(root, name)
            if name == 'line_items':
                paths = [path for path in paths if path.rsplit('.', 1)[-1] not in {'supplier_name', 'unit'}]
            referenced = bool(paths) and all(path in verified for path in paths)
            def supported_parts(part):
                if isinstance(part, dict):
                    return all(supported_parts(v) for v in part.values() if v is not None)
                if isinstance(part, list):
                    return all(supported_parts(v) for v in part)
                if isinstance(part, Decimal):
                    return amount_supported(part, evidence)
                return compact(str(part)) in compact(evidence)
            legacy = not any(path.startswith(name+'.') for path in supplied)
            supported = referenced or (legacy and found and supported_parts(root))
            found = referenced or found
        # Independently verified field references are sufficient for scalar fields too.
        checked = name in verified or (bool(text.strip()) and found and supported)
        inv.field_quality[name] = Quality.GROUNDED if checked else Quality.UNVERIFIED
        if text.strip() and not checked:
            inv.warnings.append(f'Grounding failed: {name}')
