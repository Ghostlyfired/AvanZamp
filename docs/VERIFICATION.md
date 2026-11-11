# Executed verification ? 2026-11-11

Current workflow and contract amendments: [WORKFLOW.md](WORKFLOW.md).

| Check | Observed result |
| --- | --- |
| Full offline suite, Python 3.12.16 on Windows | **239 passed, 6 skipped**, 44.89 seconds |
| Live final document evaluation | **21/21 passed**, each with one fresh successful configured OpenAI request and no extraction cache hit |
| Positive final layouts | **4/4 AUTO_APPROVE**; each isolated workspace committed exactly USD 3,196.00 to PO-1241 with actor system |
| Negative/problematic final documents | **17/17 NEEDS_REVIEW or REJECT**; all corresponding ledgers remained empty |
| Automatic posting and concurrency | Local and durable job-service tests cover partial invoices, duplicate prevention, competing approvals and current-policy reevaluation |
| Browser acceptance | Real headless Chrome verifies editable generated IDs, readable labels/errors, structured values, escaping, review reasons/actions, polling and PO progress |
| Authenticated UI adapter | Offline HTTP tests cover sessions, CSRF, roles, ownership, procurement CRUD, document access and report presentation |
| Vendor identifiers | Concurrent/idempotent reservations, custom IDs, deletion high-water marks and old SQLite data preservation pass |
| Golden demo | Original twelve outcomes preserved; missing hash-paired synthetic fixtures restored without modifying PDF bytes |
| Lint | Ruff E9/F63/F7/F82 passes |
| Type contracts | Mypy passes domain contracts/master and interface security |
| Dependency consistency | 59 installed packages pass uv pip check; runtime/dev pins installed in Python 3.12 environment |
| Migration smoke | Fresh SQLite Alembic upgrade reaches 0002_identifiers; alembic check detects no pending operations |
| Local runtime | Server starts successfully on Windows; invoice page, config, vendors and purchase-order endpoints return HTTP 200 |
| Whitespace | git diff --check passes |

The six skips are two PostgreSQL integration cases (no dedicated server configured), three POSIX Make integration cases (Windows host), and one Windows symlink-permission case. Other storage/privacy assertions, CLI and database tests run normally. Offline tests block external network access, including direct loopback connections; the Windows asyncio socketpair exemption is limited to its internal wakeup pair.

## Live evaluation evidence

Private combined evidence: `private/final-verification/final-report.json`. Original live source reports are under `live-positive-1/` and `live-negative-1/`; source documents, provider results, audit provenance and database postings remain private. The combined report distinguishes genuine fresh extraction from deterministic revalidation under the final numeric PO-reference normalization rule; revalidation performs no additional provider calls. An earlier interrupted sequential evaluation is retained separately and is not counted as a successful complete run.

The final-test master is explicitly synthetic: the supplied invoice layouts define test assumptions about Apple products, supplier aliases, USD and 12% tax. Independent procurement authorization was unavailable. Thus these results demonstrate extraction/workflow behavior against that baseline, not compliance with real approved POs. The positive layouts repeat invoice identities and were evaluated independently; separate sequence tests enforce duplicate and cumulative billing rules. Production code never reads case filenames, folder labels or expected verdicts.

## Remaining production release checks

No PostgreSQL server or Docker runtime is available on this host. PostgreSQL migration/concurrency/restore checks, container builds, live OIDC/TLS acceptance, deployment credentials, approved production procurement data and operational ownership remain unverified. SQLite adapter and session tests do not substitute for those checks. Production startup requires a configured evaluated dated model snapshot; local live evaluation used the configured model alias and does not certify an arbitrary deployment snapshot. `release_accepted=false` remains truthful.

The included deployment template and runbook support these checks. Production financial history and test databases are separate; no historical recommendations are automatically posted by migration.
