"""Versioned immutable pilot contracts, separate from Section 5 report adapters."""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from .models import CheckResult, Decision, ExtractedInvoice, NormalizedInvoice
from .policy import Policy

RULE_VERSION = 'invoice-rules-6'


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str, allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class EvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: str
    snippet: str
    page: int | None = Field(default=None, ge=1)
    verification: str = 'UNVERIFIED'


class DocumentFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_sha256: str
    revision: int = 1
    parent_revision: int | None = None
    raw_response_json: str
    evidence: tuple[EvidenceReference, ...] = ()
    provenance: str
    extractor_fingerprint: str


class SectionValidation(BaseModel):
    model_config = ConfigDict(frozen=True)
    section: str
    status: str
    arithmetic_status: str
    verification_status: str
    report_json: str


class InvoiceInterpretation(BaseModel):
    model_config = ConfigDict(frozen=True)
    extraction_revision: int
    invoice_json: str
    normalized_json: str
    route: str


@dataclass(frozen=True)
class ProcurementSnapshot:
    """JSON text makes the underlying snapshot immutable; accessors return copies."""
    revision: int
    vendors_json: str
    pos_json: str
    runs_json: str
    balances: tuple[tuple[str, str], ...]

    def vendors(self) -> list[dict]:
        return json.loads(self.vendors_json)

    def pos(self) -> list[dict]:
        return json.loads(self.pos_json)

    def runs(self) -> list[dict]:
        return json.loads(self.runs_json)

    def billed(self, po: str) -> Decimal:
        return Decimal(dict(self.balances).get(po, '0'))

    def remaining(self, po: dict) -> Decimal:
        return Decimal(po['net_amount']) - self.billed(po['po_number'])

    def as_dict(self) -> dict:
        return dict(revision=self.revision, vendors=self.vendors(), pos=self.pos(),
                    runs=self.runs(), balances=dict(self.balances))


class DecisionContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    extraction_revision: int
    policy_version: str
    policy_json: str
    procurement_revision: int
    snapshot_json: str
    snapshot_sha256: str
    rule_version: str = RULE_VERSION
    profile: str = 'pilot'


@dataclass(frozen=True)
class Recommendation:
    interpretation: InvoiceInterpretation
    decision: Decision
    checks: tuple[CheckResult, ...]
    context: DecisionContext
    vendor_id: str | None
    po_number: str | None
    match_type: str | None


def interpret(invoice: ExtractedInvoice, normalized: NormalizedInvoice, revision: int) -> InvoiceInterpretation:
    return InvoiceInterpretation(extraction_revision=revision, invoice_json=invoice.model_dump_json(),
                                 normalized_json=normalized.model_dump_json(),
                                 route='invoice' if invoice.document_type == 'invoice' else 'review')


def context_for(snapshot: ProcurementSnapshot, policy: Policy, revision: int) -> DecisionContext:
    data = snapshot.as_dict()
    return DecisionContext(extraction_revision=revision, policy_version=policy.version,
                           policy_json=policy.model_dump_json(), procurement_revision=snapshot.revision,
                           snapshot_json=canonical(data), snapshot_sha256=fingerprint(data))
