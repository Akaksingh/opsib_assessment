"""Idempotency tests (Task D minimum tests #3).

Importing the exact same files twice, and re-running detection and task creation,
must not change the business result: no double-counted stock, no duplicate
exceptions, no duplicate tasks.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import (
    ExceptionCase,
    GoodsReceiptLine,
    PurchaseOrderLine,
    RowOutcome,
    Task,
)
from app.ingestion.csv_reader import read_csv_rows
from app.ingestion.service import ImportService
from app.pipeline import create_tasks, load_fixtures, run_detection


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_reimport_does_not_double_count(db, fixtures_dir):
    load_fixtures(db, fixtures_dir)
    db.commit()
    po_1 = _count(db, PurchaseOrderLine)
    rc_1 = _count(db, GoodsReceiptLine)

    # Import everything a second time.
    load_fixtures(db, fixtures_dir)
    db.commit()
    po_2 = _count(db, PurchaseOrderLine)
    rc_2 = _count(db, GoodsReceiptLine)

    assert po_1 == po_2
    assert rc_1 == rc_2


def test_identical_duplicate_row_is_marked_duplicate(db, fixtures_dir):
    # The PO file contains an identical duplicate row for PO-506; it must load once.
    rows = read_csv_rows(fixtures_dir / "product_aliases.csv")
    ImportService(db).import_dataset("product_aliases", rows)
    rows = read_csv_rows(fixtures_dir / "location_aliases.csv")
    ImportService(db).import_dataset("location_aliases", rows)
    po_rows = read_csv_rows(fixtures_dir / "purchase_order_lines.csv")
    result = ImportService(db).import_dataset("purchase_order_lines", po_rows)
    db.commit()

    # 7 input rows, one of which is an exact duplicate of PO-506.
    assert result.rows_total == 7
    assert result.rows_duplicate == 1
    assert result.rows_accepted == 6
    # Only 6 canonical PO lines persisted.
    assert _count(db, PurchaseOrderLine) == 6


def test_rerun_detection_and_tasks_is_stable(db, fixtures_dir):
    load_fixtures(db, fixtures_dir)
    db.commit()
    run_detection(db)
    db.commit()
    create_tasks(db)
    db.commit()

    exc_1 = _count(db, ExceptionCase)
    task_1 = _count(db, Task)

    # Run the whole derivation again.
    run_detection(db)
    db.commit()
    create_tasks(db)
    db.commit()

    assert _count(db, ExceptionCase) == exc_1
    assert _count(db, Task) == task_1


def test_conflicting_duplicate_id_is_flagged_not_overwritten(db, fixtures_dir):
    # Load reference + one PO row, then re-send the SAME source id with a changed qty.
    for ds in ("product_aliases", "location_aliases"):
        ImportService(db).import_dataset(ds, read_csv_rows(fixtures_dir / f"{ds}.csv"))
    db.commit()

    row = {
        "source_record_id": "ERP-PO999-L1", "po_number": "PO-999", "line_number": 1,
        "supplier_code": "SUP-07", "location_code": "MUM-014", "item_code": "ERP-SKU-001",
        "ordered_qty": 10, "ordered_uom": "EA", "status": "OPEN",
        "expected_receipt_by": "2026-10-15T12:00:00+05:30",
    }
    r1 = ImportService(db).import_dataset("purchase_order_lines", [row])
    db.commit()
    assert r1.rows_accepted == 1

    conflicting = dict(row, ordered_qty=999)  # same source id, changed content
    r2 = ImportService(db).import_dataset("purchase_order_lines", [conflicting])
    db.commit()
    assert r2.rows_conflict == 1
    assert r2.rows_accepted == 0

    # The original value is preserved (not overwritten).
    po = db.scalars(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.source_record_id == "ERP-PO999-L1")
    ).one()
    assert po.ordered_qty_base == 10
