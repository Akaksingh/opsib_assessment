"""Integration test: load the real fixture pack, verify the six required scenario
outcomes end to end (Task D minimum tests #2)."""
from __future__ import annotations

import json

from sqlalchemy import select

from app.db.models import ExceptionCase
from app.pipeline import load_fixtures, run_detection


def _exceptions_by_po(session):
    rows = session.scalars(select(ExceptionCase)).all()
    return {r.po_number: r for r in rows}


def test_fixture_pack_scenarios(db, fixtures_dir):
    load_fixtures(db, fixtures_dir)
    db.commit()
    summary = run_detection(db)
    db.commit()

    exc = _exceptions_by_po(db)

    # Exactly the three actionable shortfalls exist.
    assert set(exc) == {"PO-501", "PO-505", "PO-506"}

    # PO-501: 100 ordered, 60+32 received -> 8 EA.
    assert exc["PO-501"].expected_qty_base == 100
    assert exc["PO-501"].received_qty_base == 92
    assert exc["PO-501"].variance_qty_base == 8
    assert exc["PO-501"].base_uom == "EA"

    # PO-505: 10 BOX * 12 = 120 expected vs 96 -> 24 EA (UOM conversion).
    assert exc["PO-505"].expected_qty_base == 120
    assert exc["PO-505"].received_qty_base == 96
    assert exc["PO-505"].variance_qty_base == 24

    # PO-506: duplicate receipt source id counted once -> 10 EA (not 0).
    assert exc["PO-506"].expected_qty_base == 20
    assert exc["PO-506"].received_qty_base == 10
    assert exc["PO-506"].variance_qty_base == 10

    # PO-502 (met), PO-503 (future), PO-504 (cancelled) produce NO exception.
    assert "PO-502" not in exc
    assert "PO-503" not in exc
    assert "PO-504" not in exc

    assert summary["created"] == 3
    assert summary["skipped"] == 3


def test_finding_is_traceable_to_source_records(db, fixtures_dir):
    load_fixtures(db, fixtures_dir)
    db.commit()
    run_detection(db)
    db.commit()

    exc = _exceptions_by_po(db)["PO-501"]
    refs = json.loads(exc.evidence_refs)
    # PO line + both receipt source ids are cited.
    assert "ERP-PO501-L1" in refs
    assert "WMS-GRN501-A-L1" in refs
    assert "WMS-GRN501-B-L1" in refs
    assert exc.rule_version == "shortfall-v1"
    assert exc.mapping_version == "mapping-v1"


def test_finding_explanation_is_neutral_about_cause(db, fixtures_dir):
    load_fixtures(db, fixtures_dir)
    db.commit()
    run_detection(db)
    db.commit()

    exc = _exceptions_by_po(db)["PO-505"]
    text = exc.explanation.lower()
    # No root-cause/fault assertion; missing evidence named.
    assert "no cause or fault is asserted" in text
    assert "dispatch" in text
    assert "supplier" in text
