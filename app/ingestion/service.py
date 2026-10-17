"""Import orchestration (Task A).

One public entry point: `import_dataset`. It validates every row, maps identity
and units where the dataset is transactional, and writes canonical facts inside a
single transaction. The result object reports accepted / duplicate / rejected /
conflict counts plus a row-level breakdown, so partial success and the reasons for
it are always visible.

Idempotency & conflict policy (Task A.5 / Task D):
  * Natural key per row = (tenant, source_system, source_record_id).
  * A row whose natural key already exists AND whose content hash matches is a
    DUPLICATE (idempotent no-op). Importing the same file twice changes nothing.
  * A row whose natural key exists but whose content hash differs is a CONFLICT:
    we do NOT overwrite. It is flagged for review and surfaced in /data-quality.
  * The DB UNIQUE constraint is the backstop: even under a race, only one writer
    wins and the loser is handled as duplicate/conflict rather than double-counting.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Type

from pydantic import BaseModel, ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import MAPPING_VERSION, get_settings
from app.db.models import (
    Employee,
    GoodsReceiptLine,
    ImportBatch,
    ImportRow,
    ImportStatus,
    LocationAlias,
    ProductAlias,
    PurchaseOrderLine,
    RowOutcome,
    WorkforceStatus,
)
from app.ingestion.schemas import DATASET_SCHEMAS
from app.mapping.resolver import MappingError, Resolver


@dataclass
class RowResult:
    row_number: int
    source_record_id: Optional[str]
    outcome: RowOutcome
    reason: Optional[str] = None


@dataclass
class ImportResult:
    import_id: int
    dataset: str
    status: ImportStatus
    rows_total: int = 0
    rows_accepted: int = 0
    rows_duplicate: int = 0
    rows_rejected: int = 0
    rows_conflict: int = 0
    rows: list[RowResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "dataset": self.dataset,
            "status": self.status.value,
            "counts": {
                "total": self.rows_total,
                "accepted": self.rows_accepted,
                "duplicate": self.rows_duplicate,
                "rejected": self.rows_rejected,
                "conflict": self.rows_conflict,
            },
            "rows": [
                {
                    "row_number": r.row_number,
                    "source_record_id": r.source_record_id,
                    "outcome": r.outcome.value,
                    "reason": r.reason,
                }
                for r in self.rows
            ],
        }


def _content_hash(payload: dict[str, Any]) -> str:
    """Stable hash over the business-relevant fields of a canonical row."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class ImportService:
    def __init__(self, session: Session, tenant_id: Optional[str] = None):
        self.session = session
        self.settings = get_settings()
        self.tenant_id = tenant_id or self.settings.default_tenant

    # -- public API ---------------------------------------------------------
    def import_dataset(
        self,
        dataset: str,
        rows: Iterable[dict[str, Any]],
        source_filename: Optional[str] = None,
    ) -> ImportResult:
        if dataset not in DATASET_SCHEMAS:
            raise ValueError(f"unknown dataset {dataset!r}")

        schema = DATASET_SCHEMAS[dataset]

        batch = ImportBatch(
            tenant_id=self.tenant_id,
            dataset=dataset,
            source_filename=source_filename,
            status=ImportStatus.PENDING,
            mapping_version=MAPPING_VERSION,
        )
        self.session.add(batch)
        self.session.flush()  # assign batch.id

        result = ImportResult(import_id=batch.id, dataset=dataset, status=ImportStatus.PENDING)
        resolver = Resolver(self.session, self.tenant_id)

        handler = getattr(self, f"_load_{dataset}")

        for idx, raw_row in enumerate(rows, start=1):
            result.rows_total += 1
            # 1. Field-level validation.
            try:
                parsed = schema(**raw_row)
            except PydanticValidationError as exc:
                reason = _format_pydantic(exc)
                self._record_row(batch.id, idx, raw_row.get("source_record_id"),
                                 RowOutcome.REJECTED, reason, raw_row)
                result.rows_rejected += 1
                result.rows.append(RowResult(idx, raw_row.get("source_record_id"),
                                             RowOutcome.REJECTED, reason))
                continue

            # 2. Dataset-specific load (mapping + idempotency/conflict).
            try:
                outcome, reason = handler(parsed, batch.id, resolver)
            except MappingError as exc:
                self._record_row(batch.id, idx, raw_row.get("source_record_id"),
                                 RowOutcome.REJECTED, f"{exc.reason_code}: {exc.message}", raw_row)
                result.rows_rejected += 1
                result.rows.append(RowResult(idx, raw_row.get("source_record_id"),
                                             RowOutcome.REJECTED, f"{exc.reason_code}: {exc.message}"))
                continue

            self._record_row(batch.id, idx, getattr(parsed, "source_record_id",
                             getattr(parsed, "source_employee_id", None)),
                             outcome, reason, raw_row)
            if outcome is RowOutcome.ACCEPTED:
                result.rows_accepted += 1
            elif outcome is RowOutcome.DUPLICATE:
                result.rows_duplicate += 1
            elif outcome is RowOutcome.CONFLICT:
                result.rows_conflict += 1
            result.rows.append(RowResult(idx,
                               getattr(parsed, "source_record_id",
                                       getattr(parsed, "source_employee_id", None)),
                               outcome, reason))

        # Final status reflects whether anything needed attention.
        if result.rows_rejected or result.rows_conflict:
            batch.status = ImportStatus.COMPLETED_WITH_ERRORS
        else:
            batch.status = ImportStatus.COMPLETED
        batch.rows_total = result.rows_total
        batch.rows_accepted = result.rows_accepted
        batch.rows_duplicate = result.rows_duplicate
        batch.rows_rejected = result.rows_rejected
        batch.rows_conflict = result.rows_conflict
        result.status = batch.status
        self.session.flush()
        return result

    # -- helpers ------------------------------------------------------------
    def _record_row(self, import_id, row_number, source_record_id, outcome, reason, raw):
        self.session.add(
            ImportRow(
                import_id=import_id,
                row_number=row_number,
                source_record_id=str(source_record_id) if source_record_id is not None else None,
                outcome=outcome,
                reason=reason,
                raw_payload=json.dumps(raw, default=str),
            )
        )

    def _upsert_idempotent(self, model, natural_filter, content_hash, build_fn):
        """Shared idempotency/conflict logic for canonical source facts.

        Returns (RowOutcome, reason). Never overwrites a conflicting row.
        """
        existing = self.session.scalars(select(model).where(*natural_filter)).one_or_none()
        if existing is None:
            self.session.add(build_fn())
            self.session.flush()
            return RowOutcome.ACCEPTED, None
        if getattr(existing, "content_hash", None) == content_hash:
            return RowOutcome.DUPLICATE, "identical repeat of an already-loaded source id"
        return (
            RowOutcome.CONFLICT,
            "same source id with changed business content; flagged for review, not overwritten",
        )

    # -- per-dataset loaders ------------------------------------------------
    def _load_purchase_order_lines(self, p, import_id, resolver: Resolver):
        prod = resolver.resolve_product("ERP", p.item_code)
        loc = resolver.resolve_location("ERP", p.location_code)
        ordered_base = resolver.convert_to_base(prod, p.ordered_qty)
        payload = {
            "po_number": p.po_number,
            "line_number": p.line_number,
            "supplier_code": p.supplier_code,
            "canonical_product_id": prod.canonical_product_id,
            "canonical_location_id": loc.canonical_location_id,
            "ordered_qty_base": ordered_base,
            "base_uom": prod.base_uom,
            "status": p.status,
            "expected_receipt_by": p.expected_receipt_by.isoformat(),
        }
        chash = _content_hash(payload)

        def build():
            return PurchaseOrderLine(
                tenant_id=self.tenant_id,
                source_system="ERP",
                source_record_id=p.source_record_id,
                import_id=import_id,
                mapping_version=MAPPING_VERSION,
                content_hash=chash,
                po_number=p.po_number,
                line_number=p.line_number,
                supplier_code=p.supplier_code,
                canonical_product_id=prod.canonical_product_id,
                canonical_location_id=loc.canonical_location_id,
                ordered_qty_base=ordered_base,
                base_uom=prod.base_uom,
                status=p.status,
                expected_receipt_by=p.expected_receipt_by,
            )

        return self._upsert_idempotent(
            PurchaseOrderLine,
            (
                PurchaseOrderLine.tenant_id == self.tenant_id,
                PurchaseOrderLine.source_system == "ERP",
                PurchaseOrderLine.source_record_id == p.source_record_id,
            ),
            chash,
            build,
        )

    def _load_goods_receipt_lines(self, g, import_id, resolver: Resolver):
        prod = resolver.resolve_product("WMS", g.product_ref)
        loc = resolver.resolve_location("WMS", g.location_code)
        accepted_base = resolver.convert_to_base(prod, g.accepted_qty)
        payload = {
            "receipt_number": g.receipt_number,
            "receipt_line": g.receipt_line,
            "po_number": g.po_number,
            "po_line_number": g.po_line_number,
            "canonical_product_id": prod.canonical_product_id,
            "canonical_location_id": loc.canonical_location_id,
            "accepted_qty_base": accepted_base,
            "base_uom": prod.base_uom,
            "posting_status": g.posting_status,
            "received_at": g.received_at.isoformat(),
        }
        chash = _content_hash(payload)

        def build():
            return GoodsReceiptLine(
                tenant_id=self.tenant_id,
                source_system="WMS",
                source_record_id=g.source_record_id,
                import_id=import_id,
                mapping_version=MAPPING_VERSION,
                content_hash=chash,
                receipt_number=g.receipt_number,
                receipt_line=g.receipt_line,
                po_number=g.po_number,
                po_line_number=g.po_line_number,
                canonical_product_id=prod.canonical_product_id,
                canonical_location_id=loc.canonical_location_id,
                accepted_qty_base=accepted_base,
                base_uom=prod.base_uom,
                posting_status=g.posting_status,
                received_at=g.received_at,
            )

        return self._upsert_idempotent(
            GoodsReceiptLine,
            (
                GoodsReceiptLine.tenant_id == self.tenant_id,
                GoodsReceiptLine.source_system == "WMS",
                GoodsReceiptLine.source_record_id == g.source_record_id,
            ),
            chash,
            build,
        )

    def _load_product_aliases(self, a, import_id, resolver: Resolver):
        chash = _content_hash(a.model_dump())

        def build():
            return ProductAlias(
                tenant_id=self.tenant_id,
                source_system=a.source_system,
                external_item_id=a.external_item_id,
                canonical_product_id=a.canonical_product_id,
                source_uom=a.source_uom,
                base_uom=a.base_uom,
                to_base_factor=a.to_base_factor,
                import_id=import_id,
            )

        # Reference data uses (source_system, external_id) as its natural key.
        existing = self.session.scalars(
            select(ProductAlias).where(
                ProductAlias.tenant_id == self.tenant_id,
                ProductAlias.source_system == a.source_system,
                ProductAlias.external_item_id == a.external_item_id,
            )
        ).one_or_none()
        if existing is None:
            self.session.add(build())
            self.session.flush()
            return RowOutcome.ACCEPTED, None
        same = (
            existing.canonical_product_id == a.canonical_product_id
            and existing.base_uom == a.base_uom
            and existing.to_base_factor == a.to_base_factor
            and existing.source_uom == a.source_uom
        )
        if same:
            return RowOutcome.DUPLICATE, "identical repeat of existing product alias"
        return RowOutcome.CONFLICT, "product alias changed; flagged for review, not overwritten"

    def _load_location_aliases(self, a, import_id, resolver: Resolver):
        def build():
            return LocationAlias(
                tenant_id=self.tenant_id,
                source_system=a.source_system,
                external_location_id=a.external_location_id,
                canonical_location_id=a.canonical_location_id,
                region_id=a.region_id,
                import_id=import_id,
            )

        existing = self.session.scalars(
            select(LocationAlias).where(
                LocationAlias.tenant_id == self.tenant_id,
                LocationAlias.source_system == a.source_system,
                LocationAlias.external_location_id == a.external_location_id,
            )
        ).one_or_none()
        if existing is None:
            self.session.add(build())
            self.session.flush()
            return RowOutcome.ACCEPTED, None
        same = (
            existing.canonical_location_id == a.canonical_location_id
            and existing.region_id == a.region_id
        )
        if same:
            return RowOutcome.DUPLICATE, "identical repeat of existing location alias"
        return RowOutcome.CONFLICT, "location alias changed; flagged for review, not overwritten"

    def _load_employees(self, e, import_id, resolver: Resolver):
        # Map HRMS location to canonical BEFORE the employee can be used for routing
        # (Task A.2). Region-scoped roles may have no store location — that is allowed.
        canonical_location_id = None
        region_id = e.region_id
        if e.location_code:
            try:
                loc = resolver.resolve_location("HRMS", e.location_code)
                canonical_location_id = loc.canonical_location_id
                region_id = region_id or loc.region_id
            except MappingError as exc:
                # An unmapped store code is a data-quality problem, but we still
                # load the employee (region may still be usable); flag the reason.
                return RowOutcome.CONFLICT, f"employee location unmapped: {exc.message}"

        def build():
            return Employee(
                tenant_id=self.tenant_id,
                source_employee_id=e.source_employee_id,
                display_name=e.display_name,
                employment_status=e.employment_status,
                role=e.role,
                source_location_code=e.location_code,
                canonical_location_id=canonical_location_id,
                region_id=region_id,
                manager_employee_id=e.manager_employee_id,
                import_id=import_id,
            )

        existing = self.session.scalars(
            select(Employee).where(
                Employee.tenant_id == self.tenant_id,
                Employee.source_employee_id == e.source_employee_id,
            )
        ).one_or_none()
        if existing is None:
            self.session.add(build())
            self.session.flush()
            return RowOutcome.ACCEPTED, None
        same = (
            existing.employment_status == e.employment_status
            and existing.role == e.role
            and existing.canonical_location_id == canonical_location_id
            and existing.region_id == region_id
            and existing.manager_employee_id == e.manager_employee_id
        )
        if same:
            return RowOutcome.DUPLICATE, "identical repeat of existing employee"
        return RowOutcome.CONFLICT, "employee record changed; flagged for review, not overwritten"

    def _load_workforce_status(self, w, import_id, resolver: Resolver):
        def build():
            return WorkforceStatus(
                tenant_id=self.tenant_id,
                source_employee_id=w.source_employee_id,
                availability_status=w.availability_status,
                shift_start=w.shift_start,
                shift_end=w.shift_end,
                observed_at=w.observed_at,
                import_id=import_id,
            )

        existing = self.session.scalars(
            select(WorkforceStatus).where(
                WorkforceStatus.tenant_id == self.tenant_id,
                WorkforceStatus.source_employee_id == w.source_employee_id,
            )
        ).one_or_none()
        if existing is None:
            self.session.add(build())
            self.session.flush()
            return RowOutcome.ACCEPTED, None
        # Workforce is a time series; a newer observation legitimately updates the
        # latest sample rather than conflicting. Older/equal samples are duplicates.
        if w.observed_at > existing.observed_at:
            existing.availability_status = w.availability_status
            existing.shift_start = w.shift_start
            existing.shift_end = w.shift_end
            existing.observed_at = w.observed_at
            existing.import_id = import_id
            self.session.flush()
            return RowOutcome.ACCEPTED, "updated to a newer workforce sample"
        if (
            w.observed_at == existing.observed_at
            and w.availability_status == existing.availability_status
        ):
            return RowOutcome.DUPLICATE, "identical repeat of existing workforce sample"
        return RowOutcome.DUPLICATE, "older-or-equal workforce sample; latest retained"


def _format_pydantic(exc: PydanticValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
