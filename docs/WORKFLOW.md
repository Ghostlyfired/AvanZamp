# Current workflow contract

This amendment supersedes the historical parser-first extraction and human-only posting rules in PROJECT_PLAN.md and earlier pilot notes. The legacy outcome enums, StageEvent and compatibility imports remain available.

## Extraction and decisions

Live mode invokes the configured provider for every accepted document attempt. Deterministic parsing is independent evidence, never a live extraction bypass. Stored responses are audit artifacts; explicit offline cache/demo mode remains separate. The model is instructed to report printed facts and nulls, preserve item rows and signed adjustments, distinguish seller/buyer and invoice/PO identifiers, and ignore instructions embedded in documents. The prompt and schema are fingerprinted and versioned.

Seller identity, invoice number/date, currency, monetary totals, financial item cells, printed PO reference and adjustments require source support. Optional party addresses, contact details, due dates, supplier labels and units do not determine approval. True invoice/PO identifier conflicts remain reviewable even when both values appear somewhere in the source. Missing currency is never silently interpreted as USD. Scans may use independent Tesseract OCR; without reliable independent evidence they require human review.

Source correctness, extraction completeness, arithmetic, procurement scope and final authorization remain distinct checks. The default rounding allowance is 0.02. Aggregate overbilling bands remain 2% automatic tolerance / 5% review boundary, but do not excuse unsupported items, changed prices, excess ordered quantities or unapproved charges. Missing critical facts produce HOLD_MISSING_INFO; extraction/source uncertainty and material scope/tax problems produce NEEDS_REVIEW; blocked vendors, duplicate invoice identities, closed POs and material overbilling can produce REJECT. Existing precedence is preserved.

POs may include ordered `line_items` (description, optional part_number, quantity, unit_price, amount) and `charge_allowances` (description, maximum amount per invoice). Existing summary POs remain supported and reports disclose when item-level validation is unavailable. Current and previously approved invoice quantities/amounts are checked together. Submitted/rejected invoices are not fulfillment.

Absent PO references use content/amount/recency weights 0.7/0.2/0.1, minimum content similarity 0.8, minimum score 0.8 and winning margin 0.2. Explicit unknown references do not silently fall back to another PO.

New stable checks: `po_scope_consistency`, `po_charge_consistency`, `po_tax_consistency`; each material discrepancy requires review. Rules are versioned as invoice-rules-6; policy defaults are invoice-policy-2.

Rejected, unposted submissions remain in history but no longer reserve their invoice identity for exact/near duplicate checks. Pending submissions and all ledger commitments continue to block duplicate payment. Rejecting a mistaken upload permits a corrected replacement to be reviewed; rejected reports remain terminal and cannot be reopened. PostgreSQL snapshots take the actual review state from the job, with ledger commitments always treated as approved. SQLite review operations likewise prohibit rejecting an approved posting. No existing invoice is automatically approved by this change.

## Posting and concurrency

AUTO_APPROVE now authorizes automatic posting only when all mandatory backend checks pass. Workers reload current policy and procurement under the ledger transaction lock, recompute arithmetic and checks, and write the final report, automatic approval artifact, ledger row, audit event and terminal production job state together. Automatic approval cannot exercise human exception powers. Actor `system`, reason, policy/snapshot and report revision identify its provenance.

Manual approval retains reason/source-confirmation requirements and current-balance reevaluation. Unique job and vendor/invoice commitments plus transactional locking prevent repeated posting. Existing historical recommendations are never automatically posted by migration. No model call occurs while a financial transaction lock is held. Financial history remains append-only in production.

A human reviewer may resolve a missing source citation for an extracted adjustment only with a non-empty exception reason and explicit source confirmation. The reviewer must check the printed invoice. Failed arithmetic, other extraction warnings, missing critical facts, duplicate identities, vendor/PO conflicts, and current procurement failures remain blocking. Automatic posting never uses this manual exception.

Vendor identifiers are allocated under the same serialized/idempotent command mechanism as administration. A durable high-water mark considers existing V001 and V-001 styles and custom numeric identifiers. Dialog reservations may leave gaps; deleted IDs are not reused. Custom IDs still require uniqueness.

## Persistence and presentation

PostgreSQL remains the production store. Alembic head 0002_identifiers adds the identifier sequence; PO details are additive master JSON. SQLite adds decimal-text PO detail columns without discarding records. Money remains Decimal in application code; SQLite never sums money.

The authenticated production application exposes both existing /v1 contracts and the UI-compatible /api/v1 surface. UI requests enforce OIDC identity, role permissions, CSRF for mutations, document ownership, revisions and idempotency. The local loopback adapter uses an isolated SQLite workspace. The UI renders structured facts and reasons as text/components; raw JSON belongs in the Developer view.

## Test master assumptions

The supplied final PDFs lack independent authorized PO records. The evaluation manifest therefore defines an explicitly synthetic Apple procurement baseline, including alias assumptions, four ordered products, USD and 12% tax, and no delivery allowance. It is used only for evaluation/local demonstration, and production code never reads expected outcomes or filenames. A real purchasing compliance claim requires independently approved master data. The positive layout variants share invoice numbers and must be evaluated independently; sequence tests separately verify duplicate and cumulative behavior.
