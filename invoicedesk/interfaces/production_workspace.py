"""Authenticated operator UI adapter over the production application services.

The HTTP shell shares its presentation contract with local mode, while all durable
jobs, reports, permissions, procurement and ledger state remain in PostgreSQL.
Instances bound to an identity are request-local; credentials never cross requests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import re

import sqlalchemy as sa

from invoicedesk.application.jobs import JobService
from invoicedesk.application.review import ReviewService, report_from
from invoicedesk.application.source_review import verify_stored_source
from invoicedesk.domain.approval import Conflict
from invoicedesk.domain.contracts import canonical
from invoicedesk.domain.evaluation import evaluate
from invoicedesk.domain.master import Vendor, PurchaseOrder
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.domain.procurement import fulfillment
from invoicedesk.domain.reconciliation import reconcile
from invoicedesk.infrastructure import schema as s
from invoicedesk.infrastructure.documents import inspect_document, MAX_BYTES


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


class ProductionWorkspace:
    def __init__(self, repository, storage, identity=None):
        self.repo, self.storage, self.identity = repository, storage, identity
        self.jobs, self.reviews = JobService(repository, storage), ReviewService(repository)

    def bind(self, identity):
        return type(self)(self.repo, self.storage, identity)

    @property
    def actor(self):
        if self.identity is None:
            raise PermissionError('Authentication required.')
        return self.identity.subject

    @property
    def roles(self):
        if self.identity is None:
            raise PermissionError('Authentication required.')
        return set(self.identity.roles)

    def require(self, *roles):
        if not self.roles.intersection(roles):
            raise PermissionError('This action requires an authorized operator role.')

    def start(self):
        """The separately deployed durable worker owns production processing."""

    def close(self):
        pass

    def config(self):
        return dict(company='Acme Operations Inc', mode='production', provider=os.getenv('LLM_PROVIDER', 'openai'),
                    model=os.getenv('LLM_MODEL', ''), operator=self.actor, roles=sorted(self.roles),
                    workspace='production', upload_limit_bytes=MAX_BYTES)

    def reserve_vendor_id(self, key):
        self.require('admin')
        return self.repo.reserve_vendor_id(self.actor, key)

    def _job(self, c, identity):
        return self.repo.job(c, identity, self.actor, self.roles)

    def job(self, identity):
        with self.repo.engine.connect() as c:
            job = self._job(c, identity)
            doc = self.repo.document(c, job)
        path = self.storage.path(doc['sha256'])
        return dict(job, filename=doc['filename'], sha256=doc['sha256'],
                    media_type=self._media(doc), document_path=str(path) if path.is_file() else None)

    def _media(self, doc):
        path = self.storage.path(doc['sha256'])
        if not path.is_file():
            return 'application/octet-stream'
        try:
            return inspect_document(path).media_type
        except ValueError:
            return 'application/octet-stream'

    def _summary(self, job, doc, report=None):
        extraction = (report or {}).get('extraction', {})
        review = job['review_state']
        status = ('Not started' if job['state'] == 'queued' else
                  'Processing' if job['state'] == 'running' else
                  'Errored' if job['state'] == 'retry_required' else
                  'Approved' if review == 'approved' else
                  'Rejected' if review == 'rejected' else 'Needs review')
        return dict(id=job['id'], run_id=job['id'], state=job['state'], status=status,
                    revision=job['revision'], review_status=review,
                    created_at=timestamp(job['created_at']), filename=doc['filename'],
                    media_type=self._media(doc), imported=False,
                    invoice_number=extraction.get('invoice_number'),
                    issuer=extraction.get('vendor_name') or (extraction.get('sender') or {}).get('name'),
                    gross_amount=(report or {}).get('normalized', {}).get('gross_amount'),
                    net_amount=(report or {}).get('normalized', {}).get('net_amount'),
                    currency=extraction.get('currency'), document_type=extraction.get('document_type'),
                    vendor_id=(report or {}).get('vendor_id'), matched_po=(report or {}).get('po_number'),
                    error={'message': 'Processing needs attention. Retry the document or contact an operator.',
                           'code': job['error']} if job.get('error') and job['state'] == 'retry_required' else None)

    def invoices(self):
        with self.repo.engine.connect() as c:
            query = sa.select(s.jobs).order_by(s.jobs.c.created_at.desc(), s.jobs.c.id)
            if not self.roles.intersection({'admin', 'reviewer'}):
                query = query.where(s.jobs.c.owner == self.actor)
            result = []
            for row in c.execute(query).mappings():
                job = dict(row)
                doc = self.repo.document(c, job)
                report = self.repo.report_at(c, job) if job['revision'] else None
                if report and job['review_state'] == 'approved':
                    approval = c.execute(sa.select(s.artifacts.c.data).where(s.artifacts.c.job_id == job['id'],
                        s.artifacts.c.revision == job['revision'], s.artifacts.c.kind == 'approval_recheck')).scalar_one_or_none()
                    report = approval or report
                result.append(self._summary(job, doc, report))
            return result

    def events(self, identity):
        with self.repo.engine.connect() as c:
            self._job(c, identity)
            return list(c.execute(sa.select(s.events.c.data).where(s.events.c.job_id == identity)
                                  .order_by(s.events.c.seq)).scalars())

    def detail(self, identity):
        with self.repo.engine.connect() as c:
            job = self._job(c, identity)
            doc = self.repo.document(c, job)
        report = self.repo.report(identity, self.actor, self.roles) if job['revision'] else None
        if report:
            if job['review_state'] == 'approved' and report.get('approval'):
                report = dict(report['approval'], revision=job['revision'])
            report['summary'] = ('Invoice approved. Net amount posted to the PO ledger.'
                                 if job['review_state'] == 'approved' else report['decision']['summary'])
        summary = self._summary(job, doc, report)
        retry_available = job['state'] == 'retry_required' or (
            job['state'] == 'completed' and (report or {}).get('extraction', {}).get('extraction_path') == 'failed')
        return dict(summary, report=report, events=self.events(identity),
                    review_available=bool(job['state'] == 'completed' and job['review_state'] == 'pending'
                                          and self.roles.intersection({'admin', 'reviewer'})),
                    retry_available=retry_available and job['attempt'] < 3,
                    document_available=self.storage.path(doc['sha256']).is_file(),
                    activity=self.activity(entity='invoices', entity_id=identity),
                    attempts=[dict(id=identity, state=job['state'], created_at=timestamp(job['created_at']))])

    def developer(self, identity):
        self.require('admin', 'reviewer')
        detail = self.detail(identity)
        with self.repo.engine.connect() as c:
            self._job(c, identity)
            artifacts = {row['kind']: row['data'] for row in c.execute(sa.select(s.artifacts).where(
                s.artifacts.c.job_id == identity, s.artifacts.c.revision == detail['revision'])).mappings()}
        facts = artifacts.get('facts', {})
        raw = facts.get('raw_response_json')
        if raw:
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                pass
            artifacts.setdefault('provider', dict(raw_response=raw, provenance=facts.get('provenance')))
        return dict(id=identity, run_id=identity, events=detail['events'], error=detail['error'],
                    artifacts=artifacts, context=(detail['report'] or {}).get('context'),
                    raw_available=bool(artifacts.get('provider', {}).get('raw_response')))

    def submit(self, filename, content, key):
        result = self.jobs.submit(content, filename, actor=self.actor, roles=self.roles, key=key)
        return dict(result, id=result['job_id'])

    def review(self, identity, approve, revision, reason, confirmed_source, key):
        result = self.reviews.act(identity, actor=self.actor, roles=self.roles, key=key,
                                  revision=revision, approve=approve, reason=reason,
                                  confirmed_source=confirmed_source)
        return dict(result, id=result['job_id'])

    def retry(self, identity, revision, key):
        result = self.jobs.retry(identity, actor=self.actor, roles=self.roles, key=key, revision=revision)
        return dict(result, id=result['job_id'])

    def recheck(self, identity, revision, key):
        self.require('admin', 'reviewer')

        def action(c, company):
            job = self.repo.job(c, identity, self.actor, self.roles, review=True)
            if job['revision'] != revision or job['state'] != 'completed' or job['review_state'] != 'pending':
                raise Conflict('Only the current pending report can be rechecked.')
            previous = self.repo.report_at(c, job)
            doc = self.repo.document(c, job)
            invoice, references, source = verify_stored_source(
                ExtractedInvoice.model_validate(previous['extraction']),
                self.storage.path(doc['sha256']), doc['sha256'])
            policy = self.repo.policy(c)
            findings = reconcile(invoice, policy.extraction.reconcile_abs_tolerance)
            current = evaluate(invoice, findings, self.repo.snapshot(c, exclude=identity), policy,
                               revision + 1, source_evidence=tuple(references))
            from invoicedesk.infrastructure.extraction.sections import section_report
            report = report_from(current, invoice, findings,
                                 section_report(invoice, findings=findings, source_evidence=references))
            report['source_validation'] = source
            report['source_evidence'] = [ref.model_dump(mode='json') for ref in references]
            self.repo.artifact(c, identity, revision + 1, 'report', report)
            self.repo.update_job(c, identity, revision=revision + 1)
            self.repo.audit(c, self.actor, 'rechecked', identity, {'previous_revision': revision})
            return dict(id=identity, revision=revision + 1)

        return self.repo.command(self.actor, key, dict(action='recheck', id=identity, revision=revision), action)

    def _master_rows(self, c, entity):
        table = s.vendors if entity == 'vendors' else s.pos
        return [dict(data) for data in c.execute(sa.select(table.c.data)).scalars()]

    def records(self, entity):
        self.require('admin', 'reviewer')
        invoices = self.invoices()
        with self.repo.engine.connect() as c:
            revision = c.execute(sa.select(s.company.c.revision)).scalar_one()
            rows = self._master_rows(c, entity)
            pos = self._master_rows(c, 'purchase-orders')
            vendors = {row['vendor_id']: row['name'] for row in self._master_rows(c, 'vendors')}
            balances, accepted_counts = {}, {}
            for po, amount in c.execute(sa.select(s.ledger.c.po_number, s.ledger.c.amount)):
                balances[po] = balances.get(po, Decimal(0)) + amount
                accepted_counts[po] = accepted_counts.get(po, 0) + 1
        for row in rows:
            row['revision'] = revision
            if entity == 'vendors':
                row['aliases'] = json.loads(row.pop('aliases_json', '[]'))
                row['invoice_count'] = sum(inv['vendor_id'] == row['vendor_id'] for inv in invoices)
                row['po_count'] = sum(po['vendor_id'] == row['vendor_id'] for po in pos)
            else:
                approved, net = balances.get(row['po_number'], Decimal(0)), Decimal(row['net_amount'])
                row.update(**fulfillment(net, approved),
                           accepted_invoice_count=accepted_counts.get(row['po_number'], 0),
                           pending_amount=str(sum((Decimal(inv['net_amount']) for inv in invoices
                                                   if inv['matched_po'] == row['po_number']
                                                   and inv['status'] == 'Needs review' and inv.get('net_amount')), Decimal(0))),
                           receipt_count=sum(inv['matched_po'] == row['po_number'] for inv in invoices),
                           vendor_name=vendors.get(row['vendor_id'], 'Unavailable'))
        return rows

    def record(self, entity, identity):
        field = 'vendor_id' if entity == 'vendors' else 'po_number'
        row = next((row for row in self.records(entity) if row[field] == identity), None)
        if row is None:
            raise LookupError('Record not found.')
        row['invoices'] = [inv for inv in self.invoices() if inv['vendor_id' if entity == 'vendors' else 'matched_po'] == identity]
        row['activity'] = self.activity(entity=entity, entity_id=identity)
        return row

    def _validate_master(self, entity, data):
        raw = dict(data)
        raw.pop('revision', None)
        if entity == 'vendors':
            raw['aliases_json'] = canonical(raw.pop('aliases', []))
            model = Vendor.model_validate(raw)
        else:
            model = PurchaseOrder.model_validate(raw)
        result = model.model_dump(mode='json')
        identity = result['vendor_id' if entity == 'vendors' else 'po_number']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identity):
            raise ValueError('IDs must contain letters, digits, hyphens or underscores.')
        return result

    def save_record(self, entity, data, key, identity=None):
        self.require('admin')
        validated = self._validate_master(entity, data)
        field = 'vendor_id' if entity == 'vendors' else 'po_number'
        selected = validated[field]
        if identity and selected != identity:
            raise ValueError('Record IDs cannot change.')

        def action(c, company):
            table = s.vendors if entity == 'vendors' else s.pos
            old = c.execute(sa.select(table.c.data).where(table.c[field] == selected)).scalar_one_or_none()
            if identity:
                if old is None:
                    raise LookupError('Record not found.')
                if company['revision'] != data.get('revision'):
                    raise Conflict('Procurement changed. Reload the record and try again.')
            elif old is not None:
                raise Conflict('This ID is already used. Choose another ID.')
            if entity == 'purchase-orders':
                if not c.execute(sa.select(s.vendors.c.vendor_id).where(s.vendors.c.vendor_id == validated['vendor_id'])).first():
                    raise ValueError('Choose an existing vendor.')
                approved = sum(c.execute(sa.select(s.ledger.c.amount).where(s.ledger.c.po_number == selected)).scalars(), Decimal(0))
                if Decimal(validated['net_amount']) < approved:
                    raise Conflict('PO amount cannot be reduced below approved postings.')
                reports = c.execute(sa.select(s.artifacts.c.data).where(s.artifacts.c.kind == 'report')).scalars()
                used = any(report.get('po_number') == selected for report in reports)
                if old and used and any(old[name] != validated[name] for name in ('vendor_id', 'currency')):
                    raise Conflict('Vendor and currency cannot change after invoice receipt.')
            self.repo.administer(c, 'vendor' if entity == 'vendors' else 'po', validated)
            self.repo.audit(c, self.actor, 'updated' if identity else 'created', selected,
                            dict(entity=entity, data=validated))
            return {field: selected, 'revision': company['revision'] + 1}

        return self.repo.command(self.actor, key, dict(action='save', entity=entity, identity=identity, data=data), action)

    def toggle_vendor(self, identity, blocked, revision, reason, key):
        self.require('admin')
        row = self.record('vendors', identity)
        data = {field: row[field] for field in ('vendor_id', 'name', 'aliases', 'notes')}
        data.update(status='blocked' if blocked else 'approved', revision=revision)
        return self.save_record('vendors', data, key, identity)

    def delete_record(self, entity, identity, revision, confirmation, key):
        self.require('admin')
        if confirmation != identity:
            raise ValueError('Type the record ID to confirm deletion.')

        def action(c, company):
            if company['revision'] != revision:
                raise Conflict('Procurement changed. Reload the record and try again.')
            table, field = (s.vendors, 'vendor_id') if entity == 'vendors' else (s.pos, 'po_number')
            old = c.execute(sa.select(table.c.data).where(table.c[field] == identity)).scalar_one_or_none()
            if old is None:
                raise LookupError('Record not found.')
            reports = c.execute(sa.select(s.artifacts.c.data).where(s.artifacts.c.kind.in_(['report', 'approval_recheck']))).scalars()
            report_field = 'vendor_id' if entity == 'vendors' else 'po_number'
            used = any(report.get(report_field) == identity for report in reports)
            if entity == 'vendors':
                used = used or any(po['vendor_id'] == identity for po in self._master_rows(c, 'purchase-orders'))
            if used:
                raise Conflict('Referenced records cannot be deleted. Block the vendor or close the PO instead.')
            c.execute(table.delete().where(table.c[field] == identity))
            self.repo.bump_revision(c)
            self.repo.audit(c, self.actor, 'deleted', identity, dict(entity=entity, data=old))
            return dict(deleted=True)

        return self.repo.command(self.actor, key, dict(action='delete', entity=entity, id=identity, revision=revision), action)

    def activity(self, *, entity='', entity_id='', action='', start='', end=''):
        roles = self.roles
        with self.repo.engine.connect() as c:
            if not roles.intersection({'admin', 'reviewer'}):
                if entity_id:
                    self._job(c, entity_id)
                owned = set(c.execute(sa.select(s.jobs.c.id).where(s.jobs.c.owner == self.actor)).scalars())
            else:
                owned = None
            known_jobs = set(c.execute(sa.select(s.jobs.c.id)).scalars())
            rows = c.execute(sa.select(s.audit).order_by(s.audit.c.created_at.desc())).mappings()
            result = []
            for row in rows:
                if owned is not None and row['target'] not in owned:
                    continue
                resource = 'invoices' if row['target'] in known_jobs else row['data'].get('entity', 'company')
                if entity and resource != entity or entity_id and row['target'] != entity_id or action and row['action'] != action:
                    continue
                date = timestamp(row['created_at'])
                if start and date[:10] < start or end and date[:10] > end:
                    continue
                result.append(dict(created_at=date, actor=row['actor'], entity=resource,
                                   entity_id=row['target'], action=row['action'], details=row['data']))
            return result
