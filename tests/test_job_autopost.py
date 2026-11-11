"""Production job services with SQLite test persistence and synthetic extraction.

The model facts are independent offline fixtures, not real provider recordings.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import threading

import pytest
import sqlalchemy as sa

from invoicedesk.application.jobs import JobService
from invoicedesk.infrastructure import schema as s
from invoicedesk.infrastructure.repository import Repository
from invoicedesk.infrastructure.storage import LocalStorage
from invoicedesk.infrastructure.extraction.llm import LlmClient
from invoicedesk.infrastructure.extraction.providers import ProviderResponse
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.policy import Policy
from scripts.pdf_fallback import write_pdf


@pytest.fixture
def backend(tmp_path):
    repo = Repository('sqlite:///'+str(tmp_path/'jobs.sqlite3'),allow_sqlite=True)
    s.metadata.create_all(repo.engine)
    repo.initialize(Policy.load(),[dict(vendor_id='V1',name='Example Supplier',aliases_json='[]',status='approved',notes='')],
        [dict(po_number='PO-ABC',vendor_id='V1',description='Office furniture',net_amount='2000',tax_rate='.08',
              currency='USD',status='open',issue_date='2026-01-01')])
    service = JobService(repo,LocalStorage(tmp_path/'documents'))
    yield repo, service
    repo.engine.dispose()


def submit(backend,tmp_path,number,net,date='2026-11-20',delivery=None,rounding='0',on_extract=None):
    repo, service = backend
    net, charge = Decimal(net), Decimal(delivery or '0')
    tax = ((net+charge)*Decimal('.08')).quantize(Decimal('.01'))
    inv = ExtractedInvoice(document_type='invoice',vendor_name='Example Supplier',invoice_number=number,
        invoice_date=date,po_reference='PO-ABC',currency='USD',subtotal=net,tax_amount=tax,
        total=net+charge+tax+Decimal(rounding),prices_include_tax=False,
        line_items=[dict(description='Office furniture',quantity='1',unit_price=str(net),amount=str(net))],
        adjustments=[dict(description='Delivery',amount=str(charge))] if delivery is not None else [])
    facts = inv.model_dump(mode='json',exclude={'warnings','extraction_path','field_quality'})
    snippets = dict(vendor_name='Vendor: Example Supplier',invoice_number='Invoice No: '+number,
        invoice_date='Invoice Date: '+date,po_reference='PO Number: PO-ABC',currency='Currency: USD',
        subtotal='Subtotal: '+str(net),tax_amount='Tax: '+str(tax),total='Total: '+str(inv.total))
    row = f'Office furniture | 1 | {net} | {net}'
    facts['field_evidence'] = [dict(path=key,snippet=value,page=1) for key,value in snippets.items()]
    facts['field_evidence'].extend(dict(path='line_items.0.'+key,snippet=row,page=1)
                                   for key in ('description','quantity','unit_price','amount'))
    lines = ['INVOICE',*snippets.values(),row]
    if delivery is not None:
        snippet = 'Delivery: '+str(charge)
        lines.append(snippet)
        facts['field_evidence'].extend(dict(path='adjustments.0.'+key,snippet=snippet,page=1) for key in ('description','amount'))
    source = tmp_path/(number+'-'+net.to_eng_string()+'.pdf')
    write_pdf(source,lines)
    result = service.submit(source.read_bytes(),source.name,actor='uploader',roles={'uploader'},key='upload-'+number+'-'+net.to_eng_string())
    job = repo.claim()
    assert job['id']==result['job_id']

    class SyntheticProvider:
        name = 'synthetic'
        calls = 0

        def extract(self,path,*,model,schema):
            self.calls += 1
            if on_extract:
                on_extract()
            return ProviderResponse(facts,resolved_model='synthetic-offline')

    provider = SyntheticProvider()
    client = LlmClient('live',provider=provider,model='synthetic-offline',cache_dir=tmp_path/'synthetic-audit')
    return job,client,provider


def report(backend,job):
    return backend[0].report(job['id'],'reviewer',{'reviewer'})


def test_job_automatic_posting_revision_actor_and_final_events(backend,tmp_path):
    repo,service = backend
    job,client,provider = submit(backend,tmp_path,'AUTO-1','600')
    service.process(job,client)
    saved = report(backend,job)
    assert saved['review_state']=='approved' and saved['revision']==1
    assert saved['decision']['outcome']=='AUTO_APPROVE'
    with repo.engine.connect() as c:
        ledger = dict(c.execute(sa.select(s.ledger)).mappings().one())
        assert ledger['job_id']==job['id'] and ledger['revision']==saved['revision']
        assert ledger['actor']=='system' and ledger['amount']==Decimal('600')
        assert ledger['context']==saved['context']
        events = list(c.execute(sa.select(s.events.c.data).where(s.events.c.job_id==job['id']).order_by(s.events.c.seq)).scalars())
        assert [event['seq'] for event in events]==list(range(1,len(events)+1))
        assert events[-1]['stage']=='complete' and events[-1]['payload']['revision']==1
        assert events[-1]['payload']['decision']==saved['decision']
        audit = list(c.execute(sa.select(s.audit.c.action).where(s.audit.c.target==job['id'])).scalars())
        assert 'provider_dispatch' in audit and 'auto_approved' in audit
    assert provider.calls==1


def test_sequential_partial_jobs_then_duplicate_never_double_post(backend,tmp_path):
    repo,service = backend
    for number,net,day in [('PART-A','600','2026-11-20'),('PART-B','1400','2026-12-20')]:
        job,client,_ = submit(backend,tmp_path,number,net,day)
        service.process(job,client)
        assert report(backend,job)['review_state']=='approved'
    # A new submission of an already committed invoice still invokes extraction.
    job,client,provider = submit(backend,tmp_path,'PART-A','600.00')
    service.process(job,client)
    saved = report(backend,job)
    assert saved['decision']['outcome']=='REJECT' and saved['review_state']=='pending'
    assert provider.calls==1
    with repo.engine.connect() as c:
        amounts = list(c.execute(sa.select(s.ledger.c.amount)).scalars())
        assert len(amounts)==2 and sum(amounts)==Decimal('2000')


@pytest.mark.parametrize('amount,delivery,expected',[('600','20','NEEDS_REVIEW'),('2500',None,'REJECT')])
def test_review_and_rejected_jobs_consume_no_balance(backend,tmp_path,amount,delivery,expected):
    repo,service = backend
    job,client,_ = submit(backend,tmp_path,'EXCEPTION',amount,delivery=delivery)
    service.process(job,client)
    saved = report(backend,job)
    assert saved['decision']['outcome']==expected and saved['review_state']=='pending'
    with repo.engine.connect() as c:
        assert list(c.execute(sa.select(s.ledger)).mappings())==[]


def test_final_report_uses_policy_changed_during_extraction(backend,tmp_path):
    repo,service = backend
    def tighten_policy():
        with repo.engine.begin() as c:
            repo.lock(c)
            policy = repo.policy(c).model_dump(mode='json')
            policy['version']='tighter-current-policy'
            policy['extraction']['reconcile_abs_tolerance']='0'
            repo.administer(c,'policy',policy)
    job,client,_ = submit(backend,tmp_path,'POLICY-CHANGE','600',rounding='.01',on_extract=tighten_policy)
    service.process(job,client)
    saved = report(backend,job)
    assert saved['context']['policy_version']=='tighter-current-policy'
    assert json.loads(saved['context']['policy_json'])['extraction']['reconcile_abs_tolerance']=='0'
    assert saved['decision']['outcome']=='NEEDS_REVIEW' and saved['review_state']=='pending'
    assert any(not finding['passed'] for finding in saved['findings'])
    with repo.engine.connect() as c:
        assert list(c.execute(sa.select(s.ledger)).mappings())==[]


def test_concurrent_jobs_serialize_final_balance_checks(backend,tmp_path):
    repo,service = backend
    barrier = threading.Barrier(2,timeout=15)
    first = submit(backend,tmp_path,'CONCURRENT-A','1200','2026-01-20',on_extract=barrier.wait)
    second = submit(backend,tmp_path,'CONCURRENT-B','1200','2026-03-20',on_extract=barrier.wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.process,job,client) for job,client,_ in (first,second)]
        for future in futures:
            future.result(timeout=30)
    saved = [report(backend,job) for job,_,_ in (first,second)]
    assert sorted(row['decision']['outcome'] for row in saved)==['AUTO_APPROVE','REJECT']
    assert sorted(row['review_state'] for row in saved)==['approved','pending']
    with repo.engine.connect() as c:
        assert list(c.execute(sa.select(s.ledger.c.amount)).scalars())==[Decimal('1200')]
