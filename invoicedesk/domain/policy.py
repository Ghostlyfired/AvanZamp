from decimal import Decimal
import math
from pydantic import BaseModel, model_validator


class Tolerance(BaseModel):
    auto_pct: Decimal
    review_pct: Decimal

class VendorMatch(BaseModel):
    accept: int
    ambiguous_low: int

class Duplicates(BaseModel):
    near_total_pct: Decimal
    near_window_days: int
    near_desc_similarity: float

class ImpliedPo(BaseModel):
    min_confidence: float
    min_margin: float
    min_content_similarity: float = 0.80
    weights: dict[str, float]

class Extraction(BaseModel):
    reconcile_abs_tolerance: Decimal

class Policy(BaseModel):
    version: str = "invoice-policy-2"
    currency: str = "USD"
    matching_adapter: str = "rapidfuzz"
    critical_fields: list[str]
    tolerance: Tolerance
    vendor_match: VendorMatch
    duplicates: Duplicates
    implied_po: ImpliedPo
    extraction: Extraction

    @model_validator(mode='after')
    def validate_policy(self):
        if self.matching_adapter not in {'rapidfuzz', 'development'}:
            raise ValueError('Unknown matching adapter.')
        if len(self.currency) != 3 or not self.currency.isalpha() or self.currency != self.currency.upper():
            raise ValueError('An uppercase approval currency is required.')
        if set(self.critical_fields) - {'vendor_name', 'invoice_number', 'invoice_date', 'total'}:
            raise ValueError('Unsupported critical fact.')
        if set(self.critical_fields) != {'vendor_name', 'invoice_number', 'invoice_date', 'total'}:
            raise ValueError('The four pilot critical fields are mandatory.')
        if not 0 <= self.tolerance.auto_pct <= self.tolerance.review_pct:
            raise ValueError('Invalid tolerance bands.')
        if not 0 <= self.vendor_match.ambiguous_low <= self.vendor_match.accept <= 100:
            raise ValueError('Invalid vendor thresholds.')
        if self.extraction.reconcile_abs_tolerance < 0 or self.duplicates.near_total_pct < 0 or self.duplicates.near_window_days < 0:
            raise ValueError('Tolerances and date windows must be nonnegative.')
        if set(self.implied_po.weights) != {'amount', 'description', 'recency'} or any(not math.isfinite(v) or v < 0 for v in self.implied_po.weights.values()):
            raise ValueError('Invalid implied PO weights.')
        if abs(sum(self.implied_po.weights.values()) - 1) > 1e-9:
            raise ValueError('Implied PO weights must sum to one.')
        if not all(0 <= v <= 1 for v in [self.implied_po.min_confidence, self.implied_po.min_margin, self.implied_po.min_content_similarity, self.duplicates.near_desc_similarity]):
            raise ValueError('Confidence thresholds must be between zero and one.')
        return self

