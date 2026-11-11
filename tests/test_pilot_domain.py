"""Offline characterization of pilot invariants; synthetic facts throughout."""
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from invoicedesk.db import Db
from invoicedesk.seed import reset
from invoicedesk.policy import Policy
from invoicedesk.domain.approval import ApprovalBlocked, authorize
from invoicedesk.domain.contracts import ProcurementSnapshot, EvidenceReference
from invoicedesk.domain.evaluation import evaluate
from invoicedesk.domain.models import ExtractedInvoice, Quality
from invoicedesk.extract.reconcile import reconcile
from invoicedesk.extract.llm import LlmClient
from invoicedesk.pipeline import run_invoice


@pytest.fixture
def pilot(tmp_path):
    db = Db(tmp_path/'pilot.sqlite3')
    reset(db)
    yield db
    db.close()


def invoice(**changes):
    fields = dict(document_type='invoice', vendor_name='Northwind Office Supplies Ltd', invoice_number='PILOT-1',
                  invoice_date='2026-11-01', po_reference='PO-1001', currency='USD', subtotal='3000',
                  tax_amount='240', total='3240', prices_include_tax=False,
                  field_quality={name: Quality.VERIFIED for name in ['vendor_name', 'invoice_number', 'invoice_date', 'total']})
    fields.update(changes)
    return ExtractedInvoice(**fields)


def recommendation(db, inv=None, refs=()):
    inv = inv or invoice()
    policy = Policy.load()
    return evaluate(inv, reconcile(inv, policy.extraction.reconcile_abs_tolerance), db.snapshot(), policy, source_evidence=refs)


def grounded(inv):
    return tuple(EvidenceReference(path=name, snippet=str(getattr(inv, name)), verification='GROUNDED')
                 for name in [*Policy.load().critical_fields,'currency','subtotal','tax_amount','po_reference'])


def test_recommendation_is_not_a_commitment(pilot):
    inv = invoice()
    rec = recommendation(pilot, inv, grounded(inv))
    assert rec.decision.outcome == 'AUTO_APPROVE'
    assert pilot.billed('PO-1001') == 0
    assert authorize(rec, Policy.load(), '', False) == Decimal('3000')


def test_missing_evidence_needs_confirmation_and_recorded_reason(pilot):
    rec = recommendation(pilot)
    assert 'source_grounding' in rec.decision.driving_checks
    with pytest.raises(ApprovalBlocked, match='reason'):
        authorize(rec, Policy.load(), '', True)
    with pytest.raises(ApprovalBlocked, match='source'):
        authorize(rec, Policy.load(), 'Reviewed', False)
    assert authorize(rec, Policy.load(), 'Compared all fields to the source', True) == 3000


@pytest.mark.parametrize('changes', [dict(document_type='unknown'), dict(document_type='advance_request'),
    dict(document_type='bill_of_materials'), dict(invoice_number=None), dict(total=None),
    dict(currency='EUR'), dict(extraction_path='failed'), dict(warnings=['Partial extraction: total (invalid)'])])
def test_unresolved_facts_never_authorize(pilot, changes):
    rec = recommendation(pilot, invoice(**changes))
    with pytest.raises(ApprovalBlocked):
        authorize(rec, Policy.load(), 'Exception requested', True)


@pytest.mark.parametrize('target,sql', [
    ('vendor', "UPDATE vendors SET status='blocked' WHERE vendor_id='V001'"),
    ('po', "UPDATE purchase_orders SET status='closed' WHERE po_number='PO-1001'"),
    ('balance', "UPDATE purchase_orders SET net_amount='1000' WHERE po_number='PO-1001'")])
def test_current_procurement_blocks_stale_recommendation(pilot, target, sql):
    original = recommendation(pilot)
    with pilot.conn:
        pilot.conn.execute(sql)
    current = recommendation(pilot)
    assert original.context.snapshot_sha256 != current.context.snapshot_sha256
    with pytest.raises(ApprovalBlocked):
        authorize(current, Policy.load(), 'Old recommendation looked good', True)


def test_checks_do_not_mutate_input_and_snapshots_are_immutable(pilot):
    inv = invoice(subtotal=None)
    original = inv.model_dump_json()
    snapshot = pilot.snapshot()
    recommendation(pilot, inv)
    assert inv.model_dump_json() == original
    copy = snapshot.pos()
    copy[0]['net_amount'] = '0'
    assert snapshot.pos()[0]['net_amount'] != '0'
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 2


def test_reconstruct_recommendation_from_recorded_context(pilot):
    inv = invoice()
    first = recommendation(pilot, inv)
    recorded = json.loads(first.context.snapshot_json)
    snapshot = ProcurementSnapshot(recorded['revision'], json.dumps(recorded['vendors']), json.dumps(recorded['pos']),
                                   json.dumps(recorded['runs']), tuple(recorded['balances'].items()))
    policy = Policy.model_validate_json(first.context.policy_json)
    again = evaluate(inv, reconcile(inv, policy.extraction.reconcile_abs_tolerance), snapshot, policy)
    assert first.decision == again.decision and first.context == again.context


def create_run(db, tmp_path, number, day):
    from scripts.pdf_fallback import write_pdf
    path = tmp_path/(number+'.pdf')
    write_pdf(path, ['INVOICE', 'Vendor: Northwind Office Supplies Ltd', 'Invoice No: '+number,
        'Invoice date: '+day, 'PO Number: PO-1001', 'Currency: USD', 'Subtotal: 3000.00',
        'Tax: 240.00', 'Total Due: 3240.00', 'Item: Service | 1 | 3000.00 | 3000.00'])
    return list(run_invoice(path, db=db, policy=Policy.load(), llm=LlmClient('off')))[-1].run_id


def test_repeated_approval_commands_are_idempotent(pilot, tmp_path):
    rid = create_run(pilot, tmp_path, 'ONCE-1', '2026-01-01')
    assert pilot.billed('PO-1001') == 0
    for _ in range(2):
        pilot.review(rid, True, 'Checked source', key='same-command', confirmed_source=True)
    assert pilot.billed('PO-1001') == Decimal('3000')
    assert len(pilot.rows('SELECT * FROM pilot_ledger')) == 1
    assert len(pilot.rows('SELECT * FROM review_audit')) == 1
    with pytest.raises(ValueError, match='reused'):
        pilot.review(rid, False, 'Different action', key='same-command')


def test_concurrent_approvals_cannot_reuse_balance(pilot, tmp_path):
    runs = [create_run(pilot, tmp_path, 'FIRST-1', '2026-01-01'), create_run(pilot, tmp_path, 'SECOND-2', '2026-03-01')]
    barrier = threading.Barrier(2)
    policy = Policy.load()
    def approve(rid):
        # Policy adapter selection is explicit in each thread; no network or providers.
        db = Db(pilot.path)
        barrier.wait(timeout=10)
        try:
            db.review(rid, True, 'Checked source', key=rid, confirmed_source=True)
            return 'approved'
        except ApprovalBlocked:
            return 'blocked'
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, runs))
    assert sorted(outcomes) == ['approved', 'blocked']
    assert pilot.billed('PO-1001') == Decimal('3000')


def test_no_lock_is_held_while_cli_consumer_reads_stages(pilot, tmp_path):
    from scripts.pdf_fallback import write_pdf
    path = tmp_path/'document.pdf'
    write_pdf(path, ['INVOICE', 'Vendor: Northwind Office Supplies Ltd'])
    stream = run_invoice(path, db=pilot, policy=Policy.load(), llm=LlmClient('off'))
    try:
        for event in stream:
            assert not pilot.conn.in_transaction
    finally:
        stream.close()


def test_production_startup_fails_without_required_dependencies(monkeypatch):
    from invoicedesk.infrastructure.preflight import require_production_dependencies
    monkeypatch.setitem(sys.modules, 'pdfplumber', None)
    with pytest.raises(RuntimeError, match='pdfplumber'):
        require_production_dependencies()


def test_explicit_development_scoring_cannot_silently_replace_required_matching(monkeypatch):
    from invoicedesk.domain.matching import similarity
    monkeypatch.setitem(sys.modules, 'rapidfuzz.fuzz', None)
    with pytest.raises(ModuleNotFoundError):
        similarity('Vendor', 'Vendor')
    assert similarity('Vendor', 'Vendor', 'development') == 100


def test_domain_has_no_adapter_imports():
    import ast
    for path in (Path(__file__).parents[1]/'invoicedesk/domain').glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(part in node.module for part in ('infrastructure', 'interfaces', 'application', 'sqlite', 'logging', 'httpx'))


def test_evidence_cell_page_and_numeric_support_are_independent():
    from invoicedesk.infrastructure.extraction.evidence import verify_references
    inv = invoice(field_evidence=[{'path':'total','snippet':'Total: 3240.00','page':1},
                                  {'path':'subtotal','snippet':'Total: 3240.00','page':1},
                                  {'path':'vendor_name','snippet':'Northwind Office Supplies Ltd','page':4}])
    refs = verify_references(inv, ['Total: 3240.00\nNorthwind Office Supplies Ltd'])
    assert [r.verification for r in refs] == ['GROUNDED', 'UNVERIFIED', 'UNVERIFIED']
    assert all(r.verification == 'UNVERIFIED' for r in verify_references(inv, []))


def test_signed_invoice_adjustments_change_net_comparison_without_changing_printed_subtotal(pilot):
    from invoicedesk.domain.normalize import normalize
    inv = invoice(subtotal='3000',tax_amount='240',total='3140',adjustments=[{'description':'Discount','amount':'-100'}])
    assert normalize(inv).net_amount == Decimal('2900')
    assert inv.subtotal == Decimal('3000')
    assert all(f['passed'] for f in reconcile(inv,Decimal('.02')))


def test_live_pilot_requires_an_evaluated_dated_model(monkeypatch):
    from invoicedesk.infrastructure.preflight import require_evaluated_model
    monkeypatch.setenv('LLM_MODE','live')
    monkeypatch.setenv('LLM_PROVIDER','openai')
    monkeypatch.setenv('LLM_MODEL','')
    with pytest.raises(RuntimeError): require_evaluated_model()
    monkeypatch.setenv('LLM_MODEL','model-alias')
    monkeypatch.setenv('INVOICEDESK_EVALUATED_MODEL','model-alias')
    with pytest.raises(RuntimeError,match='dated'): require_evaluated_model()
    monkeypatch.setenv('LLM_MODEL','example-2026-01-01')
    monkeypatch.setenv('INVOICEDESK_EVALUATED_MODEL','example-2026-01-01')
    require_evaluated_model()  # syntax and equality only, never a claim about an evaluated/available model


def test_zero_gross_and_negative_tax_do_not_authorize_commitment(pilot):
    rec = recommendation(pilot,invoice(total='0',tax_amount='-3000'))
    assert rec.decision.outcome != 'AUTO_APPROVE'
    with pytest.raises(ApprovalBlocked):
        authorize(rec,Policy.load(),'Request exception',True)


def test_multiple_invoice_headings_require_interpretation(pilot):
    from invoicedesk.extract.tier1 import parse
    inv = parse('INVOICE\nINVOICE\nVendor: Northwind Office Supplies Ltd\nInvoice No: REPEAT-1\n'
                'Invoice date: 2026-11-01\nPO Number: PO-1001\nCurrency: USD\nSubtotal: 3000.00\nTax: 240.00\nTotal: 3240.00')
    rec = recommendation(pilot,inv)
    assert rec.decision.outcome == 'NEEDS_REVIEW'
    with pytest.raises(ApprovalBlocked): authorize(rec,Policy.load(),'Two pages reviewed',True)
