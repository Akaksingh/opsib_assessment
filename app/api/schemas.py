"""API request/response models (the HTTP contract)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


# -- imports ----------------------------------------------------------------
class ImportRowOut(BaseModel):
    row_number: int
    source_record_id: Optional[str]
    outcome: str
    reason: Optional[str] = None


class ImportResultOut(BaseModel):
    import_id: int
    dataset: str
    status: str
    counts: dict[str, int]
    rows: list[ImportRowOut]


# -- data quality -----------------------------------------------------------
class DataQualityItem(BaseModel):
    import_id: int
    dataset: str
    row_number: int
    source_record_id: Optional[str]
    outcome: str
    reason: Optional[str]


class DataQualityOut(BaseModel):
    unresolved: list[DataQualityItem]
    total: int


# -- exceptions -------------------------------------------------------------
class ExceptionOut(BaseModel):
    public_id: str
    case_key: str
    type: str
    status: str
    rule_version: str
    mapping_version: str
    po_number: str
    po_line_number: int
    canonical_product_id: str
    canonical_location_id: str
    expected_qty_base: float
    received_qty_base: float
    variance_qty_base: float
    base_uom: str
    evidence_refs: list[str]
    explanation: str
    resolution_code: Optional[str]
    detected_at: Optional[str]


class SourceRecordOut(BaseModel):
    kind: str  # "po_line" | "receipt_line"
    source_record_id: str
    detail: dict[str, Any]


class ExceptionDetailOut(ExceptionOut):
    source_records: list[SourceRecordOut]
    tasks: list["TaskOut"]


# -- tasks ------------------------------------------------------------------
class TaskEventOut(BaseModel):
    event_type: str
    from_status: Optional[str]
    to_status: Optional[str]
    actor: str
    reason: Optional[str]
    detail: Optional[str]
    created_at: Optional[str]


class TaskOut(BaseModel):
    public_id: str
    exception_public_id: Optional[str] = None
    status: str
    canonical_location_id: str
    required_role: str
    assignee_employee_id: Optional[str]
    assignment_reason: Optional[str]
    due_at: Optional[str]
    resolution_code: Optional[str]


class TaskDetailOut(TaskOut):
    events: list[TaskEventOut]


# -- task actions -----------------------------------------------------------
class TransitionIn(BaseModel):
    actor: str
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None


class CompleteIn(TransitionIn):
    resolution_code: Optional[str] = None
    resolution_evidence_ref: Optional[str] = None


class ReassignIn(BaseModel):
    actor: str
    new_assignee_id: str
    reason: str


ExceptionDetailOut.model_rebuild()
