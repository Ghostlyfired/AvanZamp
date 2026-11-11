"""One-command demos must be repeatable and must not reset an operator's database."""
import json
import os
import shlex
import sqlite3
import subprocess
import shutil
import pytest

pytestmark = pytest.mark.skipif(not shutil.which('make') or os.name=='nt', reason='Make targets require a POSIX shell; CLI is tested separately.')

from invoicedesk.policy import ROOT


def demo_environment(tmp_path, monkeypatch):
    protected = tmp_path/'existing workspace.sqlite3'
    with sqlite3.connect(protected) as connection:
        connection.execute('CREATE TABLE operator_data(value TEXT)')
        connection.execute('INSERT INTO operator_data VALUES (?)', ('preserve existing work',))
    original = protected.read_bytes()
    envfile = tmp_path/'demo.env'
    # An incorrect dispatch cannot make a real provider request, even if the cache flag is lost.
    envfile.write_text('LLM_MODE=live\nLLM_PROVIDER=unused_test_provider\nLLM_MODEL=test-model\n'
                       'INVOICEDESK_DB='+shlex.quote(str(protected))+'\n')
    for variable in ('FILE', 'DB', 'DEMO_DB', 'DEMO_FILE', 'LLM_MODE', 'LLM_PROVIDER', 'LLM_MODEL'):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv('INVOICEDESK_ENV_FILE', str(envfile))
    monkeypatch.setenv('INVOICEDESK_DB', str(protected))
    return protected, original


def test_make_default_and_run_are_repeatable_without_resetting_configured_database(tmp_path, monkeypatch):
    protected, original = demo_environment(tmp_path, monkeypatch)
    demo = tmp_path/'demo workspace.sqlite3'
    for goal in ([], ['run'], ['demo']):
        result = subprocess.run(['make', *goal, 'DEMO_DB='+str(demo)], cwd=ROOT,
                                env=os.environ.copy(), capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert 'Decision: AUTO_APPROVE' in result.stdout
        assert 'Extraction mode: cache' in result.stderr
        assert 'parser=poppler matcher=development' in result.stdout
        with sqlite3.connect(demo) as connection:
            assert connection.execute('SELECT count(*) FROM runs').fetchone()[0] == 7
        assert protected.read_bytes() == original


def test_make_demo_all_resets_only_demo_and_uses_golden_order(tmp_path, monkeypatch):
    protected, original = demo_environment(tmp_path, monkeypatch)
    demo = tmp_path/'all samples.sqlite3'
    result = subprocess.run(['make', 'demo-all', 'DEMO_DB='+str(demo)], cwd=ROOT,
                            env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = [line.split() for line in result.stdout.splitlines() if '.pdf' in line]
    expected = json.loads((ROOT/'data/golden.json').read_text())
    assert lines == [[case['file'], case['expected_outcome']] for case in expected]
    assert protected.read_bytes() == original
