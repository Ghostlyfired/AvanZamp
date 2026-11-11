"""Read-only verification for an isolated restored pilot database and document volume."""
import hashlib
import json
import os
from pathlib import Path
import sqlalchemy as sa
from invoicedesk.domain.contracts import fingerprint
from invoicedesk.infrastructure.repository import Repository
from invoicedesk.infrastructure.storage import LocalStorage
from invoicedesk.infrastructure import schema as s


def main():
    repo = Repository(os.environ['DATABASE_URL'])
    storage = LocalStorage(Path(os.environ['DOCUMENT_ROOT']))
    checked, missing, errors = 0, 0, []
    with repo.engine.begin() as c:
        repo.lock(c)
        heads = list(c.execute(sa.text('SELECT version_num FROM alembic_version')).scalars())
        if heads != ['0002_identifiers']:
            errors.append('Unexpected migration head.')
        for row in c.execute(sa.select(s.artifacts)).mappings():
            if fingerprint(row['data']) != row['sha256']:
                errors.append('Artifact digest mismatch.')
            checked += 1
        for row in c.execute(sa.select(s.documents)).mappings():
            path = storage.path(row['sha256'])
            if not path.exists():
                missing += 1
            elif hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
                errors.append('Document digest mismatch.')
        for entry in c.execute(sa.select(s.ledger)).mappings():
            job = repo.job(c, entry['job_id'], entry['actor'], {'admin'})
            if job['review_state'] != 'approved' or entry['amount'] <= 0:
                errors.append('Ledger/review-state mismatch.')
            recheck = c.execute(sa.select(s.artifacts.c.data).where(s.artifacts.c.job_id == entry['job_id'],
                s.artifacts.c.revision == entry['revision'], s.artifacts.c.kind == 'approval_recheck')).scalar_one_or_none()
            if not recheck or recheck['context'] != entry['context']:
                errors.append('Missing approval provenance.')
        snapshot = repo.snapshot(c)
        balances = dict(snapshot.balances)
    result = dict(artifacts_checked=checked, missing_documents=missing, balances=balances,
                  errors=errors, release_accepted=False)
    print(json.dumps(result,indent=2))
    if errors or missing:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
