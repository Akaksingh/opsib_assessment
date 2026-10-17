"""Typed application errors.

Every error carries a machine-readable `code` and an HTTP status so the API can
return structured, useful error bodies instead of bare 500s. Business-rule
violations (bad state transitions, ineligible assignees) are first-class errors,
not generic exceptions.
"""
from __future__ import annotations

from typing import Any, Optional


class OpsibError(Exception):
    """Base class for all expected application errors."""

    code: str = "opsib_error"
    http_status: int = 400

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class NotFoundError(OpsibError):
    code = "not_found"
    http_status = 404


class ValidationError(OpsibError):
    code = "validation_error"
    http_status = 422


class ConflictError(OpsibError):
    """A source ID reappeared with changed business content, or a uniqueness
    invariant would be violated. The caller must resolve it explicitly."""

    code = "conflict"
    http_status = 409


class InvalidTransitionError(OpsibError):
    """A task state change the server refuses to make."""

    code = "invalid_transition"
    http_status = 409


class IneligibleAssigneeError(OpsibError):
    code = "ineligible_assignee"
    http_status = 409
