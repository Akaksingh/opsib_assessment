"""Identity and assignment tests (Task D minimum tests #4).

Covers HRMS source-location mapping, the off-shift active employee, the inactive
employee that appears on-shift, and the store-manager fallback.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Employee, Task, TaskStatus
from app.pipeline import create_tasks, load_fixtures, run_detection
from app.routing.eligibility import EmployeeView, evaluate_employee, select_assignee

FROZEN = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)  # 15:00 IST
FRESH = datetime(2026, 10, 16, 9, 20, tzinfo=timezone.utc)   # 14:50 IST, 10 min old


def _emp(eid, status="ACTIVE", role="receiving_associate", loc="STORE-014",
         avail="ON_SHIFT", observed=FRESH):
    return EmployeeView(
        source_employee_id=eid, employment_status=status, role=role,
        canonical_location_id=loc, region_id="REGION-MH",
        availability_status=avail, observed_at=observed,
    )


KW = dict(required_role="receiving_associate", required_location="STORE-014",
          frozen_now=FROZEN, require_on_shift=True, freshness_minutes=120)


def test_active_on_shift_is_eligible():
    chk = evaluate_employee(_emp("HR-102"), **KW)
    assert chk.eligible


def test_off_shift_active_is_not_eligible():
    # HR-101 shape: ACTIVE but OFF_SHIFT.
    chk = evaluate_employee(_emp("HR-101", avail="OFF_SHIFT"), **KW)
    assert not chk.eligible
    assert any("ON_SHIFT" in r for r in chk.reasons)


def test_inactive_but_on_shift_is_not_eligible():
    # HR-103 shape: INACTIVE but the shift export says ON_SHIFT. Employment wins.
    chk = evaluate_employee(_emp("HR-103", status="INACTIVE"), **KW)
    assert not chk.eligible
    assert any("ACTIVE" in r for r in chk.reasons)


def test_stale_workforce_sample_blocks_eligibility():
    stale = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)  # ~4.5h old
    chk = evaluate_employee(_emp("HR-102", observed=stale), **KW)
    assert not chk.eligible
    assert any("stale" in r for r in chk.reasons)


def test_selection_prefers_primary_role():
    decision = select_assignee(
        [_emp("HR-101", avail="OFF_SHIFT"), _emp("HR-102"), _emp("HR-103", status="INACTIVE")],
        fallback_role="store_manager", **KW,
    )
    assert decision.assignee_employee_id == "HR-102"
    assert not decision.used_fallback


def test_fallback_to_store_manager_when_no_receiver():
    # STORE-015 shape: no eligible receiving associate; fall back to the manager.
    kw = dict(KW, required_location="STORE-015")
    decision = select_assignee(
        [_emp("HR-401", role="store_manager", loc="STORE-015")],
        fallback_role="store_manager", **kw,
    )
    assert decision.assignee_employee_id == "HR-401"
    assert decision.used_fallback


def test_no_eligible_and_no_fallback_leaves_manual():
    kw = dict(KW, required_location="STORE-099")
    decision = select_assignee([], fallback_role="store_manager", **kw)
    assert decision.assignee_employee_id is None
    assert "manual" in decision.reason


def test_fixture_routing_end_to_end(db, fixtures_dir):
    load_fixtures(db, fixtures_dir)
    db.commit()

    # HRMS codes (ST-014/ST-015) must be mapped to canonical stores.
    emps = {e.source_employee_id: e for e in db.scalars(select(Employee)).all()}
    assert emps["HR-102"].canonical_location_id == "STORE-014"
    assert emps["HR-401"].canonical_location_id == "STORE-015"
    # Region-scoped employee has no store location.
    assert emps["HR-501"].canonical_location_id is None
    assert emps["HR-501"].region_id == "REGION-MH"

    run_detection(db)
    db.commit()
    create_tasks(db)
    db.commit()

    tasks = db.scalars(select(Task)).all()
    by_loc = {}
    for t in tasks:
        by_loc.setdefault(t.canonical_location_id, []).append(t)

    # STORE-014 shortfall tasks go to HR-102.
    for t in by_loc["STORE-014"]:
        assert t.assignee_employee_id == "HR-102"
        assert t.status == TaskStatus.ASSIGNED
    # STORE-015 (PO-505) falls back to HR-401.
    for t in by_loc["STORE-015"]:
        assert t.assignee_employee_id == "HR-401"
