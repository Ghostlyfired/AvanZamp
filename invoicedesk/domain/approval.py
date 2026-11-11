"""Human authorization invariants. No persistence, authentication or transport."""
from decimal import Decimal
from .contracts import Recommendation
from .models import ExtractedInvoice, NormalizedInvoice, Outcome
from .policy import Policy
from .validation import blocking_warnings


class Conflict(ValueError):
    pass


class ApprovalBlocked(ValueError):
    pass


def authorize_automatic(recommendation: Recommendation, policy: Policy) -> Decimal:
    """Automation cannot exercise any of a human reviewer's exception powers."""
    if recommendation.decision.outcome != Outcome.AUTO_APPROVE:
        raise ApprovalBlocked('Automatic approval requires all mandatory checks to pass.')
    return authorize(recommendation, policy, '', False)


def authorize(recommendation: Recommendation, policy: Policy, reason: str,
              confirmed_source: bool) -> Decimal:
    inv = ExtractedInvoice.model_validate_json(recommendation.interpretation.invoice_json)
    norm = NormalizedInvoice.model_validate_json(recommendation.interpretation.normalized_json)
    if inv.document_type != 'invoice' or inv.extraction_path == 'failed':
        raise ApprovalBlocked('A complete, explicitly identified invoice is required.')
    if any(getattr(inv, name, None) is None for name in policy.critical_fields):
        raise ApprovalBlocked('Required facts remain unresolved.')
    if not norm.invoice_number_key:
        raise ApprovalBlocked('A usable normalized invoice number is required.')
    if not recommendation.vendor_id or not recommendation.po_number:
        raise ApprovalBlocked('A confirmed vendor and PO are required.')
    if not inv.currency or norm.currency != policy.currency or norm.net_amount is None or norm.net_amount <= 0 or inv.total is None or inv.total <= 0:
        raise ApprovalBlocked('A positive amount in the approval currency is required.')
    if inv.tax_amount is not None and inv.tax_amount < 0:
        raise ApprovalBlocked('Negative tax requires a corrected invoice or a separate review workflow.')
    if recommendation.decision.outcome in {Outcome.REJECT, Outcome.HOLD_MISSING_INFO}:
        raise ApprovalBlocked('Current procurement checks block approval.')
    if any(c.check_id == 'po_vendor_consistency' and c.proposed for c in recommendation.checks):
        raise ApprovalBlocked('The PO must belong to the invoice vendor.')
    # Confirmation can resolve source uncertainty, but never an arithmetic failure or partial parse.
    integrity = next(c for c in recommendation.checks if c.check_id == 'extraction_integrity')
    warnings = blocking_warnings(inv)
    # A reviewer who checked the printed source may resolve a missing citation
    # for a charge row manually. Keep other extraction warnings and every failed
    # arithmetic finding blocking, including automatic posting.
    manual_adjustment_confirmation = (
        confirmed_source
        and bool(reason.strip())
        and any(c.check_id == 'source_grounding' and c.proposed for c in recommendation.checks)
        and any(warning.startswith('Grounding failed: adjustments') for warning in warnings)
    )
    if manual_adjustment_confirmation:
        warnings = [warning for warning in warnings if not warning.startswith('Grounding failed: adjustments')]
    if warnings or any(not f['passed'] for f in integrity.evidence['findings']):
        raise ApprovalBlocked('Resolve extraction issues in a new revision before approval.')
    if not any(f.get('scope') == 'total' and f['passed'] for f in integrity.evidence['findings']):
        raise ApprovalBlocked('Net/tax arithmetic is unresolved.')
    exceptions = [c for c in recommendation.checks if c.proposed == Outcome.NEEDS_REVIEW]
    if exceptions and not reason.strip():
        raise ApprovalBlocked('A recorded reason is required for a review exception.')
    if (integrity.proposed or any(c.check_id == 'source_grounding' and c.proposed for c in exceptions)) and not confirmed_source:
        raise ApprovalBlocked('Confirm the invoice against its source before approval.')
    if norm.net_amount >= Decimal('1e18') or norm.net_amount != norm.net_amount.quantize(Decimal('0.000001')):
        raise ApprovalBlocked('Approval amounts support at most six decimal places and must be below 1e18.')
    return norm.net_amount
