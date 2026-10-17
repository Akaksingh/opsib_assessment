"""Unit tests for the pure shortfall detector (Task D minimum tests #1).

These exercise the rule directly, with no database, covering UOM conversion,
partial/split receipts, the deadline boundary, cancellation and duplicate-safe
aggregation. The detector is deterministic, so these are exact assertions.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.detection.shortfall import POLineView, ReceiptLineView, detect_shortfalls

FROZEN = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)  # 15:00 IST


def _po(**kw):
    base = dict(
        source_record_id="ERP-X-L1",
        po_number="PO-X",
        line_number=1,
        canonical_product_id="SKU-001",
        canonical_location_id="STORE-014",
        ordered_qty_base=100.0,
        base_uom="EA",
        status="OPEN",
        expected_receipt_by=datetime(2026, 10, 15, 6, 30, tzinfo=timezone.utc),
    )
    base.update(kw)
    return POLineView(**base)


def _rc(**kw):
    base = dict(
        source_record_id="WMS-X-L1",
        po_number="PO-X",
        po_line_number=1,
        canonical_product_id="SKU-001",
        canonical_location_id="STORE-014",
        accepted_qty_base=60.0,
        base_uom="EA",
        posting_status="POSTED",
        received_at=datetime(2026, 10, 15, 4, 30, tzinfo=timezone.utc),
    )
    base.update(kw)
    return ReceiptLineView(**base)


def _one(po_lines, receipts):
    outcomes = detect_shortfalls(po_lines, receipts, frozen_now=FROZEN)
    assert len(outcomes) == 1
    return outcomes[0]


def test_partial_split_receipts_shortfall():
    # 100 ordered; 60 + 32 received => shortfall 8 (PO-501 shape).
    oc = _one(
        [_po(ordered_qty_base=100)],
        [_rc(source_record_id="A", accepted_qty_base=60),
         _rc(source_record_id="B", accepted_qty_base=32)],
    )
    assert oc.finding is not None
    assert oc.finding.expected_qty_base == 100
    assert oc.finding.received_qty_base == 92
    assert oc.finding.variance_qty_base == 8


def test_met_in_full_no_exception():
    # 40 ordered, 40 received => no shortfall (PO-502 shape).
    oc = _one([_po(ordered_qty_base=40)], [_rc(accepted_qty_base=40)])
    assert oc.finding is None
    assert "no shortfall" in oc.skipped_reason


def test_future_deadline_not_actionable():
    # Deadline after the frozen time => not actionable yet (PO-503 shape).
    future = datetime(2026, 10, 17, 12, 30, tzinfo=timezone.utc)
    oc = _one(
        [_po(ordered_qty_base=50, expected_receipt_by=future)],
        [_rc(accepted_qty_base=20)],
    )
    assert oc.finding is None
    assert "has not closed" in oc.skipped_reason


def test_cancelled_po_no_exception():
    # Cancelled line => never a shortfall (PO-504 shape).
    oc = _one([_po(status="CANCELLED", ordered_qty_base=20)], [])
    assert oc.finding is None
    assert "CANCELLED" in oc.skipped_reason


def test_uom_conversion_applied_before_compare():
    # 120 EA expected (10 BOX * 12) vs 96 EA => shortfall 24 (PO-505 shape).
    oc = _one([_po(ordered_qty_base=120)], [_rc(accepted_qty_base=96)])
    assert oc.finding is not None
    assert oc.finding.variance_qty_base == 24


def test_duplicate_receipt_counted_once():
    # Same source id twice => counted once => shortfall 10, not 0 (PO-506 shape).
    oc = _one(
        [_po(ordered_qty_base=20)],
        [_rc(source_record_id="DUP", accepted_qty_base=10),
         _rc(source_record_id="DUP", accepted_qty_base=10)],
    )
    assert oc.finding is not None
    assert oc.finding.received_qty_base == 10
    assert oc.finding.variance_qty_base == 10


def test_deadline_boundary_equal_is_actionable():
    # Deadline exactly at the frozen time => window closed => actionable.
    oc = _one(
        [_po(ordered_qty_base=100, expected_receipt_by=FROZEN)],
        [_rc(accepted_qty_base=90)],
    )
    assert oc.finding is not None
    assert oc.finding.variance_qty_base == 10


def test_unposted_receipts_excluded():
    # A DRAFT receipt does not count toward accepted quantity.
    oc = _one(
        [_po(ordered_qty_base=100)],
        [_rc(source_record_id="P", accepted_qty_base=60, posting_status="POSTED"),
         _rc(source_record_id="D", accepted_qty_base=40, posting_status="DRAFT")],
    )
    assert oc.finding is not None
    assert oc.finding.received_qty_base == 60
    assert oc.finding.variance_qty_base == 40


def test_product_mismatch_is_data_quality_not_exception():
    # A receipt mapped to a different canonical product is a data-quality block.
    oc = _one(
        [_po(ordered_qty_base=100, canonical_product_id="SKU-001")],
        [_rc(accepted_qty_base=60, canonical_product_id="SKU-999")],
    )
    assert oc.finding is None
    assert oc.data_quality_reason is not None


def test_over_receipt_no_exception():
    oc = _one([_po(ordered_qty_base=50)], [_rc(accepted_qty_base=60)])
    assert oc.finding is None


def test_case_key_is_stable():
    po = _po(po_number="PO-77", line_number=3, canonical_product_id="SKU-ABC",
             canonical_location_id="STORE-099", ordered_qty_base=10)
    oc = _one([po], [_rc(po_number="PO-77", po_line_number=3,
                         canonical_product_id="SKU-ABC",
                         canonical_location_id="STORE-099", accepted_qty_base=1)])
    assert oc.finding.case_key == "SHORTFALL::PO-77::L3::SKU-ABC::STORE-099"
