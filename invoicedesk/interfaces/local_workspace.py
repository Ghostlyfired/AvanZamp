"""Isolated local SQLite workspace. All UI mutations use short serialized transactions."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import shutil
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from invoicedesk.config import ROOT, load_environment
from invoicedesk.db import Db, now
from invoicedesk.domain.approval import ApprovalBlocked, Conflict, authorize
from invoicedesk.domain.contracts import EvidenceReference, canonical
from invoicedesk.domain.evaluation import evaluate
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.domain.reconciliation import reconcile
from invoicedesk.infrastructure.documents import inspect_document
from invoicedesk.infrastructure.extraction.llm import LlmClient
from invoicedesk.pipeline import run_invoice
from invoicedesk.policy import Policy
from invoicedesk.domain.procurement import fulfillment, next_vendor_number
from invoicedesk.infrastructure.locking import lock_file

SCHEMA = '''
CREATE TABLE IF NOT EXISTS ui_jobs(
 id TEXT PRIMARY KEY, run_id TEXT UNIQUE, parent_id TEXT, filename TEXT NOT NULL,
 document_path TEXT, sha256 TEXT, media_type TEXT, state TEXT NOT NULL,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error_json TEXT,
 revision INTEGER NOT NULL DEFAULT 1, imported INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ui_events(job_id TEXT, seq INTEGER, data_json TEXT, PRIMARY KEY(job_id,seq));
CREATE TABLE IF NOT EXISTS ui_artifacts(job_id TEXT, kind TEXT, data_json TEXT, PRIMARY KEY(job_id,kind));
CREATE TABLE IF NOT EXISTS ui_revisions(entity TEXT, entity_id TEXT, revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(entity,entity_id));
CREATE TABLE IF NOT EXISTS ui_commands(key TEXT PRIMARY KEY, payload_json TEXT, response_json TEXT);
CREATE TABLE IF NOT EXISTS ui_audit(id INTEGER PRIMARY KEY, created_at TEXT, actor TEXT, entity TEXT,
 entity_id TEXT, action TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS ui_metadata(key TEXT PRIMARY KEY, value TEXT);
'''


def one(db, sql, args=()):
    rows = db.rows(sql, args)
    if not rows:
        raise LookupError('Record not found.')
    return rows[0]


def decimal_text(value, *, positive=False):
    if not isinstance(value, str):
        raise ValueError('Amounts must be decimal text.')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or abs(amount) >= Decimal('1e18') or amount != amount.quantize(Decimal('.000001')):
            raise ValueError('Amount must be finite with at most six decimal places.')
        if amount < 0 or (positive and amount == 0):
            raise ValueError('Amount must be positive.' if positive else 'Amount cannot be negative.')
        return str(amount)
    except InvalidOperation:
        raise ValueError('Invalid decimal amount.') from None


class LocalDb(Db):
    """Exclude superseded, unposted attempts only within an explicit retry chain."""
    job_id = None

    def effective_run(self, row):
        """Use immutable validated artifacts for duplicate identities, retaining raw history."""
        jobs = self.rows('SELECT id FROM ui_jobs WHERE run_id=?',(row['run_id'],))
        if not jobs:
            return row
        identity = jobs[0]['id']
        artifacts = self.rows("SELECT kind,data_json FROM ui_artifacts WHERE job_id=? AND (kind='approval_recheck' OR kind LIKE 'validation_recheck:%') ORDER BY kind DESC",(identity,))
        selected = next((a for a in artifacts if a['kind']=='approval_recheck' and row['review_status']=='approved'),None) or next((a for a in artifacts if a['kind'].startswith('validation_recheck:')),None)
        if not selected:
            return row
        report = json.loads(selected['data_json'])
        if 'normalized' not in report:
            return row
        result = dict(row)
        result.update(extraction_json=canonical(report['extraction']),normalized_json=canonical(report['normalized']),
            decision=report['decision']['outcome'],decision_json=canonical(report['decision']),
            vendor_id=report.get('vendor_id',row['vendor_id']),matched_po=report.get('po_number',row['matched_po']))
        return result

    def snapshot(self, exclude=None):
        own_transaction = not self.conn.in_transaction
        if own_transaction:
            self.conn.execute('BEGIN')
        try:
            snapshot = super().snapshot(exclude)
            from dataclasses import replace
            snapshot = replace(snapshot,runs_json=canonical([self.effective_run(row) for row in snapshot.runs()]))
        finally:
            if own_transaction:
                self.conn.rollback()
        if not self.job_id:
            return snapshot
        ignored = set()
        job = one(self,'SELECT * FROM ui_jobs WHERE id=?',(self.job_id,))
        while job['parent_id']:
            job = one(self,'SELECT * FROM ui_jobs WHERE id=?',(job['parent_id'],))
            if job['run_id'] and not self.rows('SELECT run_id FROM pilot_ledger WHERE run_id=?',(job['run_id'],)):
                ignored.add(job['run_id'])
        from dataclasses import replace
        return replace(snapshot,runs_json=canonical([r for r in snapshot.runs() if r['run_id'] not in ignored]))


class Workspace:
    def __init__(self, root: Path | None = None, source: Path | None = None):
        load_environment()
        if not os.getenv('INVOICEDESK_PARSER'):
            reader = 'pdfplumber' if importlib.util.find_spec('pdfplumber') else 'poppler' if shutil.which('pdftotext') else 'pdfplumber'
            os.environ['INVOICEDESK_PARSER'] = reader
            from invoicedesk.infrastructure.telemetry.logger import get_logger
            get_logger(__name__).info('Local UI PDF text reader: %s', reader)
        self.root = (root or ROOT/'private/ui').resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root/'workspace.sqlite3'
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        # One server owns this workspace, including across separate processes.
        self._lock_file = None
        self.source = source if source is not None else Path(os.getenv('INVOICEDESK_DB', str(ROOT/'data/local-preview.sqlite3')))
        self.source = self.source.resolve()
        self.initialize()

    @contextmanager
    def db(self, job_id=None):
        db = LocalDb(self.path, mode='pilot')
        db.job_id = job_id
        try:
            yield db
        finally:
            db.close()

    def initialize(self):
        if not self.path.exists():
            staging = self.root/'initializing.sqlite3'
            if self.source.is_file() and self.source != self.path:
                # Read-only URI plus SQLite backup includes committed WAL data.
                with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(staging)) as dest:
                    src.backup(dest)
            else:
                from invoicedesk.seed import reset
                seed_db = Db(staging)
                try:
                    reset(seed_db)  # Procurement masters only; never processes sample invoices.
                finally:
                    seed_db.close()
            staging.replace(self.path)
            self.path.chmod(0o600)
        with self.db() as db:
            db.conn.executescript(SCHEMA)
            db.conn.execute('PRAGMA journal_mode=WAL')
            with db.conn:
                db.conn.execute('INSERT OR IGNORE INTO ui_metadata VALUES (?,?)', ('source_database', str(self.source)))
                previous = db.rows("SELECT value FROM ui_metadata WHERE key='vendor_sequence'")
                high = next_vendor_number([v['vendor_id'] for v in db.vendors()], int(previous[0]['value']) if previous else 0)-1
                db.conn.execute("INSERT INTO ui_metadata(key,value) VALUES ('vendor_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(high),))
                for entity, rows, field in [('vendors', db.vendors(), 'vendor_id'), ('purchase-orders', db.pos(), 'po_number')]:
                    for row in rows:
                        db.conn.execute('INSERT OR IGNORE INTO ui_revisions VALUES (?,?,1)', (entity, row[field]))
                for run in db.runs():
                    if db.rows('SELECT id FROM ui_jobs WHERE run_id=?', (run['run_id'],)):
                        continue
                    source = self.find_source(run)
                    media = inspect_document(source).media_type if source else None
                    db.conn.execute('INSERT INTO ui_jobs(id,run_id,filename,document_path,sha256,media_type,state,created_at,finished_at,imported) VALUES (?,?,?,?,?,?,?,?,?,1)',
                        (run['run_id'],run['run_id'],run['filename'],str(source) if source else None,run['file_sha256'],media,'completed',run['created_at'],run['created_at']))
                    self.audit(db,'invoices',run['run_id'],'imported', {'original_run_id':run['run_id']}, actor='system')
                # Historical review actions remain individually visible.
                if not db.rows("SELECT value FROM ui_metadata WHERE key='history_imported'"):
                    for row in db.rows('SELECT * FROM review_audit ORDER BY id'):
                        self.audit(db,'invoices',row['run_id'],row['action'],{'reason':row['note'],'historical':True},actor='historical operator',created_at=row['created_at'])
                    db.conn.execute("INSERT INTO ui_metadata VALUES ('history_imported','true')")

    def find_source(self, run):
        # Never serve arbitrary paths from an imported filename. Verify content hash.
        name = Path(run['filename']).name
        for directory in [ROOT/'data', ROOT/'outputs', ROOT/'archives']:
            if not directory.exists():
                continue
            for path in directory.rglob(name):
                if path.is_file() and path.stat().st_size <= 20*1024*1024:
                    if hashlib.sha256(path.read_bytes()).hexdigest() == run['file_sha256']:
                        # Copy historical sources so subsequent workspace use is independent.
                        dest = self.root/'documents'/run['run_id']/name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(path.read_bytes())
                        dest.chmod(0o600)
                        return dest
        return None

    @staticmethod
    def audit(db, entity, entity_id, action, details, *, actor='local operator', created_at=None):
        db.conn.execute('INSERT INTO ui_audit(created_at,actor,entity,entity_id,action,detail_json) VALUES (?,?,?,?,?,?)',
            (created_at or now(),actor,entity,entity_id,action,canonical(details)))

    def command(self, key, payload, action):
        if not key or len(key) > 200:
            raise ValueError('A valid Idempotency-Key header is required.')
        with self.db() as db:
            db.conn.execute('BEGIN IMMEDIATE')
            try:
                prior = db.rows('SELECT * FROM ui_commands WHERE key=?', (key,))
                if prior:
                    if prior[0]['payload_json'] != canonical(payload):
                        raise Conflict('Idempotency key was already used for a different request.')
                    return json.loads(prior[0]['response_json'])
                result = action(db)
                db.conn.execute('INSERT INTO ui_commands VALUES (?,?,?)', (key,canonical(payload),canonical(result)))
                db.conn.commit()
                return result
            except Exception:
                db.conn.rollback()
                raise

    def revision(self, db, entity, identity):
        return one(db,'SELECT revision FROM ui_revisions WHERE entity=? AND entity_id=?',(entity,identity))['revision']

    def check_revision(self, db, entity, identity, revision):
        if self.revision(db,entity,identity) != revision:
            raise Conflict('Record changed. Reload it and try again.')

    def records(self, entity):
        with self.db() as db:
            rows = db.vendors() if entity == 'vendors' else db.pos()
            for row in rows:
                identity = row['vendor_id'] if entity == 'vendors' else row['po_number']
                row['revision'] = self.revision(db,entity,identity)
                if entity == 'vendors':
                    row['aliases'] = json.loads(row.pop('aliases_json') or '[]')
                    row['invoice_count'] = len(db.rows('SELECT run_id FROM runs WHERE vendor_id=?',(identity,)))
                    row['po_count'] = len(db.rows('SELECT po_number FROM purchase_orders WHERE vendor_id=?',(identity,)))
                else:
                    approved = db.billed(identity)
                    net = Decimal(row['net_amount'])
                    row.update(**fulfillment(net, approved),receipt_count=len(db.rows('SELECT run_id FROM runs WHERE matched_po=?',(identity,))),
                        vendor_name=one(db,'SELECT name FROM vendors WHERE vendor_id=?',(row['vendor_id'],))['name'])
                    row['accepted_invoice_count'] = len(db.rows('SELECT run_id FROM pilot_ledger WHERE po_number=?',(identity,)))
                    row['pending_amount'] = str(sum((Decimal(r['net_amount']) for r in db.rows("SELECT net_amount FROM runs WHERE matched_po=? AND review_status='pending'",(identity,)) if r['net_amount']), Decimal(0)))
                    for name in ('line_items', 'charge_allowances'):
                        row[name] = json.loads(row.pop(name+'_json', '[]'))
            return rows

    def reserve_vendor_id(self, key):
        def action(db):
            previous = db.rows("SELECT value FROM ui_metadata WHERE key='vendor_sequence'")
            number = next_vendor_number([v['vendor_id'] for v in db.vendors()], int(previous[0]['value']) if previous else 0)
            db.conn.execute("INSERT INTO ui_metadata(key,value) VALUES ('vendor_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(number),))
            return {'vendor_id': f'V-{number:03d}'}
        return self.command(key,dict(action='reserve_vendor_id'),action)

    def record(self, entity, identity):
        field = 'vendor_id' if entity == 'vendors' else 'po_number'
        rows = [r for r in self.records(entity) if r[field] == identity]
        if not rows:
            raise LookupError('Record not found.')
        row = rows[0]
        row['invoices'] = [r for r in self.invoices() if r.get('vendor_id' if entity == 'vendors' else 'matched_po') == identity]
        row['activity'] = self.activity(entity=entity,entity_id=identity)
        return row

    def save_record(self, entity, data, key, identity=None):
        data = dict(data)
        field = 'vendor_id' if entity == 'vendors' else 'po_number'
        selected = identity or str(data.get(field,'')).strip()
        if not selected or len(selected)>100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in selected):
            raise ValueError('ID must contain 1–100 letters, digits, hyphens or underscores.')
        def action(db):
            if identity:
                self.check_revision(db,entity,identity,data.get('revision'))
            if entity == 'vendors':
                name = str(data.get('name','')).strip()
                aliases = data.get('aliases',[])
                if not name or len(name)>300 or not isinstance(aliases,list) or any(not isinstance(a,str) or not a.strip() or len(a)>300 for a in aliases):
                    raise ValueError('Vendor name and non-empty alias strings are required.')
                status = data.get('status','approved')
                if status not in {'approved','blocked'}:
                    raise ValueError('Invalid vendor status.')
                values = dict(vendor_id=selected,name=name,aliases_json=canonical(aliases),status=status,notes=str(data.get('notes','')))
            else:
                vendor_id = data.get('vendor_id')
                if not db.rows('SELECT vendor_id FROM vendors WHERE vendor_id=?',(vendor_id,)):
                    raise ValueError('Choose an existing vendor.')
                description = str(data.get('description','')).strip()
                if not description:
                    raise ValueError('PO description is required.')
                currency = str(data.get('currency','')).strip().upper()
                if len(currency)!=3 or not currency.isalpha():
                    raise ValueError('Use a three-letter currency code.')
                status = data.get('status','open')
                if status not in {'open','closed'}:
                    raise ValueError('Invalid PO status.')
                issued = date.fromisoformat(str(data.get('issue_date',''))).isoformat()
                values = dict(po_number=selected,vendor_id=vendor_id,description=description,net_amount=decimal_text(data.get('net_amount'),positive=True),
                    tax_rate=decimal_text(data.get('tax_rate','0')),currency=currency,status=status,issue_date=issued)
                if Decimal(values['tax_rate'])>1:
                    raise ValueError('Tax rate must be between 0 and 1.')
                from invoicedesk.domain.master import PurchaseOrderLine, ChargeAllowance
                for name, model in [('line_items', PurchaseOrderLine), ('charge_allowances', ChargeAllowance)]:
                    values[name+'_json'] = canonical([model.model_validate(v).model_dump(mode='json') for v in data.get(name,[])])
                if identity:
                    old = one(db,'SELECT * FROM purchase_orders WHERE po_number=?',(identity,))
                    if db.rows('SELECT run_id FROM runs WHERE matched_po=?',(identity,)) and any(values[k] != old[k] for k in ('vendor_id','currency')):
                        raise Conflict('Vendor and currency cannot change after invoice receipt.')
                    if Decimal(values['net_amount']) < db.billed(identity):
                        raise Conflict('PO net amount cannot be reduced below approved postings.')
            table = 'vendors' if entity == 'vendors' else 'purchase_orders'
            if entity == 'vendors':
                previous = db.rows("SELECT value FROM ui_metadata WHERE key='vendor_sequence'")
                high = next_vendor_number([selected], int(previous[0]['value']) if previous else 0)-1
                db.conn.execute("INSERT INTO ui_metadata(key,value) VALUES ('vendor_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(high),))
            if identity:
                updates = {k:v for k,v in values.items() if k!=field}
                db.conn.execute(f"UPDATE {table} SET {','.join(k+'=?' for k in updates)} WHERE {field}=?",(*updates.values(),identity))
                db.conn.execute('UPDATE ui_revisions SET revision=revision+1 WHERE entity=? AND entity_id=?',(entity,identity))
            else:
                fields = list(values)
                db.conn.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",tuple(values.values()))
                db.conn.execute('INSERT INTO ui_revisions VALUES (?,?,1)',(entity,selected))
            self.audit(db,entity,selected,'updated' if identity else 'created',values)
            return {field:selected,'revision':self.revision(db,entity,selected)}
        return self.command(key,dict(action='save',entity=entity,identity=identity,data=data),action)

    def toggle_vendor(self, identity, blocked, revision, reason, key):
        def action(db):
            self.check_revision(db,'vendors',identity,revision)
            one(db,'SELECT * FROM vendors WHERE vendor_id=?',(identity,))
            db.conn.execute('UPDATE vendors SET status=? WHERE vendor_id=?',('blocked' if blocked else 'approved',identity))
            db.conn.execute("UPDATE ui_revisions SET revision=revision+1 WHERE entity='vendors' AND entity_id=?",(identity,))
            self.audit(db,'vendors',identity,'blocked' if blocked else 'unblocked',{'reason':reason})
            return {'vendor_id':identity,'revision':revision+1}
        return self.command(key,dict(action='block' if blocked else 'unblock',identity=identity,revision=revision,reason=reason),action)

    def delete_record(self, entity, identity, revision, confirmation, key):
        def action(db):
            self.check_revision(db,entity,identity,revision)
            if confirmation != identity:
                raise ValueError('Type the exact ID to confirm deletion.')
            if entity == 'vendors':
                used = db.rows('SELECT run_id FROM runs WHERE vendor_id=?',(identity,)) or db.rows('SELECT po_number FROM purchase_orders WHERE vendor_id=?',(identity,))
                table,field = 'vendors','vendor_id'
            else:
                used = db.rows('SELECT run_id FROM runs WHERE matched_po=?',(identity,)) or db.rows('SELECT run_id FROM pilot_ledger WHERE po_number=?',(identity,))
                table,field = 'purchase_orders','po_number'
            if used:
                raise Conflict('Referenced records cannot be deleted. Block the vendor or close the PO.')
            db.conn.execute(f'DELETE FROM {table} WHERE {field}=?',(identity,))
            db.conn.execute('DELETE FROM ui_revisions WHERE entity=? AND entity_id=?',(entity,identity))
            self.audit(db,entity,identity,'deleted',{'confirmation':confirmation})
            return {'deleted':identity}
        return self.command(key,dict(action='delete',entity=entity,identity=identity,revision=revision,confirmation=confirmation),action)

    def submit(self, filename, content, key):
        identity = uuid.uuid4().hex
        name = Path(filename.replace('\\','/')).name[:240]
        if not name:
            raise ValueError('A filename is required.')
        path = self.root/'documents'/identity/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
        try:
            document = inspect_document(path)
            def action(db):
                db.conn.execute('INSERT INTO ui_jobs(id,filename,document_path,sha256,media_type,state,created_at) VALUES (?,?,?,?,?,?,?)',
                    (identity,name,str(path),document.sha256,document.media_type,'queued',now()))
                self.audit(db,'invoices',identity,'submitted',{'filename':name,'extraction_mode':'live'})
                return {'id':identity,'state':'queued'}
            result = self.command(key,dict(action='upload',filename=name,sha256=document.sha256),action)
            if result['id'] != identity:
                path.unlink()
            self.wake.set()
            return result
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def invoices(self):
        with self.db() as db:
            rows = db.rows('SELECT j.*,r.extraction_json,r.matched_po,r.vendor_id,r.gross_amount,r.decision,r.review_status,r.extraction_path FROM ui_jobs j LEFT JOIN runs r ON j.run_id=r.run_id ORDER BY j.created_at DESC,j.id DESC')
            for row in rows:
                inv = json.loads(row.pop('extraction_json') or '{}')
                artifacts = db.rows("SELECT kind,data_json FROM ui_artifacts WHERE job_id=? AND (kind='approval_recheck' OR kind LIKE 'validation_recheck:%') ORDER BY kind DESC",(row['id'],))
                selected = next((a for a in artifacts if a['kind']=='approval_recheck' and row['review_status']=='approved'),None) or next((a for a in artifacts if a['kind'].startswith('validation_recheck:')),None)
                if selected:
                    current_report = json.loads(selected['data_json'])
                    inv = current_report.get('extraction',inv)
                    row['decision'] = current_report.get('decision',{}).get('outcome',row['decision'])
                    row['vendor_id'] = current_report.get('vendor_id',row['vendor_id'])
                    row['matched_po'] = current_report.get('po_number',row['matched_po'])
                    row['gross_amount'] = current_report.get('normalized',{}).get('gross_amount',row['gross_amount'])
                row['invoice_number'] = inv.get('invoice_number')
                row['issuer'] = inv.get('vendor_name') or (inv.get('sender') or {}).get('name')
                row['currency'] = inv.get('currency')
                row['document_type'] = inv.get('document_type')
                row['error'] = json.loads(row.pop('error_json') or 'null')
                row['status'] = ('Not started' if row['state']=='queued' else 'Processing' if row['state']=='running' else
                    'Errored' if row['state']=='errored' or row['extraction_path']=='failed' else
                    'Approved' if row['review_status']=='approved' else 'Rejected' if row['review_status']=='rejected' else 'Needs review')
                row.pop('document_path',None)
            return rows

    def job(self, identity):
        with self.db() as db:
            return one(db,'SELECT * FROM ui_jobs WHERE id=?',(identity,))

    def events(self, identity):
        job = self.job(identity)
        with self.db() as db:
            live = db.rows('SELECT data_json FROM ui_events WHERE job_id=? ORDER BY seq',(identity,))
            if live:
                return [json.loads(r['data_json']) for r in live]
            return [dict(run_id=e['run_id'],seq=e['seq'],stage=e['stage'],state=e['state'],title=e['title'],detail=e['detail'],
                checks=json.loads(e['checks_json']),payload=json.loads(e['payload_json']),elapsed_ms=e['elapsed_ms'])
                for e in db.rows('SELECT * FROM events WHERE run_id=? ORDER BY seq',(job['run_id'],))]

    def detail(self, identity):
        job = self.job(identity)
        events = self.events(identity)
        summary = next(r for r in self.invoices() if r['id']==identity)
        with self.db() as db:
            rows = db.rows('SELECT * FROM runs WHERE run_id=?',(job['run_id'],))
            report = None
            review_available = False
            if rows:
                run = rows[0]
                context = db.rows('SELECT context_json FROM run_context WHERE run_id=?',(job['run_id'],))
                context_data = json.loads(context[0]['context_json']) if context else None
                report = dict(extraction=json.loads(run['extraction_json']),normalized=json.loads(run['normalized_json']),
                    decision=json.loads(run['decision_json']),checks=[c for e in events for c in e.get('checks',[])],
                    sections=next((e['payload'].get('sections',[]) for e in events if e['stage']=='extract' and e['state']=='completed'),[]),context=context_data)
                original_report = report
                approved_artifacts = db.rows("SELECT data_json FROM ui_artifacts WHERE job_id=? AND kind='approval_recheck'",(identity,))
                validation_artifacts = db.rows("SELECT data_json FROM ui_artifacts WHERE job_id=? AND kind LIKE 'validation_recheck:%' ORDER BY kind DESC LIMIT 1",(identity,))
                artifact = (json.loads(approved_artifacts[0]['data_json']) if run['review_status']=='approved' and approved_artifacts else
                            json.loads(validation_artifacts[0]['data_json']) if validation_artifacts else None)
                if artifact and 'extraction' in artifact:
                    report = artifact
                    report['original_decision'] = original_report['decision']
                    report['revalidated'] = True
                # Compact presentation fields keep technical paths in the full checks below.
                report['summary'] = ('Checks passed. Awaiting final approval transaction.'
                    if report['decision']['outcome']=='AUTO_APPROVE' else report['decision']['summary'])
                if run['review_status']=='approved':
                    report['summary'] = 'Invoice approved. Authorized net amount posted to the PO ledger.'
                report['summary_reasons'] = [c['detail']
                    for c in report['checks'] if c.get('proposed') or c['status'] in {'WARN','INFO'}]
                review_available = bool(context_data and context_data.get('profile')=='pilot' and run['review_status']=='pending' and summary['status']=='Needs review' and not db.rows('SELECT id FROM ui_jobs WHERE parent_id=?',(identity,)))
            return dict(**summary,report=report,events=events,review_available=review_available,document_available=bool(job['document_path'] and Path(job['document_path']).is_file()),
                activity=self.activity(entity='invoices',entity_id=identity),attempts=db.rows('SELECT id,parent_id,state,created_at FROM ui_jobs WHERE sha256=? ORDER BY created_at',(job['sha256'],)))

    def developer(self, identity):
        detail = self.detail(identity)
        with self.db() as db:
            artifacts = {r['kind']:json.loads(r['data_json']) for r in db.rows('SELECT * FROM ui_artifacts WHERE job_id=?',(identity,))}
        return dict(id=identity,run_id=detail['run_id'],events=detail['events'],error=detail['error'],artifacts=artifacts,
            extraction_metadata=next((e['payload'] for e in detail['events'] if e['stage']=='extract' and e['state']=='completed'),None),
            context=(detail['report'] or {}).get('context'),raw_available=bool(artifacts.get('provider',{}).get('raw_response')))

    def source_review(self, identity):
        from invoicedesk.application.source_review import verify_stored_source
        job = self.job(identity)
        if job['state']!='completed':
            raise Conflict('A completed extraction is required before rechecking or approving.')
        with self.db() as db:
            run = one(db,'SELECT * FROM runs WHERE run_id=?',(job['run_id'],))
            artifacts = db.rows("SELECT data_json FROM ui_artifacts WHERE job_id=? AND kind='provider'",(identity,))
        provider = json.loads(artifacts[0]['data_json']) if artifacts else {}
        raw_response = provider.get('raw_response') if provider.get('provenance') in {'api_recording','api_recording_partial'} else None
        inv = ExtractedInvoice.model_validate_json(run['extraction_json'])
        return verify_stored_source(inv,Path(job['document_path']) if job['document_path'] else None,job['sha256'],raw_response=raw_response)

    @staticmethod
    def validation_report(recommendation, invoice, findings, references, metadata):
        from invoicedesk.application.review import report_from
        from invoicedesk.infrastructure.extraction.sections import section_report
        sections = section_report(invoice,findings=findings,source_evidence=references)
        report = report_from(recommendation,invoice,findings,sections)
        report['source_evidence'] = [ref.model_dump(mode='json') for ref in references]
        report['source_validation'] = metadata
        return report

    def recheck(self, identity, revision, key):
        invoice, references, metadata = self.source_review(identity)
        def action(db):
            job = one(db,'SELECT * FROM ui_jobs WHERE id=?',(identity,))
            run = one(db,'SELECT * FROM runs WHERE run_id=?',(job['run_id'],))
            context = db.rows('SELECT context_json FROM run_context WHERE run_id=?',(job['run_id'],))
            if (job['revision']!=revision or job['state']!='completed' or run['review_status']!='pending'
                    or not context or json.loads(context[0]['context_json']).get('profile')!='pilot'
                    or db.rows('SELECT id FROM ui_jobs WHERE parent_id=?',(identity,))):
                raise Conflict('This report changed, is historical, or is no longer pending review.')
            db.job_id = identity
            policy = Policy.load()
            findings = reconcile(invoice,policy.extraction.reconcile_abs_tolerance)
            rec = evaluate(invoice,findings,db.snapshot(exclude=job['run_id']),policy,revision+1,source_evidence=references)
            report = self.validation_report(rec,invoice,findings,references,metadata)
            db.conn.execute('INSERT INTO ui_artifacts VALUES (?,?,?)',(identity,f'validation_recheck:{revision+1:010d}',canonical(report)))
            db.conn.execute('UPDATE ui_jobs SET revision=revision+1 WHERE id=?',(identity,))
            self.audit(db,'invoices',identity,'rechecked',dict(revision=revision+1,outcome=rec.decision.outcome.value,**metadata))
            return dict(id=identity,revision=revision+1,outcome=rec.decision.outcome.value)
        return self.command(key,dict(action='recheck',identity=identity,revision=revision),action)

    def review(self, identity, approve, revision, reason, confirmed_source, key):
        verified = self.source_review(identity) if approve else None
        def action(db):
            job = one(db,'SELECT * FROM ui_jobs WHERE id=?',(identity,))
            if job['revision'] != revision:
                raise Conflict('Report changed. Reload and review the current report.')
            run = one(db,'SELECT * FROM runs WHERE run_id=?',(job['run_id'],))
            context = db.rows('SELECT context_json FROM run_context WHERE run_id=?',(job['run_id'],))
            if job['state']!='completed' or run['review_status']!='pending' or not context or json.loads(context[0]['context_json']).get('profile')!='pilot':
                raise Conflict('This historical or finished run is not available for review. Reprocess its source.')
            if db.rows('SELECT id FROM ui_jobs WHERE parent_id=?',(identity,)):
                raise Conflict('A newer attempt supersedes this report. Review the linked attempt.')
            db.job_id = identity
            invoice = ExtractedInvoice.model_validate_json(run['extraction_json'])
            if invoice.extraction_path=='failed':
                raise ApprovalBlocked('Extraction failed. Retry before reviewing.')
            if approve:
                policy = Policy.load()
                invoice, references, metadata = verified
                findings = reconcile(invoice,policy.extraction.reconcile_abs_tolerance)
                rec = evaluate(invoice,findings,db.snapshot(exclude=job['run_id']),policy,revision,source_evidence=references)
                self.commit_approval(db,job,rec,invoice,findings,references,metadata,'local operator',reason,confirmed_source)
            elif not reason.strip():
                raise ValueError('A rejection reason is required.')
            state = 'approved' if approve else 'rejected'
            db.conn.execute('UPDATE runs SET review_status=?,reviewer_note=?,reviewed_at=? WHERE run_id=?',(state,reason,now(),job['run_id']))
            db.conn.execute('INSERT INTO review_audit(run_id,action,note,created_at) VALUES (?,?,?,?)',(job['run_id'],state,reason,now()))
            db.conn.execute('UPDATE ui_jobs SET revision=revision+1 WHERE id=?',(identity,))
            self.audit(db,'invoices',identity,state,dict(reason=reason,confirmed_source=confirmed_source))
            return {'id':identity,'review_status':state,'revision':revision+1}
        return self.command(key,dict(action='approve' if approve else 'reject',identity=identity,revision=revision,reason=reason,confirmed_source=confirmed_source),action)

    def commit_approval(self, db, job, rec, invoice, findings, references, metadata, actor, reason, confirmed_source=False):
        """Caller holds BEGIN IMMEDIATE; manual and automatic posting share invariants."""
        from invoicedesk.domain.approval import authorize_automatic
        policy = Policy.load()
        amount = authorize_automatic(rec,policy) if actor=='system' else authorize(rec,policy,reason,confirmed_source)
        decimal_text(str(amount),positive=True)
        run = one(db,'SELECT * FROM runs WHERE run_id=?',(job['run_id'],))
        if rec.po_number != run['matched_po']:
            raise ApprovalBlocked('PO resolution changed. Reprocess before approval.')
        number = json.loads(rec.interpretation.normalized_json)['invoice_number_key']
        for original in db.rows('SELECT r.* FROM runs r JOIN pilot_ledger l ON l.run_id=r.run_id'):
            row = db.effective_run(original)
            if row['vendor_id']==rec.vendor_id and json.loads(row['normalized_json']).get('invoice_number_key')==number:
                raise ApprovalBlocked('This vendor invoice is already posted to the ledger.')
        db.conn.execute('INSERT INTO pilot_ledger VALUES (?,?,?,?,?,?)',(job['run_id'],rec.po_number,str(amount),actor,reason,rec.context.model_dump_json()))
        db.conn.execute('INSERT INTO ui_artifacts VALUES (?,?,?)',(job['id'],'approval_recheck',canonical(self.validation_report(rec,invoice,findings,references,metadata))))

    def retry(self, identity, revision, key):
        new_id = uuid.uuid4().hex
        def action(db):
            job = one(db,'SELECT * FROM ui_jobs WHERE id=?',(identity,))
            if job['revision'] != revision or job['state'] in {'running','queued'}:
                raise Conflict('Attempt changed or is still processing.')
            if db.rows("SELECT run_id FROM runs WHERE run_id=? AND review_status='approved'",(job['run_id'],)):
                raise Conflict('Approved invoices cannot be reprocessed.')
            if not job['document_path'] or not Path(job['document_path']).is_file():
                raise Conflict('Source document is unavailable. Upload it again.')
            if db.rows("SELECT id FROM ui_jobs WHERE parent_id=?",(identity,)):
                raise Conflict('A retry already exists. Open the linked attempt.')
            db.conn.execute('INSERT INTO ui_jobs(id,parent_id,filename,document_path,sha256,media_type,state,created_at) VALUES (?,?,?,?,?,?,?,?)',
                (new_id,identity,job['filename'],job['document_path'],job['sha256'],job['media_type'],'queued',now()))
            self.audit(db,'invoices',identity,'retried',{'new_attempt':new_id})
            self.audit(db,'invoices',new_id,'submitted',{'parent_id':identity,'extraction_mode':'live'})
            return {'id':new_id,'parent_id':identity,'state':'queued'}
        result = self.command(key,dict(action='retry',identity=identity,revision=revision),action)
        self.wake.set()
        return result

    def activity(self, *, entity=None, entity_id=None, action=None, start=None, end=None):
        sql = 'SELECT * FROM ui_audit WHERE 1=1'
        args = []
        for column,value in [('entity',entity),('entity_id',entity_id),('action',action)]:
            if value:
                sql += f' AND {column}=?'; args.append(value)
        if start:
            sql += ' AND created_at>=?'; args.append(date.fromisoformat(start).isoformat())
        if end:
            sql += ' AND substr(created_at,1,10)<=?'; args.append(date.fromisoformat(end).isoformat())
        with self.db() as db:
            rows = db.rows(sql+' ORDER BY created_at DESC,id DESC',tuple(args))
        for row in rows:
            row['details'] = json.loads(row.pop('detail_json'))
        return rows

    def clean_invoices(self):
        """Explicit local test reset. Procurement data and its history are preserved."""
        from datetime import datetime, timezone
        if self.thread and self.thread.is_alive():
            raise RuntimeError('Stop the running UI server before cleaning invoices.')
        with (self.root/'worker.lock').open('a') as lock:
            try:
                lock_file(lock)
            except BlockingIOError:
                raise RuntimeError('Stop the running UI server before cleaning invoices.') from None
            with self.db() as db:
                db.conn.execute('BEGIN IMMEDIATE')
                try:
                    counts = {table:db.rows(f'SELECT count(*) AS n FROM {table}')[0]['n']
                        for table in ('runs','ui_jobs','pilot_ledger')}
                    directory = self.root.parent/'ui-backups'
                    directory.mkdir(exist_ok=True,mode=0o700)
                    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                    backup = directory/f'before-invoice-clean-{stamp}.sqlite3'
                    # A separate reader can back up the committed snapshot while this
                    # connection holds the writer lock, before anything is removed.
                    with sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True) as reader, sqlite3.connect(backup) as dest:
                        reader.backup(dest)
                    backup.chmod(0o600)
                    for table in ('ui_events','ui_artifacts','review_commands','review_audit',
                                  'pilot_ledger','run_context','events','ui_jobs','runs'):
                        db.conn.execute(f'DELETE FROM {table}')
                    for row in db.rows('SELECT key,payload_json FROM ui_commands'):
                        payload = json.loads(row['payload_json'])
                        if payload.get('action') in {'upload','retry','approve','reject','recheck'} or payload.get('entity')=='invoices':
                            db.conn.execute('DELETE FROM ui_commands WHERE key=?',(row['key'],))
                    db.conn.execute("DELETE FROM ui_revisions WHERE entity='invoices'")
                    db.conn.execute("DELETE FROM ui_audit WHERE entity='invoices'")
                    self.audit(db,'workspace','local','invoices_cleared',dict(removed=counts,backup=backup.name),actor='local operator')
                    db.conn.commit()
                    return dict(removed=counts,backup=str(backup))
                except Exception:
                    db.conn.rollback()
                    raise

    def start(self):
        self._lock_file = (self.root/'worker.lock').open('a')
        try:
            lock_file(self._lock_file)
        except BlockingIOError:
            self._lock_file.close()
            raise RuntimeError('Another local UI server already owns this workspace.') from None
        with self.db() as db, db.conn:
            interrupted = db.rows("SELECT id FROM ui_jobs WHERE state='running'")
            for row in interrupted:
                db.conn.execute("UPDATE ui_jobs SET state='errored',error_json=?,finished_at=? WHERE id=?",(canonical({'code':'interrupted','message':'Processing was interrupted. Explicit retry required; the provider may have received the request.'}),now(),row['id']))
                self.audit(db,'invoices',row['id'],'interrupted',{},actor='system')
        self.stop.clear()
        self.thread = threading.Thread(target=self.worker,name='invoicedesk-local-worker',daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread:
            self.thread.join(timeout=10)
        if self._lock_file and (not self.thread or not self.thread.is_alive()):
            self._lock_file.close()

    def worker(self):
        while not self.stop.is_set():
            identity = self.claim()
            if identity:
                self.process(identity)
            else:
                self.wake.wait(1); self.wake.clear()

    def claim(self):
        with self.db() as db:
            db.conn.execute('BEGIN IMMEDIATE')
            queued = db.rows("SELECT id FROM ui_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1")
            if not queued:
                db.conn.rollback(); return None
            identity = queued[0]['id']
            db.conn.execute("UPDATE ui_jobs SET state='running',started_at=? WHERE id=?",(now(),identity))
            self.audit(db,'invoices',identity,'processing',{},actor='system')
            db.conn.commit()
            return identity

    def process(self, identity):
        job = self.job(identity)
        client = LlmClient('live',cache_dir=self.root/'extraction-cache',provider=os.getenv('LLM_PROVIDER','openai'),model=os.getenv('LLM_MODEL',''),refresh=bool(job['parent_id']))
        if client.provider_name not in {'openai','anthropic'}:
            self.fail(identity,'configuration','Select the OpenAI or Anthropic live provider.'); return
        try:
            with self.db(identity) as db:
                if inspect_document(Path(job['document_path'])).sha256 != job['sha256']:
                    raise ValueError('Stored document integrity mismatch.')
                db_events = []
                for event in run_invoice(Path(job['document_path']),db=db,policy=Policy.load(),llm=client):
                    db_events.append(event)
                    # Each yielded stage becomes visible before the next (possibly slow) stage.
                    with db.conn:
                        db.conn.execute('UPDATE ui_jobs SET run_id=COALESCE(run_id,?) WHERE id=?',(event.run_id,identity))
                        db.conn.execute('INSERT INTO ui_events VALUES (?,?,?)',(identity,event.seq,event.model_dump_json()))
                db.conn.execute('BEGIN IMMEDIATE')
                with db.conn:
                    # Correct the storage basename to the original sender filename.
                    db.conn.execute('UPDATE runs SET filename=? WHERE run_id=?',(job['filename'],event.run_id))
                    db.conn.execute('INSERT INTO ui_artifacts VALUES (?,?,?)',(identity,'provider',canonical(dict(raw_response=json.loads(client.raw_response_json),
                        requested_model=client.model,resolved_model=client.resolved_model,provider=client.provider_name,provenance=client.provenance,
                        cache_hit=client.cache_hit,usage=client.usage,token_usage=client.token_usage,response_ms=client.response_ms,fingerprint=client.fingerprint,pdf_reader=os.getenv('INVOICEDESK_PARSER')))))
                    run = one(db,'SELECT extraction_path FROM runs WHERE run_id=?',(event.run_id,))
                    failed = run['extraction_path']=='failed'
                    if not failed:
                        from invoicedesk.domain.contracts import EvidenceReference
                        row = one(db,'SELECT * FROM runs WHERE run_id=?',(event.run_id,))
                        invoice = ExtractedInvoice.model_validate_json(row['extraction_json'])
                        refs = tuple(EvidenceReference.model_validate(ref) for ref in next(e.payload.get('source_evidence',[]) for e in db_events if e.stage=='extract' and e.state=='completed'))
                        policy = Policy.load()
                        findings = reconcile(invoice,policy.extraction.reconcile_abs_tolerance)
                        rec = evaluate(invoice,findings,db.snapshot(exclude=event.run_id),policy,source_evidence=refs)
                        report = self.validation_report(rec,invoice,findings,refs,{'verification':'processing'})
                        db.conn.execute('INSERT INTO ui_artifacts VALUES (?,?,?)',(identity,'validation_recheck:0000000001',canonical(report)))
                        if rec.decision.outcome.value=='AUTO_APPROVE':
                            posting_job = dict(job,run_id=event.run_id)
                            reason = 'All mandatory procurement checks passed.'
                            self.commit_approval(db,posting_job,rec,invoice,findings,refs,{},'system',reason)
                            db.conn.execute("UPDATE runs SET review_status='approved',reviewer_note=?,reviewed_at=? WHERE run_id=?",(reason,now(),event.run_id))
                            self.audit(db,'invoices',identity,'auto_approved',{'context':rec.context.model_dump(mode='json')},actor='system')
                    error = dict(code=client.failure_kind or 'extraction_failed',message='Extraction could not complete. See retained checks and timeline; retry explicitly.') if failed else None
                    db.conn.execute('UPDATE ui_jobs SET state=?,finished_at=?,error_json=? WHERE id=?',('errored' if failed else 'completed',now(),canonical(error) if error else None,identity))
                    self.audit(db,'invoices',identity,'errored' if failed else 'processed',{'run_id':event.run_id,'error':error},actor='system')
        except Exception as exc:
            # Unexpected exception text can contain private provider data. Expose only its type.
            self.fail(identity,'processing_failed',f'Processing stopped ({type(exc).__name__}). Explicit retry required.')

    def fail(self, identity, code, message):
        with self.db() as db, db.conn:
            db.conn.execute("UPDATE ui_jobs SET state='errored',finished_at=?,error_json=? WHERE id=?",(now(),canonical(dict(code=code,message=message)),identity))
            self.audit(db,'invoices',identity,'errored',dict(code=code,message=message),actor='system')
