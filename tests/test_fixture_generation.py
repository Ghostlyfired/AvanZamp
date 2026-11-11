"""Restoring synthetic demo extraction fixtures must preserve source document bytes."""
import hashlib
import json
import shutil

from scripts import generate_test_data as generator


def test_restore_offline_fixtures_preserves_documents_and_oracle(tmp_path,monkeypatch):
    source_root = generator.ROOT
    documents = tmp_path/'data/invoices'
    documents.mkdir(parents=True)
    for spec in generator.SPECS:
        if spec[8] in {'C','S'} or spec[2] is None:
            shutil.copyfile(source_root/'data/invoices'/(spec[0]+'.pdf'),documents/(spec[0]+'.pdf'))
    oracle = tmp_path/'data/golden.json'
    oracle.write_text('independent oracle sentinel',encoding='utf-8')
    before = {path:hashlib.sha256(path.read_bytes()).hexdigest() for path in (tmp_path/'data').rglob('*') if path.is_file()}
    monkeypatch.setattr(generator,'ROOT',tmp_path)
    fixtures = generator.generate_fixtures()
    assert len(fixtures)==3
    assert {path:hashlib.sha256(path.read_bytes()).hexdigest() for path in before}==before
    for path in fixtures:
        fixture = json.loads(path.read_text(encoding='utf-8'))
        assert fixture['provenance']=='synthetic_fixture'
        assert 'NOT an API recording' in fixture['source']
        assert path.stem in before.values()
    # Rebuilding is deterministic and never changes a document's hash.
    first = {path:path.read_bytes() for path in fixtures}
    generator.generate_fixtures()
    assert {path:path.read_bytes() for path in fixtures}==first
