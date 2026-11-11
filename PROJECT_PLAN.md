# InvoiceDesk — Project Plan (PS-1: Invoice processing, PDF → decision)

> **Current workflow amendment:** [docs/WORKFLOW.md](docs/WORKFLOW.md) supersedes historical extraction and posting rules below. Live accepted uploads always call the provider; deterministic checks can automatically approve and post clean invoices. This original candidate plan is retained as historical context.

> Self-contained build plan for a 24-hour take-home case study. It is written so that it can be handed to AI coding agents, whole or in pieces. Sections 0–5 are shared context; Section 12 splits the build into work packages that can run in parallel.

---

## 0. How to use this document (for AI agents)

- **Giving a work package to an agent:** paste Sections 0, 5 (contracts) and the relevant WP from Section 12, plus any data/spec section the WP references.
- **Do not change contracts** (Section 5) without flagging it. Every module depends on them.
- **Conventions:** Python 3.11+, type hints everywhere, pydantic v2 models, `Decimal` for money (never float), `pathlib`, no global state except the DB path, no network calls in tests.
- **Determinism first:** decisions come from plain Python rules. The LLM is used only where listed in Section 7.
- **Everything the UI shows comes from `StageEvent`s and DB rows.** The UI never recomputes logic.
- **Build order:** CLI pipeline first, UI last. A pipeline that passes the golden tests is a submittable product.

---

## 1. Context and goal

**Situation.** A candidate for an "AI Solutions Analyst" role at Zamp (per the candidate guide, the company works on hard operational problems such as invoice processing, vendor onboarding and GTM execution) has a take-home case study. They must pick one of three problems, build a working automated process that runs on real inputs, and submit (a) a live, runnable link and (b) a 5-minute demo video. If it advances, they demo it live in an interview ("no slides, the process runs in real time").

**Chosen problem: PS-1, Invoice processing.** A mid-size company receives hundreds of vendor invoices per month as emailed PDFs. An AP clerk opens each, finds the matching PO in a spreadsheet, checks the numbers, and decides what to do. Invoices are messy: different layouts, some scanned, line items itemised or bundled, tax embedded or separate, PO references explicit or implied, sometimes critical fields missing. The procurement side has approved vendors, PO amounts, tolerance thresholds and duplicate-detection rules. Matching is not always obvious (split POs, near-but-not-exact amounts). The process must **make a decision and explain it**.

**The brief, paraphrased:** take an invoice as input; produce a clear, reasoned decision as output with everything in between visible; the candidate decides what to extract, how to validate, which rules matter, and what the output looks like. Design and build **2–4 non-trivial edge cases** where behaviour must differ from the happy path.

**What the evaluators say they grade:**
1. Does it actually run on real inputs (not a mockup)?
2. Quality of judgment behind design choices.
3. Can it be explained to a non-technical buyer?
4. The UI is graded: an intuitive interface with a **live run view** (each stage as it executes) and a **dashboard** (history, status, outputs across runs). (The FAQ says a console log would also be acceptable, so treat the UI as important but keep it simple.)
5. Deliberate use of AI tools, with the candidate able to explain why each tool is there.
6. Edge cases handled deliberately, not ignored. Quality over quantity: "3 scenarios done well beats 10 half-done."

**Hard constraints from the brief:** any stack is allowed except Zamp/Pace products. The process must run live in the interview. Candidates create their own test inputs (a few realistic invoice PDFs and a PO dataset). Ambiguities are resolved by making and documenting assumptions.

---

## 2. Product concept: what makes this more than an "AI wrapper"

**Working name:** InvoiceDesk. **One-line pitch:** *"An AP decision engine that reads any invoice, but decides like an accountant, with rules you can read, a ledger that remembers, and a reason for every outcome."*

### Design principles (these are also the interview talking points)

1. **The LLM never makes the decision.** It is an extraction tool of last resort. Matching, tolerances, duplicate detection and the final outcome are deterministic Python driven by a human-readable `policy.yaml`. Auditors can read the rules; the same invoice always gets the same decision.
2. **Cheapest sufficient extraction, with escalation.** Tier 1 is a deterministic parser (`pdfplumber` + label/regex). The LLM is called only when the PDF has no text layer, required fields are missing, or the arithmetic does not reconcile. The dashboard shows which path each invoice took and what it cost.
3. **Extraction is verified, not trusted.** Every extraction is checked by (a) arithmetic reconciliation (line items → subtotal, subtotal + tax → total) and (b) grounding (each LLM-extracted field must carry an evidence snippet that is actually found in the PDF's text layer). Fields from vision-only scans are labelled `UNVERIFIED` and push the decision towards human review.
4. **"Missing" is a first-class result.** The LLM is told to return `null` rather than guess. If both tiers find nothing, the invoice is genuinely missing that field. The process holds it and drafts a vendor email, rather than inventing a value.
5. **The system has memory.** A PO ledger tracks cumulative billing against split POs. Duplicate detection is stateful and tuned so that legitimate milestone invoices are not flagged while renumbered resubmissions are.
6. **Layered uncertainty is handled explicitly.** When two weak signals stack (scanned/unverified extraction plus an inferred PO), the system refuses to auto-approve even if every individual check passes, and gives the reviewer a recommended action.
7. **Every decision is traceable.** Each outcome lists the exact checks (with evidence) that drove it. The LLM may paraphrase reasons but cannot add facts or change outcomes.
8. **Graceful degradation.** If the LLM is unavailable (no key, timeout), the pipeline does not crash. It routes to NEEDS_REVIEW with "extraction incomplete" and carries on.
9. **Human in the loop where it matters.** A review queue shows the system's recommendation. The reviewer's action updates the ledger and is audit-logged.

---

## 3. Scope and assumptions

### MoSCoW

| Priority | Item |
|---|---|
| **Must** | Tier 1 deterministic extraction + Tier 2 LLM fallback; reconciliation; vendor / duplicate / PO-match / amount checks; PO ledger; decision engine with 4 outcomes and reasons; live run view; dashboard with history; 4 demo edge cases; seed/reset; golden regression tests passing; deployed live link |
| **Should** | Grounding check; review queue with approve/reject actions; drafted vendor/internal comms; policy page (read-only); extraction-path chips and per-run cost/time |
| **Could** | LLM-written plain-English summary (facts-only prompt, template fallback); PDF side-by-side with extracted fields; CSV export |
| **Won't (state as "next steps")** | Email ingestion, auth/roles, 3-way match (goods receipt), real ERP integration, multi-currency FX, learning from reviewer overrides |

### Assumptions to document (the brief asks for this)

- Single currency (USD). Tax is compared on a **net-of-tax basis**, since POs are stored net.
- **Two-way match** (invoice ↔ PO). No goods-receipt data.
- The PO ledger counts only invoices that are **approved** (auto or by a human). Pending/held/rejected invoices do not consume PO balance.
- Under-billing against a PO is allowed (partial billing); over-billing is what triggers tolerance rules.
- Critical fields: vendor name, invoice number, invoice date, total. A PO reference is **not** critical, because it can be inferred.
- Tolerances and thresholds in `policy.yaml` are illustrative defaults an AP team would tune.

---

## 4. Architecture and pipeline

```
 PDF / image
     │
 [1 Intake] → [2 Extract] → [3 Normalize] → [4 Completeness] → [5 Vendor] → [6 Duplicates]
                  │                                                              │
                  │ Tier 0: text layer? ──no──► Tier 2 (LLM vision)              ▼
                  │ Tier 1: deterministic parse                      [7 PO match] → [8 Amounts]
                  │ Reconcile arithmetic ──fail/missing──► Tier 2 (LLM)              │
                  │ Ground LLM output against text layer                             ▼
                  └──────────────────────────────────────────────────  [9 Decide] → [10 Explain & Act]
```

- **Stack:** Python, Streamlit (UI), SQLite (state), `pdfplumber` (text), `rapidfuzz` (fuzzy matching), `pydantic`, `PyYAML`, `reportlab` + `Pillow` (test-data generation), `anthropic` SDK (Tier 2; provider kept behind a small interface), `pytest`.
- **Each stage emits `StageEvent`s as it runs.** The pipeline is a generator, so the UI can render stages live.
- **State:** SQLite file, seeded deterministically; a "Reset demo data" action rebuilds it.

### Repository layout

```
invoicedesk/
  README.md
  requirements.txt
  .env.example                  # ANTHROPIC_API_KEY, LLM_MODEL, LLM_MODE
  policy.yaml
  app.py                        # Streamlit entry (Process page)
  pages/
    1_Dashboard.py
    2_Review_queue.py
    3_Policy.py
  invoicedesk/
    models.py                   # contracts (Section 5)
    policy.py                   # loads policy.yaml
    db.py                       # SQLite access, ledger queries
    seed.py                     # reset + load vendors/POs + batch-run golden set
    extract/
      text.py                   # Tier 0: text layer detection
      tier1.py                  # deterministic parser
      reconcile.py              # arithmetic self-check
      grounding.py              # evidence-in-text check
      llm.py                    # Tier 2 client + cache
      router.py                 # tier routing + merge rules
    normalize.py
    checks/
      completeness.py
      vendor.py
      duplicates.py
      po_match.py
      amounts.py
      integrity.py              # extraction integrity + stacked uncertainty
    decide.py
    comms.py
    pipeline.py                 # run_invoice(...) -> Iterator[StageEvent]
    cli.py                      # python -m invoicedesk.cli run <file> | batch | reset
  scripts/generate_test_data.py
  data/
    vendors.csv
    purchase_orders.csv
    golden.json                 # expected outcomes (test oracle)
    invoices/*.pdf
  .cache/llm/                   # cached LLM responses keyed by file SHA-256 (COMMIT THIS for golden files)
  tests/test_golden.py
```

---

## 5. Shared contracts

### 5.1 `models.py`

```python
from __future__ import annotations
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field

class Outcome(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    HOLD_MISSING_INFO = "HOLD_MISSING_INFO"
    REJECT = "REJECT"

# When several checks propose different outcomes, the first in this list wins.
PRECEDENCE = [Outcome.REJECT, Outcome.HOLD_MISSING_INFO, Outcome.NEEDS_REVIEW, Outcome.AUTO_APPROVE]

class Quality(str, Enum):
    VERIFIED = "VERIFIED"      # deterministic parse AND arithmetic reconciles
    GROUNDED = "GROUNDED"      # LLM value whose evidence snippet was found in the PDF text layer
    UNVERIFIED = "UNVERIFIED"  # LLM value from vision only (no text layer to check against)
    MISSING = "MISSING"        # genuinely absent after all tiers

class LineItem(BaseModel):
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None

class ExtractedInvoice(BaseModel):
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    po_reference: str | None = None        # as printed, e.g. "PO-1001"; None if absent
    currency: str | None = None
    subtotal: Decimal | None = None        # pre-tax, if shown
    tax_amount: Decimal | None = None
    total: Decimal | None = None           # amount due as printed
    prices_include_tax: bool | None = None # True if the printed total already embeds tax
    line_items: list[LineItem] = Field(default_factory=list)
    reference_text: str | None = None      # free-text description / "Re:" line, used for implied PO matching
    field_quality: dict[str, Quality] = Field(default_factory=dict)  # per field name
    evidence: dict[str, str] = Field(default_factory=dict)           # per field: verbatim snippet from the doc
    extraction_path: Literal["deterministic", "llm_text", "llm_vision", "failed"] = "deterministic"
    warnings: list[str] = Field(default_factory=list)

class NormalizedInvoice(BaseModel):
    vendor_name: str | None
    invoice_number: str | None             # raw
    invoice_number_key: str | None         # upper-cased, alphanumerics only
    invoice_date: date | None
    po_reference_key: str | None           # e.g. "PO1001"
    currency: str
    net_amount: Decimal | None             # tax-normalised; basis for PO comparison
    tax_amount: Decimal | None
    gross_amount: Decimal | None
    net_is_inferred: bool = False          # True if net had to be derived from a PO tax rate
    description_text: str                  # reference_text + line item descriptions, joined

class CheckStatus(str, Enum):
    PASS = "PASS"; WARN = "WARN"; FAIL = "FAIL"; SKIP = "SKIP"; INFO = "INFO"

class CheckResult(BaseModel):
    check_id: str                          # stable id, see 5.3
    stage: str
    status: CheckStatus
    proposed: Outcome | None = None        # None = no objection
    title: str                             # e.g. "Vendor is approved"
    detail: str                            # one deterministic sentence a human can read
    evidence: dict[str, Any] = Field(default_factory=dict)

class Comms(BaseModel):
    audience: Literal["vendor", "internal"]
    subject: str
    body: str

class Decision(BaseModel):
    outcome: Outcome
    summary: str                           # one sentence
    reasons: list[str]                     # ordered; each backed by a check in driving_checks
    driving_checks: list[str]              # check_ids that determined the outcome
    recommended_action: str
    comms: Comms | None = None

class StageEvent(BaseModel):
    run_id: str
    seq: int
    stage: str                             # intake|extract|normalize|completeness|vendor|duplicates|po_match|amounts|decide|explain|complete
    state: Literal["started", "completed", "failed"]
    title: str
    detail: str = ""
    checks: list[CheckResult] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)   # stage-specific data; final event carries the Decision
    elapsed_ms: int = 0
```

### 5.2 `policy.yaml`

```yaml
critical_fields: [vendor_name, invoice_number, invoice_date, total]

tolerance:                 # over-billing vs the PO's REMAINING net balance
  auto_pct: 2.0            # <= 2%  -> pass (with a note)
  review_pct: 5.0          # 2-5%   -> NEEDS_REVIEW ; > 5% -> REJECT (dispute)

vendor_match:
  accept: 90               # rapidfuzz token_set_ratio >= 90 -> matched
  ambiguous_low: 75        # 75-90 -> NEEDS_REVIEW (could be a lookalike); < 75 -> unknown vendor

duplicates:
  near_total_pct: 0.5      # totals within 0.5%
  near_window_days: 14     # invoice dates within 14 days
  near_desc_similarity: 0.80  # description similarity (0-1) if no shared PO ref

implied_po:                # used only when the invoice prints no PO reference
  min_confidence: 0.80
  min_margin: 0.20         # best score must beat runner-up by this much
  weights: {amount: 0.5, description: 0.3, recency: 0.2}

extraction:
  reconcile_abs_tolerance: 0.02   # rounding slack when reconciling totals
```

### 5.3 Check IDs and who owns them

| check_id | Stage | Proposes (when it fails) |
|---|---|---|
| `completeness` | completeness | `HOLD_MISSING_INFO` (a critical field is MISSING) |
| `extraction_integrity` | extract | `NEEDS_REVIEW` (reconciliation failed, tiers disagree, or LLM unavailable) |
| `vendor_identity` | vendor | `REJECT` if unknown (<75); `NEEDS_REVIEW` if ambiguous (75–90) |
| `vendor_status` | vendor | `REJECT` if vendor is blocked |
| `duplicate_exact` | duplicates | `REJECT` (same vendor + same normalised invoice number already processed) |
| `duplicate_near` | duplicates | `NEEDS_REVIEW` (probable duplicate; see 8.3) |
| `po_resolution` | po_match | `NEEDS_REVIEW` if no PO found, or implied match is weak/ambiguous |
| `po_vendor_consistency` | po_match | `NEEDS_REVIEW` if the PO belongs to a different vendor |
| `po_status` | po_match | `REJECT` if PO is closed |
| `amount_vs_balance` | amounts | tolerance bands per policy (pass / `NEEDS_REVIEW` / `REJECT`) |
| `stacked_uncertainty` | amounts | `NEEDS_REVIEW` when extraction is UNVERIFIED **and** the PO was implied |

### 5.4 Decision rule

1. Collect all `CheckResult.proposed` values that are not `None`.
2. Outcome = the highest in `PRECEDENCE` among them; if none, `AUTO_APPROVE`.
3. `driving_checks` = the checks that proposed that outcome. `reasons` = their `detail` strings first, then any other `WARN`/`INFO` notes.
4. Checks whose prerequisites are missing return `SKIP` (e.g. no vendor identified → no duplicate check), so the reason list stays truthful.

### 5.5 Pipeline entry point

```python
def run_invoice(path: Path, *, db: Db, policy: Policy, llm: LlmClient) -> Iterator[StageEvent]:
    """Yields StageEvents. The final event has stage='complete' and payload={'decision': Decision, 'run_id': ...}.
    Persists the run and all events to SQLite before yielding the final event."""
```

---

## 6. Data

### 6.1 Vendor master (`data/vendors.csv`)

| vendor_id | name | status | notes |
|---|---|---|---|
| V001 | Northwind Office Supplies Ltd | approved | alias: Northwind Office Supplies |
| V002 | Brightline IT Services Inc | approved | |
| V003 | Apex Logistics LLC | approved | |
| V004 | Meridian Facilities Co | approved | |
| V005 | Orion Consulting Group | **blocked** | "Bank details changed 2026-10-28, pending re-verification" |
| V006 | Cobalt Cloud Hosting Inc | approved | |
| V007 | Summit Print & Packaging | approved | |
| V008 | Greenfield Catering Co | approved | |

### 6.2 Purchase orders (`data/purchase_orders.csv`)

Columns: `po_number, vendor_id, description, net_amount, tax_rate, currency, status, issue_date`.

| PO | Vendor | Description | Net | Tax | Status |
|---|---|---|---|---|---|
| PO-1001 | V001 | Office furniture – Q4 order | 4,800.00 | 8% | open |
| PO-1002 | V002 | Managed IT services – November | 12,500.00 | 10% | open |
| PO-1003 | V003 | Freight – warehouse transfer | 2,000.00 | 8% | open |
| PO-1004 | V004 | Facilities maintenance – Q4 | 7,500.00 | 8% | open |
| PO-1005 | V005 | Strategy consulting engagement | 18,000.00 | 8% | open |
| PO-2001 | V006 | Cloud platform migration (3 milestones) | 30,000.00 | 8% | open |
| PO-3001 | V007 | Printed brochures – product launch | 2,400.00 | 8% | open |
| PO-4001 | V008 | Q4 team offsite catering | 3,000.00 | 8% | open |
| PO-4002 | V008 | Monthly pantry supplies – November | 1,200.00 | 8% | open |
| PO-9001..9003 | filler | unrelated, incl. one `closed` | — | — | mixed |

### 6.3 Golden invoice set (12 files; this is also the test oracle)

Template A = classic table layout; B = modern layout with different labels; C = narrative/unusual labels; S = image-only scan. "Order" is the processing order used by `seed.py` and tests (the ledger is stateful).

| # | File | Order | Vendor / PO | Key content | Template | Expected extraction path | **Expected outcome** | Why |
|---|---|---|---|---|---|---|---|---|
| 01 | `01_clean_exact.pdf` | 1 | Northwind / PO-1001 | INV-10482, 2026-10-14, net 4,800.00, tax 384.00, total 5,184.00 | A | deterministic | **AUTO_APPROVE** | Happy path: exact match |
| 02 | `02_tax_inclusive.pdf` | 2 | Brightline / PO-1002 | BIT-7731, 2026-10-20, total 13,750.00 "includes 10% tax of 1,250.00" | B | deterministic | **AUTO_APPROVE** | Tax normalisation: net 12,500 matches PO |
| 03 | `03_within_tolerance.pdf` | 3 | Meridian / PO-1004 | MFC-5590, 2026-10-22, net 7,620.00 (+1.6%), tax 609.60, total 8,229.60 | A | deterministic | **AUTO_APPROVE** (with note) | Within 2% tolerance |
| 04 | `04_over_tolerance.pdf` | 4 | Apex / PO-1003 | APX-20931, 2026-10-23, net 2,080.00 (+4%), tax 166.40, total 2,246.40 | B | deterministic | **NEEDS_REVIEW** | In the 2–5% grey band |
| 05 | `05_blocked_vendor.pdf` | 5 | Orion / PO-1005 | OCG-0192, 2026-10-30, net 18,000.00, tax 1,440.00, total 19,440.00 | A | deterministic | **REJECT** | Blocked vendor is a hard stop even though PO matches perfectly. Internal escalation only; do not email the vendor |
| 06 | `06_split_po_1of3.pdf` | 6 | Cobalt / PO-2001 | CCH-2026-0412, 2026-09-15, "Milestone 1 – Platform migration: discovery & design", net 10,000.00, total 10,800.00 | A | deterministic | **AUTO_APPROVE** | Partial billing; ledger 10,000/30,000 |
| 09 | `09_near_duplicate.pdf` | 7 | Cobalt / PO-2001 | **CH-88120**, 2026-09-18, same milestone-1 description, same net 10,000.00 / total 10,800.00, different layout | C | **llm_text** | **NEEDS_REVIEW** | Edge case E2: re-issued under a new number; probable duplicate of 06 |
| 07 | `07_split_po_2of3.pdf` | 8 | Cobalt / PO-2001 | CCH-2026-0498, 2026-10-15, "Milestone 2 – Data migration & cutover", net 10,000.00, total 10,800.00 | A | deterministic | **AUTO_APPROVE** | Same vendor/PO/amount as 06 but 30 days later with a different description, so **not** a duplicate. Ledger 20,000/30,000 |
| 08 | `08_split_po_3of3_overbill.pdf` | 9 | Cobalt / PO-2001 | CCH-2026-0511, 2026-11-05, "Milestone 3 – Hypercare & decommissioning", net 12,500.00, tax 1,000.00, total 13,500.00 | A | deterministic | **REJECT** | Edge case E1: remaining PO balance is 10,000; over by 2,500 (25% > 5%); drafted dispute email to vendor |
| 10 | `10_missing_invoice_number.pdf` | 10 | Summit / PO-3001 | **No invoice number**; date 2026-10-27; PO-3001 printed; net 2,400.00, total 2,592.00. Traps: "Customer account no: 88341", "Delivery note: DN-5520" | B | llm_text (Tier 1 fails, Tier 2 confirms absent) | **HOLD_MISSING_INFO** | Edge case E3: must not mistake account/delivery numbers for the invoice number; drafted vendor email lists exactly what is missing |
| 11 | `11_scanned_implied_po.pdf` | 11 | Greenfield / (no PO printed) | GC-2210, 2026-10-29, "Q4 team offsite catering – 40 guests", net 3,000.00, tax 240.00, total 3,240.00; image-only | S | llm_vision | **NEEDS_REVIEW** (recommend: approve against PO-4001) | Edge case E4: scan (UNVERIFIED) + inferred PO triggers stacked-uncertainty rule |
| 12 | `12_exact_duplicate.pdf` | 12 | Northwind / PO-1001 | Byte-for-byte copy of 01, resubmitted | A | deterministic | **REJECT** | Exact duplicate of an already-approved invoice |

**The four demo edge cases:** E1 split-PO over-billing (08), E2 renumbered near-duplicate (09), E3 missing invoice number with look-alike fields (10), E4 scanned invoice with implied PO (11). Files 02, 03, 04, 05, 07 and 12 are regression coverage and optional extras.

**Why these edge cases are non-trivial:** E1 needs cumulative state; E2 must catch a renumbered duplicate without false-positiving on 07; E3 distinguishes "extractor failed" from "truly absent" and resists look-alike fields; E4 combines two uncertainty sources and demonstrates the safety rule.

### 6.4 Test-data generator (`scripts/generate_test_data.py`)

- Table-driven from a list of invoice specs (the table above). Deterministic, with no randomness unless seeded.
- Templates A, B, C rendered with `reportlab`. Each should look like a plausible vendor invoice (logo-ish header, address block, line-item table, totals, footer) with **different label vocabularies** (A: "Invoice No / PO Number / Subtotal / Tax / Total Due"; B: "Document ref / Your order / Amount due / VAT incl."; C: narrative style with "Bill ref", "Re:", "Grand total").
- Scan (`11`): draw the invoice with `Pillow` onto a white page, add slight rotation (~0.8°), grey noise and a blur, and save as an **image-only PDF** (no text layer). This avoids needing Poppler or Tesseract.
- Writes `vendors.csv`, `purchase_orders.csv`, `golden.json` (file, order, expected outcome, expected path, expected driving check ids), and the 12 PDFs.

---

## 7. Extraction design

### 7.1 Routing

```
1. text = pdfplumber text layer (all pages)
2. if text is essentially empty (< ~40 chars/page):   → Tier 2 vision          (path = llm_vision)
3. else Tier 1 deterministic parse → reconcile
4.    if all critical fields found AND reconcile passes → done                   (path = deterministic, quality VERIFIED)
5.    else → Tier 2 on the PDF → ground against text layer → merge               (path = llm_text)
6. Merge rule: Tier 2 fills fields that are missing/failed in Tier 1.
               If both tiers have a value and they disagree → keep Tier 1, add a warning, and let `extraction_integrity` propose NEEDS_REVIEW.
               If both tiers have nothing → quality MISSING (genuinely absent).
7. If Tier 2 is required but unavailable (LLM_MODE=off, or API/cache miss) → path = "failed"; `extraction_integrity` proposes NEEDS_REVIEW ("extraction incomplete"). Never crash.
```

### 7.2 Tier 1 (deterministic)

- Labels (case-insensitive) must include an invoice-specific word for the invoice number (`invoice no|number|#|inv`). Do **not** accept bare "No." or "account no" or "delivery note" (this is what makes E3 a real test).
- Amounts: parse with `Decimal`, strip currency symbols and thousands separators. Find `subtotal`, `tax|vat|gst`, `total|amount due|grand total`.
- Detect tax-inclusive phrasing ("incl.", "includes", "inclusive of") and set `prices_include_tax`.
- PO reference: regex `PO[-\s]?\d{3,}` (and "your order" label variants).
- Line items: `page.extract_tables()` with a fallback to row-wise regex.
- Tier 1 is allowed to fail on Template C. That is the point of the escalation story.

### 7.3 Reconciliation (`reconcile.py`)

- `sum(line_items.amount) ≈ subtotal` (if both present).
- `subtotal + tax ≈ total` (if no tax-inclusive flag), or `total − tax ≈ subtotal` (if tax-inclusive).
- Tolerance from `policy.extraction.reconcile_abs_tolerance`.
- Returns a list of pass/fail findings with numbers (shown in the UI).

### 7.4 Tier 2 (LLM) — `llm.py`

- **Interface:** `LlmClient.extract_invoice(path: Path) -> dict` returning the structured fields plus per-field `evidence` snippets. The provider sits behind this interface (default: Anthropic).
- **Modes via `LLM_MODE`:** `live` (cache first, then API), `cache` (cache only; a miss counts as unavailable), `off`.
- **Cache:** `.cache/llm/<sha256-of-file>.json`. Commit the cache entries for the golden files so the deployed demo works without a key and a live demo never depends on API latency. Fresh uploads use the live API.
- **Call:** send the PDF as a document block (the model reads text PDFs and scans natively), temperature 0, **forced tool call** with a JSON schema so output is structured. Default model from env `LLM_MODEL` (suggest `claude-haiku-5-5`; verify the model string against current docs). Typical cost is a fraction of a cent to a few cents per invoice.
- **System prompt (core rules):**
  1. Extract only what is **printed** on the document. If a field is not present, return `null`. Never infer or guess.
  2. The invoice number is the number the *vendor assigned to this invoice*. Do not confuse it with customer/account numbers, delivery notes, order numbers, or PO numbers.
  3. For every non-null field, include `evidence`: a short verbatim snippet from the document that supports it.
  4. Amounts as decimal strings without currency symbols. Report whether the printed total already includes tax.
  5. Return a free-text `reference_text` capturing any "Re:", description or subject line (used for PO matching).
- **Grounding (`grounding.py`):** for text PDFs, normalise (lower-case, collapse whitespace; digits-only for amounts) and verify each evidence snippet is a substring of the text layer. Pass → `GROUNDED`. Fail → drop to `UNVERIFIED` and add a warning. For image-only scans there is nothing to check against, so all LLM fields are `UNVERIFIED`.

---

## 8. Checks (rules the engine runs)

All checks return `CheckResult`s with deterministic `detail` text and an `evidence` dict (numbers, candidates, scores).

### 8.1 Completeness and extraction integrity
- `completeness`: for each field in `critical_fields`, if quality is `MISSING` → FAIL, proposes `HOLD_MISSING_INFO`; the detail names every missing field.
- `extraction_integrity`: FAIL/WARN → proposes `NEEDS_REVIEW` if reconcile failed, tiers disagreed, grounding failed on a critical field, or extraction path is `failed`.

### 8.2 Vendor
- Fuzzy match the extracted vendor name to the master (name + aliases) using `rapidfuzz.fuzz.token_set_ratio` after normalising (strip "Ltd/Inc/LLC/Co", punctuation).
- ≥ 90 → matched; 75–90 → ambiguous (`NEEDS_REVIEW`, list candidates); < 75 → unknown (`REJECT`, "route to vendor onboarding").
- `vendor_status`: `blocked` → `REJECT`. Blocked-vendor rejection includes **no vendor-facing email** (the vendor may be the compromised party or the fraudster); it creates an internal escalation note instead.

### 8.3 Duplicates
- **Exact:** same `vendor_id` + same `invoice_number_key` as any previous run that was not itself rejected as a duplicate → `REJECT`.
- **Near (probable):** same vendor AND |total difference| ≤ `near_total_pct` AND |date difference| ≤ `near_window_days` AND (same PO ref OR description similarity ≥ `near_desc_similarity`) → `NEEDS_REVIEW` with a side-by-side evidence dict (both runs, scores).
- **Design note:** file 07 must **not** match 06: same vendor, PO and total, but 30 days apart and a different milestone description. Document this in the demo. It is the false-positive guard.

### 8.4 PO resolution
1. **Explicit:** PO reference printed → look it up. Not found → `NEEDS_REVIEW`. Found but belongs to another vendor → `NEEDS_REVIEW` (possible mis-reference or fraud). `closed` → `REJECT`.
2. **Implied** (no PO printed): candidates = open POs for the matched vendor. For each, score = `w_amount·amount_fit + w_desc·desc_similarity + w_recency·recency`, where `amount_fit` is 1.0 when the invoice net ≤ PO remaining and decays to 0 as it exceeds remaining; `desc_similarity` is `token_set_ratio/100` between `description_text` and the PO description; `recency` favours recent POs. Accept only if best ≥ `min_confidence` **and** best − runner-up ≥ `min_margin`; otherwise → `NEEDS_REVIEW` listing ranked candidates. An accepted implied match is recorded as `implied` with its score (expected for 11: PO-4001 ≈ 0.95, PO-4002 low).

### 8.5 Amounts and the ledger
- `net_amount`: if the document shows a pre-tax subtotal use it; if prices include tax use `total − tax_amount`; if only a gross total is shown, derive net from the PO's tax rate and set `net_is_inferred = True` (surface this in the UI).
- `remaining = PO.net − Σ(net of approved invoices on this PO)` (approved = `AUTO_APPROVE`, or human-approved).
- If `net ≤ remaining` → PASS (note "partial billing; X remaining" when under).
- Else `over_pct = (net − remaining) / remaining`: ≤ `auto_pct` → PASS with note; ≤ `review_pct` → `NEEDS_REVIEW`; > `review_pct` → `REJECT`.
- `stacked_uncertainty`: if any critical field is `UNVERIFIED` **and** the PO match is implied → `NEEDS_REVIEW` with a **recommended action** ("approve against PO-4001").

---

## 9. Decision engine and communications

- `decide.py` implements Section 5.4 and builds `Decision` (summary, reasons, driving checks, recommended action).
- Recommended actions (deterministic text templates), e.g.: *Approve and schedule payment* / *Review: variance of 4.0% over PO balance; approve if justified* / *Hold: request invoice number from vendor* / *Reject: invoice would exceed PO-2001 remaining balance by 2,500.00 (25%); send dispute* / *Escalate to procurement: vendor is blocked*.
- `comms.py` drafts messages from templates (the LLM is not needed here):
  - **HOLD_MISSING_INFO** → vendor email listing exactly the missing fields, quoting what was received, asking for a corrected invoice.
  - **REJECT (over-billing)** → vendor dispute email with PO number, billed vs remaining balance.
  - **REJECT (blocked vendor / exact duplicate)** → **internal** note only.
  - **NEEDS_REVIEW** → internal reviewer note with the recommended action and the evidence summary.
- *(Could)* an LLM may rewrite `summary` in plainer language **only from the structured facts** passed to it; if it fails or `LLM_MODE != live`, use the template. It cannot change `outcome` or add facts.

---

## 10. Persistence (SQLite)

```sql
vendors(vendor_id PK, name, aliases_json, status, notes)
purchase_orders(po_number PK, vendor_id, description, net_amount, tax_rate, currency, status, issue_date)
runs(
  run_id PK, created_at, filename, file_sha256,
  extraction_path, extraction_json, normalized_json,
  vendor_id, matched_po, po_match_type,          -- 'explicit' | 'implied' | NULL
  net_amount, gross_amount,
  decision, decision_json,                       -- full Decision
  review_status,                                 -- 'none' | 'pending' | 'approved' | 'rejected'
  reviewer_note, reviewed_at,
  llm_used, llm_cost_usd, duration_ms
)
events(id PK, run_id, seq, stage, state, title, detail, checks_json, payload_json, elapsed_ms)

-- ledger: net billed per PO (approved only)
SELECT matched_po, SUM(net_amount) FROM runs
WHERE decision='AUTO_APPROVE' OR review_status='approved'
GROUP BY matched_po;
```

`seed.py reset`: drop and recreate tables, load CSVs, and batch-run the golden set **in the Section 6.3 order** so the DB contains a realistic history. Offer a separate **"demo state"** option for live demos: it seeds only files 02, 03, 04, 05, 06 and 07 (in that order). That gives the dashboard a realistic history and puts PO-2001 at 20,000/30,000, but leaves every file you will run live (01, 08, 09, 10, 11, then 12) unprocessed. This matters because the duplicate check would otherwise reject a live run of a file already in the DB as an exact duplicate (e.g. 09 would be flagged against itself instead of against 06). Do **not** use the full batch seed right before a live demo. Run order and what each file needs from history: 01 (nothing), 08 (needs 06 and 07), 09 (needs 06), 10 and 11 (nothing), 12 (needs 01 to have been run live first).

---

## 11. UI specification (Streamlit)

Keep it clean: sidebar navigation, consistent decision colours (green approve, amber review, blue hold, red reject), and no custom CSS rabbit holes.

### Page 1: Process (live run view)
- **Input:** file uploader + a dropdown of the 12 sample invoices. "Run" button.
- **Left/main: stage timeline.** Ten stages, each with a status icon, name, elapsed time and one-line result, updating **as the generator yields** events. Expanders show details: extracted fields table (with a quality badge per field: VERIFIED / GROUNDED / UNVERIFIED / MISSING), reconciliation numbers, the check results and their evidence.
- **Extraction path chip:** `deterministic` / `llm_text` / `llm_vision`, plus "LLM calls: 0 · cost $0.00".
- **PO ledger mini-panel:** the matched PO as a progress bar (billed / remaining), showing this invoice's contribution.
- **Decision card (bottom/side):** outcome badge, one-line summary, ordered reasons (each tagged with its check id), recommended action, drafted comms (copyable).
- **Right (optional):** PDF preview of the input.
- Optional `DEMO_PACING_MS` (e.g. 250 ms) to make fast stages visible. Be honest that this is only for visibility.
- Sidebar: **Reset demo data**.

### Page 2: Dashboard
- KPI row: invoices processed, auto-approved %, held/needs review count, total $ approved, average processing time, LLM calls (and what share of invoices needed none).
- Runs table (newest first) with filters (decision, vendor), columns: time, file, vendor, PO, amount, path, decision, review status; a run-detail view (select a row → the same timeline/decision card from stored events).
- Charts: decisions by outcome; extraction path split.
- **PO ledger view:** every PO with a billed / remaining progress bar.

### Page 3: Review queue
- Lists `NEEDS_REVIEW` and `HOLD_MISSING_INFO` runs with the recommended action and evidence. Buttons: **Approve** / **Reject** (+ note). Approving updates `review_status` and therefore the ledger; both actions are logged.

### Page 4: Policy (read-only)
- Renders `policy.yaml` in a friendly form: "this is why it behaves this way."

---

## 12. Work packages (parallelisable)

**Dependency graph:** WP0 → { WP1 ∥ WP2 } → { WP3 ∥ WP4 ∥ WP5 } → WP6 → WP7 → WP8. WP3–WP5 can start from the contracts alone and unit-test with hand-made inputs before WP1's PDFs exist.

### WP0. Scaffold (30 min)
- Repo layout (Section 4), `requirements.txt`, `.env.example`, `models.py` (verbatim from 5.1), `policy.yaml` (5.2), `policy.py` loader (typed).
- **Done when:** `python -c "import invoicedesk.models"` works and the policy loads into a typed object.

### WP1. Test data generator
- Implement 6.4. Output the 12 PDFs, CSVs and `golden.json`.
- **Done when:** the PDFs open and look like plausible invoices; PDFs 01–10 and 12 have extractable text; PDF 11 has none; template C visibly differs in label vocabulary; `golden.json` matches Section 6.3 exactly.

### WP2. Persistence, seed, CLI skeleton
- `db.py` (schema 10, helpers: insert run/events, ledger queries, vendor/PO lookups, previous-run lookups for duplicates), `seed.py` (reset + batch + demo-state), `cli.py` (`reset`, `run <file>`, `batch`).
- **Done when:** `reset` produces an empty-history DB with vendors and POs loaded; ledger query is correct against hand-inserted rows.

### WP3. Extraction Tier 0/1, reconcile, normalise, router (no LLM)
- Implement 7.1–7.3 and `normalize.py` (dates, Decimal parsing, invoice-number key, tax-basis normalisation per 8.5).
- Router takes an injectable `LlmClient` (stubbed in tests).
- **Done when:** Tier 1 extracts all critical fields from files 01–08 and 12 with reconciliation passing; Tier 1 fails (as designed) on 09 and 10's invoice number; 11 is detected as image-only; unit tests cover each.

### WP4. Tier 2 LLM client, cache, grounding
- Implement 7.4: client, prompt, tool schema, cache, `LLM_MODE`, grounding, plus a **recorded-fixture** path so tests can run without network.
- **Done when:** with a real key, files 09, 10, 11 produce cached JSON; 10's `invoice_number` is `null`; the grounding check passes for 09 and 10's other fields; 11's fields are `UNVERIFIED`; `LLM_MODE=cache` works with no key; `LLM_MODE=off` yields `extraction_path="failed"` without raising.

### WP5. Checks
- Implement Section 8 (`completeness`, `vendor`, `duplicates`, `po_match`, `amounts`, `integrity`) as pure functions over (`ctx`, `policy`, `db`) returning `list[CheckResult]`.
- **Done when:** unit tests per check with hand-built inputs, including: fuzzy-name edge (alias match, ambiguous lookalike), 06 vs 09 flagged, 06 vs 07 not flagged, implied-match scoring for the Greenfield case (PO-4001 chosen with margin), tolerance bands at 1.6%, 4%, 25%, ledger after two approved milestones.

### WP6. Decision, comms, pipeline, golden tests
- `decide.py`, `comms.py`, `pipeline.py` (generator emitting `StageEvent`s; persists runs/events), CLI wiring.
- `tests/test_golden.py`: for each entry in `golden.json` (in order), run the pipeline with `LLM_MODE=cache` and assert outcome, extraction path, and that the expected driving check id is present.
- **Done when:** all 12 golden cases pass from a clean `reset`, and `python -m invoicedesk.cli batch` prints a clean outcome table. **This is the "submittable" checkpoint.**

### WP7. UI
- Implement Section 11, consuming `run_invoice` events and DB rows only.
- **Done when:** the Process page shows stages updating live for file 01 and file 11; the dashboard shows all history after `batch`; the review queue's Approve changes the ledger visibly.

### WP8. Deploy, docs, rehearsal, video
- README (setup, architecture in a paragraph, assumptions, limitations, next steps, how to reproduce the golden run).
- Deploy (Section 15), rehearse (Section 16), record the video.

### Prompt template for handing a WP to an agent

> You are implementing **WP-N: <name>** of the InvoiceDesk project. Context and contracts are below (Sections 0 and 5, plus the specs this WP references). Implement only this WP. Do not modify shared contracts; if you believe one needs to change, say so explicitly. Write unit tests that run offline. Report which acceptance criteria you verified and how.

---

## 13. Timeline (≈15.5 focused hours) and cut lines

| Block | Effort | Work | Gate |
|---|---|---|---|
| 0 | 0.5h | WP0 scaffold | imports + policy load |
| 1 | 2h | WP1 data generator ∥ WP2 DB/seed/CLI | PDFs + seeded DB exist |
| 2 | 3h | WP3 Tier 1 ∥ WP5 checks | unit tests green |
| 3 | 2h | WP4 LLM tier + cache + grounding | 09, 10, 11 cached |
| 4 | 1h | WP6 decision + pipeline + golden tests | **G1: all 12 golden pass in CLI (submittable)** |
| 5 | 3h | WP7 Process page + Dashboard | **G2: live run view + history work** |
| 6 | 1.5h | Review queue, comms polish, policy page | |
| 7 | 1.5h | Deploy, commit cache, rehearse | **G3: public link works with no API key for golden files** |
| 8 | 1h | Record the 5-minute video | |

**Cut lines if behind (cut in this order):** LLM-written summary → PDF side-by-side → policy page → review-queue actions (keep the list view) → extra regression files (keep 01, 06/07/08, 09, 10, 11, 12). **Never cut:** golden tests for the 4 demo edge cases, the live run view, the dashboard history.

---

## 14. Testing

- **Unit tests per module** (offline, fixtures only).
- **Golden test:** `tests/test_golden.py` drives from `data/golden.json`; runs from a clean DB in the defined order; `LLM_MODE=cache`.
- **Property checks worth adding quickly:** reconciliation never passes when line items are altered; duplicate check is symmetric on near-dup evidence; ledger never counts non-approved runs.
- **Manual pre-demo test:** one fresh upload not in the golden set (e.g. a quickly generated new Template-A invoice) to prove it is not hard-coded.

---

## 15. Deployment

- **Target:** Streamlit Community Cloud (public link from a GitHub repo). Secrets: `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_MODE=live`.
- **Commit** `data/` and `.cache/llm/` so the golden files run with no network dependency. On startup, if the SQLite file is missing (ephemeral filesystem), call `seed.reset()` automatically.
- **Keep a local fallback:** same repo running locally with `LLM_MODE=cache`, and a screen recording of the full flow in case the network fails in the interview.
- Don't commit the API key. Add a small spend cap on the key.

---

## 16. Demo script and rehearsal

### 16.1 The 5-minute video (suggested timing)

| Time | What to show | What to say |
|---|---|---|
| 0:00–0:30 | Dashboard, then the one-line pitch | The problem in AP terms; design stance: "rules decide, the LLM only reads" |
| 0:30–1:15 | **Happy path (01)** in the Process page | Stages; `deterministic` path chip (0 LLM calls); reconciliation; every check; AUTO_APPROVE with reasons |
| 1:15–2:00 | **E1: split-PO over-billing (08)** | Ledger bar at 20,000/30,000; invoice net 12,500; over by 25%; REJECT; drafted dispute email. Mention 07 passing |
| 2:00–2:50 | **E2: renumbered near-duplicate (09)** | Different layout → Tier 1 fails → LLM fallback → grounding passes; near-duplicate evidence; why 07 was *not* flagged |
| 2:50–3:30 | **E3: missing invoice number (10)** | Look-alike account/delivery numbers ignored; Tier 2 confirms absence; HOLD + drafted email |
| 3:30–4:20 | **E4: scanned + implied PO (11)** | Vision path, `UNVERIFIED`; implied match PO-4001 with score and margin; stacked-uncertainty → NEEDS_REVIEW with recommendation |
| 4:20–5:00 | Review queue → approve → ledger updates; dashboard | Human in the loop; limitations and next steps in two sentences |

### 16.2 Live interview run order
01 → 08 → 09 → 10 → 11 → (review queue approve), then 12 if asked about duplicates (it is an exact copy of 01, so it only works after 01 has been run live). Files 02, 03, 04 and 05 are already in the history from the demo-state seed, so for questions like "show me tolerance" or "show me a blocked vendor", open their stored runs from the dashboard's run-detail view rather than re-running them (a re-run would be rejected as an exact duplicate). Always **Reset demo data → demo state** (Section 10) immediately before the call, and before recording the video.

### 16.3 Pre-demo checklist
- [ ] Reset to demo state; run the full order once end-to-end; then **reset to demo state again** so the live run starts clean (the rehearsal consumed 01, 08, 09, 10, 11).
- [ ] `LLM_MODE` set deliberately; cached entries present for 09, 10, 11.
- [ ] Browser zoomed, tabs closed, notifications off; local fallback running in a second window.
- [ ] One surprise upload tested.
- [ ] Assumptions slide-less cheat sheet (Section 3) open for reference.

---

## 17. Interview preparation

**Likely questions and strong answers**
- *"Why not just send every invoice to an LLM?"* Cost, latency, determinism and auditability. We use the cheapest sufficient method, and verify any LLM output (reconciliation + grounding) before trusting it. Decisions are rules, not model opinions.
- *"What happens with a vendor format you've never seen?"* Tier 1 fails reconciliation or completeness → escalates to Tier 2 → grounded and reconciled. The dashboard shows the escalation rate, which is the signal for when to add a template.
- *"How do you avoid false duplicates on milestone billing?"* The near-duplicate rule combines total, date window and PO/description similarity. 07 vs 06 is the live example.
- *"What if the model hallucinates an invoice number?"* The prompt demands null over guessing; grounding requires a verbatim snippet in the text layer; scans are explicitly `UNVERIFIED` and can't auto-approve when stacked with an inferred PO.
- *"Why NEEDS_REVIEW for 11 when everything matches?"* Two independent weak signals (unverifiable extraction + inferred PO). The system recommends the approval and lets a human confirm, so a clerk needs one click instead of the full work.
- *"How would this scale?"* Queue-based ingestion from an AP inbox, a real DB, per-vendor learned templates, async LLM calls, caching by file hash (already there).
- *"What would you do next?"* 3-way match with goods receipts; email ingestion; learning from reviewer overrides to tune tolerances; ERP write-back; confidence calibration on the LLM tier; multi-currency.

**Limitations to state openly:** two-way match only; single currency; synthetic data; regex Tier 1 covers a limited set of layouts by design; grounding cannot verify scans.

---

## 18. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| LLM call fails/slow during the live demo | Medium | Commit the cache for golden files; `LLM_MODE=cache` as default for demo; graceful `failed` path |
| Tier 1 regex brittle on generated PDFs | Medium | Generator and Tier 1 developed against the same label sets; reconciliation catches errors; Tier 2 as a safety net |
| Scope creep (UI polish, extra rules) | High | Gates G1–G3; cut lines in Section 13; the CLI-first order |
| Ledger state confuses the live demo | Medium | Deterministic seed, "demo state" button, rehearsed order |
| Streamlit live-update quirks | Low–Med | Render events in a loop into placeholders; `DEMO_PACING_MS`; test on the deployed instance, not just locally |
| Ephemeral filesystem on hosting resets the DB | Medium | Auto-reseed on startup |
| Package install / env issues | Low | Pin versions; test a clean install from `requirements.txt` before deploying |

---

## 19. Definition of done

- [ ] `reset` + `batch` produces 12/12 golden outcomes.
- [ ] Public link works; golden files run without an API key; a fresh upload runs with one.
- [ ] Live run view shows each stage executing; dashboard shows history, status, outputs, PO ledger.
- [ ] 4 edge cases demonstrable in under 5 minutes in the prepared order.
- [ ] README lists assumptions, limitations and next steps.
- [ ] 5-minute video recorded; link tested in a private window.
- [ ] Problem choice emailed to the hiring coordinator if not already done (guide, Day 1): subject `ASA Case Study — [Your Name] — PS-1`.
- [ ] Day-7 submission email sent to the hiring coordinator with exactly two things: the live process link and the demo video link.
- [ ] Assumptions noted somewhere reference-able in the live pitch (README), as the FAQ asks.
