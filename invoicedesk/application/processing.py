import time
import uuid
from pathlib import Path
from collections.abc import Iterator
from invoicedesk.domain.models import StageEvent
from invoicedesk.infrastructure.telemetry.logger import get_logger
from invoicedesk.infrastructure.documents import inspect_document
from invoicedesk.interfaces.progress import log_event
from invoicedesk.infrastructure.sqlite import Db, now
from invoicedesk.policy import Policy
from invoicedesk.infrastructure.extraction.llm import LlmClient
from invoicedesk.infrastructure.extraction.router import extract
from invoicedesk.infrastructure.extraction.sections import section_report
from invoicedesk.domain.normalize import normalize, normalize_po
from invoicedesk.domain import checks as engine
from invoicedesk.domain.comms import draft

def _run_invoice(path: Path, *, db: Db, policy: Policy, llm: LlmClient) -> Iterator[StageEvent]:
    if db.mode == 'demo':
        policy = policy.model_copy(update={'version': 'demo-1'})
    llm.calls=0; llm.cache_hit=False; llm.provenance=None; llm.usage={}; llm.response_ms=0.0; llm.validation_issues=[]; llm.provided_fields=set(); llm.provided_paths=set()
    started=time.monotonic(); run_id=uuid.uuid4().hex; events=[]; checks=[]
    def event(stage: str,state: str,title: str,detail: str='',results: list | None=None,**payload) -> StageEvent:
        e=StageEvent(run_id=run_id,seq=len(events)+1,stage=stage,state=state,title=title,detail=detail,checks=results or [],payload=payload,elapsed_ms=int((time.monotonic()-started)*1000))
        events.append(e);checks.extend(results or []);return e
    yield event('intake','started','Receive invoice')
    document = inspect_document(path)
    digest = document.sha256
    yield event('intake','completed','Invoice received',path.name,file_sha256=digest,
                media_type=document.media_type,size_bytes=document.size_bytes)
    yield event('extract','started','Extract and verify fields')
    inv,findings=extract(path,policy,llm)
    from invoicedesk.infrastructure.extraction.evidence import verify_references
    from invoicedesk.infrastructure.extraction.text import read_document_text
    try:
        pages = read_document_text(path).pages
    except Exception:
        pages = []
    references = verify_references(inv, pages)
    sections = section_report(inv, llm.validation_issues, llm.provided_fields, findings, llm.provided_paths, source_evidence=references)
    yield event('extract','completed','Extraction finished',inv.extraction_path,engine.integrity(inv,findings,policy),invoice=inv.model_dump(mode='json'),reconciliation=findings,cache_hit=llm.cache_hit,provenance=llm.provenance,llm_calls=llm.calls,usage=llm.usage,api_response_ms=llm.response_ms,token_usage=llm.token_usage,provider=llm.provider_name,model=llm.model,requested_model=llm.model,resolved_model=llm.resolved_model,source_evidence=[r.model_dump(mode="json") for r in references],sections=sections,validation_issues=llm.validation_issues)
    yield event('normalize','started','Normalize dates and money')
    norm=normalize(inv)
    yield event('normalize','completed','Invoice normalized',normalized=norm.model_dump(mode='json'))
    # Serialize state-dependent checks through persistence to prevent double spending.
    snapshot = db.snapshot()
    yield event('completeness','started','Check required fields')
    yield event('completeness','completed','Completeness checked',results=engine.completeness(inv,policy))
    yield event('vendor','started','Identify approved vendor')
    if inv.extraction_path == 'failed' or inv.document_type not in {'invoice', 'unknown'}:
        vendor = None
        results = [engine.check('vendor_identity', 'vendor', 'SKIP', 'Vendor-invoice checks are unavailable for incomplete extraction or a different document type.')]
    else:
        vendor,results=engine.vendor(inv,snapshot,policy)
    yield event('vendor','completed','Vendor checked',results=results)
    yield event('duplicates','started','Search previous submissions')
    yield event('duplicates','completed','Duplicates checked',results=engine.duplicates(inv,norm,vendor,snapshot,policy))
    yield event('po_match','started','Resolve purchase order')
    po,match_type,results=engine.resolve_po(inv,norm,vendor,snapshot,policy)
    yield event('po_match','completed','PO matching finished',results=results,po=po,match_type=match_type)
    yield event('amounts','started','Compare against remaining balance')
    norm=normalize_po(norm,po)
    yield event('amounts','completed','Amounts checked',results=engine.amounts(inv,norm,po,match_type,snapshot,policy),normalized=norm.model_dump(mode='json'))
    yield event('decide','started','Apply decision policy')
    from invoicedesk.domain.evaluation import evaluate
    recommendation = evaluate(inv,findings,snapshot,policy,pilot=db.mode == 'pilot',source_evidence=references)
    existing = {c.check_id for c in checks}
    additional = [c for c in recommendation.checks if c.check_id not in existing]
    context = recommendation.context
    decision = recommendation.decision
    yield event('decide','completed','Decision ready',decision.outcome.value,results=additional,decision=decision.model_dump(mode='json'),context=context.model_dump(mode='json'))
    yield event('explain','started','Prepare explanation and action')
    decision.comms=draft(decision,inv.invoice_number)
    yield event('explain','completed','Explanation prepared',decision.recommended_action,decision=decision.model_dump(mode='json'))
    final=event('complete','completed','Run saved',decision.outcome.value,decision=decision.model_dump(mode='json'),run_id=run_id)
    row=dict(run_id=run_id,created_at=now(),filename=path.name,file_sha256=digest,extraction_path=inv.extraction_path,
        extraction_json=inv.model_dump_json(),normalized_json=norm.model_dump_json(),vendor_id=vendor['vendor_id'] if vendor else None,
        matched_po=po['po_number'] if po else None,po_match_type=match_type,net_amount=str(norm.net_amount) if norm.net_amount is not None else None,
        gross_amount=str(norm.gross_amount) if norm.gross_amount is not None else None,decision=decision.outcome.value,decision_json=decision.model_dump_json(),
        review_status='pending' if db.mode == 'pilot' or decision.outcome.value in {'NEEDS_REVIEW','HOLD_MISSING_INFO'} else 'none',reviewer_note=None,reviewed_at=None,
        llm_used=llm.calls if inv.extraction_path!='deterministic' else 0,llm_cost_usd=None if llm.calls else '0',duration_ms=final.elapsed_ms)
    db.persist(row,events)
    yield final


def run_invoice(path: Path, *, db: Db, policy: Policy, llm: LlmClient) -> Iterator[StageEvent]:
    """Stream stages; state checks and final persistence are one serialized transaction."""
    started = time.perf_counter()
    logger = get_logger(__name__)
    stream = _run_invoice(path, db=db, policy=policy, llm=llm)
    status = 'cancelled'
    try:
        for event in stream:
            log_event(event)
            if event.stage == 'complete':
                status = 'completed'
                logger.info('Pipeline total response time: %.2f ms', (time.perf_counter()-started)*1000)
            yield event
    except Exception:
        status = 'failed'
        logger.error('Invoice processing stopped; uncommitted changes will be rolled back.')
        raise
    finally:
        stream.close()
        if db.conn.in_transaction:
            db.conn.rollback()
        if status != 'completed':
            logger.info('Pipeline total response time: %.2f ms (%s)', (time.perf_counter()-started)*1000, status)
