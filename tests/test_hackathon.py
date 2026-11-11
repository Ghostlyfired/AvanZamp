"""Offline Render deployment boundary tests; never use actual provider credentials."""
import base64
import hashlib
from pathlib import Path
import shutil
import time

from fastapi.testclient import TestClient
import pytest
import yaml

from invoicedesk.interfaces import hackathon
from invoicedesk.interfaces.local_workspace import Workspace
from invoicedesk.policy import ROOT

ORIGIN = 'https://judges.onrender.com'
AUTH = ('judge', 'synthetic-password-for-tests')


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('DEMO_PUBLIC_ORIGIN', ORIGIN)
    monkeypatch.setenv('DEMO_USERNAME', AUTH[0])
    monkeypatch.setenv('DEMO_PASSWORD', AUTH[1])
    monkeypatch.setenv('LLM_MODEL', 'offline-test-model')


@pytest.fixture
def client(configured, tmp_path):
    app = hackathon.create_app(root=tmp_path/'demo', start_worker=False)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


@pytest.mark.parametrize('path', ['/', '/invoices', '/assets/app.js', '/assets/index.html',
                                  '/api/v1/config', '/api/v1/invoices', '/openapi.json', '/healthz/'])
def test_all_workspace_surfaces_require_login(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers['www-authenticate'].startswith('Basic ')
    assert response.headers['cache-control'] == 'no-store'
    assert AUTH[1] not in response.text


@pytest.mark.parametrize('value', ['Basic !invalid!', 'Bearer token', 'Basic '+base64.b64encode(b'no-colon').decode(),
                                   'Basic '+base64.b64encode(b'judge:wrong').decode(), 'Basic '+('a'*5000)])
def test_bad_credentials_fail_closed(client, value):
    assert client.get('/api/v1/config', headers={'Authorization': value}).status_code == 401


def test_public_health_has_no_invoice_data(client):
    response = client.get('/healthz')
    assert response.status_code == 200 and response.json() == {'status': 'ok'}
    assert client.head('/healthz').status_code == 200
    assert client.post('/healthz').status_code == 401
    assert client.get('/healthz', headers={'Host': 'attacker.example'}).status_code == 400


def test_https_host_origin_and_upload_validation(client, monkeypatch):
    assert client.get('http://judges.onrender.com/invoices', auth=AUTH).status_code == 403
    assert client.get('/', auth=AUTH, headers={'Host': 'attacker.example'}).status_code == 400
    assert client.get('/', auth=AUTH, headers={'Host': 'localhost'}).status_code == 400
    for origin in [None, 'null', 'http://judges.onrender.com', 'https://attacker.example']:
        headers = {'Idempotency-Key': 'rejected'}
        if origin is not None:
            headers['Origin'] = origin
        assert client.post('/api/v1/vendors', auth=AUTH, headers=headers, json={}).status_code == 403
    headers = {'Origin': ORIGIN, 'Idempotency-Key': 'invalid'}
    assert client.post('/api/v1/invoices/upload', auth=AUTH, headers=headers,
                       files={'file': ('bad.txt', b'not an invoice', 'text/plain')}).status_code == 422
    monkeypatch.setenv('OPENAI_API_KEY', 'secret-canary-never-in-browser')
    config = client.get('/api/v1/config', auth=AUTH)
    assert config.json()['demo_notice'] == hackathon.NOTICE
    assert config.json()['workspace'] == 'shared demo'
    assert 'secret-canary' not in config.text and AUTH[1] not in config.text
    assert client.get('/assets/app.js', auth=AUTH).status_code == 200
    assert 'demo-banner' in client.get('/assets/app.js', auth=AUTH).text
    assert client.get('/invoices', auth=AUTH).headers['content-security-policy']
    headers = {'Origin': ORIGIN, 'Idempotency-Key': 'valid'}
    result = client.post('/api/v1/invoices/upload', auth=AUTH, headers=headers,
                         files={'file': ('invoice.pdf', (ROOT/'data/invoices/01_clean_exact.pdf').read_bytes(), 'application/pdf')})
    assert result.status_code == 202
    identity = result.json()['id']
    assert client.get('/api/v1/invoices/'+identity+'/document').status_code == 401
    assert client.get('/api/v1/invoices/'+identity, auth=AUTH).json()['state'] == 'queued'


@pytest.mark.parametrize('key,value', [
    ('DEMO_PUBLIC_ORIGIN', ''), ('DEMO_PUBLIC_ORIGIN', 'http://judges.onrender.com'),
    ('DEMO_PUBLIC_ORIGIN', ORIGIN+'/'), ('DEMO_PUBLIC_ORIGIN', ORIGIN+'/path'),
    ('DEMO_PUBLIC_ORIGIN', ORIGIN+'?query'), ('DEMO_PUBLIC_ORIGIN', 'https://user@judges.onrender.com'),
    ('DEMO_PUBLIC_ORIGIN', 'https://*.onrender.com'), ('DEMO_USERNAME', ''),
    ('DEMO_USERNAME', 'judge:password'), ('DEMO_PASSWORD', 'short'),
    ('LLM_PROVIDER', 'cache'), ('LLM_MODEL', ''), ('ANTHROPIC_API_KEY', ''),
])
def test_invalid_configuration_fails_before_workspace_creation(configured, monkeypatch, tmp_path, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        hackathon.create_app(root=tmp_path/'demo')
    assert not (tmp_path/'demo').exists()


def test_separate_history_restart_and_reseed(configured, tmp_path, monkeypatch):
    local = Workspace(tmp_path/'local', tmp_path/'absent')
    local.submit('local.pdf', (ROOT/'data/invoices/01_clean_exact.pdf').read_bytes(), 'local')
    digest = hashlib.sha256(local.path.read_bytes()).hexdigest()
    monkeypatch.setenv('INVOICEDESK_DB', str(local.path))
    root = tmp_path/'demo'
    for iteration in range(3):
        app = hackathon.create_app(root=root, start_worker=False)
        with TestClient(app, base_url=ORIGIN) as client:
            workspace = app.state.workspace
            assert len(workspace.invoices()) == (1 if iteration == 1 else 0)
            assert workspace.records('vendors') and workspace.records('purchase-orders')
            if iteration == 0:
                workspace.submit('demo.pdf', (ROOT/'data/invoices/01_clean_exact.pdf').read_bytes(), 'demo')
            assert client.get('/api/v1/config', auth=AUTH).json()['demo_notice']
        if iteration == 1:
            # Simulate Render discarding only this test-owned temporary filesystem.
            shutil.rmtree(root)
    assert hashlib.sha256(local.path.read_bytes()).hexdigest() == digest
    assert len(local.invoices()) == 1


def test_single_worker_lifecycle_and_health(configured, tmp_path):
    app = hackathon.create_app(root=tmp_path/'demo')
    with TestClient(app, base_url=ORIGIN) as client:
        worker = app.state.workspace.thread
        assert worker.is_alive()
        assert client.get('/healthz').status_code == 200
        competing = Workspace(tmp_path/'demo', tmp_path/'demo')
        with pytest.raises(RuntimeError, match='already owns'):
            competing.start()
        app.state.workspace.close()
        assert client.get('/healthz').status_code == 503
    assert not worker.is_alive()


def test_render_assigned_port(configured, monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setenv('PORT', '12345')
    monkeypatch.setattr(hackathon, 'create_app', lambda: 'application')
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: calls.append((app, kwargs)))
    hackathon.main()
    assert calls[0][0] == 'application'
    assert calls[0][1]['port'] == 12345 and calls[0][1]['host'] == '0.0.0.0'
    assert calls[0][1]['workers'] == 1 and calls[0][1]['access_log'] is False


def test_authenticated_worker_extraction_details_approval_and_fulfillment(configured, tmp_path, monkeypatch):
    """Full HTTP/worker flow with synthetic facts, never claimed as live API evidence."""
    from decimal import Decimal
    from invoicedesk.domain.models import ExtractedInvoice
    from invoicedesk.infrastructure.extraction.llm import LlmClient
    from invoicedesk.infrastructure.extraction.providers import ProviderResponse
    from scripts.pdf_fallback import write_pdf

    responses = []
    class SyntheticProvider:
        name = 'anthropic'
        calls = 0

        def extract(self, path, *, model, schema):
            self.calls += 1
            return ProviderResponse(responses.pop(0), resolved_model='synthetic-offline')

    provider = SyntheticProvider()
    app = hackathon.create_app(root=tmp_path/'demo')
    monkeypatch.setattr('invoicedesk.interfaces.local_workspace.LlmClient',
                        lambda *args, **kwargs: LlmClient('live', provider=provider, model='synthetic-offline',
                                                         cache_dir=tmp_path/'demo'/'synthetic-audit'))
    with TestClient(app, base_url=ORIGIN) as client:
        def upload(number, vendor, po, net):
            net = Decimal(net)
            tax = net*Decimal('.08')
            inv = ExtractedInvoice(document_type='invoice', vendor_name=vendor, invoice_number=number,
                invoice_date='2026-11-11', po_reference=po, currency='USD', subtotal=net,
                tax_amount=tax, total=net+tax, prices_include_tax=False,
                line_items=[dict(description='Office furniture' if po=='PO-1001' else 'Freight - warehouse transfer',
                                 quantity='1', unit_price=str(net), amount=str(net))])
            facts = inv.model_dump(mode='json', exclude={'field_quality', 'warnings', 'extraction_path'})
            snippets = dict(vendor_name='Vendor: '+vendor, invoice_number='Invoice No: '+number,
                invoice_date='Invoice Date: 2026-11-11', po_reference='PO Number: '+po, currency='Currency: USD',
                subtotal='Subtotal: '+str(net), tax_amount='Tax: '+str(tax), total='Total: '+str(net+tax))
            row = f'{inv.line_items[0].description} | 1 | {net} | {net}'
            facts['field_evidence'] = [dict(path=k, snippet=v, page=1) for k,v in snippets.items()]
            facts['field_evidence'].extend(dict(path='line_items.0.'+k, snippet=row, page=1)
                for k in ['description', 'quantity', 'unit_price', 'amount'])
            source = tmp_path/(number+'.pdf')
            write_pdf(source, ['Invoice', *snippets.values(), row])
            responses.append(facts)
            submitted = client.post('/api/v1/invoices/upload', auth=AUTH,
                headers={'Origin': ORIGIN, 'Idempotency-Key': number},
                files={'file': (source.name, source.read_bytes(), 'application/pdf')})
            assert submitted.status_code == 202
            identity = submitted.json()['id']
            deadline = time.monotonic()+10
            while True:
                detail = client.get('/api/v1/invoices/'+identity, auth=AUTH).json()
                if detail['state'] in {'completed', 'errored'}:
                    break
                assert time.monotonic() < deadline, detail
                time.sleep(.01)
            assert detail['state'] == 'completed', detail
            return identity, detail

        identity, detail = upload('DEMO-CLEAN', 'Northwind Office Supplies Ltd', 'PO-1001', '4800')
        assert detail['status'] == 'Approved'
        assert detail['report']['extraction']['invoice_number'] == 'DEMO-CLEAN'
        assert detail['report']['checks'] and detail['report']['sections']
        po = client.get('/api/v1/purchase-orders/PO-1001', auth=AUTH).json()
        assert po['fully_approved'] and Decimal(po['remaining_amount']) == 0
        identity, detail = upload('DEMO-REVIEW', 'Apex Logistics LLC', 'PO-1003', '2080')
        assert detail['report']['decision']['outcome'] == 'NEEDS_REVIEW'
        approval = client.post('/api/v1/invoices/'+identity+'/approve', auth=AUTH,
            headers={'Origin': ORIGIN, 'Idempotency-Key': 'approve-review'},
            json={'revision': detail['revision'], 'reason': 'Synthetic source reviewed and variance authorized.',
                  'confirmed_source': True})
        assert approval.status_code == 200, approval.text
        assert client.get('/api/v1/invoices/'+identity, auth=AUTH).json()['status'] == 'Approved'
        assert Decimal(client.get('/api/v1/purchase-orders/PO-1003', auth=AUTH).json()['approved_amount']) == 2080
        assert provider.calls == 2


def test_blueprint_and_build_context():
    blueprint = yaml.safe_load((ROOT/'render.yaml').read_text())
    assert set(blueprint) == {'services'}
    service, = blueprint['services']
    assert service['plan'] == 'free' and service['numInstances'] == 1
    assert service['healthCheckPath'] == '/healthz' and 'disk' not in service
    secrets = {entry['key']: entry for entry in service['envVars']}
    for key in ['DEMO_PASSWORD', 'DEMO_USERNAME', 'DEMO_PUBLIC_ORIGIN', 'OPENAI_API_KEY']:
        assert secrets[key] == {'key': key, 'sync': False}
    dockerfile = ROOT/service['dockerfilePath']
    assert Path(str(dockerfile)+'.dockerignore').is_file()
    copies = [line for line in dockerfile.read_text().splitlines() if line.startswith('COPY ')]
    assert copies == ['COPY requirements.lock .', 'COPY invoicedesk invoicedesk',
                      'COPY policy.yaml .', 'COPY data/vendors.csv data/purchase_orders.csv data/']
    ignore = Path(str(dockerfile)+'.dockerignore').read_text().splitlines()
    assert '*' in ignore and '!data/' in ignore
    assert [line for line in ignore if line.startswith('!data/')] == ['!data/', '!data/vendors.csv', '!data/purchase_orders.csv']
