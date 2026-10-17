"""Persist detector output as idempotent ExceptionCases (Task B persistence).

The pure detector in `shortfall.py` decides *what* is true. This service decides
*how it is stored*, with the single invariant that re-running detection over the
same data must not create a second case for the same business issue. The
(tenant, case_key) UNIQUE constraint enforces that at the database level; the
service upserts on the case_key.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import MAPPING_VERSION, RULE_VERSION, get_settings
from app.db.models import (
    ExceptionCase,
    ExceptionStatus,
    ExceptionType,
    GoodsReceiptLine,
    PurchaseOrderLine,
)
from app.detection.shortfall import (
    POLineView,
    ReceiptLineView,
    detect_shortfalls,
)
from app.core.idgen import new_public_id


class DetectionService:
    def __init__(self, session: Session, tenant_id: Optional[str] = None):
        self.session = session
        self.settings = get_settings()
        self.tenant_id = tenant_id or self.settings.default_tenant

    def _load_views(self):
        po_rows = self.session.scalars(
            select(PurchaseOrderLine).where(PurchaseOrderLine.tenant_id == self.tenant_id)
        ).all()
        rc_rows = self.session.scalars(
            select(GoodsReceiptLine).where(GoodsReceiptLine.tenant_id == self.tenant_id)
        ).all()
        po_views = [
            POLineView(
                source_record_id=r.source_record_id,
                po_number=r.po_number,
                line_number=r.line_number,
                canonical_product_id=r.canonical_product_id,
                canonical_location_id=r.canonical_location_id,
                ordered_qty_base=r.ordered_qty_base,
                base_uom=r.base_uom,
                status=r.status,
                expected_receipt_by=r.expected_receipt_by,
            )
            for r in po_rows
        ]
        rc_views = [
            ReceiptLineView(
                source_record_id=r.source_record_id,
                po_number=r.po_number,
                po_line_number=r.po_line_number,
                canonical_product_id=r.canonical_product_id,
                canonical_location_id=r.canonical_location_id,
                accepted_qty_base=r.accepted_qty_base,
                base_uom=r.base_uom,
                posting_status=r.posting_status,
                received_at=r.received_at,
            )
            for r in rc_rows
        ]
        return po_views, rc_views

    def run_detection(self) -> dict:
        """Detect and persist. Returns a summary of created/updated/skipped."""
        po_views, rc_views = self._load_views()
        outcomes = detect_shortfalls(
            po_views,
            rc_views,
            frozen_now=self.settings.frozen_eval_time,
            tolerance=self.settings.shortfall_tolerance,
        )

        created, updated, skipped, data_quality = 0, 0, 0, 0
        for oc in outcomes:
            if oc.data_quality_reason:
                data_quality += 1
                continue
            if oc.finding is None:
                skipped += 1
                continue

            f = oc.finding
            existing = self.session.scalars(
                select(ExceptionCase).where(
                    ExceptionCase.tenant_id == self.tenant_id,
                    ExceptionCase.case_key == f.case_key,
                )
            ).one_or_none()

            if existing is None:
                self.session.add(
                    ExceptionCase(
                        tenant_id=self.tenant_id,
                        case_key=f.case_key,
                        public_id=new_public_id("EXC"),
                        type=ExceptionType.RECEIPT_SHORTFALL,
                        status=ExceptionStatus.OPEN,
                        rule_version=RULE_VERSION,
                        mapping_version=MAPPING_VERSION,
                        po_number=f.po_number,
                        po_line_number=f.po_line_number,
                        canonical_product_id=f.canonical_product_id,
                        canonical_location_id=f.canonical_location_id,
                        expected_qty_base=f.expected_qty_base,
                        received_qty_base=f.received_qty_base,
                        variance_qty_base=f.variance_qty_base,
                        base_uom=f.base_uom,
                        evidence_refs=json.dumps(f.evidence_refs),
                        explanation=f.explanation,
                    )
                )
                created += 1
            else:
                # Re-detection: refresh the computed numbers but keep identity,
                # status and any human resolution. Never fork the case.
                existing.expected_qty_base = f.expected_qty_base
                existing.received_qty_base = f.received_qty_base
                existing.variance_qty_base = f.variance_qty_base
                existing.evidence_refs = json.dumps(f.evidence_refs)
                existing.explanation = f.explanation
                existing.rule_version = RULE_VERSION
                existing.mapping_version = MAPPING_VERSION
                updated += 1

        self.session.flush()
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "data_quality_blocked": data_quality,
            "rule_version": RULE_VERSION,
        }
