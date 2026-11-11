"""One pure recommendation function shared by workers and approval rechecks."""
from . import checks as engine
from .contracts import ProcurementSnapshot, Recommendation, EvidenceReference, context_for, interpret
from .models import ExtractedInvoice
from .normalize import normalize, normalize_po
from .policy import Policy
from .decide import decide
from .comms import draft


def evaluate(invoice: ExtractedInvoice, findings: list[dict], snapshot: ProcurementSnapshot,
             policy: Policy, revision: int = 1, *, pilot: bool = True,
             source_evidence: tuple[EvidenceReference, ...] = ()) -> Recommendation:
    inv = invoice.model_copy(deep=True)
    norm = normalize(inv)
    checks = engine.integrity(inv, findings, policy) + engine.completeness(inv, policy)
    if pilot:
        if (inv.total is not None and inv.total <= 0) or (inv.tax_amount is not None and inv.tax_amount < 0):
            checks.append(engine.check('financial_semantics', 'amounts', 'WARN',
                'Non-positive totals or negative tax require a separate review workflow.', 'NEEDS_REVIEW'))
        grounded = {e.path for e in source_evidence if e.verification in {'VERIFIED', 'GROUNDED'}}
        critical = [*policy.critical_fields, 'currency', 'subtotal', 'tax_amount', 'po_reference']
        missing_evidence = [name for name in critical if getattr(inv, name, None) is not None
                            and name not in grounded]
        required_cells = [f'line_items.{index}.{name}' for index, row in enumerate(inv.line_items)
                          for name, value in row.model_dump().items()
                          if name in {'description', 'part_number', 'quantity', 'unit_price', 'amount'} and value is not None]
        required_cells.extend(f'adjustments.{index}.{name}' for index, row in enumerate(inv.adjustments)
                              for name in ('description', 'amount') if getattr(row, name) is not None)
        paths = grounded
        missing_evidence.extend(path for path in required_cells if path not in paths)
        if missing_evidence:
            supplied = {e.path for e in source_evidence}
            unverified = [p for p in missing_evidence if p in supplied]
            absent = [p for p in missing_evidence if p not in supplied]
            checks.append(engine.check('source_grounding', 'extract', 'WARN',
                f'Source confirmation required: {len(unverified)} values have unverified evidence; {len(absent)} have no evidence.',
                'NEEDS_REVIEW', unverified_paths=unverified, absent_paths=absent))
    if pilot and inv.document_type == 'unknown':
        checks.append(engine.check('document_interpretation', 'completeness', 'WARN',
                                   'Document type is unresolved; confirm an invoice before approval.', 'NEEDS_REVIEW'))
    if inv.extraction_path == 'failed' or inv.document_type not in {'invoice', 'unknown'}:
        vendor = None
    else:
        vendor, results = engine.vendor(inv, snapshot, policy)
        checks.extend(results)
    checks.extend(engine.duplicates(inv, norm, vendor, snapshot, policy))
    po, match_type, results = engine.resolve_po(inv, norm, vendor, snapshot, policy)
    checks.extend(results)
    norm = normalize_po(norm, po)
    checks.extend(engine.amounts(inv, norm, po, match_type, snapshot, policy))
    decision = decide(checks, po)
    decision.comms = draft(decision, inv.invoice_number)
    context = context_for(snapshot, policy, revision).model_copy(update={'profile': 'pilot' if pilot else 'demo'})
    return Recommendation(interpret(inv, norm, revision), decision, tuple(checks),
                          context,
                          vendor['vendor_id'] if vendor else None, po['po_number'] if po else None, match_type)
