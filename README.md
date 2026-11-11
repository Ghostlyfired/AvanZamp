# InvoiceDesk

For a resettable, password-protected hackathon link on one Render Free service, follow [the Render demo deployment guide](docs/HACKATHON_RENDER.md). The demo starts with sample vendors/POs and separate, empty invoice history.

InvoiceDesk extracts invoice facts with a fresh LLM request, validates them against authorized purchase orders, and automatically posts invoices that pass all mandatory checks. Other invoices retain a review report with the verdict, justification, source evidence, and next action. The model extracts facts; deterministic backend rules own decisions and ledger writes. No payments are executed.

## Run locally

Python 3.12 is the supported runtime. On Windows:

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.lock
.venv/Scripts/python.exe -m invoicedesk.interfaces.local_api
```

The provided workspace already has a Python 3.12 environment. Open **http://127.0.0.1:8000**. Configure the provider, model and credentials in the ignored `.env`; never commit credentials. On POSIX use `.venv/bin/python` and the existing Make targets.

The local UI uses a private SQLite workspace and one trusted local operator. The authenticated production UI uses the same presentation with PostgreSQL, OIDC roles, CSRF protection and a separate durable worker. Local mode binds only to loopback.

The local database created for this task contains sample vendors/POs plus the explicitly synthetic final-test procurement baseline. This baseline is suitable for exercising the supplied PDFs; it is not evidence of actual purchasing authorization. Production initialization never imports these test assumptions automatically.

## Workflow

- Vendor dialogs reserve sequential `V-001` identifiers; custom identifiers remain editable.
- Every accepted live upload makes a fresh provider request, including repeated documents. Audit recordings never substitute for live extraction. Invalid or unsafe documents are rejected before provider access.
- Critical identities, currency, financial cells, arithmetic, PO scope, charges and cumulative quantities are checked independently. Optional metadata errors remain visible without unnecessarily blocking an otherwise valid invoice.
- Explicit PO references take precedence. Missing references require substantive content agreement and an unambiguous candidate.
- Clean invoices post once under a transaction lock as actor `system`. Pending/rejected invoices consume no balance. Human approval uses the same financial invariants.
- PO progress measures accepted billing, not physical receipt. Extra delivery charges require explicit authorization even when the PO has enough remaining money.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m invoicedesk.interfaces.final_verify --output private/final-verification/run --workers 4
```

The first command is offline. The second uses the configured real provider and makes billable calls for all final PDFs in isolated workspaces. The four positive layouts repeat invoice identities, so independent cases are deliberate; submitting them together still invokes duplicate protection. Folder names and expected verdicts are never production decision inputs.

To create only the synthetic evaluation master, without processing invoices:

```powershell
.venv/Scripts/python.exe -m invoicedesk.interfaces.final_verify --bootstrap-only private/evaluation-master.sqlite3
```

See [executed verification](docs/VERIFICATION.md), [workflow contracts](docs/WORKFLOW.md), [local UI](docs/LOCAL_UI.md), and [production deployment](docs/PILOT.md). Production release additionally requires real OIDC/TLS acceptance, PostgreSQL concurrency/migration/restore checks, approved procurement data, and deployment ownership. Those external checks must not be inferred from offline tests.
