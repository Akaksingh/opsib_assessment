"""Typed row schemas for each dataset (Task A — typed validation).

Pydantic does the field-level validation: required columns, types, enums and
explicit timezone-aware timestamp parsing. A row that fails here becomes a
REJECTED import row with a human-readable reason — it is never silently dropped.

These schemas validate *shape and field semantics only*. Identity mapping and UOM
conversion happen later in the ingestion loaders via the Resolver, keeping
source-specific parsing separate from canonical business state.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.timeutils import parse_aware


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class PurchaseOrderLineIn(_Base):
    source_record_id: str
    po_number: str
    line_number: int
    supplier_code: str | None = None
    location_code: str
    item_code: str
    ordered_qty: float
    ordered_uom: str
    status: str
    expected_receipt_by: datetime

    @field_validator("expected_receipt_by", mode="before")
    @classmethod
    def _parse_deadline(cls, v):
        if isinstance(v, datetime):
            return v
        return parse_aware(str(v))

    @field_validator("ordered_qty")
    @classmethod
    def _qty_nonneg(cls, v):
        if v < 0:
            raise ValueError("ordered_qty must be >= 0")
        return v

    @field_validator("status")
    @classmethod
    def _status_known(cls, v):
        allowed = {"OPEN", "CANCELLED", "CLOSED"}
        if v.upper() not in allowed:
            raise ValueError(f"unknown PO status {v!r}; expected one of {sorted(allowed)}")
        return v.upper()


class GoodsReceiptLineIn(_Base):
    source_record_id: str
    receipt_number: str
    receipt_line: int
    po_number: str
    po_line_number: int
    location_code: str
    product_ref: str
    accepted_qty: float
    uom: str
    received_at: datetime
    posting_status: str

    @field_validator("received_at", mode="before")
    @classmethod
    def _parse_received(cls, v):
        if isinstance(v, datetime):
            return v
        return parse_aware(str(v))

    @field_validator("accepted_qty")
    @classmethod
    def _qty_nonneg(cls, v):
        if v < 0:
            raise ValueError("accepted_qty must be >= 0")
        return v

    @field_validator("posting_status")
    @classmethod
    def _status_known(cls, v):
        allowed = {"POSTED", "DRAFT", "REVERSED"}
        if v.upper() not in allowed:
            raise ValueError(f"unknown posting_status {v!r}; expected one of {sorted(allowed)}")
        return v.upper()


class ProductAliasIn(_Base):
    source_system: str
    external_item_id: str
    canonical_product_id: str
    source_uom: str
    base_uom: str
    to_base_factor: float

    @field_validator("to_base_factor")
    @classmethod
    def _factor_positive(cls, v):
        if v <= 0:
            raise ValueError("to_base_factor must be > 0")
        return v

    @field_validator("source_system")
    @classmethod
    def _sys_upper(cls, v):
        return v.upper()


class LocationAliasIn(_Base):
    source_system: str
    external_location_id: str
    canonical_location_id: str
    region_id: str | None = None

    @field_validator("source_system")
    @classmethod
    def _sys_upper(cls, v):
        return v.upper()


class EmployeeIn(_Base):
    source_employee_id: str
    display_name: str | None = None
    employment_status: str
    role: str
    location_code: str | None = None
    manager_employee_id: str | None = None
    region_id: str | None = None

    @field_validator("employment_status")
    @classmethod
    def _emp_status(cls, v):
        allowed = {"ACTIVE", "INACTIVE", "TERMINATED", "ON_LEAVE"}
        if v.upper() not in allowed:
            raise ValueError(f"unknown employment_status {v!r}")
        return v.upper()

    @field_validator("location_code", "manager_employee_id", "region_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None


class WorkforceStatusIn(_Base):
    source_employee_id: str
    availability_status: str
    shift_start: datetime | None = None
    shift_end: datetime | None = None
    observed_at: datetime

    @field_validator("shift_start", "shift_end", "observed_at", mode="before")
    @classmethod
    def _parse_times(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, datetime):
            return v
        return parse_aware(str(v))

    @field_validator("availability_status")
    @classmethod
    def _avail_status(cls, v):
        allowed = {"ON_SHIFT", "OFF_SHIFT", "ON_BREAK"}
        if v.upper() not in allowed:
            raise ValueError(f"unknown availability_status {v!r}")
        return v.upper()


# Registry mapping dataset name -> (schema, loader key). Adding a new retailer's
# export means adding a schema + loader here, not touching the detector.
DATASET_SCHEMAS = {
    "purchase_order_lines": PurchaseOrderLineIn,
    "goods_receipt_lines": GoodsReceiptLineIn,
    "product_aliases": ProductAliasIn,
    "location_aliases": LocationAliasIn,
    "employees": EmployeeIn,
    "workforce_status": WorkforceStatusIn,
}
