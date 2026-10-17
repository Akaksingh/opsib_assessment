"""Server-enforced task state machine (Task C).

The client never sets status directly. It requests an *action*; the server maps
the action to a transition and refuses any transition not in this table. This is
the single source of truth for "what can happen next".

  CREATED ──assign──▶ ASSIGNED ──acknowledge──▶ ACKNOWLEDGED ──start──▶ IN_PROGRESS
  IN_PROGRESS ──complete──▶ COMPLETED ──submit_verification──▶ PENDING_VERIFICATION
  PENDING_VERIFICATION ──verify──▶ VERIFIED
  PENDING_VERIFICATION ──reject──▶ REJECTED ──reopen──▶ ASSIGNED
  (ASSIGNED|ACKNOWLEDGED|IN_PROGRESS) ──block──▶ BLOCKED ──unblock──▶ (prior-ish) ASSIGNED
  CREATED ──mark_manual──▶ NEEDS_MANUAL_ASSIGNMENT ──assign──▶ ASSIGNED

Reassignment is handled separately (it changes assignee, not status) and is legal
from any non-terminal state.
"""
from __future__ import annotations

from app.core.errors import InvalidTransitionError
from app.db.models import TaskStatus

# action -> (allowed_from_states, resulting_state)
TRANSITIONS: dict[str, tuple[set[TaskStatus], TaskStatus]] = {
    "assign": (
        {TaskStatus.CREATED, TaskStatus.NEEDS_MANUAL_ASSIGNMENT, TaskStatus.REJECTED},
        TaskStatus.ASSIGNED,
    ),
    "mark_manual": ({TaskStatus.CREATED}, TaskStatus.NEEDS_MANUAL_ASSIGNMENT),
    "acknowledge": ({TaskStatus.ASSIGNED}, TaskStatus.ACKNOWLEDGED),
    "start": ({TaskStatus.ACKNOWLEDGED, TaskStatus.ASSIGNED}, TaskStatus.IN_PROGRESS),
    "complete": ({TaskStatus.IN_PROGRESS}, TaskStatus.COMPLETED),
    "submit_verification": ({TaskStatus.COMPLETED}, TaskStatus.PENDING_VERIFICATION),
    "verify": ({TaskStatus.PENDING_VERIFICATION}, TaskStatus.VERIFIED),
    "reject": ({TaskStatus.PENDING_VERIFICATION}, TaskStatus.REJECTED),
    "reopen": ({TaskStatus.REJECTED, TaskStatus.BLOCKED}, TaskStatus.ASSIGNED),
    "block": (
        {TaskStatus.ASSIGNED, TaskStatus.ACKNOWLEDGED, TaskStatus.IN_PROGRESS},
        TaskStatus.BLOCKED,
    ),
    "unblock": ({TaskStatus.BLOCKED}, TaskStatus.ASSIGNED),
}

TERMINAL_STATES = {TaskStatus.VERIFIED}


def next_state(action: str, current: TaskStatus) -> TaskStatus:
    """Return the resulting state for an action, or raise InvalidTransitionError.

    This is intentionally strict: an unknown action, or a valid action from a
    disallowed state, is rejected with a clear message rather than guessed.
    """
    if action not in TRANSITIONS:
        raise InvalidTransitionError(
            f"unknown task action {action!r}",
            details={"action": action, "allowed_actions": sorted(TRANSITIONS)},
        )
    allowed_from, result = TRANSITIONS[action]
    if current not in allowed_from:
        raise InvalidTransitionError(
            f"cannot '{action}' a task in state {current.value}",
            details={
                "action": action,
                "current_state": current.value,
                "allowed_from": sorted(s.value for s in allowed_from),
            },
        )
    return result


def target_state(action: str) -> TaskStatus:
    """The state an action aims to reach, independent of the current state.

    Used for retry-safety: if the task is already in this state, repeating the
    action is a no-op rather than an illegal transition. Raises only for an
    unknown action.
    """
    if action not in TRANSITIONS:
        raise InvalidTransitionError(
            f"unknown task action {action!r}",
            details={"action": action, "allowed_actions": sorted(TRANSITIONS)},
        )
    return TRANSITIONS[action][1]


def is_completion_action(action: str) -> bool:
    return action == "complete"
