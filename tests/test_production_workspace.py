"""Offline acceptance of the authenticated UI's production persistence adapter."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

sa = pytest.importorskip('sqlalchemy')

from invoicedesk.domain.approval import Conflict
from invoicedesk.infrastructure import schema as s
from invoicedesk.infrastructure.repository import Repository
from invoicedesk.infrastructure.storage import LocalStorage
from invoicedesk.interfaces.production_workspace import ProductionWorkspace
from invoicedesk.policy import Policy



@pytest.fixture
def production(tmp_path):
    repo = Repository('sqlite:///' + str(tmp_path/'ui.db'), allow_sqlite=True)
    s.metadata.create_all(repo.engine)
    repo.initialize(Policy.load(), [dict(vendor_id='V-001', name='Supplier', aliases_json='[]', status='approved', notes='')],
                    [dict(po_number='PO-1', vendor_id='V-001', description='Services', net_amount='200', tax_rate='0',
                          currency='USD', status='open', issue_date='2026-01-01')])
    workspace = ProductionWorkspace(repo, LocalStorage(tmp_path/'documents'))
    yield workspace.bind(SimpleNamespace(subject='admin', roles={'admin', 'reviewer', 'uploader'}))
    repo.engine.dispose()


def completed(production):
    identity = production.submit('invoice.pdf', b'%PDF-1.4\nsynthetic UI adapter input', 'upload')['id']
    report = dict(extraction=dict(vendor_name='Supplier', invoice_number='INV-1', currency='USD',
                                 document_type='invoice', extraction_path='llm_text'),
                  normalized=dict(gross_amount='100', net_amount='100', currency='USD', invoice_number_key='INV1'),
                  vendor_id='V-001', po_number='PO-1', checks=[], sections=[],
                  context=dict(snapshot_json='private snapshot'),
                  decision=dict(outcome='AUTO_APPROVE', summary='Checks passed.', reasons=['Matched PO.'],
                                recommended_action='Approve.', comms=None))
    with production.repo.engine.begin() as c:
        production.repo.lock(c)
        production.repo.artifact(c, identity, 1, 'report', report)
        production.repo.update_job(c, identity, state='completed', revision=1)
    return identity


def test_identity_is_request_local_and_documents_are_private(production):
    identity = completed(production)
    stranger = production.bind(SimpleNamespace(subject='stranger', roles={'uploader'}))
    assert stranger.invoices() == []
    assert production.actor == 'admin'
    with pytest.raises(LookupError):
        stranger.job(identity)
    with pytest.raises(LookupError):
        stranger.detail(identity)
    with pytest.raises(PermissionError):
        stranger.records('vendors')
    with pytest.raises(PermissionError):
        stranger.developer(identity)
    assert stranger.activity() == []
    assert production.detail(identity)['document_available']


def test_production_reports_render_saved_facts_and_ledger_progress(production):
    identity = completed(production)
    detail = production.detail(identity)
    assert detail['invoice_number'] == 'INV-1'
    assert detail['matched_po'] == 'PO-1'
    assert detail['report']['decision']['reasons'] == ['Matched PO.']
    assert detail['review_available']
    with production.repo.engine.begin() as c:
        production.repo.lock(c)
        production.repo.update_job(c, identity, review_state='approved')
        job = production.repo.job(c, identity, production.actor, production.roles)
        production.repo.post_ledger(c, job, production.repo.report_at(c, job), Decimal('100'), 'system', 'Synthetic acceptance state')
    po = production.record('purchase-orders', 'PO-1')
    assert po['approved_amount'] == '100'
    assert po['remaining_amount'] == '100'
    assert po['fulfillment_percent'] == '50.00'
    assert po['fulfillment_status'] == 'Partially fulfilled'
    assert len(po['invoices']) == 1
    assert production.detail(identity)['status'] == 'Approved'
    assert not production.detail(identity)['review_available']
    with pytest.raises(Conflict):
        production.recheck(identity, 1, 'cannot-recheck-approved')


def test_procurement_crud_revisions_and_referenced_deletion(production):
    payload = dict(vendor_id='CUSTOM', name='Custom supplier', aliases=['Alias'], status='approved', notes='')
    saved = production.save_record('vendors', payload, 'create')
    assert production.save_record('vendors', payload, 'create') == saved
    assert production.record('vendors', 'CUSTOM')['aliases'] == ['Alias']
    with pytest.raises(Conflict):
        production.save_record('vendors', dict(payload, revision=saved['revision']-1), 'stale', 'CUSTOM')
    blocked = production.toggle_vendor('CUSTOM', True, saved['revision'], 'Compliance review', 'block')
    assert production.record('vendors', 'CUSTOM')['status'] == 'blocked'
    assert production.delete_record('vendors', 'CUSTOM', blocked['revision'], 'CUSTOM', 'delete')['deleted']
    revision = production.record('vendors', 'V-001')['revision']
    with pytest.raises(Conflict):
        production.delete_record('vendors', 'V-001', revision, 'V-001', 'delete-referenced')
    actions = production.activity(entity='vendors', entity_id='CUSTOM')
    assert {action['action'] for action in actions} == {'created', 'updated', 'deleted'}


def test_submitters_cannot_mutate_procurement_or_review(production):
    identity = completed(production)
    submitter = production.bind(SimpleNamespace(subject='admin', roles={'uploader'}))
    with pytest.raises(PermissionError):
        submitter.reserve_vendor_id('forbidden-reservation')
    with pytest.raises(PermissionError):
        submitter.review(identity, True, 1, '', False, 'forbidden-review')
    with pytest.raises(PermissionError):
        submitter.recheck(identity, 1, 'forbidden-recheck')
    detail = submitter.detail(identity)
    assert not detail['review_available']
    assert detail['report']['context']['snapshot_json'] is None


def test_operator_http_routes_require_session_csrf_and_permissions(production):
    pytest.importorskip('authlib')
    from fastapi.testclient import TestClient
    from invoicedesk.interfaces.api import create_app
    from invoicedesk.interfaces.security import SecuritySettings, DatabaseSessions, COOKIE
    settings = SecuritySettings(issuer='https://id.example.com', client_id='id', client_secret='test',
                                origin='https://invoice.example.com',
                                allowed_users={'admin': {'admin', 'reviewer', 'uploader'}, 'owner': {'uploader'}})
    app = create_app(production.repo, production.storage, settings, production=False)
    with TestClient(app, base_url=settings.origin) as client:
        for path in ('/invoices', '/api/v1/config', '/api/v1/invoices', '/api/v1/vendors'):
            assert client.get(path).status_code == 401
        sessions = DatabaseSessions(production.repo, 3600)
        sessions.save('admin-session', {'subject': 'admin', 'csrf': 'csrf-test'})
        client.cookies.set(COOKIE, 'admin-session')
        config = client.get('/api/v1/config').json()
        assert config['mode'] == 'production' and config['csrf_token'] == 'csrf-test'
        assert client.get('/invoices').status_code == 200
        assert client.post('/api/v1/vendors/reserve-id', json={}, headers={'Idempotency-Key': 'no-csrf'}).status_code == 403
        headers = {'Idempotency-Key': 'reserve', 'X-CSRF-Token': 'csrf-test', 'Origin': settings.origin}
        reserved = client.post('/api/v1/vendors/reserve-id', json={}, headers=headers)
        assert reserved.status_code == 200, reserved.text
        assert reserved.json()['vendor_id'].startswith('V-')
        upload = client.post('/api/v1/invoices/upload', files={'file': ('invoice.pdf', b'%PDF-1.4\nsynthetic')},
                             headers=dict(headers, **{'Idempotency-Key': 'http-upload'}))
        assert upload.status_code == 202, upload.text
        identity = upload.json()['id']
        assert client.get('/api/v1/invoices/' + identity + '/document').headers['content-type'].startswith('application/pdf')
        sessions.save('owner-session', {'subject': 'owner', 'csrf': 'csrf-test'})
        client.cookies.set(COOKIE, 'owner-session')
        assert client.get('/api/v1/invoices/' + identity).status_code == 404
        assert client.get('/api/v1/vendors').status_code == 403
        assert client.post('/api/v1/vendors/reserve-id', json={}, headers=dict(headers, **{'Idempotency-Key': 'owner-reserve'})).status_code == 403
