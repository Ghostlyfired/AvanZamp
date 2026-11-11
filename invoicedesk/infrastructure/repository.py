"""PostgreSQL repository. Company-row locks serialize the single company's mutations."""
import time
import uuid
from decimal import Decimal
from typing import Callable
import sqlalchemy as sa
from invoicedesk.domain.contracts import ProcurementSnapshot, canonical, fingerprint
from invoicedesk.domain.approval import Conflict
from invoicedesk.domain.policy import Policy
from . import schema as s


def uid() -> str:
    return uuid.uuid4().hex


class Repository:
    def __init__(self, url: str, *, allow_sqlite: bool = False):
        if not url.startswith('postgresql+psycopg://') and not (allow_sqlite and url.startswith('sqlite')):
            raise ValueError('Pilot requires postgresql+psycopg. SQLite is an explicit test adapter only.')
        self.engine = sa.create_engine(url, pool_pre_ping=True)

    def lock(self, c) -> dict:
        if self.engine.dialect.name == 'sqlite':
            c.exec_driver_sql('BEGIN IMMEDIATE')
        row = c.execute(sa.select(s.company).where(s.company.c.id == 1).with_for_update()).mappings().first()
        if row is None:
            raise RuntimeError('Initialize procurement data before starting the pilot.')
        return dict(row)

    def initialize(self, policy: Policy, vendors: list[dict], pos: list[dict]) -> None:
        with self.engine.begin() as c:
            if c.execute(sa.select(s.company.c.id)).first():
                raise Conflict('Pilot already initialized; use revision-aware administration.')
            c.execute(s.company.insert().values(id=1, revision=1, policy=policy.model_dump(mode='json')))
            for row in vendors:
                c.execute(s.vendors.insert().values(vendor_id=row['vendor_id'], data=row))
            for row in pos:
                c.execute(s.pos.insert().values(po_number=row['po_number'], data=row, net_amount=Decimal(row['net_amount'])))
            from invoicedesk.domain.procurement import next_vendor_number
            c.execute(s.sequences.insert().values(key='vendor', value=next_vendor_number([v['vendor_id'] for v in vendors])-1))
            self.audit(c, 'system', 'initialize', 'company', {'revision': 1})

    def policy(self, c) -> Policy:
        return Policy.model_validate(c.execute(sa.select(s.company.c.policy)).scalar_one())

    def snapshot(self, c, exclude: str | None = None) -> ProcurementSnapshot:
        revision = c.execute(sa.select(s.company.c.revision)).scalar_one()
        vendors = list(c.execute(sa.select(s.vendors.c.data)).scalars())
        pos = list(c.execute(sa.select(s.pos.c.data)).scalars())
        rows = c.execute(sa.select(s.artifacts).where(s.artifacts.c.kind == 'report')).mappings()
        latest = {}
        for row in rows:
            if row['job_id'] == exclude:
                continue
            old = latest.get(row['job_id'])
            if old is None or row['revision'] > old['revision']:
                latest[row['job_id']] = row
        # Include pending interpretations in duplicate detection; only approved ledger entries consume money.
        commitments = set(c.execute(sa.select(s.ledger.c.job_id)).scalars())
        runs = []
        for job_id, row in latest.items():
            report = row['data']
            runs.append(dict(run_id=job_id, vendor_id=report['vendor_id'],
                             matched_po=report['po_number'], extraction_json=canonical(report['extraction']),
                             review_status='approved' if job_id in commitments else 'pending',
                             decision_json=canonical(report['decision']),
                             normalized_json=canonical(report['normalized'])))
        balances: dict[str, Decimal] = {}
        for po, amount in c.execute(sa.select(s.ledger.c.po_number, s.ledger.c.amount)):
            balances[po] = balances.get(po, Decimal(0)) + amount
        return ProcurementSnapshot(revision, canonical(vendors), canonical(pos), canonical(runs),
                                   tuple(sorted((po, str(amount)) for po, amount in balances.items())))

    def command(self, actor: str, key: str, payload: dict, action: Callable) -> dict:
        if not key or len(key) > 128:
            raise ValueError('An idempotency key of 1–128 characters is required.')
        digest = fingerprint(payload)
        with self.engine.begin() as c:
            company = self.lock(c)
            prior = c.execute(sa.select(s.commands).where(s.commands.c.actor == actor, s.commands.c.key == key)).mappings().first()
            if prior:
                if prior['fingerprint'] != digest:
                    raise Conflict('Idempotency key was already used for a different command.')
                return prior['result']
            result = action(c, company)
            c.execute(s.commands.insert().values(actor=actor, key=key, fingerprint=digest, result=result))
            return result

    def reserve_vendor_id(self, actor, key):
        from invoicedesk.domain.procurement import next_vendor_number
        def action(c, company):
            previous = c.execute(sa.select(s.sequences.c.value).where(s.sequences.c.key=='vendor')).scalar_one_or_none()
            number = next_vendor_number(c.execute(sa.select(s.vendors.c.vendor_id)).scalars(), previous or 0)
            if previous is None:
                c.execute(s.sequences.insert().values(key='vendor',value=number))
            else:
                c.execute(s.sequences.update().where(s.sequences.c.key=='vendor').values(value=number))
            return {'vendor_id': f'V-{number:03d}'}
        return self.command(actor,key,dict(action='reserve_vendor_id'),action)

    def audit(self, c, actor: str, action: str, target: str, data: dict) -> None:
        c.execute(s.audit.insert().values(id=uid(), actor=actor, action=action, target=target, data=data, created_at=time.time()))

    def artifact(self, c, job_id: str, revision: int, kind: str, data: dict) -> None:
        c.execute(s.artifacts.insert().values(job_id=job_id, revision=revision, kind=kind, data=data,
                                            sha256=fingerprint(data), created_at=time.time()))

    def job(self, c, job_id: str, actor: str, roles: set[str], *, review: bool = False, lock: bool = False) -> dict:
        query = sa.select(s.jobs).where(s.jobs.c.id == job_id)
        if lock:
            query = query.with_for_update()
        row = c.execute(query).mappings().first()
        if row is None or (row['owner'] != actor and not roles & {'reviewer', 'admin'}):
            raise LookupError('Job not found.')
        if review and not roles & {'reviewer', 'admin'}:
            raise PermissionError('Reviewer role required.')
        return dict(row)

    def report_at(self, c, job: dict) -> dict:
        row = c.execute(sa.select(s.artifacts.c.data).where(s.artifacts.c.job_id == job['id'],
                        s.artifacts.c.revision == job['revision'], s.artifacts.c.kind == 'report')).first()
        if row is None:
            raise Conflict('The report is not ready.')
        return row[0]

    def report(self, job_id: str, actor: str, roles: set[str], revision: int | None = None) -> dict:
        with self.engine.connect() as c:
            job = self.job(c, job_id, actor, roles)
            current_revision = job['revision']
            if revision is not None:
                if not 1 <= revision <= job['revision']:
                    raise LookupError('Revision not found.')
                job['revision'] = revision
            result = dict(self.report_at(c, job), review_state=job['review_state'] if job['revision'] == current_revision else 'superseded',
                          revision=job['revision'], current_revision=current_revision)
            result['revisions'] = list(c.execute(sa.select(s.artifacts.c.revision).where(
                s.artifacts.c.job_id == job_id, s.artifacts.c.kind == 'report').order_by(s.artifacts.c.revision)).scalars())
            approval = c.execute(sa.select(s.artifacts.c.data).where(s.artifacts.c.job_id == job_id,
                s.artifacts.c.revision == job['revision'], s.artifacts.c.kind == 'approval_recheck')).scalar_one_or_none()
            result['approval'] = approval
            if roles & {'reviewer', 'admin'}:
                result['audit'] = [dict(row) for row in c.execute(sa.select(s.audit).where(s.audit.c.target == job_id)
                                   .order_by(s.audit.c.created_at)).mappings()]
            else:
                # A submitter's own report must not reveal every other invoice in the audit snapshot.
                result['context'] = dict(result['context'], snapshot_json=None)
                result['checks'] = [dict(check, evidence={}) if check['check_id'].startswith('duplicate_') else check
                                    for check in result['checks']]
                if approval:
                    result['approval'] = dict(approval, context=dict(approval['context'], snapshot_json=None),
                        checks=[dict(check, evidence={}) if check['check_id'].startswith('duplicate_') else check
                                for check in approval['checks']])
            return result

    def append_event(self, job_id: str, token: str, event: dict) -> None:
        with self.engine.begin() as c:
            job = c.execute(sa.select(s.jobs).where(s.jobs.c.id == job_id).with_for_update()).mappings().one()
            self.check_lease(job, token)
            seq = (c.execute(sa.select(sa.func.max(s.events.c.seq)).where(s.events.c.job_id == job_id)).scalar() or 0) + 1
            event = dict(event, run_id=job_id, seq=seq)
            c.execute(s.events.insert().values(job_id=job_id, seq=seq, data=event))
        # Consumers read committed events; there is no socket or subscriber inside this transaction.

    @staticmethod
    def check_lease(job: dict, token: str) -> None:
        if job['state'] != 'running' or job['lease_token'] != token or job['lease_until'] < time.time():
            raise Conflict('Worker lease expired or was superseded.')

    def claim(self, lease_seconds: int = 90) -> dict | None:
        with self.engine.begin() as c:
            # A crashed call may already have been billed. Never automatically retry it.
            expired = c.execute(s.jobs.update().where(s.jobs.c.state == 'running', s.jobs.c.lease_until < time.time())
                      .values(state='retry_required', lease_token=None, lease_until=None, error='worker_lease_expired').returning(s.jobs.c.id)).scalars().all()
            for job_id in expired:
                self.audit(c, 'worker', 'lease_expired', job_id, {'explicit_retry_required': True})
            row = c.execute(sa.select(s.jobs).where(s.jobs.c.state == 'queued').order_by(s.jobs.c.created_at)
                            .with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not row:
                return None
            token = uid()
            values = dict(state='running', lease_token=token, lease_until=time.time()+lease_seconds,
                          started_at=time.time(), heartbeat_at=time.time(), attempt=row['attempt']+1)
            c.execute(s.jobs.update().where(s.jobs.c.id == row['id']).values(**values))
            return dict(row, **values)

    def heartbeat(self, job_id: str, token: str, lease_seconds: int = 90) -> bool:
        with self.engine.begin() as c:
            result = c.execute(s.jobs.update().where(s.jobs.c.id == job_id, s.jobs.c.state == 'running',
                               s.jobs.c.lease_token == token, s.jobs.c.lease_until >= time.time())
                               .values(heartbeat_at=time.time(), lease_until=time.time()+lease_seconds))
            return result.rowcount == 1

    def fail(self, job_id: str, token: str, code: str) -> None:
        with self.engine.begin() as c:
            result = c.execute(s.jobs.update().where(s.jobs.c.id == job_id, s.jobs.c.state == 'running', s.jobs.c.lease_token == token)
                      .values(state='retry_required', error=code, lease_token=None, lease_until=None))
            if not result.rowcount:
                return
            self.audit(c, 'worker', 'processing_failed', job_id, {'code': code})

    def submit(self, c, actor: str, digest: str, filename: str, media_type: str) -> dict:
        doc_id, job_id = uid(), uid()
        c.execute(s.documents.insert().values(id=doc_id, sha256=digest, owner=actor,
                  filename=filename, media_type=media_type, created_at=time.time()))
        c.execute(s.jobs.insert().values(id=job_id, document_id=doc_id, owner=actor, state='queued',
                  created_at=time.time(), attempt=0, revision=0, review_state='pending', provider_started=False))
        self.audit(c, actor, 'submitted', job_id, {'document_sha256': digest})
        return {'job_id': job_id, 'revision': 0, 'state': 'queued'}

    def document(self, c, job: dict) -> dict:
        return dict(c.execute(sa.select(s.documents).where(s.documents.c.id == job['document_id'])).mappings().one())

    def update_job(self, c, job_id: str, **values) -> None:
        c.execute(s.jobs.update().where(s.jobs.c.id == job_id).values(**values))

    def post_ledger(self, c, job: dict, report: dict, amount: Decimal, actor: str, reason: str) -> None:
        c.execute(s.ledger.insert().values(job_id=job['id'], revision=job['revision'], po_number=report['po_number'],
                  vendor_id=report['vendor_id'], invoice_key=report['normalized']['invoice_number_key'], amount=amount,
                  currency=report['normalized']['currency'], actor=actor, reason=reason,
                  context=report['context'], created_at=time.time()))
        self.bump_revision(c)

    def bump_revision(self, c) -> None:
        c.execute(s.company.update().where(s.company.c.id == 1).values(revision=s.company.c.revision + 1))

    def approved_duplicate(self, c, vendor_id: str, invoice_key: str) -> bool:
        return c.execute(sa.select(s.ledger.c.job_id).where(s.ledger.c.vendor_id == vendor_id,
                         s.ledger.c.invoice_key == invoice_key)).first() is not None

    def history(self, actor: str, roles: set[str], limit: int = 50, offset: int = 0) -> list[dict]:
        query = sa.select(s.jobs.c.id, s.jobs.c.owner, s.jobs.c.state, s.jobs.c.created_at,
                          s.jobs.c.revision, s.jobs.c.review_state, s.jobs.c.error).order_by(s.jobs.c.created_at.desc(), s.jobs.c.id)
        if not roles & {'admin', 'reviewer'}:
            query = query.where(s.jobs.c.owner == actor)
        with self.engine.connect() as c:
            return [dict(row) for row in c.execute(query.limit(limit).offset(offset)).mappings()]

    def replay(self, job_id: str, actor: str, roles: set[str], after: int = 0) -> list[dict]:
        with self.engine.connect() as c:
            self.job(c, job_id, actor, roles)
            return list(c.execute(sa.select(s.events.c.data).where(s.events.c.job_id == job_id,
                        s.events.c.seq > after).order_by(s.events.c.seq).limit(200)).scalars())

    def administer(self, c, kind: str, data: dict) -> None:
        if kind == 'vendor':
            from invoicedesk.domain.procurement import next_vendor_number
            previous = c.execute(sa.select(s.sequences.c.value).where(s.sequences.c.key=='vendor')).scalar_one_or_none()
            high = next_vendor_number([data['vendor_id']], previous or 0)-1
            if previous is None:
                c.execute(s.sequences.insert().values(key='vendor',value=high))
            else:
                c.execute(s.sequences.update().where(s.sequences.c.key=='vendor').values(value=high))
        if kind == 'policy':
            policy = Policy.model_validate(data)
            c.execute(s.company.update().where(s.company.c.id == 1).values(policy=policy.model_dump(mode='json')))
        else:
            table, field = (s.vendors, 'vendor_id') if kind == 'vendor' else (s.pos, 'po_number')
            values = {field: data[field], 'data': data}
            if kind == 'po':
                if not c.execute(sa.select(s.vendors.c.vendor_id).where(s.vendors.c.vendor_id==data['vendor_id'])).first():
                    raise ValueError('Purchase orders must reference an existing vendor.')
                billed = sum(c.execute(sa.select(s.ledger.c.amount).where(s.ledger.c.po_number==data['po_number'])).scalars(),Decimal(0))
                if Decimal(data['net_amount']) < billed:
                    raise Conflict('PO amount cannot be reduced below committed invoices.')
                old = c.execute(sa.select(s.pos.c.data).where(s.pos.c.po_number==data['po_number'])).scalar_one_or_none()
                referenced = any(report.get('po_number')==data['po_number'] for report in c.execute(sa.select(s.artifacts.c.data).where(s.artifacts.c.kind=='report')).scalars())
                if old and referenced and any(old[k]!=data[k] for k in ('vendor_id','currency')):
                    raise Conflict('Vendor and currency cannot change after invoice receipt.')
                values['net_amount'] = Decimal(data['net_amount'])
            exists = c.execute(sa.select(table.c[field]).where(table.c[field] == data[field])).first()
            if exists:
                c.execute(table.update().where(table.c[field] == data[field]).values(**values))
            else:
                c.execute(table.insert().values(**values))
        self.bump_revision(c)

    def provider_started(self, job_id: str, token: str) -> None:
        with self.engine.begin() as c:
            row = c.execute(sa.select(s.jobs).where(s.jobs.c.id == job_id).with_for_update()).mappings().one()
            self.check_lease(row, token)
            self.update_job(c, job_id, provider_started=True)
            self.audit(c, 'worker', 'provider_dispatch', job_id, {'attempt': row['attempt']})
