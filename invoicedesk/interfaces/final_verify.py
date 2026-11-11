"""Fresh live evaluation against explicit expectations in isolated workspaces."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from invoicedesk.config import ROOT, load_environment
from invoicedesk.db import Db
from invoicedesk.seed import reset
from invoicedesk.interfaces.local_workspace import Workspace


def bootstrap_test_master(path: Path, manifest: dict) -> None:
    """Create a new synthetic evaluation master, never alter an existing database."""
    if path.exists():
        raise ValueError('Choose a new master database path; existing data is preserved.')
    db = Db(path)
    try:
        reset(db)
        with db.conn:
            db.conn.execute('CREATE TABLE evaluation_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.conn.execute('INSERT INTO evaluation_metadata VALUES (?, ?)',
                            ('provenance', json.dumps(dict(kind='synthetic_evaluation_master',
                                procurement_authorization_verified=False,
                                assumptions=manifest.get('assumptions', []),
                                sources=[dict(file=c['file'], sha256=c['sha256']) for c in manifest.get('cases', [])]))))
            for table in ('vendors', 'purchase_orders'):
                for row in manifest['master'][table]:
                    columns = ','.join(row)
                    values = ','.join('?' for _ in row)
                    db.conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({values})', tuple(row.values()))
    finally:
        db.close()


def verify(manifest: dict, folder: Path, output: Path, master: Path | None = None, *, workers: int = 1) -> dict:
    if not 1 <= workers <= 4:
        raise ValueError('Evaluation workers must be between one and four.')
    if not manifest.get('cases'):
        raise ValueError('The evaluation manifest must contain at least one case.')
    if output.exists():
        raise ValueError('Choose a new output directory to preserve prior evaluation evidence.')
    # Validate every source before making chargeable provider calls.
    for case in manifest['cases']:
        path = (folder/case['file']).resolve()
        if not path.is_relative_to(folder.resolve()):
            raise ValueError('Case path escapes the input directory.')
        if hashlib.sha256(path.read_bytes()).hexdigest() != case['sha256']:
            raise ValueError('Source hash changed: '+case['file'])
    output.mkdir(parents=True)
    if master is None:
        master = output/'synthetic-procurement.sqlite3'
        bootstrap_test_master(master, manifest)
    elif not master.is_file():
        raise ValueError('The requested procurement database does not exist.')
    results = []
    summary = dict(mode='fresh_live_independent_cases', synthetic_master=master == output/'synthetic-procurement.sqlite3',
                   assumptions=manifest.get('assumptions', []), release_accepted=False, results=results)
    def run_case(index, case):
        path = folder/case['file']
        workspace = Workspace(output/f'case-{index:02d}', master)
        # A provided source may contain historical invoices. Acceptance cases are
        # isolated against its procurement data, without prior invoice postings.
        with workspace.db() as db, db.conn:
            for table in ('review_commands', 'pilot_ledger', 'run_context', 'review_audit', 'events', 'runs',
                          'ui_jobs', 'ui_events', 'ui_artifacts'):
                db.conn.execute(f'DELETE FROM {table}')
        job = workspace.submit(path.name, path.read_bytes(), 'acceptance-'+case['sha256'])
        assert workspace.claim() == job['id']
        workspace.process(job['id'])
        detail = workspace.detail(job['id'])
        report = detail.get('report') or {}
        provider = workspace.developer(job['id'])['artifacts'].get('provider', {})
        actual = report.get('decision', {}).get('outcome')
        fields = report.get('extraction', {})
        mismatches = []
        from decimal import Decimal
        for name, expected in case.get('fields', {}).items():
            value = report.get('normalized', {}).get('currency') if name == 'currency' else fields.get(name)
            equal = value is not None and (Decimal(str(value)) == Decimal(expected) if name in {'total','subtotal','tax_amount'} else value == expected)
            if not equal:
                mismatches.append(name)
        calls = provider.get('token_usage', {}).get('api_calls', 0)
        # Older reports may expose the count only through persisted run metrics.
        if not calls:
            with workspace.db() as db:
                rows = db.rows('SELECT llm_used FROM runs WHERE run_id=?', (detail.get('run_id'),))
                calls = rows[0]['llm_used'] if rows else 0
        extraction_succeeded = bool(detail.get('state') == 'completed' and not detail.get('error')
                                    and provider.get('provenance') in {'api_recording', 'api_recording_partial'})
        passed = bool(actual in case['allowed_outcomes'] and not mismatches and calls >= 1
                      and not provider.get('cache_hit') and extraction_succeeded)
        result = dict(file=case['file'], sha256=case['sha256'], passed=passed, outcome=actual, allowed_outcomes=case['allowed_outcomes'],
                      api_calls=calls, field_mismatches=mismatches, error=detail.get('error'), report=report,
                      extraction_succeeded=extraction_succeeded,
                      provider={key: value for key, value in provider.items() if key != 'raw_response'})
        print(f'{case["file"]}: {actual}; {"PASS" if passed else "FAIL"}; live calls={calls}', flush=True)
        return result
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(run_case, index, case) for index, case in enumerate(manifest['cases'], 1)]
        for future in as_completed(futures):
            results.append(future.result())
            results.sort(key=lambda result: result['file'])
            summary.update(finished=len(results), total=len(manifest['cases']), passed=sum(r['passed'] for r in results))
            (output/'report.json').write_text(json.dumps(summary, indent=2)+'\n')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT/'evaluation/final_tests.json')
    parser.add_argument('--folder', type=Path, default=ROOT/'data/final_tests')
    parser.add_argument('--master', type=Path, help='Procurement database copied into disposable workspaces.')
    parser.add_argument('--bootstrap-only', type=Path, help='Create the explicitly synthetic test master and exit; no LLM calls.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--workers', type=int, default=1, help='Independent live cases in parallel (1 to 4).')
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    if args.bootstrap_only:
        bootstrap_test_master(args.bootstrap_only, manifest)
        print('Synthetic evaluation master created: '+str(args.bootstrap_only))
        return 0
    load_environment()
    os.environ['LLM_MODE'] = 'live'
    output = args.output or ROOT/'private/final-verification'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6])
    result = verify(manifest, args.folder, output, args.master, workers=args.workers)
    print(f'{result["passed"]}/{result["total"]} cases passed. Report: {output/"report.json"}')
    return 0 if result['passed'] == result['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
