# Pilot implementation and operational acceptance

The product boundary remains vendor invoice → procurement checks → explained decision. The authenticated operator UI now shares the production application services. Local operation uses an isolated SQLite workspace; PostgreSQL/OIDC deployment acceptance remains separate. See [executed verification](VERIFICATION.md) for current results.

## Current contract

The newer [workflow amendment](WORKFLOW.md) takes precedence: clean invoices automatically post under the same locked approval invariants. The authenticated operator UI is now included in the production API. Historical recommendations are not posted retroactively. Alembic head is `0002_identifiers`.

## Historical pilot contract amendment

This explicitly supersedes the prototype ledger behavior in PROJECT_PLAN.md Sections 3, 8.5, and 10. `AUTO_APPROVE` is a recommendation, and consumes **zero balance** in the pilot. Human approval is the only ledger write. The four outcome labels, CheckResult/Decision/StageEvent fields, legacy `run_invoice` signature, and existing CLI JSON keys remain compatible through import/report adapters.

`ExtractedInvoice` gains an additive `field_evidence` array of `{path, snippet, page}` references. The new primary contracts separate `DocumentFacts`, `SectionValidation`, `InvoiceInterpretation`, `DecisionContext`, `Recommendation`, and immutable `ProcurementSnapshot`. The overloaded old invoice remains a report/provider compatibility type. Raw provider responses survive validation failures and corrections. Corrections append revisions; they cannot edit an approved ledger entry.

The explicit demo SQLite profile retains automatic recommendation posting to reproduce all 12 golden scenarios. Golden order comes from `data/golden.json`. Demo fixtures retain synthetic provenance and live extraction never reads them. Pilot PostgreSQL starts fresh; no prototype SQLite rows or old recordings are imported. A private copy of the existing prototype database and recordings was made under ignored `archives/prototype-before-pilot`; originals were preserved.

## Decision and approval design

Domain checks receive immutable JSON-backed procurement, prior-invoice, and Decimal ledger snapshots. Reports record the full policy, policy version, procurement revision, snapshot hash and contents, extraction revision, and rule version. Reconstruct a decision using its invoice interpretation, source-evidence results, recorded snapshot, policy, and matching/rule implementation version. Keep the corresponding application image with financial artifacts.

Recommendation processing does not reserve or consume money. Approval and procurement edits serialize through a single company row, appropriate to this workload. In one short transaction, approval verifies the report revision, reads current policy/vendors/POs/duplicates/balances, recomputes the recommendation, checks invariants, inserts the unique ledger row, updates review state, and appends audit/idempotency records. PostgreSQL enforces uniqueness by run and vendor/invoice key. No SQL money aggregation is used. PostgreSQL uses exact NUMERIC(24,6); the explicit SQLite test adapter uses decimal text.

Blocked vendors, closed POs, unresolved required facts, unknown/non-invoice document types, unsupported currencies, arithmetic/partial-parse failures, duplicate commitments, and rejected current recommendations block approval. A reviewer must record a reason for review exceptions. Source uncertainty requires explicit confirmation against the source and a reason. PO vendor mismatch cannot be overridden. Corrections preserve unrelated extraction warnings and are auditable revisions, never changes to original model output. A failed extraction must explicitly retry successfully before posting; correction alone cannot relabel it as a successful model response.

The original pure check functions remain available for stage rendering. Both CLI and API services use the same domain recommendation and review code. Legacy console progress renders StageEvents; it does not implement decision rules. The trusted durable CLI is `invoicedesk.cli pilot`.

## Queue and event behavior

PostgreSQL claims jobs with `FOR UPDATE SKIP LOCKED`. Each claim has a unique fencing token and a 90-second lease. A heartbeat extends it every 20 seconds. Stage events commit individually. Final artifacts, final event, and terminal job state commit atomically while the job row is locked and the lease is checked. A superseded worker cannot append or finish.

Expired workers route jobs to `retry_required`; explicit retry is required because a provider request may already have been billed. Provider calls have no automatic retry. At most three attempts are allowed. A completed partial report remains reviewable and can explicitly retry failed extraction. SSE reads committed events using `Last-Event-ID`/`after`; disconnecting never cancels a worker or holds a transaction open. API requests never start background processing themselves.

Routine production logs contain metrics/error categories, with no request/session ID fields or raw responses, document text, credentials, or file contents. Durable IDs and detailed facts live in protected database artifacts. Reports retain token counts, provider latency, queue/processing time, attempt counts, cache provenance, and error categories. Costs remain unknown without reviewed pricing inputs or billing exports.

## Extraction and evaluation

One fresh extraction call per accepted live document attempt, including parser-friendly and repeated uploads. Live recordings are retained for audit and never skip a provider request. The prompt has a versioned task, trust boundary, extraction rules, ambiguity handling, and output requirements. No examples, voting, RAG, or agent workflow were added without evaluation evidence. Model selection is deployment configuration: the worker requires `LLM_MODEL == INVOICEDESK_EVALUATED_MODEL` in live mode, and rejects moving aliases for OpenAI/Anthropic in favor of dated snapshots. Operators must supply an exact snapshot they actually evaluated; equality is a configuration check, not evidence of an evaluation.

The canonical Pydantic schema has provider adapters. Schema compatibility and nullable evidence have offline regression coverage. Explicit extraction results preserve raw facts, issues, evidence, usage, timing, and provenance. Cache keys include document SHA, preprocessing, provider, configured resolved model, prompt contents, schema, and inference settings. Uncertainty survives cache replay. A model-written scan snippet is always unverified; matching arithmetic does not authenticate it. Field/cell evidence supports page references when available.

Follow [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) and [evaluation guidance](https://developers.openai.com/api/docs/guides/evaluation-best-practices): structural conformance is separate from correctness. The release dataset must be human labeled and anonymized, with unfamiliar layouts, scans, bundled rows, inclusive tax, adjustments, missing fields, and confusing references. Keep development and held-out cases separate.

`evaluation/manifest.example.json` deliberately has **zero real cases and no reported performance**. Required per-case keys: `id`, `file`, `tags`, `fields`, `safe_to_recommend_approval`, and optional complete `line_items`. Required tags across the dataset: `unfamiliar_layout`, `scan`, `bundled_items`, `tax_inclusive`, `adjustments`, `missing_fields`, `confusing_references`. Optional per-case `snapshot` can provide labeled prior runs and balances for stateful cases. Put private documents/labels in `evaluation/private/`.

```sh
python -m invoicedesk.interfaces.evaluate evaluation/private/manifest.json \
  --vendors procurement/vendors.csv --pos procurement/purchase_orders.csv \
  --output evaluation/results/candidate.json
# Add --live only to opt in to billable requests with configured credentials/model pins.
# Add --baseline evaluation/results/baseline.json to compare the identical manifest.
```

The runner reports exact labeled-field accuracy, row completeness, incorrect AUTO_APPROVE recommendations, review rate, latency, token usage, and cost only when enough pricing data exists. An empty/undersized dataset or any incorrect AUTO_APPROVE cannot pass the extraction gate. This gate alone never sets `release_accepted=true`. Real holdout measurement and prompt/model comparisons were not executed here.

## Deployment runbook

Use a trusted operator shell. `.env.pilot` and private data never belong in source control. Containers have a read-only root filesystem, unprivileged UID, dropped capabilities, memory/CPU/PID limits, private persistent volumes, and bounded temporary storage. The worker validates actual PDF structure, page count (1–40), encryption, page dimensions, and embedded-image budgets before extraction. No pages are silently dropped. A 180-second worker deadline and provider timeouts bound processing.

1. Copy `.env.pilot.example` to `.env.pilot` with restrictive permissions and supply PostgreSQL/OIDC secrets and HTTPS origin. Register the exact callback `https://HOST/auth/callback`. Use stable OIDC subjects in the allowlist, with `uploader`, `reviewer`, and `admin` roles. Production has no development login bypass.
2. Run `docker compose build` and `docker compose up -d db`. Then run `docker compose run --rm migrate` to apply Alembic migrations.
3. Initialize a **fresh** database using reviewed company procurement CSVs/policy, mounted read-only into the container:

   ```sh
   docker compose run --rm -v "$PWD/procurement:/input:ro" api \
     python -m invoicedesk.interfaces.operations initialize \
     --vendors /input/vendors.csv --pos /input/purchase_orders.csv --policy /input/policy.yaml
   ```

4. Run `docker compose up -d api worker proxy`. API health is `/health/live` and `/health/ready`; worker health checks its heartbeat. Verify TLS, OIDC state/nonce validation, callback, allowlist denial, role restrictions, CSRF, upload limits, protected downloads, event replay, and reviews before onboarding operators.
5. Use a distinct migration owner and runtime database role. Grant runtime SELECT/INSERT on immutable financial tables and UPDATE on job/master/session tables; no runtime DDL or trigger control. The first migration installs update/delete rejection triggers for artifacts, ledger, events, audit, and idempotency records. The Compose development defaults use one database user; separate credentials/privileges are an operational release gate.
6. Set evaluated live-model configuration only after release evaluation. Worker/API share the image; scale workers conservatively after concurrency/load testing. The proxy is the only publicly exposed service. Keep the API off untrusted networks, because forwarded proxy headers are trusted inside the Compose network.

The candidate dependency lock is pinned, including runtime transitive dependencies. It could not be installed or resolved in this environment; clean-install `pip check`, supply-chain review, and container build must pass before use. Update locks in a network-enabled development environment, rerun all tests/evaluations, and keep the resulting image digest. Image tags in Compose/Dockerfile must be pinned to approved digests in the deployed environment.

## Backup, restore, and retention

Stop API/worker writes for a consistent database/document-volume backup. Keep secrets separate and encrypt backups under your deployment's access controls.

```sh
docker compose stop api worker
mkdir -p private/backups
chmod 700 private/backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > private/backups/pilot.dump
docker compose run --rm --user 0:0 -v "$PWD/private/backups:/backup" --entrypoint tar api \
  -czf /backup/documents.tgz -C /private documents extraction-cache
docker compose start api worker
```

Restore into an isolated database and new private volumes, **never over the running company**:

```sh
# Target .env.pilot must point to an isolated restore database/Compose project.
docker compose stop api worker
docker compose exec -T db sh -c 'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < private/backups/pilot.dump
docker compose run --rm --user 0:0 -v "$PWD/private/backups:/backup:ro" --entrypoint tar api \
  -xzf /backup/documents.tgz -C /private
```

Verify Alembic head, row counts, artifact hashes, exact ledger balances, document SHA integrity, event replay, and a fresh approval/retry before routing traffic. Run the restore verifier below; record the restore time and recovered backup timestamp. A successful backup command is not a restore test. No production restore was executed here.

```sh
python -m invoicedesk.interfaces.restore_check
```

Schedule retention with deployment-configured `DOCUMENT_RETENTION_DAYS`. `operations retention` is a count-only dry run; `--apply` removes expired private documents/cache recordings and expired server sessions, appending an audit event. Shared document hashes use the newest submission date; queued/running jobs retain their documents. Financial artifacts/audit/ledger remain append-only for reconstruction; define their archival retention separately with the pilot company. Expired document downloads return HTTP 410. Never delete financial tables through application maintenance.

OIDC uses Authlib's authorization-code/PKCE integration and database-held opaque sessions with Secure/HttpOnly/SameSite cookies. CSRF requires both the session token and matching Origin. Only the document owner or reviewer/admin can read job artifacts/documents/events; only reviewers/admins can review. Live identity-provider acceptance is still required.

## Verification status and release gates

Offline prototype and new domain/SQLite/security tests run without model calls; integration modules explicitly skip when their dependencies are missing. Local PostgreSQL integration tests require a dedicated loopback test database through `INVOICEDESK_TEST_POSTGRES_URL` and create/drop only isolated test schemas. CI installs all dependencies, runs that suite, checks migrations, lint, and selected type contracts. The source uses PostgreSQL's [queue locking semantics](https://www.postgresql.org/docs/current/sql-select.html) and [Authlib OIDC integration](https://docs.authlib.org/en/v1.5.2/client/starlette.html).

Outstanding gates, without implied acceptance:

- Clean pinned-dependency install, lint/type jobs, container build, and migrations on PostgreSQL.
- PostgreSQL concurrency, actual worker-crash/recovery, SSE disconnect/replay, resource/load tests and observed worker health.
- Live OIDC/TLS/session validation and least-privilege database credentials.
- Verified backup restoration, scheduled retention, monitoring and operational ownership.
- Human-labeled held-out real-document results with no incorrect AUTO_APPROVE recommendations; no synthetic/API provenance substitution.
- Later operator UI and end-to-end operator acceptance.

Current verification installed the Python 3.12 dependency stack and executed offline, browser, SQLite migration and live model evaluations. No running PostgreSQL/Docker service or live deployment identity provider was available. See [executed verification](VERIFICATION.md) for results and remaining gates.
