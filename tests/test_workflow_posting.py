"""Offline workflow integration with a synthetic provider and independent PDF source.

No provider response in this module represents a real API extraction.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import json
import sqlite3

import pytest

from invoicedesk.db import Db
from invoicedesk.domain.models import ExtractedInvoice
from invoicedesk.infrastructure.extraction.llm import LlmClient
from invoicedesk.infrastructure.extraction.providers import ProviderResponse
from invoicedesk.interfaces.local_workspace import Workspace
from scripts.pdf_fallback import write_pdf


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path/'workspace',tmp_path/'absent.sqlite3')
    po = workspace.record('purchase-orders','PO-1001')
    po.update(net_amount='2000',description='Office furniture',tax_rate='.08')
    workspace.save_record('purchase-orders',po,'configure-order','PO-1001')

    class SyntheticProvider:
        name = 'openai'  # Exercise the configured-provider boundary without network.
        responses = []
        calls = 0

        def extract(self, path, *, model, schema):
            self.calls += 1
            return ProviderResponse(deepcopy(self.responses.pop(0)),resolved_model='synthetic-offline')

    provider = SyntheticProvider()
    def client(*args, **kwargs):
        return LlmClient('live',provider=provider,model='synthetic-offline',cache_dir=workspace.root/'synthetic-audit')
    monkeypatch.setattr('invoicedesk.interfaces.local_workspace.LlmClient',client)
    return workspace, provider


def process(workflow, tmp_path, number, net, date='2026-11-20', delivery=None):
    workspace, provider = workflow
    net = Decimal(net)
    adjustment = Decimal(delivery or '0')
    tax = ((net+adjustment)*Decimal('.08')).quantize(Decimal('.01'))
    total = net+adjustment+tax
    inv = ExtractedInvoice(document_type='invoice',vendor_name='Northwind Office Supplies Ltd',
        invoice_number=number,invoice_date=date,po_reference='PO-1001',currency='USD',
        subtotal=net,tax_amount=tax,total=total,prices_include_tax=False,
        line_items=[dict(description='Office furniture',quantity='1',unit_price=str(net),amount=str(net))],
        adjustments=[dict(description='Delivery',amount=str(adjustment))] if delivery is not None else [])
    facts = inv.model_dump(mode='json',exclude={'field_quality','warnings','extraction_path'})
    snippets = dict(vendor_name='Vendor: Northwind Office Supplies Ltd',invoice_number='Invoice No: '+number,
        invoice_date='Invoice Date: '+date,po_reference='PO Number: PO-1001',currency='Currency: USD',
        subtotal='Subtotal: '+str(net),tax_amount='Tax: '+str(tax),total='Total: '+str(total))
    row = f'Office furniture | 1 | {net} | {net}'
    facts['field_evidence'] = [dict(path=key,snippet=value,page=1) for key,value in snippets.items()]
    facts['field_evidence'].extend(dict(path='line_items.0.'+key,snippet=row,page=1)
                                   for key in ('description','quantity','unit_price','amount'))
    lines = ['Invoice',*snippets.values(),row]
    if delivery is not None:
        charge = 'Delivery: '+str(adjustment)
        lines.append(charge)
        facts['field_evidence'].extend(dict(path='adjustments.0.'+key,snippet=charge,page=1)
                                       for key in ('description','amount'))
    source = tmp_path/(number+'-'+str(provider.calls)+'.pdf')
    write_pdf(source,lines)
    provider.responses.append(facts)
    identity = workspace.submit(source.name,source.read_bytes(),'upload-'+source.name)['id']
    assert workspace.claim() == identity
    workspace.process(identity)
    assert workspace.job(identity)['state'] == 'completed', workspace.job(identity).get('error_json')
    return identity


def test_live_upload_auto_posts_once_and_partial_invoices_complete_po(workflow,tmp_path):
    workspace, provider = workflow
    first = process(workflow,tmp_path,'PART-1','600')
    assert workspace.detail(first)['status']=='Approved'
    assert Decimal(workspace.record('purchase-orders','PO-1001')['approved_amount'])==600
    second = process(workflow,tmp_path,'PART-2','1400',date='2026-12-20')
    assert workspace.detail(second)['status']=='Approved'
    po = workspace.record('purchase-orders','PO-1001')
    assert Decimal(po['approved_amount'])==2000 and Decimal(po['remaining_amount'])==0
    assert po['fully_approved'] is True
    with workspace.db() as db:
        ledger = db.rows('SELECT * FROM pilot_ledger')
        assert len(ledger)==2 and all(row['actor']=='system' for row in ledger)
    duplicate = process(workflow,tmp_path,'PART-1','600')
    detail = workspace.detail(duplicate)
    assert detail['status']!='Approved'
    assert detail['report']['decision']['outcome']=='REJECT'
    assert provider.calls==3  # A duplicate still follows the full model extraction path.
    with workspace.db() as db:
        assert len(db.rows('SELECT * FROM pilot_ledger'))==2


def test_review_invoice_does_not_consume_balance(workflow,tmp_path):
    workspace, provider = workflow
    identity = process(workflow,tmp_path,'EXTRA-CHARGE','600',delivery='20')
    detail = workspace.detail(identity)
    assert detail['status']=='Needs review'
    assert 'po_charge_consistency' in detail['report']['decision']['driving_checks']
    assert provider.calls==1
    with workspace.db() as db:
        assert db.rows('SELECT * FROM pilot_ledger')==[]
    assert Decimal(workspace.record('purchase-orders','PO-1001')['remaining_amount'])==2000


def test_vendor_reservations_concurrent_idempotent_and_never_reuse(workflow):
    workspace, _ = workflow
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda index:workspace.reserve_vendor_id('reserve-'+str(index))['vendor_id'],range(8)))
    assert len(set(ids))==8
    numbers = sorted(int(identity[2:]) for identity in ids)
    assert numbers==list(range(numbers[0],numbers[0]+8))
    assert workspace.reserve_vendor_id('reserve-0')['vendor_id']==ids[0]
    workspace.save_record('vendors',dict(vendor_id='V-700',name='Custom supplier',aliases=[]),'custom')
    workspace.delete_record('vendors','V-700',1,'V-700','delete-custom')
    assert workspace.reserve_vendor_id('after-deletion')['vendor_id']=='V-701'
    workspace.save_record('vendors',dict(vendor_id='ACME-CUSTOM',name='Other supplier',aliases=[]),'custom-text')
    assert workspace.reserve_vendor_id('after-custom-text')['vendor_id']=='V-702'


def test_sqlite_additive_migration_preserves_existing_master_and_history(tmp_path):
    path = tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE purchase_orders(po_number TEXT PRIMARY KEY,vendor_id TEXT,description TEXT,net_amount TEXT,tax_rate TEXT,currency TEXT,status TEXT,issue_date TEXT)')
        conn.execute('INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?)',('PO-OLD','V-OLD','Legacy services','123.4500','0','USD','open','2026-01-01'))
    db = Db(path)
    try:
        row = db.pos()[0]
        assert row['net_amount']=='123.4500' and row['description']=='Legacy services'
        assert json.loads(row['line_items_json'])==[] and json.loads(row['charge_allowances_json'])==[]
        with db.conn:
            db.conn.execute('INSERT INTO pilot_ledger VALUES (?,?,?,?,?,?)',('historic','PO-OLD','23.45','operator','Approved before migration','{}'))
    finally:
        db.close()
    db = Db(path)
    try:
        assert db.billed('PO-OLD')==Decimal('23.45')
        assert db.remaining(db.pos()[0])==Decimal('100.0000')
        assert len(db.rows('SELECT * FROM pilot_ledger'))==1
    finally:
        db.close()
