"""SQLite persistence. Money is stored as decimal text, summed only with Decimal."""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from invoicedesk.infrastructure.telemetry.logger import get_logger

logger = get_logger(__name__)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS vendors(vendor_id TEXT PRIMARY KEY, name TEXT, aliases_json TEXT, status TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders(po_number TEXT PRIMARY KEY, vendor_id TEXT, description TEXT, net_amount TEXT, tax_rate TEXT, currency TEXT, status TEXT, issue_date TEXT);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, created_at TEXT, filename TEXT, file_sha256 TEXT,
 extraction_path TEXT, extraction_json TEXT, normalized_json TEXT, vendor_id TEXT, matched_po TEXT,
 po_match_type TEXT, net_amount TEXT, gross_amount TEXT, decision TEXT, decision_json TEXT,
 review_status TEXT, reviewer_note TEXT, reviewed_at TEXT, llm_used INTEGER, llm_cost_usd TEXT,
 duration_ms INTEGER);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER, stage TEXT, state TEXT,
 title TEXT, detail TEXT, checks_json TEXT, payload_json TEXT, elapsed_ms INTEGER, UNIQUE(run_id, seq));
CREATE TABLE IF NOT EXISTS pilot_ledger(run_id TEXT PRIMARY KEY, po_number TEXT, amount TEXT, actor TEXT, reason TEXT, context_json TEXT);
CREATE TABLE IF NOT EXISTS run_context(run_id TEXT PRIMARY KEY, context_json TEXT);
CREATE TABLE IF NOT EXISTS review_commands(command_key TEXT PRIMARY KEY, payload_json TEXT);
CREATE TABLE IF NOT EXISTS review_audit(id INTEGER PRIMARY KEY, run_id TEXT, action TEXT, note TEXT, created_at TEXT);
'''

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

class Db:
    def __init__(self, path: Path, *, mode: str = 'pilot'):
        if mode not in {'pilot', 'demo'}: raise ValueError('Select pilot or demo.')
        self.mode = mode
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(purchase_orders)')}
        for name in ('line_items_json', 'charge_allowances_json'):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE purchase_orders ADD COLUMN {name} TEXT NOT NULL DEFAULT '[]'")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, args)]

    def vendors(self) -> list[dict]:
        return self.rows('SELECT * FROM vendors')

    def pos(self) -> list[dict]:
        return self.rows('SELECT * FROM purchase_orders')

    def runs(self) -> list[dict]:
        return self.rows('SELECT * FROM runs ORDER BY created_at DESC')

    def billed(self, po: str) -> Decimal:
        if self.mode == 'pilot':
            return sum((Decimal(r['amount']) for r in self.rows('SELECT amount FROM pilot_ledger WHERE po_number=?', (po,))), Decimal(0))
        rows = self.rows("SELECT net_amount FROM runs WHERE matched_po=? AND review_status!='rejected' AND (decision='AUTO_APPROVE' OR review_status='approved')", (po,))
        return sum((Decimal(r['net_amount']) for r in rows if r['net_amount'] is not None), Decimal('0'))

    def remaining(self, po: dict) -> Decimal:
        return Decimal(po['net_amount']) - self.billed(po['po_number'])

    def snapshot(self, exclude: str | None = None):
        from invoicedesk.domain.contracts import ProcurementSnapshot, canonical
        vendors, pos = self.vendors(), self.pos()
        runs = [r for r in self.runs() if r['run_id'] != exclude]
        return ProcurementSnapshot(1, canonical(vendors), canonical(pos), canonical(runs),
            tuple((p['po_number'], str(self.billed(p['po_number']))) for p in pos))

    def persist(self, run: dict, events: list) -> None:
        with self.conn:
            keys = list(run)
            self.conn.execute(f"INSERT INTO runs ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(run.values()))
            context = next((e.payload['context'] for e in events if 'context' in e.payload), None)
            if context:
                self.conn.execute('INSERT INTO run_context VALUES (?,?)', (run['run_id'], json.dumps(context)))
            for event in events:
                e = event.model_dump(mode='json')
                self.conn.execute('INSERT INTO events(run_id,seq,stage,state,title,detail,checks_json,payload_json,elapsed_ms) VALUES (?,?,?,?,?,?,?,?,?)',
                    (e['run_id'], e['seq'], e['stage'], e['state'], e['title'], e['detail'], json.dumps(e['checks']), json.dumps(e['payload']), e['elapsed_ms']))
        logger.debug('Run and %d stage events committed.', len(events))

    def review(self, run_id: str, approve: bool, note: str, *, actor: str = 'local-reviewer',
               key: str | None = None, confirmed_source: bool = False) -> None:
        from invoicedesk.domain.approval import authorize, ApprovalBlocked
        from invoicedesk.domain.evaluation import evaluate
        from invoicedesk.domain.contracts import canonical
        from invoicedesk.domain.models import ExtractedInvoice
        from invoicedesk.policy import Policy
        from invoicedesk.domain.reconciliation import reconcile
        payload = canonical(dict(run_id=run_id, approve=approve, note=note, actor=actor, confirmed_source=confirmed_source))
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if key:
                prior = self.rows('SELECT payload_json FROM review_commands WHERE command_key=?', (key,))
                if prior:
                    if prior[0]['payload_json'] != payload: raise ValueError('Idempotency key reused.')
                    self.conn.commit()
                    return
            rows = self.rows('SELECT * FROM runs WHERE run_id=?', (run_id,))
            if not rows or rows[0]['review_status'] != 'pending':
                raise ValueError('This run is not pending review.')
            row = rows[0]
            policy = Policy.load()
            if self.mode == 'demo':
                policy = policy.model_copy(update={'version': 'demo-1'})
            if approve:
                invoice = ExtractedInvoice.model_validate_json(row['extraction_json'])
                findings = reconcile(invoice, policy.extraction.reconcile_abs_tolerance)
                current = evaluate(invoice, findings, self.snapshot(exclude=run_id), policy, pilot=self.mode == 'pilot')
                if self.mode == 'demo':
                    # Historical fixtures predate document classification and source confirmation.
                    invoice.document_type = 'invoice'
                    current = evaluate(invoice, findings, self.snapshot(exclude=run_id), policy, pilot=False)
                    confirmed_source = True
                amount = authorize(current, policy, note, confirmed_source)
                if current.po_number != row['matched_po']:
                    raise ApprovalBlocked('PO resolution changed; process a new revision.')
                self.conn.execute('INSERT INTO pilot_ledger VALUES (?,?,?,?,?,?)',
                    (run_id,current.po_number,str(amount),actor,note,current.context.model_dump_json()))
            action = 'approved' if approve else 'rejected'
            self.conn.execute('UPDATE runs SET review_status=?,reviewer_note=?,reviewed_at=? WHERE run_id=?', (action,note,now(),run_id))
            self.conn.execute('INSERT INTO review_audit(run_id,action,note,created_at) VALUES(?,?,?,?)', (run_id,action,note,now()))
            if key: self.conn.execute('INSERT INTO review_commands VALUES (?,?)', (key,payload))
            self.conn.commit()
            logger.info('Reviewer action saved: %s', action)
        except Exception:
            self.conn.rollback()
            raise
