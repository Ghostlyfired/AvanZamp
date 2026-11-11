"""Offline SQL adapter/API contracts. PostgreSQL locking has a separate local-DB suite."""
import json
import time
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import pytest
sa = pytest.importorskip('sqlalchemy', reason='Install requirements-dev.lock for pilot persistence tests')
from invoicedesk.infrastructure import schema as s
from invoicedesk.infrastructure.repository import Repository
from invoicedesk.infrastructure.storage import LocalStorage
from invoicedesk.application.jobs import JobService
from invoicedesk.application.review import ReviewService, report_from
from invoicedesk.domain.evaluation import evaluate
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.domain.approval import ApprovalBlocked, Conflict
from invoicedesk.policy import Policy
from invoicedesk.extract.reconcile import reconcile
from invoicedesk.domain.contracts import EvidenceReference


@pytest.fixture
def repo(tmp_path):
    repository = Repository('sqlite:///'+str(tmp_path/'pilot.db'), allow_sqlite=True)
    s.metadata.create_all(repository.engine)
    repository.initialize(Policy.load(), [dict(vendor_id='V1', name='Example Vendor', aliases_json='[]', status='approved', notes='')],
        [dict(po_number='PO-123',vendor_id='V1',description='Services',net_amount='1500',tax_rate='0',currency='USD',status='open',issue_date='2026-01-01')])
    yield repository
    repository.engine.dispose()


def completed_job(repo, tmp_path, number='ONE', day='2026-01-01'):
    storage = LocalStorage(tmp_path/'documents')
    result = JobService(repo, storage).submit(b'%PDF-1.4\nsynthetic test body', 'example.pdf', actor='uploader', roles={'uploader'}, key=number)
    job_id = result['job_id']
    inv = ExtractedInvoice(document_type='invoice', vendor_name='Example Vendor', invoice_number=number,
        invoice_date=day, po_reference='PO-123', currency='USD', subtotal='1000', tax_amount='0', total='1000',
        extraction_path='llm_vision')
    refs = tuple(EvidenceReference(path=name, snippet=str(getattr(inv,name)), verification='GROUNDED') for name in [*Policy.load().critical_fields,'currency','subtotal','tax_amount','po_reference'])
    with repo.engine.begin() as c:
        repo.lock(c)
        policy = repo.policy(c)
        findings = reconcile(inv, Decimal('.02'))
        rec = evaluate(inv, findings, repo.snapshot(c, exclude=job_id), policy, source_evidence=refs)
        report = report_from(rec, inv, findings)
        report['source_evidence'] = [ref.model_dump(mode='json') for ref in refs]
        repo.artifact(c,job_id,1,'report',report)
        repo.update_job(c,job_id,state='completed',revision=1)
    return job_id


def test_approval_rechecks_balance_and_idempotency(repo,tmp_path):
    first = completed_job(repo,tmp_path,'FIRST','2026-01-01')
    second = completed_job(repo,tmp_path,'SECOND','2026-03-01')
    service = ReviewService(repo)
    assert service.act(first,actor='reviewer',roles={'reviewer'},key='approve-1',revision=1,approve=True,reason='') == service.act(
        first,actor='reviewer',roles={'reviewer'},key='approve-1',revision=1,approve=True,reason='')
    with pytest.raises(ApprovalBlocked):
        service.act(second,actor='reviewer',roles={'reviewer'},key='approve-2',revision=1,approve=True,reason='Old balance looked sufficient')
    with repo.engine.connect() as c:
        assert list(c.execute(sa.select(s.ledger.c.amount)).scalars()) == [Decimal('1000')]
    with pytest.raises(Conflict):
        service.act(first,actor='reviewer',roles={'reviewer'},key='approve-1',revision=1,approve=True,reason='changed')


def test_reviewer_corrections_are_immutable_revisions(repo,tmp_path):
    job = completed_job(repo,tmp_path)
    service = ReviewService(repo)
    assert service.correct(job,actor='reviewer',roles={'reviewer'},key='fix',revision=1,
                           changes={'invoice_number':'CORRECTED'},reason='Compared printed number')['revision'] == 2
    with pytest.raises(Conflict):
        service.act(job,actor='reviewer',roles={'reviewer'},key='stale',revision=1,approve=True,reason='')
    with repo.engine.connect() as c:
        old = repo.report_at(c, {'id':job,'revision':1})
        assert old['extraction']['invoice_number'] == 'ONE'
    assert repo.report(job,'reviewer',{'reviewer'})['extraction']['invoice_number'] == 'CORRECTED'


def test_access_control_and_upload_idempotency(repo,tmp_path):
    service = JobService(repo,LocalStorage(tmp_path/'documents'))
    args = dict(actor='owner',roles={'uploader'},key='upload')
    first = service.submit(b'%PDF-1.4\nsynthetic', '../../example.pdf', **args)
    assert first == service.submit(b'%PDF-1.4\nsynthetic', '../../example.pdf', **args)
    with pytest.raises(Conflict):
        service.submit(b'%PDF-1.4\ndifferent', '../../example.pdf', **args)
    with repo.engine.connect() as c:
        with pytest.raises(LookupError):
            repo.job(c,first['job_id'],'stranger',{'uploader'})
    with pytest.raises(PermissionError):
        ReviewService(repo).act(first['job_id'],actor='owner',roles={'uploader'},key='forbidden',revision=1,approve=True,reason='')


def test_leases_recover_and_stale_workers_cannot_append(repo,tmp_path):
    result = JobService(repo,LocalStorage(tmp_path/'documents')).submit(b'%PDF-1.4\nsynthetic', 'example.pdf', actor='owner',roles={'uploader'},key='job')
    job = repo.claim(lease_seconds=1)
    assert job['id'] == result['job_id']
    with repo.engine.begin() as c:
        repo.update_job(c,job['id'],lease_until=time.time()-1)
    assert repo.claim() is None
    with pytest.raises(Conflict):
        repo.append_event(job['id'],job['lease_token'],{'stage':'complete'})
    with repo.engine.connect() as c:
        assert repo.job(c,job['id'],'owner',set())['state'] == 'retry_required'
    retry = JobService(repo,LocalStorage(tmp_path/'documents')).retry(job['id'],actor='owner',roles={'uploader'},key='retry',revision=0)
    assert retry['state'] == 'queued'
    assert repo.claim()['lease_token'] != job['lease_token']


def test_api_authorization_csrf_and_bounded_upload(repo,tmp_path):
    pytest.importorskip('fastapi')
    pytest.importorskip('authlib')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from invoicedesk.interfaces.api import create_app
    from invoicedesk.interfaces.security import SecuritySettings, DatabaseSessions, COOKIE
    config = SecuritySettings(issuer='https://id.example.com',client_id='id',client_secret='test',
               origin='https://invoice.example.com',allowed_users={'owner':{'uploader'},'other':{'uploader'},'reviewer':{'reviewer'}})
    app = create_app(repo,LocalStorage(tmp_path/'documents'),config,production=False)
    with TestClient(app,base_url=config.origin) as client:
        assert client.get('/v1/jobs').status_code == 401
        store = DatabaseSessions(repo,3600)
        store.save('test-owner',{'subject':'owner','csrf':'token'})
        client.cookies.set(COOKIE,'test-owner')
        assert client.post('/v1/jobs',content=b'%PDF-1.4\nsynthetic',headers={'Idempotency-Key':'first'}).status_code == 403
        headers = {'Idempotency-Key':'first','X-CSRF-Token':'token','Origin':config.origin}
        response = client.post('/v1/jobs',content=b'%PDF-1.4\nsynthetic',headers=headers)
        assert response.status_code == 202, response.text
        job_id = response.json()['job_id']
        assert client.get(f'/v1/jobs/{job_id}/document').status_code == 200
        assert client.post(f'/v1/jobs/{job_id}/approve',json={'revision':1},headers=dict(headers,**{'Idempotency-Key':'approve'})).status_code == 403
        store.save('test-other',{'subject':'other','csrf':'token'})
        client.cookies.set(COOKIE,'test-other')
        for suffix in ('','/report','/document','/events'):
            assert client.get(f'/v1/jobs/{job_id}'+suffix).status_code == 404
        assert client.get('/v1/jobs').json()['items'] == []
        assert app.openapi()['paths']['/v1/jobs/{job_id}/approve']
