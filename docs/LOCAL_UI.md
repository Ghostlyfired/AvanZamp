# Operator UI

The current contracts are in [WORKFLOW.md](WORKFLOW.md). The frontend is shared between the loopback local application and the authenticated production API; both use backend decisions rather than recomputing rules in JavaScript.

## Local startup

Use Python 3.12. On Windows run `.venv/Scripts/python.exe -m invoicedesk.interfaces.local_api`; on POSIX run `make ui`. Open http://127.0.0.1:8000. Configure the real OpenAI or Anthropic provider, credentials and model in `.env`. Every accepted upload makes a fresh model request. There is no live cache shortcut or synthetic provider switch in the UI.

On first launch, the configured SQLite source `INVOICEDESK_DB` is copied into `private/ui/workspace.sqlite3`. Existing workspaces are preserved on subsequent launches. If the source is absent, sample procurement CSVs are loaded; no invoices are processed automatically. The task's initialized local source additionally includes the documented synthetic final-test reference PO. Databases, uploads, audit recordings and backups remain ignored by Git.

The local workspace supports Windows and POSIX process locks and refuses a second worker. It binds only to loopback and represents one trusted operator. It is not a public deployment configuration.

## Processing and review

Upload queues a durable job; leaving a browser view does not cancel it. Invoice sections, validation findings, verdict, reasons and corrective actions remain available in the expanded view. Provider failures preserve partial data and require explicit retry. Critical source evidence, arithmetic, procurement scope, charges, currency and cumulative billing are independent checks.

Clean invoices become Approved and post exactly once with actor system. Review/missing/rejected recommendations consume no balance. Human approval rechecks current procurement and source confirmation; corrections and review history remain auditable. Reprocessing an already approved invoice is prohibited. Independently uploaded duplicates still make a model request and cannot post again.

POs expose accepted billing, pending matched amounts, remaining net amount, overage, invoice count and billing fulfillment percentage. This is not a goods-receipt system. Optional PO item rows enable quantity/price checks across partial invoices. Charge allowances authorize a maximum charge per invoice; they do not increase the overall authorized PO net amount.

Vendor IDs are prepopulated from a durable monotonic allocator and remain editable. Cancelled dialogs leave harmless gaps. Existing or referenced vendors/POs should be blocked/closed rather than deleted. New PO dialogs support structured ordered items and separately authorized charges without JSON entry.

Section labels, money and dates have dedicated renderers. Nested values appear as readable components, model text is escaped, and raw JSON appears only in Developer. Polling observes complete displayed report content, including rechecks that leave a revision/event count unchanged.

## Explicit local reset

Stop the local server before running `.venv/Scripts/python.exe -m invoicedesk.interfaces.local_api --clean-invoices` (POSIX: `make ui-clean`). It backs up the local database, clears invoice records/postings and preserves procurement data. Source databases are unchanged. Production financial history has no equivalent browser deletion endpoint.

## Production

Run the PostgreSQL/OIDC API and separate worker using [PILOT.md](PILOT.md). The production application serves the same operator interface and protects its `/api/v1` presentation endpoints with session identity, role checks, CSRF, revisions and idempotency. Existing `/v1` API clients remain supported. Anonymous HTML navigation redirects to OIDC login; anonymous data requests fail authentication. Only owners or authorized reviewers can read invoice details/documents; procurement mutations require administrators.

## Final document verification

`python -m invoicedesk.interfaces.final_verify --workers 4 --output private/final-verification/new-run` makes real provider calls in disposable workspaces against the explicit evaluation manifest. Positive layouts repeat invoice identities and therefore run independently. Use separate sequence tests to check partial billing/duplicate enforcement. Test-only reference data and provider success do not prove actual purchasing authorization; production procurement must be independently approved.
