"""Offline integration contracts; mocked model output is not real extraction evidence."""
import json
import logging
import sys
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import pytest
from PIL import Image
from invoicedesk.db import Db
from invoicedesk.documents import inspect_document
from invoicedesk.policy import ROOT, Policy
from invoicedesk.seed import reset
from invoicedesk.pipeline import run_invoice
from invoicedesk.extract.llm import LlmClient, LlmUnavailable
from invoicedesk.extract.router import extract
from invoicedesk.extract.tier1 import parse
from invoicedesk.extract.grounding import ground
from invoicedesk.extract.text import DocumentText
from invoicedesk.models import ExtractedInvoice, Quality
from invoicedesk.cli import main

@pytest.fixture
def db(tmp_path):
    db = Db(tmp_path/'test.sqlite3', mode='demo')
    reset(db)
    yield db
    db.close()


def example_fields():
    return dict(vendor_name='Northwind Office Supplies Ltd', invoice_number='REAL-123',
                invoice_date='2026-11-09', currency='USD', po_reference='PO-1001',
                subtotal='100.00', tax_amount='8.00', total='108.00',
                prices_include_tax=None, line_items=[], evidence={})


def mock_provider(monkeypatch, fields):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type='tool_use', name='extract_invoice', input=fields)],
            stop_reason='tool_use', usage=SimpleNamespace(model_dump=lambda: {'input_tokens': 10, 'output_tokens': 20}),
        )
    monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(
        Anthropic=lambda **kwargs: SimpleNamespace(messages=SimpleNamespace(create=create))))
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-not-a-real-key')
    monkeypatch.setenv('LLM_MODEL', 'test-model')
    return calls


@pytest.mark.parametrize('fmt,suffix', [('PNG','.png'), ('JPEG','.jpg'), ('WEBP','.webp')])
def test_image_pipeline_with_mock_provider(db,tmp_path,monkeypatch,fmt,suffix):
    path = tmp_path/('invoice'+suffix)
    Image.new('RGB',(400,600),'white').save(path,fmt)
    calls = mock_provider(monkeypatch, example_fields())
    llm = LlmClient('live',cache_dir=tmp_path/'private-cache')
    events = list(run_invoice(path,db=db,policy=Policy.load(),llm=llm))
    assert len(calls)==1
    block = calls[0]['messages'][0]['content'][0]
    assert block['type']=='image'
    assert block['source']['media_type']=='image/jpeg'
    assert events[-1].payload['decision']['outcome']=='NEEDS_REVIEW'
    assert 'extraction_integrity' in events[-1].payload['decision']['driving_checks']
    row = db.runs()[0]
    assert row['extraction_path']=='llm_vision'
    assert db.billed('PO-1001')==0
    assert json.loads(row['extraction_json'])['field_quality']['total']=='UNVERIFIED'


def test_image_without_model_is_review_not_missing(db,tmp_path):
    path = tmp_path/'invoice.png'; Image.new('RGB',(100,100),'white').save(path)
    final = list(run_invoice(path,db=db,policy=Policy.load(),llm=LlmClient('off')))[-1]
    assert final.payload['decision']['outcome']=='NEEDS_REVIEW'
    assert db.runs()[0]['vendor_id'] is None


def test_live_never_uses_synthetic_fixture(tmp_path,monkeypatch):
    path = ROOT/'data/invoices/09_near_duplicate.pdf'
    document = inspect_document(path)
    (tmp_path/(document.sha256+'.json')).write_text(json.dumps({
        'provenance':'synthetic_fixture','extraction':example_fields()}))
    calls = mock_provider(monkeypatch,example_fields())
    client = LlmClient('live',cache_dir=tmp_path)
    client.extract_invoice(path)
    assert len(calls)==1 and not client.cache_hit
    assert client.provenance=='api_recording'
    # Every live extraction makes a new request, even with an audit recording.
    client.extract_invoice(path)
    assert len(calls)==2 and not client.cache_hit


def test_default_mode_makes_no_network_calls(monkeypatch):
    monkeypatch.delenv('LLM_MODE',raising=False)
    assert LlmClient().mode=='off'


def test_missing_key_never_crashes_image_pipeline(db,tmp_path,monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False)
    path = tmp_path/'invoice.png'; Image.new('RGB',(100,100)).save(path)
    final = list(run_invoice(path,db=db,policy=Policy.load(),llm=LlmClient('live',tmp_path/'cache')))[-1]
    assert final.payload['decision']['outcome']=='NEEDS_REVIEW'


def test_blank_label_does_not_capture_next_line():
    inv=parse('Vendor: Northwind Office Supplies Ltd\nInvoice No:\nCustomer account no: 1234\nDelivery note: DN-5\n')
    assert inv.invoice_number is None


def test_non_colon_labels_and_dates():
    inv=parse('Supplier  Northwind Office Supplies Ltd\nInvoice #  N-200\nInvoice date  09 November 2026\nSubtotal  $100.00\nTax  $8.00\nTotal Due  $108.00\nCurrency  USD\n')
    assert inv.invoice_number=='N-200'
    assert inv.invoice_date.isoformat()=='2026-11-09'
    assert inv.total==Decimal('108')


def test_amount_evidence_requires_whole_number():
    inv=ExtractedInvoice(total='100.00',evidence={'total':'Total: 1100.00'})
    ground(inv,'Total: 1100.00')
    assert inv.field_quality['total']==Quality.UNVERIFIED


def test_mixed_pdf_cannot_pass_on_cover_page(monkeypatch):
    assert DocumentText(['This is a text cover page with enough characters to appear readable.', '']).needs_vision
    from invoicedesk.extract import router
    text=(ROOT/'data/invoices/01_clean_exact.pdf')
    from invoicedesk.extract.text import read_text
    original = read_text(text)
    monkeypatch.setattr(router,'read_document_text',lambda _:DocumentText([original,'']))
    inv,_=extract(text,Policy.load(),LlmClient('off'))
    assert inv.extraction_path=='failed'


def test_omitted_keys_are_not_confirmed_missing(db,tmp_path,monkeypatch):
    path=tmp_path/'invoice.png';Image.new('RGB',(100,100)).save(path)
    mock_provider(monkeypatch,{'vendor_name':'Northwind Office Supplies Ltd'})
    events=list(run_invoice(path,db=db,policy=Policy.load(),llm=LlmClient('live',tmp_path/'cache')))
    assert events[-1].payload['decision']['outcome']=='NEEDS_REVIEW'
    assert 'completeness' not in events[-1].payload['decision']['driving_checks']


def test_terminal_logs_and_json_report(tmp_path,caplog,capsys):
    output=tmp_path/'report.json'
    with caplog.at_level(logging.INFO,logger='invoicedesk'):
        result=main(['--profile','demo','--db',str(tmp_path/'cli.sqlite3'),'run',str(ROOT/'data/invoices/01_clean_exact.pdf'),
                     '--llm-mode','off','--output',str(output),'--json'])
    assert result==0
    assert 'intake' in caplog.text and 'amount_vs_balance' in caplog.text and 'complete' in caplog.text
    report=json.loads(capsys.readouterr().out)
    assert report['decision']['outcome']=='AUTO_APPROVE'
    assert json.loads(output.read_text())['extraction']['total']=='5184.00'


def test_invalid_image_exits_cleanly(tmp_path,caplog):
    path=tmp_path/'not-an-image.png';path.write_text('invalid')
    assert main(['--profile','demo','--db',str(tmp_path/'cli.sqlite3'),'run',str(path)])==1
    db=Db(tmp_path/'cli.sqlite3');assert not db.runs();db.close()

def test_float_model_amounts_are_not_silently_accepted():
    from invoicedesk.extract.llm import validate_response
    fields=example_fields(); fields['total']=108.01
    with pytest.raises(LlmUnavailable,match='decimal strings'):
        validate_response(fields)

def test_provider_injection_and_cache_namespace(tmp_path,monkeypatch):
    from invoicedesk.extract.providers import ProviderResponse
    monkeypatch.delenv('ANTHROPIC_API_KEY',raising=False)
    path=tmp_path/'invoice.png';Image.new('RGB',(100,100)).save(path)
    calls=[]
    class CustomProvider:
        def __init__(self,name): self.name=name
        def extract(self,path,*,model,schema):
            calls.append((self.name,model))
            assert 'invoice_number' in schema['properties']
            return ProviderResponse(example_fields(),{'tokens':12})
    first=LlmClient('live',tmp_path/'cache',provider=CustomProvider('provider-a'),model='model-1')
    first.extract_invoice(path)
    assert calls==[('provider-a','model-1')]
    first.extract_invoice(path)
    assert not first.cache_hit and len(calls)==2
    second=LlmClient('live',tmp_path/'cache',provider=CustomProvider('provider-b'),model='model-1')
    second.extract_invoice(path)
    assert not second.cache_hit and calls[-1]==('provider-b','model-1')
    third=LlmClient('live',tmp_path/'cache',provider=CustomProvider('provider-b'),model='model-2')
    third.extract_invoice(path)
    assert not third.cache_hit and calls[-1]==('provider-b','model-2')


def test_configured_provider_factory(tmp_path,monkeypatch):
    from invoicedesk.extract.providers import ProviderResponse
    adapter=SimpleNamespace(name='custom',extract=lambda path,**kwargs:ProviderResponse(example_fields()))
    monkeypatch.setitem(sys.modules,'test_invoice_adapter',SimpleNamespace(build=lambda:adapter))
    path=tmp_path/'invoice.png';Image.new('RGB',(100,100)).save(path)
    client=LlmClient('live',tmp_path/'cache',provider='test_invoice_adapter:build',model='test-model')
    assert client.extract_invoice(path)['invoice_number']=='REAL-123'
    assert client.calls==1

def test_malformed_cache_routes_to_review(db,tmp_path):
    path=ROOT/'data/invoices/11_scanned_implied_po.pdf'
    document=inspect_document(path)
    (tmp_path/(document.sha256+'.json')).write_text('[]')
    client=LlmClient('cache',tmp_path)
    events=list(run_invoice(path,db=db,policy=Policy.load(),llm=client))
    assert events[-1].payload['decision']['outcome']=='NEEDS_REVIEW'
