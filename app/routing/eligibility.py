"""Employee eligibility and assignee selection (Task C).

Pure decision logic, independent of persistence, so it is directly testable.

Eligibility invariants (the safety rules we never violate):
  1. Employment status ACTIVE is mandatory and always wins. An INACTIVE employee
     who appears ON_SHIFT in the workforce export is NOT eligible (HR-103).
  2. If the policy requires on-shift (default), the employee must be ON_SHIFT and
     the workforce sample must be fresh enough (within a configured window of the
     frozen evaluation time). An ACTIVE but OFF_SHIFT employee is NOT eligible
     (HR-101).
  3. Role + canonical location must match the task's requirement (same store).
  4. Fallback: if no eligible primary-role employee exists at the location, fall
     back to an eligible store_manager at that location (HR-401 at STORE-015).
  5. If nothing eligible is found, the task is left as NEEDS_MANUAL_ASSIGNMENT.
     We never silently assign an ineligible person.

Selection is deterministic: among equally-eligible candidates we pick the lowest
employee id, so results are reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence


@dataclass(frozen=True)
class EmployeeView:
    source_employee_id: str
    employment_status: str
    role: str
    canonical_location_id: Optional[str]
    region_id: Optional[str]
    availability_status: Optional[str]       # from workforce snapshot, may be None
    observed_at: Optional[datetime]          # when the shift sample was taken


@dataclass(frozen=True)
class EligibilityCheck:
    employee_id: str
    eligible: bool
    reasons: list[str]  # why eligible, or every reason it is not


@dataclass(frozen=True)
class AssignmentDecision:
    assignee_employee_id: Optional[str]
    required_role: str
    reason: str
    # Full per-candidate trace, persisted for auditability.
    evaluated: list[EligibilityCheck]
    used_fallback: bool = False


def evaluate_employee(
    emp: EmployeeView,
    *,
    required_role: str,
    required_location: str,
    frozen_now: datetime,
    require_on_shift: bool,
    freshness_minutes: int,
) -> EligibilityCheck:
    reasons: list[str] = []
    eligible = True

    if emp.employment_status != "ACTIVE":
        eligible = False
        reasons.append(f"employment_status is {emp.employment_status}, not ACTIVE")

    if emp.role != required_role:
        eligible = False
        reasons.append(f"role is {emp.role}, not {required_role}")

    if emp.canonical_location_id != required_location:
        eligible = False
        loc = emp.canonical_location_id or "none"
        reasons.append(f"canonical_location is {loc}, not {required_location}")

    if require_on_shift:
        if emp.availability_status != "ON_SHIFT":
            eligible = False
            avail = emp.availability_status or "unknown"
            reasons.append(f"availability is {avail}, not ON_SHIFT")
        elif emp.observed_at is None:
            eligible = False
            reasons.append("workforce sample has no observed_at timestamp")
        else:
            age = frozen_now - emp.observed_at
            if age > timedelta(minutes=freshness_minutes) or age < timedelta(0):
                eligible = False
                reasons.append(
                    f"workforce sample is stale (observed {emp.observed_at.isoformat()}, "
                    f"older than {freshness_minutes} min before evaluation time)"
                )

    if eligible:
        reasons.append(
            f"ACTIVE {required_role} at {required_location}"
            + (" and ON_SHIFT with a fresh sample" if require_on_shift else "")
        )
    return EligibilityCheck(employee_id=emp.source_employee_id, eligible=eligible, reasons=reasons)


def select_assignee(
    employees: Sequence[EmployeeView],
    *,
    required_role: str,
    required_location: str,
    frozen_now: datetime,
    require_on_shift: bool,
    freshness_minutes: int,
    fallback_role: Optional[str] = "store_manager",
) -> AssignmentDecision:
    """Pick the best eligible assignee, or return a no-assignee decision."""

    evaluated: list[EligibilityCheck] = []

    # Primary role at the location.
    primary_eligible = []
    for emp in employees:
        chk = evaluate_employee(
            emp,
            required_role=required_role,
            required_location=required_location,
            frozen_now=frozen_now,
            require_on_shift=require_on_shift,
            freshness_minutes=freshness_minutes,
        )
        evaluated.append(chk)
        if chk.eligible:
            primary_eligible.append(emp.source_employee_id)

    if primary_eligible:
        chosen = sorted(primary_eligible)[0]
        return AssignmentDecision(
            assignee_employee_id=chosen,
            required_role=required_role,
            reason=f"selected eligible {required_role} {chosen} at {required_location}",
            evaluated=evaluated,
        )

    # Fallback role (e.g. store_manager) at the same location.
    if fallback_role and fallback_role != required_role:
        fallback_eligible = []
        for emp in employees:
            chk = evaluate_employee(
                emp,
                required_role=fallback_role,
                required_location=required_location,
                frozen_now=frozen_now,
                require_on_shift=require_on_shift,
                freshness_minutes=freshness_minutes,
            )
            evaluated.append(chk)
            if chk.eligible:
                fallback_eligible.append(emp.source_employee_id)
        if fallback_eligible:
            chosen = sorted(fallback_eligible)[0]
            return AssignmentDecision(
                assignee_employee_id=chosen,
                required_role=fallback_role,
                reason=(
                    f"no eligible {required_role} at {required_location}; "
                    f"fell back to eligible {fallback_role} {chosen}"
                ),
                evaluated=evaluated,
                used_fallback=True,
            )

    return AssignmentDecision(
        assignee_employee_id=None,
        required_role=required_role,
        reason=(
            f"no eligible {required_role} or fallback at {required_location}; "
            "task left for manual assignment"
        ),
        evaluated=evaluated,
    )
