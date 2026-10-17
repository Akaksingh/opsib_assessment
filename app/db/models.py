"""SQLAlchemy ORM models — the persistence boundary.

Layering, deliberately kept separate (assessment Task A.6):

  RAW layer        : exactly what arrived, per source row, keyed by a natural
                     source identity. Idempotent on re-import; conflicts flagged.
  CANONICAL layer  : business state after mapping (identity + UOM normalized).
  BUSINESS layer   : exceptions, tasks, audit — derived, never hand-edited.
  REFERENCE layer  : alias crosswalks (product / location) and employee data.

Key invariants are expressed as database constraints, not just application code,
so a second import or a concurrent worker cannot produce a different result:

  * (tenant_id, source_system, source_record_id) is UNIQUE on every raw table
    -> re-importing the same row is a no-op; it can never double-count.
  * (tenant_id, case_key) is UNIQUE on exceptions
    -> two workers detecting the same shortfall cannot create two cases.
  * exceptions<->tasks are distinct tables (one case may have many tasks).

Every canonical/business row carries provenance: source_system, source_record_id,
import_id and the mapping/rule version used, so any finding traces back to rows.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A timezone-aware datetime that is always stored and returned as UTC.

    SQLite (and some drivers) drop tzinfo. We refuse to persist a naive datetime,
    store UTC, and re-attach UTC on load so downstream code never has to guess.
    On Postgres this maps to TIMESTAMP WITH TIME ZONE.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("refusing to store a naive datetime; make it tz-aware")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class ImportStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class RowOutcome(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"   # identical repeat of an already-loaded source id
    REJECTED = "REJECTED"     # failed validation; reason recorded
    CONFLICT = "CONFLICT"     # same source id, different business content


class ExceptionType(str, enum.Enum):
    RECEIPT_SHORTFALL = "RECEIPT_SHORTFALL"


class ExceptionStatus(str, enum.Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    # Cannot be evaluated from current evidence (stale/missing/conflicting/unmapped).
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"


class TaskStatus(str, enum.Enum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    NEEDS_MANUAL_ASSIGNMENT = "NEEDS_MANUAL_ASSIGNMENT"


# ---------------------------------------------------------------------------
# Imports (Task A.1 — a usable import result, not console output)
# ---------------------------------------------------------------------------
class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    dataset: Mapped[str] = mapped_column(String(64))
    source_filename: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[ImportStatus] = mapped_column(SAEnum(ImportStatus), default=ImportStatus.PENDING)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_accepted: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    rows_conflict: Mapped[int] = mapped_column(Integer, default=0)
    mapping_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)

    rows: Mapped[list["ImportRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ImportRow(Base):
    """Row-level outcome for *every* input row — accepted, duplicate, rejected or
    conflicting — so invalid rows are visible with a reason, never silently dropped."""

    __tablename__ = "import_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)  # 1-based line in the file (excluding header)
    source_record_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[RowOutcome] = mapped_column(SAEnum(RowOutcome))
    reason: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[str | None] = mapped_column(Text)  # JSON of the original row

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")


# ---------------------------------------------------------------------------
# Reference data (crosswalks + roster)
# ---------------------------------------------------------------------------
class ProductAlias(Base):
    __tablename__ = "product_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "external_item_id",
                         name="uq_product_alias"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_system: Mapped[str] = mapped_column(String(16))
    external_item_id: Mapped[str] = mapped_column(String(128))
    canonical_product_id: Mapped[str] = mapped_column(String(64), index=True)
    source_uom: Mapped[str] = mapped_column(String(16))
    base_uom: Mapped[str] = mapped_column(String(16))
    to_base_factor: Mapped[float] = mapped_column(Float)
    import_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))


class LocationAlias(Base):
    __tablename__ = "location_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "external_location_id",
                         name="uq_location_alias"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_system: Mapped[str] = mapped_column(String(16))
    external_location_id: Mapped[str] = mapped_column(String(128))
    canonical_location_id: Mapped[str] = mapped_column(String(64), index=True)
    region_id: Mapped[str | None] = mapped_column(String(64))
    import_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_employee_id", name="uq_employee"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_employee_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(128))
    employment_status: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(64))
    # Raw HRMS location code as supplied; may be empty for region-scoped roles.
    source_location_code: Mapped[str | None] = mapped_column(String(64))
    # Canonical location after mapping; NULL for region-scoped employees.
    canonical_location_id: Mapped[str | None] = mapped_column(String(64), index=True)
    region_id: Mapped[str | None] = mapped_column(String(64), index=True)
    manager_employee_id: Mapped[str | None] = mapped_column(String(64))
    import_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))


class WorkforceStatus(Base):
    """Latest observed shift/availability sample per employee (point-in-time)."""

    __tablename__ = "workforce_status"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_employee_id", name="uq_workforce"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_employee_id: Mapped[str] = mapped_column(String(64))
    availability_status: Mapped[str] = mapped_column(String(32))
    shift_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    shift_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    import_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))


# ---------------------------------------------------------------------------
# Canonical transactional facts
# ---------------------------------------------------------------------------
class PurchaseOrderLine(Base):
    """One canonical PO line. The identical duplicate ERP row collapses here via
    the UNIQUE(source_record_id) constraint — it is counted exactly once."""

    __tablename__ = "po_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "source_record_id",
                         name="uq_po_source"),
        CheckConstraint("ordered_qty_base >= 0", name="ck_po_qty_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)

    # Provenance
    source_system: Mapped[str] = mapped_column(String(16))
    source_record_id: Mapped[str] = mapped_column(String(128))
    import_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    mapping_version: Mapped[str] = mapped_column(String(32))
    # Hash of the business-relevant fields; lets us detect a *conflicting* repeat.
    content_hash: Mapped[str] = mapped_column(String(64))

    # Business identity
    po_number: Mapped[str] = mapped_column(String(64), index=True)
    line_number: Mapped[int] = mapped_column(Integer)
    supplier_code: Mapped[str | None] = mapped_column(String(64))

    # Mapped identity + normalized quantity
    canonical_product_id: Mapped[str] = mapped_column(String(64), index=True)
    canonical_location_id: Mapped[str] = mapped_column(String(64), index=True)
    ordered_qty_base: Mapped[float] = mapped_column(Float)
    base_uom: Mapped[str] = mapped_column(String(16))

    status: Mapped[str] = mapped_column(String(32))  # OPEN / CANCELLED / ...
    expected_receipt_by: Mapped[datetime] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class GoodsReceiptLine(Base):
    """One canonical receipt line. The repeated WMS source id collapses via the
    UNIQUE constraint, so a duplicated receipt is never counted twice."""

    __tablename__ = "receipt_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "source_record_id",
                         name="uq_receipt_source"),
        CheckConstraint("accepted_qty_base >= 0", name="ck_receipt_qty_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)

    source_system: Mapped[str] = mapped_column(String(16))
    source_record_id: Mapped[str] = mapped_column(String(128))
    import_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    mapping_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))

    receipt_number: Mapped[str] = mapped_column(String(64))
    receipt_line: Mapped[int] = mapped_column(Integer)
    po_number: Mapped[str] = mapped_column(String(64), index=True)
    po_line_number: Mapped[int] = mapped_column(Integer)

    canonical_product_id: Mapped[str] = mapped_column(String(64), index=True)
    canonical_location_id: Mapped[str] = mapped_column(String(64), index=True)
    accepted_qty_base: Mapped[float] = mapped_column(Float)
    base_uom: Mapped[str] = mapped_column(String(16))

    posting_status: Mapped[str] = mapped_column(String(32))  # POSTED / ...
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


# ---------------------------------------------------------------------------
# Business layer: exceptions, tasks, audit
# ---------------------------------------------------------------------------
class ExceptionCase(Base):
    """A detected business case. The (tenant, case_key) uniqueness makes detection
    idempotent: re-running the detector updates the existing case, never forks it."""

    __tablename__ = "exceptions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_key", name="uq_exception_case_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    case_key: Mapped[str] = mapped_column(String(128), index=True)  # stable business key
    public_id: Mapped[str] = mapped_column(String(64), unique=True)  # stable external id

    type: Mapped[ExceptionType] = mapped_column(SAEnum(ExceptionType))
    status: Mapped[ExceptionStatus] = mapped_column(SAEnum(ExceptionStatus), default=ExceptionStatus.OPEN)
    rule_version: Mapped[str] = mapped_column(String(32))
    mapping_version: Mapped[str] = mapped_column(String(32))

    po_number: Mapped[str] = mapped_column(String(64), index=True)
    po_line_number: Mapped[int] = mapped_column(Integer)
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64), index=True)

    expected_qty_base: Mapped[float] = mapped_column(Float)
    received_qty_base: Mapped[float] = mapped_column(Float)
    variance_qty_base: Mapped[float] = mapped_column(Float)
    base_uom: Mapped[str] = mapped_column(String(16))

    # JSON array of source_record_ids that contributed to this finding (lineage).
    evidence_refs: Mapped[str] = mapped_column(Text)
    # Neutral narrative: what is known / what is unknown. No root cause asserted.
    explanation: Mapped[str] = mapped_column(Text)

    resolution_code: Mapped[str | None] = mapped_column(String(64))
    resolution_evidence_ref: Mapped[str | None] = mapped_column(String(255))

    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    tasks: Mapped[list["Task"]] = relationship(back_populates="exception")


class Task(Base):
    """Work a person performs for a case. Separate from the exception: one case may
    spawn several tasks, and a task reassigns without forking the exception."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_task_idem"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    # Stable key used to avoid duplicate task creation for the same (case, purpose).
    idempotency_key: Mapped[str] = mapped_column(String(160))

    exception_id: Mapped[int] = mapped_column(ForeignKey("exceptions.id"), index=True)
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus), default=TaskStatus.CREATED)

    canonical_location_id: Mapped[str] = mapped_column(String(64), index=True)
    required_role: Mapped[str] = mapped_column(String(64))

    assignee_employee_id: Mapped[str | None] = mapped_column(String(64), index=True)
    assignment_reason: Mapped[str | None] = mapped_column(Text)

    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    resolution_evidence_ref: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    exception: Mapped[ExceptionCase] = relationship(back_populates="tasks")
    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskEvent.id"
    )


class TaskEvent(Base):
    """Append-only audit trail for a task: every state transition and assignment
    change, who made it, why, and what the state was before/after."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(48))  # TRANSITION / ASSIGNMENT / CREATED ...
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))  # who performed the change
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)  # JSON payload for extra context
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)

    task: Mapped[Task] = relationship(back_populates="events")
