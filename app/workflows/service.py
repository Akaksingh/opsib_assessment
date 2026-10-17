"""Task orchestration: creation+routing, transitions, reassignment, audit (Task C).

This service owns the business layer's write path for tasks. It:
  * creates exactly one task per (exception, purpose) using an idempotency key,
    so two workers reacting to the same exception cannot create two tasks;
  * routes the task to an eligible assignee via the pure eligibility logic, and
    records *why* that person/queue was chosen;
  * drives state through the server-enforced lifecycle only;
  * writes an append-only TaskEvent for every transition and assignment change.

Retry safety (Task C design question): task-changing endpoints accept an
idempotency key. A completion request that is retried after a client timeout
finds the task already COMPLETED and returns the same result instead of erroring
or double-applying — the transition is effectively idempotent on the terminal
state it targets.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.idgen import new_public_id
from app.db.models import (
    Employee,
    ExceptionCase,
    ExceptionStatus,
    Task,
    TaskEvent,
    TaskStatus,
    WorkforceStatus,
)
from app.routing.eligibility import EmployeeView, select_assignee
from app.workflows.lifecycle import next_state, target_state

# Resolution codes that count as a valid, verifiable resolution of a shortfall.
VALID_RESOLUTION_CODES = {
    "RECEIPT_CORRECTED",      # a missing/unposted receipt was found and posted
    "SUPPLIER_CONFIRMED_SHORT",  # supplier confirmed short-ship (evidence attached)
    "WRITTEN_OFF",            # authorized write-off with evidence
    "FALSE_POSITIVE",         # mapping/timing error; no real shortfall
}


class TaskService:
    def __init__(self, session: Session, tenant_id: Optional[str] = None):
        self.session = session
        self.settings = get_settings()
        self.tenant_id = tenant_id or self.settings.default_tenant

    # -- employee snapshot --------------------------------------------------
    def _employee_views(self) -> list[EmployeeView]:
        emps = self.session.scalars(
            select(Employee).where(Employee.tenant_id == self.tenant_id)
        ).all()
        wf = {
            w.source_employee_id: w
            for w in self.session.scalars(
                select(WorkforceStatus).where(WorkforceStatus.tenant_id == self.tenant_id)
            ).all()
        }
        views = []
        for e in emps:
            w = wf.get(e.source_employee_id)
            views.append(
                EmployeeView(
                    source_employee_id=e.source_employee_id,
                    employment_status=e.employment_status,
                    role=e.role,
                    canonical_location_id=e.canonical_location_id,
                    region_id=e.region_id,
                    availability_status=w.availability_status if w else None,
                    observed_at=w.observed_at if w else None,
                )
            )
        return views

    # -- creation + routing -------------------------------------------------
    def create_task_for_exception(
        self,
        exception: ExceptionCase,
        *,
        purpose: str = "investigate_shortfall",
        required_role: str = "receiving_associate",
        due_hours: int = 24,
        actor: str = "system",
    ) -> Task:
        """Create (or return the existing) task for an exception, then route it.

        Idempotent on (tenant, idempotency_key), where the key is derived from the
        exception's stable case_key and the task purpose. Called again, it returns
        the same task rather than creating a duplicate.
        """
        idem_key = f"{exception.case_key}::{purpose}"
        existing = self.session.scalars(
            select(Task).where(
                Task.tenant_id == self.tenant_id, Task.idempotency_key == idem_key
            )
        ).one_or_none()
        if existing is not None:
            return existing

        task = Task(
            tenant_id=self.tenant_id,
            public_id=new_public_id("TASK"),
            idempotency_key=idem_key,
            exception_id=exception.id,
            status=TaskStatus.CREATED,
            canonical_location_id=exception.canonical_location_id,
            required_role=required_role,
            due_at=self.settings.frozen_eval_time + timedelta(hours=due_hours),
        )
        self.session.add(task)
        self.session.flush()
        self._event(task, "CREATED", None, TaskStatus.CREATED.value, actor,
                    reason=f"task created for exception {exception.public_id}")

        # Route it now.
        self._route(task, actor=actor)
        return task

    def _route(self, task: Task, *, actor: str) -> None:
        decision = select_assignee(
            self._employee_views(),
            required_role=task.required_role,
            required_location=task.canonical_location_id,
            frozen_now=self.settings.frozen_eval_time,
            require_on_shift=self.settings.require_on_shift,
            freshness_minutes=self.settings.workforce_freshness_minutes,
        )
        trace = json.dumps(
            [
                {"employee_id": c.employee_id, "eligible": c.eligible, "reasons": c.reasons}
                for c in decision.evaluated
            ]
        )
        if decision.assignee_employee_id is None:
            task.status = TaskStatus.NEEDS_MANUAL_ASSIGNMENT
            task.assignment_reason = decision.reason
            self._event(
                task, "ASSIGNMENT", TaskStatus.CREATED.value,
                TaskStatus.NEEDS_MANUAL_ASSIGNMENT.value, actor,
                reason=decision.reason, detail=trace,
            )
        else:
            from_status = task.status.value
            task.assignee_employee_id = decision.assignee_employee_id
            task.assignment_reason = decision.reason
            task.status = TaskStatus.ASSIGNED
            self._event(
                task, "ASSIGNMENT", from_status, TaskStatus.ASSIGNED.value, actor,
                reason=decision.reason, detail=trace,
            )
        self.session.flush()

    # -- transitions --------------------------------------------------------
    def transition(
        self,
        task: Task,
        action: str,
        *,
        actor: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        resolution_code: Optional[str] = None,
        resolution_evidence_ref: Optional[str] = None,
    ) -> Task:
        """Apply a lifecycle action. Safe to retry (see module docstring)."""
        # Retry safety first: if the task already sits in the state this action
        # targets, a repeat (e.g. a client that timed out and retried 'complete')
        # is a no-op success — never an illegal-transition error or a double effect.
        if task.status == target_state(action):
            return task

        # Otherwise enforce the transition strictly from the current state.
        target = next_state(action, task.status)

        # 'complete' and 'verify' may carry resolution evidence.
        if action == "complete":
            if resolution_code is not None and resolution_code not in VALID_RESOLUTION_CODES:
                raise ValidationError(
                    f"unknown resolution_code {resolution_code!r}",
                    details={"allowed": sorted(VALID_RESOLUTION_CODES)},
                )
            if resolution_code:
                task.resolution_code = resolution_code
                task.resolution_evidence_ref = resolution_evidence_ref

        if action == "verify":
            self._verify_exception(task, actor=actor)

        from_status = task.status.value
        task.status = target
        self._event(task, "TRANSITION", from_status, target.value, actor,
                    reason=reason, detail=_idem_detail(idempotency_key))
        self.session.flush()
        return task

    def _verify_exception(self, task: Task, *, actor: str) -> None:
        """Only mark the parent exception RESOLVED when verification criteria are met:
        the task carries a valid resolution code and an evidence reference."""
        if not task.resolution_code:
            raise ValidationError(
                "cannot verify: task has no resolution_code",
                details={"task": task.public_id},
            )
        if not task.resolution_evidence_ref:
            raise ValidationError(
                "cannot verify: task has no resolution_evidence_ref",
                details={"task": task.public_id},
            )
        exc = self.session.get(ExceptionCase, task.exception_id)
        if exc is None:
            raise NotFoundError("parent exception not found")
        exc.status = ExceptionStatus.RESOLVED
        exc.resolution_code = task.resolution_code
        exc.resolution_evidence_ref = task.resolution_evidence_ref

    # -- reassignment -------------------------------------------------------
    def reassign(
        self,
        task: Task,
        *,
        new_assignee_id: str,
        reason: str,
        actor: str,
    ) -> Task:
        """Reassign to a specific employee, enforcing eligibility. Does not change
        the exception. Records who made the change and why."""
        if task.status in {TaskStatus.VERIFIED}:
            raise ConflictError("cannot reassign a verified task")

        views = {v.source_employee_id: v for v in self._employee_views()}
        target = views.get(new_assignee_id)
        if target is None:
            raise NotFoundError(f"employee {new_assignee_id} not found")

        from app.routing.eligibility import evaluate_employee

        chk = evaluate_employee(
            target,
            required_role=task.required_role,
            required_location=task.canonical_location_id,
            frozen_now=self.settings.frozen_eval_time,
            require_on_shift=self.settings.require_on_shift,
            freshness_minutes=self.settings.workforce_freshness_minutes,
        )
        if not chk.eligible:
            # Allow a manager-role fallback reassignment explicitly; otherwise refuse.
            raise ConflictError(
                f"{new_assignee_id} is not eligible for this task",
                details={"reasons": chk.reasons},
            )

        prev = task.assignee_employee_id
        task.assignee_employee_id = new_assignee_id
        task.assignment_reason = reason
        if task.status == TaskStatus.NEEDS_MANUAL_ASSIGNMENT:
            task.status = TaskStatus.ASSIGNED
        self._event(
            task, "REASSIGNMENT", task.status.value, task.status.value, actor,
            reason=reason,
            detail=json.dumps({"from": prev, "to": new_assignee_id}),
        )
        self.session.flush()
        return task

    # -- audit --------------------------------------------------------------
    def _event(self, task, event_type, from_status, to_status, actor, reason=None, detail=None):
        self.session.add(
            TaskEvent(
                task_id=task.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                actor=actor,
                reason=reason,
                detail=detail,
            )
        )


def _idem_detail(idempotency_key: Optional[str]) -> Optional[str]:
    if idempotency_key is None:
        return None
    return json.dumps({"idempotency_key": idempotency_key})
