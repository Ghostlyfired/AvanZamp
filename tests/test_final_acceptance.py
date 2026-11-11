"""Offline acceptance of known printed facts, not evidence of live model accuracy."""
import json
from decimal import Decimal
import pytest
from invoicedesk.config import ROOT
from invoicedesk.infrastructure.extraction.llm import LlmClient
from invoicedesk.infrastructure.extraction.providers import ProviderResponse
from invoicedesk.interfaces.final_verify import bootstrap_test_master
from invoicedesk.db import Db
from invoicedesk.pipeline import run_invoice
from invoicedesk.policy import Policy


@pytest.mark.parametrize('number', range(4))
def test_positive_layout_facts_follow_general_approval_path(tmp_path, number):
    manifest = json.loads((ROOT/'evaluation/final_tests.json').read_text())
    source = ROOT/'data/final_tests/positive'/f'Invoice_Apple_{number:02d}.pdf'
    master = tmp_path/'master.sqlite3'
    bootstrap_test_master(master, manifest)
    fields = dict(next(c['fields'] for c in manifest['cases'] if c['file'].endswith(source.name)))
    fields.update(document_type='invoice', vendor_name='Apple Technologies Ltd.' if number < 2 else 'Apple Technologies Inc',
                  line_items=[], adjustments=[])
    if number >= 2:
        fields['currency'] = '$'
    fields['evidence'] = {name: str(value) for name, value in fields.items() if isinstance(value, str)}
    fields['evidence'].update(invoice_date='20/11/2026' if number < 2 else 'Nov 19, 2026',
                              currency='US$' if number < 2 else '$',
                              subtotal='3,196' if number < 2 else '$3,196.00',
                              tax_amount='383.52', total='3,579.52')
    # Exact printed cells from these test inputs. The test provider does not infer
    # facts from a filename or alter domain validation.
    fields['field_evidence'] = []
    for row in json.loads(manifest['master']['purchase_orders'][0]['line_items_json']):
        index = len(fields['line_items'])
        fields['line_items'].append(row)
        for name, value in row.items():
            snippet = value
            if name in {'unit_price', 'amount'}:
                snippet = format(Decimal(value), ',.2f' if number >= 2 else ',.0f')
            fields['field_evidence'].append(dict(path=f'line_items.{index}.{name}', snippet=snippet))
    class SyntheticProvider:
        name = 'synthetic-final-acceptance'
        def extract(self, *args, **kwargs):
            return ProviderResponse(fields)
    db = Db(master, mode='pilot')
    try:
        client = LlmClient('live', tmp_path/'cache', provider=SyntheticProvider(), model='offline-test')
        events = list(run_invoice(source, db=db, policy=Policy.load(), llm=client))
        decision = events[-1].payload['decision']
        assert decision['outcome'] == 'AUTO_APPROVE', decision
        assert client.calls == 1 and not client.cache_hit
    finally:
        db.close()


def test_test_master_bootstrap_preserves_existing_database(tmp_path):
    target = tmp_path/'existing.sqlite3'
    target.write_bytes(b'preserve existing state')
    with pytest.raises(ValueError, match='existing data is preserved'):
        bootstrap_test_master(target, {})
    assert target.read_bytes() == b'preserve existing state'
