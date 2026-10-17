"""Optional AI investigation-assistant interface (Task D — designed, not required).

The boundary this enforces matters more than the model behind it:

  The deterministic detector produces the finding and the authoritative numbers.
  The AI service receives ONLY a bounded, already-computed case context and may
  return a *narrative* investigation summary with suggested next steps. It must
  NOT calculate authoritative quantities, invent source records, decide employee
  permissions, perform ERP writes, or mark anything verified.

Two guards make this safe regardless of what the model returns:
  1. The output is schema-validated (pydantic). Malformed output is discarded.
  2. Every source record id the model cites is checked against the evidence we
     actually supplied. A hallucinated id fails validation and the summary is
     rejected — it is never shown.

`NullAIProvider` is the default: it needs no key, spends no money, and lets the
whole application run and be tested without a live model. A real provider would
implement the same `summarize` signature.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ValidationError


class EvidencePackage(BaseModel):
    """The only context the AI is ever given. Deterministic, already validated."""

    exception_public_id: str
    type: str
    po_number: str
    canonical_product_id: str
    canonical_location_id: str
    expected_qty_base: float
    received_qty_base: float
    variance_qty_base: float
    base_uom: str
    evidence_refs: list[str]          # the authoritative, allowed source ids
    known_facts: str
    unknown_factors: str


class InvestigationSummary(BaseModel):
    """Schema the AI output MUST satisfy before it can be displayed."""

    headline: str
    narrative: str
    suggested_next_steps: list[str]
    cited_evidence_refs: list[str]
    # The model must restate that cause/fault is not established here.
    cause_asserted: bool = False


class AIProvider(Protocol):
    def summarize(self, package: EvidencePackage) -> InvestigationSummary | None:
        ...


class AIValidationError(Exception):
    pass


def validate_summary(
    package: EvidencePackage, summary: InvestigationSummary
) -> InvestigationSummary:
    """Reject any summary that oversteps the AI boundary."""
    allowed = set(package.evidence_refs)
    cited = set(summary.cited_evidence_refs)
    unknown = cited - allowed
    if unknown:
        raise AIValidationError(
            f"summary cites evidence not in the supplied package: {sorted(unknown)}"
        )
    if summary.cause_asserted:
        raise AIValidationError("summary asserts a cause/fault, which is not permitted")
    return summary


class NullAIProvider:
    """Default no-op provider. Produces a safe, deterministic placeholder summary
    so the feature path is exercised without any external call."""

    def summarize(self, package: EvidencePackage) -> InvestigationSummary | None:
        summary = InvestigationSummary(
            headline=f"Receipt shortfall on {package.po_number}",
            narrative=(
                "An AI investigation assistant is not configured. "
                "Deterministic detection recorded the variance; no cause is asserted."
            ),
            suggested_next_steps=[
                "Obtain supplier dispatch/ASN records to compare dispatched vs ordered.",
                "Check for unposted or in-transit receipts against this PO line.",
                "Confirm UOM and product mapping for the involved SKU.",
            ],
            cited_evidence_refs=list(package.evidence_refs),
            cause_asserted=False,
        )
        return validate_summary(package, summary)


def safe_summarize(
    provider: AIProvider, package: EvidencePackage
) -> InvestigationSummary | None:
    """Call a provider defensively. Any failure (timeout, bad schema, boundary
    violation) returns None, and the application stays fully functional."""
    try:
        raw = provider.summarize(package)
        if raw is None:
            return None
        return validate_summary(package, raw)
    except (ValidationError, AIValidationError, Exception):
        return None
