"""Independent field, party, row and evidence validation. No approval decisions."""
from dataclasses import dataclass, field
from typing import Any
from pydantic import TypeAdapter, ValidationError
from invoicedesk.domain.models import ExtractedInvoice, Party, LineItem, Adjustment, Quality

FACT_FIELDS = set(ExtractedInvoice.model_fields) - {'field_quality', 'warnings', 'extraction_path'}
MONEY_FIELDS = {'subtotal', 'tax_amount', 'total', 'quantity', 'unit_price', 'amount'}


@dataclass
class PartialExtraction:
    invoice: ExtractedInvoice
    issues: list[dict[str, str]] = field(default_factory=list)
    provided_fields: set[str] = field(default_factory=set)
    provided_paths: set[str] = field(default_factory=set)


def parse_response(raw: dict[str, Any]) -> PartialExtraction:
    if not isinstance(raw, dict):
        raise ValueError('Extraction response must be a JSON object.')
    provided_paths: set[str] = set()
    def walk(value: Any, prefix: str = '') -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                path = f'{prefix}.{key}' if prefix else str(key)
                provided_paths.add(path)
                walk(item, path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f'{prefix}.{index}')
    walk(raw)
    invoice = ExtractedInvoice()
    issues: list[dict[str, str]] = []

    def problem(path: str, code: str, message: str) -> None:
        issues.append({'field': path, 'type': code, 'message': message})

    def value(model: type, key: str, source: dict, path: str, *, report_omitted: bool = False) -> Any:
        if key not in source:
            if report_omitted:
                problem(path, 'not_returned', 'Extractor did not return this field; absence is not confirmed.')
            return None
        candidate = source[key]
        if isinstance(candidate, float) and key in MONEY_FIELDS:
            problem(path, 'decimal_string_required', 'Use a decimal string rather than a floating-point number.')
            return None
        if isinstance(candidate, str) and not candidate.strip() and key not in {'description'}:
            problem(path, 'empty_value', 'Return null for absent facts; an empty value is not a usable fact.')
            return None
        try:
            return TypeAdapter(model.model_fields[key].annotation).validate_python(candidate)
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_context=False, include_url=False):
                suffix = '.'.join(map(str, error['loc']))
                problem(path + ('.'+suffix if suffix else ''), error['type'], error['msg'])
            return None

    complex_fields = {'sender', 'recipient', 'line_items', 'adjustments', 'evidence'}
    for key in FACT_FIELDS - complex_fields:
        parsed = value(ExtractedInvoice, key, raw, key, report_omitted=key in {'vendor_name', 'invoice_number', 'invoice_date', 'total'})
        if parsed is not None:
            setattr(invoice, key, parsed)
        if key != 'document_type':
            invoice.field_quality[key] = Quality.UNVERIFIED if any(i['field'] == key for i in issues) else Quality.MISSING if parsed is None else Quality.UNVERIFIED

    for key in ('sender', 'recipient'):
        candidate = raw.get(key)
        if candidate is None:
            continue
        if not isinstance(candidate, dict):
            problem(key, 'object_required', 'Expected a party object.')
            continue
        party = Party()
        for part in Party.model_fields:
            setattr(party, part, value(Party, part, candidate, f'{key}.{part}'))
        setattr(invoice, key, party)

    for key, model in [('line_items', LineItem), ('adjustments', Adjustment)]:
        candidates = raw.get(key, [])
        if not isinstance(candidates, list):
            problem(key, 'list_required', 'Expected a list of rows.')
            continue
        rows = []
        for index, candidate in enumerate(candidates):
            row = model(description='')
            if not isinstance(candidate, dict):
                problem(f'{key}.{index}', 'object_required', 'Expected a row object; its position was preserved.')
            else:
                for part in model.model_fields:
                    parsed = value(model, part, candidate, f'{key}.{index}.{part}')
                    if parsed is not None:
                        setattr(row, part, parsed)
                if not row.description.strip():
                    problem(f'{key}.{index}.description', 'missing_description', 'Row description was not recovered.')
            rows.append(row)
        setattr(invoice, key, rows)

    evidence = raw.get('evidence', {})
    if not isinstance(evidence, dict):
        problem('evidence', 'object_required', 'Expected a field-to-snippet mapping.')
    else:
        for key, snippet in evidence.items():
            if key not in FACT_FIELDS:
                problem('evidence', 'unknown_field', 'Evidence refers to an unsupported field.')
                continue
            if not isinstance(snippet, str):
                problem(f'evidence.{key}', 'string_required', 'Evidence must be a string; the fact was retained without evidence.')
            else:
                invoice.evidence[key] = snippet
    provided_fields = set(raw) & FACT_FIELDS
    if invoice.document_type not in {'invoice', 'unknown'}:
        # Preserve an old extractor's generic document date without presenting it as an invoice date.
        if invoice.document_date is None and invoice.invoice_date is not None:
            invoice.document_date = invoice.invoice_date
            invoice.field_quality['document_date'] = invoice.field_quality.get('invoice_date', Quality.UNVERIFIED)
            provided_fields.add('document_date')
            provided_paths.add('document_date')
            if 'invoice_date' in invoice.evidence:
                invoice.evidence['document_date'] = invoice.evidence['invoice_date']
        # The issuer of a BOM/quote is not an identified invoice seller.
        for key in ('vendor_name', 'invoice_number', 'invoice_date', 'due_date'):
            setattr(invoice, key, None)
            invoice.field_quality.pop(key, None)
            invoice.evidence.pop(key, None)
    invoice.warnings = [f"Partial extraction: {issue['field']} ({issue['type']})" for issue in issues]
    return PartialExtraction(invoice, issues, provided_fields, provided_paths)


def blocking_warnings(invoice: ExtractedInvoice) -> list[str]:
    """Payment-critical issues block; optional scheduling metadata stays visible."""
    import re
    optional = {'due_date', 'sender', 'recipient', 'document_date', 'document_type', 'prices_include_tax'}
    if invoice.po_reference:
        optional.add('reference_text')  # Explicit PO resolution is checked separately.
    result = []
    for warning in invoice.warnings:
        match = re.match(r'^(?:Tiers disagree: |Model did not confirm parsed field: |Grounding failed: |Partial extraction: )(?:evidence\.)?([a-z_]+)(?:$|[. (])',warning)
        if match and match.group(1) in optional:
            continue
        # Item bookkeeping does not establish financial consistency. Keep the
        # description, part identifier and all numerical cells payment-critical.
        if re.match(r'^Partial extraction: (?:evidence\.)?line_items\.\d+\.(?:supplier_name|unit)(?:$|[. (])', warning):
            continue
        result.append(warning)
    return result
