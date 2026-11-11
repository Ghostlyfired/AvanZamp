from invoicedesk.domain.models import Decision, PRECEDENCE, Outcome, CheckResult

def decide(checks: list[CheckResult], po: dict | None) -> Decision:
    outcome=next((o for o in PRECEDENCE if any(c.proposed==o for c in checks)),Outcome.AUTO_APPROVE)
    driving=[c for c in checks if c.proposed==outcome]
    reasons=[c.detail for c in driving]+[c.detail for c in checks if c not in driving and c.status in {'WARN','INFO'}]
    if outcome == Outcome.AUTO_APPROVE:
        reasons = [c.detail for c in checks if c.check_id in {'vendor_status','po_resolution','amount_vs_balance','po_scope_consistency','po_charge_consistency','po_tax_consistency'} and c.status in {'PASS','INFO','WARN'}]
    actions={Outcome.AUTO_APPROVE:'Approve automatically and record the validated net amount against the purchase order.',Outcome.HOLD_MISSING_INFO:'Request a corrected invoice with the missing fields.',
        Outcome.REJECT:'Reject this invoice and resolve the flagged checks.',Outcome.NEEDS_REVIEW:'Review the evidence before authorizing payment.'}
    ids={c.check_id for c in driving}; action=actions[outcome]
    if 'vendor_status' in ids:action='Escalate to procurement: vendor is blocked.'
    elif 'workflow_applicability' in ids:action='Route the parsed document to the appropriate project-budget or advance-request review; vendor-invoice payment is not authorized.'
    elif 'duplicate_exact' in ids:action='Do not pay again; compare the original submission.'
    elif 'duplicate_near' in ids:action='Compare the prior invoice and confirm whether this is a resubmission.'
    elif 'stacked_uncertainty' in ids and po:action=f"Confirm the scan and approve against {po['po_number']} if correct."
    elif 'currency_consistency' in ids:action='Confirm the printed currency against the purchase order; no FX conversion is performed.'
    elif 'po_resolution' in ids:action='Confirm the purchase order from the invoice contents and available PO candidates; correct the printed reference if necessary.'
    elif 'po_charge_consistency' in ids:action='Ask procurement to authorize the additional charges or request a corrected invoice without them.'
    elif 'po_scope_consistency' in ids:action='Compare invoice items, quantities and prices with the purchase order; request a corrected invoice or an authorized PO amendment.'
    elif 'po_tax_consistency' in ids:action='Confirm the tax basis with procurement and request a corrected invoice or PO tax rate.'
    elif 'source_grounding' in ids or 'extraction_integrity' in ids:action='Check the extracted values against the source document before approving.'
    elif 'amount_vs_balance' in ids:action='Review the variance with procurement.' if outcome==Outcome.NEEDS_REVIEW else 'Dispute the overbilling with the vendor.'
    summaries={Outcome.AUTO_APPROVE:'Invoice approved automatically: required facts and procurement checks passed.',
        Outcome.NEEDS_REVIEW:'Invoice needs review before approval.',Outcome.HOLD_MISSING_INFO:'Invoice is on hold because required information is missing.',
        Outcome.REJECT:'Invoice rejected because a blocking procurement check failed.'}
    summary = 'Document sections parsed; vendor-invoice approval is not applicable.' if 'workflow_applicability' in ids else summaries[outcome]
    return Decision(outcome=outcome,summary=summary,reasons=reasons or ['All applicable checks passed.'],driving_checks=[c.check_id for c in driving],recommended_action=action)
