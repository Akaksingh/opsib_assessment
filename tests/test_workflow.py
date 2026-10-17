"""Workflow/lifecycle tests (Task D minimum tests #5).

Covers invalid transitions, repeated completion requests (retry safety), audit
history, and the resolution/verification gate.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import InvalidTransitionError, ValidationError
from app.db.models import ExceptionCase, ExceptionStatus, Task, TaskEvent, TaskStatus
from app.pipeline import load_fixtures, run_detection
from app.workflows.service import TaskService


def _seed_one_task(db, fixtures_dir):
    load_fixtures(db, fixtures_dir)
    db.commit()
    run_detection(db)
    db.commit()
    svc = TaskService(db)
    exc = db.scalars(
        select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")
    ).one()
    task = svc.create_task_for_exception(exc)
    db.commit()
    return svc, task, exc


def test_happy_path_lifecycle(db, fixtures_dir):
    svc, task, exc = _seed_one_task(db, fixtures_dir)
    assert task.status == TaskStatus.ASSIGNED  # routed on creation

    svc.transition(task, "acknowledge", actor="HR-102")
    svc.transition(task, "start", actor="HR-102")
    svc.transition(task, "complete", actor="HR-102",
                   resolution_code="RECEIPT_CORRECTED",
                   resolution_evidence_ref="DOC-123")
    svc.transition(task, "submit_verification", actor="HR-102")
    svc.transition(task, "verify", actor="HR-201")
    db.commit()

    assert task.status == TaskStatus.VERIFIED
    # Verification resolved the parent exception with the resolution code.
    db.refresh(exc)
    assert exc.status == ExceptionStatus.RESOLVED
    assert exc.resolution_code == "RECEIPT_CORRECTED"


def test_invalid_transition_rejected(db, fixtures_dir):
    svc, task, _ = _seed_one_task(db, fixtures_dir)
    # Cannot complete a task that was never started.
    with pytest.raises(InvalidTransitionError):
        svc.transition(task, "complete", actor="HR-102")


def test_unknown_action_rejected(db, fixtures_dir):
    svc, task, _ = _seed_one_task(db, fixtures_dir)
    with pytest.raises(InvalidTransitionError):
        svc.transition(task, "teleport", actor="HR-102")


def test_repeated_completion_is_idempotent(db, fixtures_dir):
    """Retry safety: a client that times out and retries 'complete' gets the same
    terminal state, not an error or a double effect."""
    svc, task, _ = _seed_one_task(db, fixtures_dir)
    svc.transition(task, "acknowledge", actor="HR-102")
    svc.transition(task, "start", actor="HR-102")
    svc.transition(task, "complete", actor="HR-102", idempotency_key="k1")
    db.commit()
    assert task.status == TaskStatus.COMPLETED

    events_before = db.scalar(
        select(__import__("sqlalchemy").func.count()).select_from(TaskEvent)
        .where(TaskEvent.task_id == task.id)
    )
    # Retry the same completion.
    svc.transition(task, "complete", actor="HR-102", idempotency_key="k1")
    db.commit()
    assert task.status == TaskStatus.COMPLETED
    events_after = db.scalar(
        select(__import__("sqlalchemy").func.count()).select_from(TaskEvent)
        .where(TaskEvent.task_id == task.id)
    )
    # No new transition event recorded for the no-op retry.
    assert events_after == events_before


def test_verify_requires_resolution_evidence(db, fixtures_dir):
    svc, task, _ = _seed_one_task(db, fixtures_dir)
    svc.transition(task, "acknowledge", actor="HR-102")
    svc.transition(task, "start", actor="HR-102")
    svc.transition(task, "complete", actor="HR-102")  # no resolution code
    svc.transition(task, "submit_verification", actor="HR-102")
    with pytest.raises(ValidationError):
        svc.transition(task, "verify", actor="HR-201")


def test_audit_history_records_every_transition(db, fixtures_dir):
    svc, task, _ = _seed_one_task(db, fixtures_dir)
    svc.transition(task, "acknowledge", actor="HR-102", reason="seen it")
    db.commit()

    events = db.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
    ).all()
    types = [e.event_type for e in events]
    assert "CREATED" in types
    assert "ASSIGNMENT" in types
    assert "TRANSITION" in types
    ack = [e for e in events if e.to_status == TaskStatus.ACKNOWLEDGED.value][0]
    assert ack.actor == "HR-102"
    assert ack.reason == "seen it"


def test_reassignment_keeps_single_exception(db, fixtures_dir):
    svc, task, exc = _seed_one_task(db, fixtures_dir)
    exc_count_before = db.scalar(
        select(__import__("sqlalchemy").func.count()).select_from(ExceptionCase))
    # Reassign to the manager (eligible store_manager would fail role check here, so
    # reassigning to another receiving associate is what a valid policy allows).
    with pytest.raises(Exception):
        # HR-101 is OFF_SHIFT -> ineligible -> refused (never silently assigned).
        svc.reassign(task, new_assignee_id="HR-101", reason="cover", actor="HR-201")
    db.rollback()
    exc_count_after = db.scalar(
        select(__import__("sqlalchemy").func.count()).select_from(ExceptionCase))
    assert exc_count_before == exc_count_after
