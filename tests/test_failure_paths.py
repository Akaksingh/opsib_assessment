"""Failure-path tests (Task D minimum tests #6).

Unknown product mapping, rejected rows visible with reasons, and the AI boundary
guards (schema + evidence validation, graceful degradation).
"""
from __future__ import annotations

from sqlalchemy import select

from app.ai.provider import (
    AIValidationError,
    EvidencePackage,
    InvestigationSummary,
    NullAIProvider,
    safe_summarize,
    validate_summary,
)
from app.db.models import PurchaseOrderLine, RowOutcome
from app.ingestion.csv_reader import read_csv_rows
from app.ingestion.service import ImportService


def _load_ref(db, fixtures_dir):
    for ds in ("product_aliases", "location_aliases"):
        ImportService(db).import_dataset(ds, read_csv_rows(fixtures_dir / f"{ds}.csv"))
    db.commit()


def test_unknown_product_mapping_rejected_with_reason(db, fixtures_dir):
    _load_ref(db, fixtures_dir)
    row = {
        "source_record_id": "ERP-PO-UNK", "po_number": "PO-UNK", "line_number": 1,
        "supplier_code": "SUP-07", "location_code": "MUM-014",
        "item_code": "ERP-DOES-NOT-EXIST", "ordered_qty": 10, "ordered_uom": "EA",
        "status": "OPEN", "expected_receipt_by": "2026-10-15T12:00:00+05:30",
    }
    result = ImportService(db).import_dataset("purchase_order_lines", [row])
    db.commit()
    assert result.rows_rejected == 1
    assert "unmapped_product" in result.rows[0].reason
    # Not persisted as a canonical fact.
    assert db.scalars(
        select(PurchaseOrderLine).where(PurchaseOrderLine.source_record_id == "ERP-PO-UNK")
    ).one_or_none() is None


def test_invalid_timestamp_rejected(db, fixtures_dir):
    _load_ref(db, fixtures_dir)
    row = {
        "source_record_id": "ERP-PO-BADTS", "po_number": "PO-BAD", "line_number": 1,
        "supplier_code": "SUP-07", "location_code": "MUM-014", "item_code": "ERP-SKU-001",
        "ordered_qty": 10, "ordered_uom": "EA", "status": "OPEN",
        "expected_receipt_by": "2026-10-15 12:00:00",  # naive, no offset
    }
    result = ImportService(db).import_dataset("purchase_order_lines", [row])
    db.commit()
    assert result.rows_rejected == 1
    assert "expected_receipt_by" in result.rows[0].reason


def test_negative_quantity_rejected(db, fixtures_dir):
    _load_ref(db, fixtures_dir)
    row = {
        "source_record_id": "ERP-PO-NEG", "po_number": "PO-NEG", "line_number": 1,
        "supplier_code": "SUP-07", "location_code": "MUM-014", "item_code": "ERP-SKU-001",
        "ordered_qty": -5, "ordered_uom": "EA", "status": "OPEN",
        "expected_receipt_by": "2026-10-15T12:00:00+05:30",
    }
    result = ImportService(db).import_dataset("purchase_order_lines", [row])
    db.commit()
    assert result.rows_rejected == 1


# -- AI boundary ------------------------------------------------------------
def _pkg(refs=("ERP-PO501-L1", "WMS-GRN501-A-L1")):
    return EvidencePackage(
        exception_public_id="EXC-1", type="RECEIPT_SHORTFALL", po_number="PO-501",
        canonical_product_id="SKU-001", canonical_location_id="STORE-014",
        expected_qty_base=100, received_qty_base=92, variance_qty_base=8, base_uom="EA",
        evidence_refs=list(refs), known_facts="...", unknown_factors="...",
    )


def test_null_provider_returns_valid_summary():
    summary = NullAIProvider().summarize(_pkg())
    assert summary is not None
    assert summary.cause_asserted is False


def test_hallucinated_evidence_is_rejected():
    pkg = _pkg()
    bad = InvestigationSummary(
        headline="x", narrative="y", suggested_next_steps=[],
        cited_evidence_refs=["WMS-GHOST-999"],  # not in the package
    )
    try:
        validate_summary(pkg, bad)
        assert False, "should have raised"
    except AIValidationError:
        pass


def test_cause_assertion_is_rejected():
    pkg = _pkg()
    bad = InvestigationSummary(
        headline="x", narrative="supplier is at fault", suggested_next_steps=[],
        cited_evidence_refs=list(pkg.evidence_refs), cause_asserted=True,
    )
    try:
        validate_summary(pkg, bad)
        assert False, "should have raised"
    except AIValidationError:
        pass


def test_safe_summarize_degrades_gracefully_on_failure():
    class Broken:
        def summarize(self, package):
            raise TimeoutError("model timed out")

    # Any provider failure returns None; the app stays functional.
    assert safe_summarize(Broken(), _pkg()) is None
