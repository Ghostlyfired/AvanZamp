"""Metrics distinguish real usage, missing usage and cache reuse."""
import json
import logging
from io import BytesIO
from urllib.error import URLError
import pytest
from invoicedesk.metrics import token_usage
from invoicedesk.extract.llm import LlmClient, LlmUnavailable
from invoicedesk.extract.providers import ProviderResponse, ProviderUnavailable
from invoicedesk.extract.providers import openai as openai_provider
from invoicedesk.policy import ROOT, Policy
from invoicedesk.db import Db
from invoicedesk.seed import reset
from invoicedesk.pipeline import run_invoice


def fields():
    return dict(vendor_name='Northwind Office Supplies Ltd', invoice_number='METRIC-1',
                invoice_date='2026-11-09', subtotal='100.00', tax_amount='8.00',total='108.00',
                po_reference='PO-1001', currency='USD')


def test_usage_is_unknown_for_failed_requests_and_zero_without_request():
    assert token_usage({},1)=={'input_tokens':None,'output_tokens':None,'total_tokens':None}
    assert token_usage({'input_tokens':999},0)=={'input_tokens':0,'output_tokens':0,'total_tokens':0}
    assert token_usage({'input_tokens':10,'output_tokens':20},1)['total_tokens']==30
    usage={'input_tokens':10,'output_tokens':20,'total_tokens':30,
           'input_tokens_details':{'cached_tokens':5},'output_tokens_details':{'reasoning_tokens':4}}
    assert token_usage(usage,1)['total_tokens']==30  # cached/reasoning are subsets, not additions
    assert token_usage(usage,1)['cached_tokens']==5
    assert token_usage(usage,1)['reasoning_tokens']==4
    assert token_usage({'input_tokens':10,'output_tokens':20,'cache_read_input_tokens':8},1)['total_tokens']==38


def test_live_api_metrics_account_for_each_fresh_request(tmp_path,caplog):
    class Provider:
        name='test-provider'
        def extract(self,*args,**kwargs):
            return ProviderResponse(fields(), {'input_tokens':100,'output_tokens':50}, response_ms=125.5)
    client=LlmClient('live',tmp_path/'cache',provider=Provider(),model='test-model')
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        client.extract_invoice(ROOT/'data/examples/office_furniture.png')
    assert 'response_time_ms=125.50' in caplog.text
    assert 'input_tokens=100 output_tokens=50 total_tokens=150' in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        client.extract_invoice(ROOT/'data/examples/office_furniture.png')
    assert client.calls==1 and client.response_ms==125.5
    assert client.token_usage['total_tokens']==150
    assert 'API metrics:' in caplog.text


def test_failure_preserves_reported_usage_and_timing(tmp_path,caplog):
    class Provider:
        name='failed-provider'
        def extract(self,*args,**kwargs):
            raise ProviderUnavailable('Truncated',api_calls=1,usage={'input_tokens':10,'output_tokens':20},response_ms=500)
    client=LlmClient('live',tmp_path/'cache',provider=Provider(),model='test')
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        with pytest.raises(LlmUnavailable):
            client.extract_invoice(ROOT/'data/examples/office_furniture.pdf')
    assert client.token_usage['total_tokens']==30
    assert 'response_time_ms=500.00' in caplog.text and 'total_tokens=30' in caplog.text


def test_transport_failure_has_timing_and_unknown_tokens(tmp_path,monkeypatch,caplog):
    ticks=iter([1.0,1.25])
    monkeypatch.setattr(openai_provider,'perf_counter',lambda:next(ticks))
    def fail(*args,**kwargs):raise URLError('network unavailable')
    monkeypatch.setattr(openai_provider,'urlopen',fail)
    client=LlmClient('live',tmp_path/'cache',provider='openai',model='gpt-5-mini')
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        with pytest.raises(LlmUnavailable):
            client.extract_invoice(ROOT/'data/examples/office_furniture.png')
    assert client.response_ms==250
    assert 'total_tokens=unknown' in caplog.text


def test_refusal_keeps_token_usage(monkeypatch):
    body={'status':'completed','usage':{'input_tokens':42,'output_tokens':12,'total_tokens':54},
          'output':[{'type':'message','content':[{'type':'refusal'}]}]}
    monkeypatch.setattr(openai_provider,'urlopen',lambda *args,**kwargs:BytesIO(json.dumps(body).encode()))
    with pytest.raises(ProviderUnavailable) as error:
        openai_provider.OpenAIProvider().extract(ROOT/'data/examples/office_furniture.pdf',model='gpt-5-mini',schema={'properties':{}})
    assert error.value.usage['total_tokens']==54
    assert error.value.response_ms>=0


def test_pipeline_logs_time_after_persistence(tmp_path,monkeypatch,caplog):
    db=Db(tmp_path/'test.sqlite3')
    reset(db)
    ticks=iter([1.0,1.875])
    monkeypatch.setattr('invoicedesk.pipeline.time.perf_counter',lambda:next(ticks))
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        stream=run_invoice(ROOT/'data/invoices/01_clean_exact.pdf',db=db,policy=Policy.load(),llm=LlmClient('off'))
        for event in stream:
            if event.stage=='complete':
                assert db.runs()
                assert 'Pipeline total response time: 875.00 ms' in caplog.text
    db.close()


def test_cli_logs_total_time_and_keeps_json_valid(tmp_path,caplog,capsys):
    from invoicedesk.cli import main
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        assert main(['--profile','demo','--db',str(tmp_path/'test.sqlite3'),'run',str(ROOT/'data/invoices/01_clean_exact.pdf'),'--json'])==0
    assert json.loads(capsys.readouterr().out)['decision']['outcome']=='AUTO_APPROVE'
    assert 'App total response time:' in caplog.text
    assert 'session_id=' not in caplog.text and 'req_id=' not in caplog.text

def test_non_api_adapter_reports_zero_api_time(tmp_path):
    class Provider:
        name='local'
        def extract(self,*args,**kwargs):
            return ProviderResponse(fields(),api_calls=0,response_ms=50)
    client=LlmClient('live',tmp_path/'cache',provider=Provider(),model='local')
    client.extract_invoice(ROOT/'data/examples/office_furniture.pdf')
    assert client.response_ms==0
    assert client.token_usage['total_tokens']==0
