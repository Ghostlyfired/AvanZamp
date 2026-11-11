"""Durable processing, independent of SSE connections and API request lifetimes."""
from pathlib import Path
import hashlib
import time
from invoicedesk.domain.approval import Conflict
from invoicedesk.domain.contracts import DocumentFacts, SectionValidation, canonical, fingerprint
from invoicedesk.domain.evaluation import evaluate
from invoicedesk.domain.models import StageEvent
from invoicedesk.infrastructure.extraction.router import extract_document
from invoicedesk.infrastructure.preflight import validate_pilot_document
from .review import report_from


class JobService:
    def __init__(self, repository, storage):
        self.repo, self.storage = repository, storage

    def submit(self, data: bytes, filename: str, *, actor: str, roles: set[str], key: str) -> dict:
        if not roles & {'uploader', 'admin'}:
            raise PermissionError('Uploader role required.')
        from invoicedesk.infrastructure.documents import MAX_BYTES
        if not data or len(data) > MAX_BYTES:
            raise ValueError('Document must be non-empty and at most 20 MB.')
        # Quarantine first; full parser validation runs only inside the restricted worker.
        if not data.startswith((b'%PDF-', b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'RIFF')):
            raise ValueError('Unsupported document contents.')
        digest = hashlib.sha256(data).hexdigest()
        filename = Path(filename.replace('\\', '/')).name[:255] or 'document'
        def action(c, company):
            # Serialize publication with retention so a just-submitted shared hash cannot disappear.
            self.storage.put(data)
            return self.repo.submit(c, actor, digest, filename, 'application/octet-stream')
        return self.repo.command(actor, key, dict(action='submit', sha256=digest, filename=filename), action)

    def retry(self, job_id: str, *, actor: str, roles: set[str], key: str, revision: int) -> dict:
        def action(c, company):
            job = self.repo.job(c, job_id, actor, roles)
            incomplete = job['state'] == 'completed' and self.repo.report_at(c, job)['extraction']['extraction_path'] == 'failed'
            if (job['state'] != 'retry_required' and not incomplete) or job['revision'] != revision or job['attempt'] >= 3:
                raise Conflict('Retry requires a failed job, current revision, and fewer than three attempts.')
            self.repo.update_job(c, job_id, state='queued', error=None, provider_started=False)
            self.repo.audit(c, actor, 'explicit_retry', job_id, {'attempt': job['attempt']+1})
            return dict(job_id=job_id, state='queued', revision=revision)
        return self.repo.command(actor, key, dict(action='retry', job_id=job_id, revision=revision), action)

    def process(self, job: dict, llm) -> None:
        job_id, token = job['id'], job['lease_token']
        started = time.monotonic()
        def event(stage: str, title: str, state: str = 'completed', **payload):
            self.repo.append_event(job_id, token, StageEvent(run_id=job_id, seq=1, stage=stage,
                title=title, state=state, elapsed_ms=int((time.monotonic()-started)*1000), payload=payload).model_dump(mode='json'))
        with self.repo.engine.connect() as c:
            doc = self.repo.document(c, job)
            policy = self.repo.policy(c)
        path = self.storage.path(doc['sha256'])
        event('intake', 'Validate document', 'started')
        document = validate_pilot_document(path)
        if document.sha256 != doc['sha256']:
            raise ValueError('Document storage integrity check failed.')
        event('intake', 'Document validated', queue_ms=max(0, int((time.time()-job['created_at'])*1000)))
        event('extract', 'Extract printed facts', 'started')
        llm.on_provider_call = lambda: self.repo.provider_started(job_id, token)
        result = extract_document(path, policy, llm)
        inv, findings = result.invoice, result.findings
        # Provider failures are retryable only through an explicit user command, never a worker loop.
        if inv.extraction_path == 'failed':
            event('extract', 'Extraction incomplete; valid fields preserved', invoice=inv.model_dump(mode='json'))
        else:
            event('extract', 'Extraction completed', usage=result.usage, response_ms=result.response_ms,
                  api_calls=result.calls, cache_hit=result.cache_hit, provenance=result.provenance)
        sections = result.sections
        from invoicedesk.infrastructure.extraction.evidence import verify_references
        from invoicedesk.infrastructure.extraction.text import read_document_text
        try:
            pages = read_document_text(path).pages
        except Exception:
            pages = []
        references = verify_references(inv, pages)
        facts = DocumentFacts(document_sha256=document.sha256, raw_response_json=result.raw_response_json,
                              provenance=result.provenance or 'deterministic_parser',
                              extractor_fingerprint=result.fingerprint or fingerprint({'parser': 'tier1-1', 'document': document.sha256}),
                              evidence=tuple(references))
        event('decide', 'Evaluate procurement snapshot', 'started')
        with self.repo.engine.begin() as c:
            self.repo.lock(c)
            current_job = self.repo.job(c, job_id, job['owner'], {'admin'}, lock=True)
            self.repo.check_lease(current_job, token)
            revision = current_job['revision']+1
            current_policy = self.repo.policy(c)
            from invoicedesk.domain.reconciliation import reconcile
            findings = reconcile(inv, current_policy.extraction.reconcile_abs_tolerance)
            from invoicedesk.infrastructure.extraction.sections import section_report
            sections = section_report(inv, result.validation_issues, findings=findings, source_evidence=references)
            rec = evaluate(inv, findings, self.repo.snapshot(c, exclude=job_id), current_policy, revision, source_evidence=references)
            report = report_from(rec, inv, findings, sections)
            report['source_evidence'] = [ref.model_dump(mode='json') for ref in references]
            report['metrics'] = dict(queue_ms=max(0, int((job['started_at']-job['created_at'])*1000)),
                processing_ms=int((time.monotonic()-started)*1000), api_calls=result.calls, model=result.model, provider=result.provider,
                usage=result.usage, api_response_ms=result.response_ms, cache_hit=result.cache_hit,
                provenance=result.provenance or 'deterministic_parser', failure_kind=result.failure_kind, attempt=job['attempt'])
            review_state = 'pending'
            if rec.decision.outcome.value == 'AUTO_APPROVE':
                from invoicedesk.domain.approval import authorize_automatic
                amount = authorize_automatic(rec, current_policy)
                approval_job = dict(current_job, revision=revision)
                self.repo.post_ledger(c, approval_job, report, amount, 'system', 'All mandatory procurement checks passed.')
                self.repo.artifact(c, job_id, revision, 'approval_recheck', report)
                self.repo.audit(c, 'system', 'auto_approved', job_id, {'revision': revision, 'context': report['context']})
                review_state = 'approved'
            self.repo.artifact(c, job_id, revision, 'facts', facts.model_copy(update={'revision': revision}).model_dump(mode='json'))
            self.repo.artifact(c, job_id, revision, 'sections', {'sections': [SectionValidation(
                section=section['section'], status=section['status'], arithmetic_status=section['arithmetic_status'],
                verification_status=section['verification_status'], report_json=canonical(section)).model_dump(mode='json')
                for section in sections]})
            self.repo.artifact(c, job_id, revision, 'report', report)
            # Final event and terminal state commit atomically. SSE never writes either.
            from invoicedesk.infrastructure import schema as s
            import sqlalchemy as sa
            seq = (c.execute(sa.select(sa.func.max(s.events.c.seq)).where(s.events.c.job_id == job_id)).scalar() or 0)+1
            final = StageEvent(run_id=job_id, seq=seq, stage='complete', state='completed', title='Invoice approved and posted' if review_state=='approved' else 'Review report saved',
                               payload={'decision': report['decision'], 'revision': revision}, elapsed_ms=int((time.monotonic()-started)*1000))
            c.execute(s.events.insert().values(job_id=job_id, seq=seq, data=final.model_dump(mode='json')))
            self.repo.update_job(c, job_id, state='completed', revision=revision, error=result.failure_kind or ('partial_extraction' if result.validation_issues else None),
                                 lease_token=None, lease_until=None, review_state=review_state)
            self.repo.audit(c, 'worker', 'recommended', job_id, {'revision': revision, 'context': report['context']})
