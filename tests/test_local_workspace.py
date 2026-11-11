"""Offline local persistence and approval tests; no extraction providers or network calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
from decimal import Decimal
from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from invoicedesk.db import Db, now
from invoicedesk.domain.approval import ApprovalBlocked, Conflict
from invoicedesk.domain.evaluation import evaluate
from invoicedesk.domain.models import ExtractedInvoice, StageEvent, FieldEvidence
from invoicedesk.domain.reconciliation import reconcile
from invoicedesk.interfaces.local_workspace import Workspace, decimal_text
from invoicedesk.policy import ROOT, Policy


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path/'ui',tmp_path/'missing-source.sqlite3')


def upload(workspace,key='upload'):
    return workspace.submit('invoice.pdf',(ROOT/'data/invoices/01_clean_exact.pdf').read_bytes(),key)['id']


def store_report(workspace,identity,number='DIRECT-FACTS-1',net='100'):
    """Exercise review with explicit domain facts, without invoking an extractor."""
    inv=ExtractedInvoice(document_type='invoice',vendor_name='Northwind Office Supplies Ltd',invoice_number=number,
        invoice_date='2026-11-09',po_reference='PO-1001',currency='USD',subtotal=net,tax_amount='0',total=net)
    with workspace.db(identity) as db:
        policy=Policy.load()
        rec=evaluate(inv,reconcile(inv,Decimal('.02')),db.snapshot(),policy)
        run_id='run-'+identity
        event=StageEvent(run_id=run_id,seq=1,stage='complete',state='completed',title='Direct domain facts',
            payload=dict(context=rec.context.model_dump(mode='json')))
        db.persist(dict(run_id=run_id,created_at=now(),filename='invoice.pdf',file_sha256=workspace.job(identity)['sha256'],
            extraction_path=inv.extraction_path,extraction_json=inv.model_dump_json(),normalized_json=rec.interpretation.normalized_json,
            vendor_id=rec.vendor_id,matched_po=rec.po_number,po_match_type=rec.match_type,net_amount=net,gross_amount=net,
            decision=rec.decision.outcome.value,decision_json=rec.decision.model_dump_json(),review_status='pending'),[event])
        with db.conn:
            db.conn.execute("UPDATE ui_jobs SET run_id=?,state='completed' WHERE id=?",(run_id,identity))
    return run_id


def approve(workspace,identity,key='approve'):
    return workspace.review(identity,True,1,'Compared with source',True,key)


def test_backup_import_and_later_starts_preserve_source(tmp_path):
    source=tmp_path/'source.sqlite3'
    original=Workspace(tmp_path/'original',tmp_path/'absent')
    identity=upload(original)
    store_report(original,identity)
    # The configured source is the existing backend DB, without UI tables.
    with original.db() as db:
        with db.conn:
            for row in db.rows("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ui_%'"):
                db.conn.execute('DROP TABLE '+row['name'])
    with sqlite_copy(original.path,source):
        pass
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    local=Workspace(tmp_path/'copy',source)
    assert len(local.invoices())==1 and local.invoices()[0]['imported']==1
    assert local.invoices()[0]['run_id']=='run-'+identity
    local.save_record('vendors',dict(vendor_id='LOCAL',name='Local vendor',aliases=[]),'new')
    again=Workspace(tmp_path/'copy',source)
    assert again.record('vendors','LOCAL')['name']=='Local vendor'
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    with DbRead(source) as db:
        assert not db.rows("SELECT * FROM vendors WHERE vendor_id='LOCAL'")


@contextmanager
def sqlite_copy(source,dest):
    with sqlite3.connect(source) as src,sqlite3.connect(dest) as dst:
        src.backup(dst)
    yield

@contextmanager
def DbRead(path):
    db=Db(path)
    try:yield db
    finally:db.close()


def test_upload_queued_immediately_idempotent_and_persistent(workspace):
    identity=upload(workspace)
    assert workspace.job(identity)['state']=='queued'
    assert upload(workspace)==identity
    assert len(workspace.invoices())==1
    again=Workspace(workspace.root,workspace.source)
    assert again.job(identity)['state']=='queued'
    with pytest.raises(Conflict):
        workspace.submit('other.pdf',(ROOT/'data/invoices/01_clean_exact.pdf').read_bytes(),'upload')
    with pytest.raises(ValueError):workspace.submit('bad.txt',b'not a document','invalid')
    with pytest.raises(ValueError):workspace.submit('large.pdf',b'%PDF-'+b'x'*(20*1024*1024),'large')


def test_revisions_deletion_restrictions_and_audit_survive(workspace):
    vendor=dict(vendor_id='V-LOCAL',name='Local vendor',aliases=['Alias'])
    saved=workspace.save_record('vendors',vendor,'create')
    assert workspace.save_record('vendors',vendor,'create')==saved
    edited=dict(vendor,name='Changed',revision=1)
    workspace.save_record('vendors',edited,'edit','V-LOCAL')
    with pytest.raises(Conflict):workspace.save_record('vendors',edited,'stale','V-LOCAL')
    po=dict(po_number='PO-LOCAL',vendor_id='V-LOCAL',description='Services',net_amount='250.00',tax_rate='0.08',currency='USD',status='open',issue_date='2026-11-01')
    workspace.save_record('purchase-orders',po,'po')
    with pytest.raises(Conflict):workspace.delete_record('vendors','V-LOCAL',2,'V-LOCAL','delete-used')
    with pytest.raises(ValueError):workspace.delete_record('purchase-orders','PO-LOCAL',1,'wrong','delete-wrong')
    workspace.delete_record('purchase-orders','PO-LOCAL',1,'PO-LOCAL','delete-po')
    workspace.delete_record('vendors','V-LOCAL',2,'V-LOCAL','delete-vendor')
    assert len(workspace.activity(action='deleted'))==2
    with pytest.raises(LookupError):workspace.record('vendors','V-LOCAL')


def test_recommendation_receipt_and_human_posting_are_distinct(workspace):
    identity=upload(workspace)
    store_report(workspace,identity)
    po=workspace.record('purchase-orders','PO-1001')
    assert po['receipt_count']==1 and Decimal(po['approved_amount'])==0
    assert workspace.detail(identity)['status']=='Needs review'
    approve(workspace,identity)
    assert approve(workspace,identity)['review_status']=='approved'
    po=workspace.record('purchase-orders','PO-1001')
    assert Decimal(po['approved_amount'])==100 and Decimal(po['remaining_amount'])==4700
    with workspace.db() as db:assert len(db.rows('SELECT * FROM pilot_ledger'))==1
    with pytest.raises(Conflict):workspace.review(identity,False,1,'Changed mind',False,'reject-stale')
    with pytest.raises(Conflict):workspace.retry(identity,2,'approved-retry')
    with pytest.raises(Conflict):workspace.delete_record('purchase-orders','PO-1001',1,'PO-1001','delete-used')


@pytest.mark.parametrize('change',['block','close','balance'])
def test_approval_rechecks_current_procurement(workspace,change):
    identity=upload(workspace)
    store_report(workspace,identity)
    if change=='block':workspace.toggle_vendor('V001',True,1,'Pending verification','block')
    elif change=='close':
        po=workspace.record('purchase-orders','PO-1001');po['status']='closed'
        workspace.save_record('purchase-orders',po,'close','PO-1001')
    else:
        with workspace.db() as db,db.conn:
            db.conn.execute('INSERT INTO pilot_ledger VALUES (?,?,?,?,?,?)',('previous','PO-1001','4800','operator','Previous authorized posting','{}'))
    with pytest.raises(ApprovalBlocked):approve(workspace,identity)
    assert workspace.detail(identity)['status']=='Needs review'


def test_source_confirmation_and_exception_reason_required(workspace):
    identity=upload(workspace)
    store_report(workspace,identity)
    with pytest.raises(ApprovalBlocked):workspace.review(identity,True,1,'',True,'no-reason')
    with pytest.raises(ApprovalBlocked):workspace.review(identity,True,1,'Checked',False,'no-confirmation')
    with pytest.raises(ValueError):workspace.review(identity,False,1,'',False,'no-rejection-reason')


def test_concurrent_review_commits_once(workspace):
    identity=upload(workspace)
    store_report(workspace,identity)
    def click(_):
        return approve(workspace,identity,'same-click')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(click,range(2)))
    assert results[0]==results[1]
    with workspace.db() as db:assert len(db.rows('SELECT * FROM pilot_ledger'))==1


def test_retry_chain_preserves_artifacts_without_self_duplicate(workspace):
    first=upload(workspace)
    first_run=store_report(workspace,first)
    second=workspace.retry(first,1,'retry')['id']
    assert workspace.retry(first,1,'retry')['id']==second
    with pytest.raises(Conflict):workspace.retry(first,1,'retry-other')
    assert workspace.job(first)['run_id']==first_run
    with workspace.db(second) as db:assert all(r['run_id']!=first_run for r in db.snapshot().runs())
    store_report(workspace,second)
    assert workspace.detail(first)['review_available'] is False
    with pytest.raises(Conflict):approve(workspace,first)
    approve(workspace,second,'approve-new')
    assert workspace.detail(second)['status']=='Approved'


def test_restart_marks_interrupted_without_automatic_provider_retry(workspace):
    identity=upload(workspace)
    assert workspace.claim()==identity
    # Empty queue: recovery cannot call an extraction provider in this test.
    workspace.start()
    try:
        job=workspace.job(identity)
        assert job['state']=='errored' and json.loads(job['error_json'])['code']=='interrupted'
        assert workspace.claim() is None
    finally:workspace.close()


@pytest.mark.parametrize('value',['NaN','Infinity','-1','0','1.0000001','1000000000000000000'])
def test_invalid_money_rejected(value):
    with pytest.raises(ValueError):decimal_text(value,positive=True)


def test_missing_currency_is_unavailable_in_summary(workspace):
    identity=upload(workspace)
    store_report(workspace,identity)
    with workspace.db() as db,db.conn:
        row=db.rows('SELECT * FROM runs')[0]
        inv=json.loads(row['extraction_json']);inv['currency']=None
        db.conn.execute('UPDATE runs SET extraction_json=?',(json.dumps(inv),))
    assert workspace.detail(identity)['currency'] is None


def test_recheck_is_revisioned_preserves_history_and_never_posts(workspace):
    identity=upload(workspace)
    run_id=store_report(workspace,identity)
    with workspace.db() as db:
        before=db.rows('SELECT extraction_json,decision_json FROM runs WHERE run_id=?',(run_id,))[0]
    result=workspace.recheck(identity,1,'recheck')
    assert result['revision']==2
    assert workspace.recheck(identity,1,'recheck')==result
    with pytest.raises(Conflict):workspace.recheck(identity,1,'stale-recheck')
    detail=workspace.detail(identity)
    assert detail['revision']==2 and detail['report']['revalidated'] is True
    assert detail['report']['source_validation']['provider_calls']==0
    with workspace.db() as db:
        assert db.rows('SELECT extraction_json,decision_json FROM runs WHERE run_id=?',(run_id,))[0]==before
        assert db.rows('SELECT * FROM pilot_ledger')==[]


def source_backed_invoice(workspace,tmp_path):
    from scripts.pdf_fallback import write_pdf
    # This source prints zero tax: its authorized test PO must have that basis.
    with workspace.db() as db, db.conn:
        db.conn.execute("UPDATE purchase_orders SET tax_rate='0' WHERE po_number='PO-1001'")
    source=tmp_path/'source-backed.pdf'
    lines=['Invoice','Vendor: Northwind Office Supplies Ltd','Invoice No: DIRECT-FACTS-1',
        'Invoice date: 20/11/2026','PO Number: PO-1001','Currency: US$',
        'Subtotal: 100.00','Tax: 0.00','Total: US$ 100.00']
    write_pdf(source,lines)
    identity=workspace.submit(source.name,source.read_bytes(),'source-upload')['id']
    run_id=store_report(workspace,identity)
    with workspace.db() as db,db.conn:
        inv=ExtractedInvoice.model_validate_json(db.rows('SELECT extraction_json FROM runs WHERE run_id=?',(run_id,))[0]['extraction_json'])
        inv.extraction_path='llm_text'  # Domain test input; no provider or API provenance claimed.
        inv.invoice_date=date(2026,11,20)
        inv.currency='US$'
        inv.field_evidence=[FieldEvidence(path=path,snippet=snippet,page=1) for path,snippet in [
            ('document_type','Invoice'),('vendor_name','Northwind Office Supplies Ltd'),('invoice_number','DIRECT-FACTS-1'),
            ('invoice_date','20/11/2026'),('po_reference','PO-1001'),('currency','US$'),
            ('subtotal','100.00'),('tax_amount','0.00'),('total','US$ 100.00')]]
        inv=ExtractedInvoice.model_validate(inv.model_dump())
        db.conn.execute('UPDATE runs SET extraction_json=? WHERE run_id=?',(inv.model_dump_json(),run_id))
    return identity


def test_source_backed_recheck_recommends_and_explicit_approval_posts(workspace,tmp_path):
    identity=source_backed_invoice(workspace,tmp_path)
    result=workspace.recheck(identity,1,'verified-recheck')
    assert result['outcome']=='AUTO_APPROVE'
    report=workspace.detail(identity)['report']
    assert report['normalized']['currency']=='USD'
    assert report['source_validation']['unverified_count']==0
    assert report['extraction']['warnings']==[]
    assert workspace.detail(identity)['status']=='Needs review'
    workspace.review(identity,True,2,'',False,'approve-verified')
    detail=workspace.detail(identity)
    assert detail['status']=='Approved' and detail['report']['decision']['outcome']=='AUTO_APPROVE'
    assert 'posted' in detail['report']['summary']
    with workspace.db() as db:assert len(db.rows('SELECT * FROM pilot_ledger'))==1


def test_current_vendor_block_wins_after_clean_recheck(workspace,tmp_path):
    identity=source_backed_invoice(workspace,tmp_path)
    assert workspace.recheck(identity,1,'clean')['outcome']=='AUTO_APPROVE'
    workspace.toggle_vendor('V001',True,1,'Verification needed','block-after-recheck')
    with pytest.raises(ApprovalBlocked):workspace.review(identity,True,2,'',False,'stale-approve')


def test_tampered_source_cannot_be_rechecked_or_approved(workspace,tmp_path):
    identity=source_backed_invoice(workspace,tmp_path)
    Path(workspace.job(identity)['document_path']).write_bytes(b'%PDF-1.4\nchanged')
    with pytest.raises(ValueError,match='integrity'):workspace.recheck(identity,1,'tampered-recheck')
    with pytest.raises(ValueError,match='integrity'):workspace.review(identity,True,1,'',False,'tampered-approval')


def test_invoice_clean_keeps_procurement_and_restores_balances(workspace):
    workspace.save_record('vendors',dict(vendor_id='KEEP',name='Retained vendor',aliases=['Alias']),'keep-vendor')
    workspace.toggle_vendor('V008',True,1,'Retain blocked state','keep-block')
    identity=upload(workspace)
    run_id=store_report(workspace,identity)
    approve(workspace,identity)
    with workspace.db() as db:
        vendors=db.vendors();pos=db.pos()
        procurement_revisions=db.rows('SELECT * FROM ui_revisions ORDER BY entity,entity_id')
        procurement_audit=db.rows("SELECT * FROM ui_audit WHERE entity IN ('vendors','purchase-orders') ORDER BY id")
    result=workspace.clean_invoices()
    assert result['removed']==dict(runs=1,ui_jobs=1,pilot_ledger=1)
    with sqlite3.connect(result['backup']) as backup:
        assert backup.execute('SELECT count(*) FROM runs').fetchone()[0]==1
        assert backup.execute('SELECT count(*) FROM pilot_ledger').fetchone()[0]==1
    with workspace.db() as db:
        assert db.vendors()==vendors and db.pos()==pos
        assert db.rows('SELECT * FROM ui_revisions ORDER BY entity,entity_id')==procurement_revisions
        assert db.rows("SELECT * FROM ui_audit WHERE entity IN ('vendors','purchase-orders') ORDER BY id")==procurement_audit
        for table in ('runs','ui_jobs','events','ui_events','ui_artifacts','pilot_ledger','run_context','review_commands','review_audit'):
            assert db.rows(f'SELECT * FROM {table}')==[]
        assert db.rows("SELECT * FROM ui_audit WHERE entity='invoices'")==[]
        assert {r['key'] for r in db.rows('SELECT key FROM ui_commands')}=={'keep-vendor','keep-block'}
        assert db.billed('PO-1001')==0
    reopened=Workspace(workspace.root,workspace.source)
    assert reopened.invoices()==[]
    assert reopened.record('vendors','V008')['status']=='blocked'
    assert Decimal(reopened.record('purchase-orders','PO-1001')['remaining_amount'])==4800
    # Old upload keys cannot resurrect a cleared job.
    assert upload(reopened)!=identity


def test_invoice_clean_refuses_a_running_server(workspace):
    from invoicedesk.infrastructure.locking import lock_file
    identity=upload(workspace)
    with (workspace.root/'worker.lock').open('a') as lock:
        lock_file(lock)
        with pytest.raises(RuntimeError,match='Stop the running UI'):
            workspace.clean_invoices()
    assert workspace.job(identity)['state']=='queued'


def test_invoice_clean_leaves_original_database_unchanged(tmp_path):
    from invoicedesk.seed import reset
    source=tmp_path/'original.sqlite3'
    with DbRead(source) as db:reset(db)
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    workspace=Workspace(tmp_path/'local',source)
    upload(workspace)
    workspace.clean_invoices()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original_hash
    assert workspace.invoices()==[] and len(workspace.records('vendors'))==8


def test_optional_due_date_issue_does_not_block_an_otherwise_verified_invoice(workspace,tmp_path):
    identity=source_backed_invoice(workspace,tmp_path)
    with workspace.db() as db,db.conn:
        run=db.rows('SELECT * FROM runs')[0]
        inv=ExtractedInvoice.model_validate_json(run['extraction_json'])
        inv.warnings=['Partial extraction: due_date (date_from_datetime_parsing)']
        db.conn.execute('UPDATE runs SET extraction_json=?',(inv.model_dump_json(),))
    assert workspace.recheck(identity,1,'optional-date')['outcome']=='AUTO_APPROVE'
    report=workspace.detail(identity)['report']
    assert any(c['check_id']=='optional_fields' and c['status']=='INFO' for c in report['checks'])
    workspace.review(identity,True,2,'',False,'approve-optional-date')
    assert workspace.detail(identity)['status']=='Approved'


def test_corrected_approved_identity_is_used_for_duplicate_protection(workspace,tmp_path):
    identity=source_backed_invoice(workspace,tmp_path)
    workspace.review(identity,True,1,'',False,'approve-correct-identity')
    with workspace.db() as db,db.conn:
        run=db.rows('SELECT * FROM runs')[0]
        original=json.loads(run['normalized_json'])
        original['invoice_number']='DATE OF ISSUE DUE DATE'
        original['invoice_number_key']='DATEOFISSUEDUEDATE'
        db.conn.execute('UPDATE runs SET normalized_json=?',(json.dumps(original),))
    with workspace.db() as db:
        assert json.loads(db.snapshot().runs()[0]['normalized_json'])['invoice_number_key']=='DIRECTFACTS1'
    duplicate=upload(workspace,'duplicate-upload')
    store_report(workspace,duplicate)
    with pytest.raises(ApprovalBlocked):approve(workspace,duplicate,'duplicate-approval')
    with workspace.db() as db:assert len(db.rows('SELECT * FROM pilot_ledger'))==1
