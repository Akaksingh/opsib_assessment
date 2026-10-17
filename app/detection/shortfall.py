"""Deterministic receipt-shortfall detector (Task B).

Pure, independently testable logic. It takes already-canonical PO lines and
receipt lines (identity and UOM resolved upstream) plus the frozen evaluation
time, and returns typed findings. It performs no I/O and no AI calls — exact
arithmetic is the detector's job, never a model's.

The rule, precisely:
  For each PO line:
    - If status is CANCELLED            -> no exception (PO-504).
    - If the expected_receipt_by deadline is still in the future at the frozen
      evaluation time -> not actionable yet (PO-503).
    - Otherwise sum accepted_qty_base over POSTED receipt lines linked to that
      PO line (deduplicated upstream by source id, so a repeated receipt counts
      once — PO-506). Compare expected (ordered_qty_base) vs received.
    - variance = expected - received. If variance > tolerance -> SHORTFALL.
      Equal or over-received -> no exception (PO-502).
    - BOX->EA conversion happens before comparison (PO-505): 10 BOX * 12 = 120.

  A line whose product/location mapping is incomplete or ambiguous never reaches
  this detector as a business exception; it is a data-quality issue instead.

Grain correctness: we aggregate receipts by *PO line*, not by product name or row
count. Each receipt line keeps its own source identity; duplicates are removed by
source id before summing, so partial, split and repeated receipts all compute
correctly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence


@dataclass(frozen=True)
class POLineView:
    """The detector's minimal view of a canonical PO line."""

    source_record_id: str
    po_number: str
    line_number: int
    canonical_product_id: str
    canonical_location_id: str
    ordered_qty_base: float
    base_uom: str
    status: str
    expected_receipt_by: datetime


@dataclass(frozen=True)
class ReceiptLineView:
    source_record_id: str
    po_number: str
    po_line_number: int
    canonical_product_id: str
    canonical_location_id: str
    accepted_qty_base: float
    base_uom: str
    posting_status: str
    received_at: datetime


@dataclass(frozen=True)
class ShortfallFinding:
    po_number: str
    po_line_number: int
    canonical_product_id: str
    canonical_location_id: str
    base_uom: str
    expected_qty_base: float
    received_qty_base: float
    variance_qty_base: float
    evidence_refs: list[str]
    explanation: str
    # case_key is stable + idempotent: re-detection reuses it, never forks a case.
    case_key: str

    @property
    def is_shortfall(self) -> bool:
        return self.variance_qty_base > 0


@dataclass
class DetectionOutcome:
    """Everything the detector concluded for one PO line.

    `finding` is set only for an actionable shortfall. `skipped_reason` explains a
    non-exception (cancelled, not yet due, met-in-full) or a data-quality block.
    """

    po_number: str
    po_line_number: int
    finding: ShortfallFinding | None = None
    skipped_reason: str | None = None
    data_quality_reason: str | None = None


def _case_key(po_number: str, line_number: int, product: str, location: str) -> str:
    # Business key for a shortfall case: the PO line + its canonical product/location.
    return f"SHORTFALL::{po_number}::L{line_number}::{product}::{location}"


def detect_shortfalls(
    po_lines: Sequence[POLineView],
    receipt_lines: Sequence[ReceiptLineView],
    *,
    frozen_now: datetime,
    tolerance: float = 0.0,
) -> list[DetectionOutcome]:
    """Run the rule over all PO lines. Deterministic and order-independent."""

    # Index POSTED receipts by (po_number, po_line_number), deduplicated by source id.
    receipts_by_line: dict[tuple[str, int], dict[str, ReceiptLineView]] = {}
    for r in receipt_lines:
        if r.posting_status != "POSTED":
            continue
        key = (r.po_number, r.po_line_number)
        bucket = receipts_by_line.setdefault(key, {})
        # Keyed by source_record_id -> a repeated source id is stored once (PO-506).
        bucket[r.source_record_id] = r

    outcomes: list[DetectionOutcome] = []
    for po in po_lines:
        outcome = DetectionOutcome(po_number=po.po_number, po_line_number=po.line_number)

        if po.status == "CANCELLED":
            outcome.skipped_reason = "PO line is CANCELLED; no shortfall raised"
            outcomes.append(outcome)
            continue

        if po.status != "OPEN":
            # CLOSED or other non-actionable status — documented, not an exception.
            outcome.skipped_reason = f"PO status {po.status} is not evaluated for shortfall"
            outcomes.append(outcome)
            continue

        if frozen_now < po.expected_receipt_by:
            outcome.skipped_reason = (
                "expected-receipt window has not closed at the evaluation time; "
                "not actionable yet"
            )
            outcomes.append(outcome)
            continue

        bucket = receipts_by_line.get((po.po_number, po.line_number), {})
        matched = list(bucket.values())

        # Grain/identity guard: a receipt that resolved to a different canonical
        # product than its PO line is a mapping problem, not a clean comparison.
        mismatched = [r for r in matched if r.canonical_product_id != po.canonical_product_id]
        if mismatched:
            outcome.data_quality_reason = (
                "receipt(s) mapped to a different canonical product than the PO line; "
                "cannot compute a trustworthy variance"
            )
            outcomes.append(outcome)
            continue

        received = sum(r.accepted_qty_base for r in matched)
        expected = po.ordered_qty_base
        variance = expected - received

        evidence = [po.source_record_id] + sorted(r.source_record_id for r in matched)
        ck = _case_key(po.po_number, po.line_number, po.canonical_product_id,
                       po.canonical_location_id)

        if variance > tolerance:
            outcome.finding = ShortfallFinding(
                po_number=po.po_number,
                po_line_number=po.line_number,
                canonical_product_id=po.canonical_product_id,
                canonical_location_id=po.canonical_location_id,
                base_uom=po.base_uom,
                expected_qty_base=expected,
                received_qty_base=received,
                variance_qty_base=variance,
                evidence_refs=evidence,
                explanation=_neutral_explanation(expected, received, variance, po.base_uom),
                case_key=ck,
            )
        else:
            outcome.skipped_reason = (
                f"received {received:g} {po.base_uom} meets or exceeds expected "
                f"{expected:g} {po.base_uom}; no shortfall"
            )
        outcomes.append(outcome)

    return outcomes


def _neutral_explanation(expected: float, received: float, variance: float, uom: str) -> str:
    """A neutral finding narrative: what is known, what is NOT.

    The fixture pack has no supplier dispatch/ASN data, so we must not assert
    supplier fault, carrier loss or where a shortage occurred (assessment §02).
    """
    return (
        f"Known: {expected:g} {uom} was ordered on this PO line and {received:g} "
        f"{uom} was accepted and posted after the expected-receipt window closed, "
        f"leaving a recorded shortfall of {variance:g} {uom}. "
        "Unknown: whether the supplier dispatched the full quantity, whether units "
        "were lost in transit, or where any discrepancy occurred. No supplier "
        "dispatch / ASN evidence is available, so no cause or fault is asserted. "
        "Evidence still needed to establish cause: supplier dispatch/ASN records, "
        "carrier proof-of-delivery, and any unposted or in-transit receipts."
    )
