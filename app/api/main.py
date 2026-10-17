"""FastAPI application and routes.

Endpoints map onto the assessment's suggested surface:
  POST /imports/{dataset}        validate + load a dataset (multipart CSV upload)
  GET  /imports/{import_id}      counts, errors, status
  GET  /data-quality            unresolved mappings / rejected / conflicting rows
  POST /detection/run           run the deterministic detector (idempotent)
  POST /tasks/generate          create tasks for open exceptions (idempotent)
  GET  /exceptions              list exceptions
  GET  /exceptions/{public_id}  one exception + its supporting source records
  GET  /tasks                   list tasks (filter by assignee_id or location)
  GET  /tasks/{public_id}       one task + its audit history
  POST /tasks/{public_id}/{action}  acknowledge/start/complete/... (lifecycle)
  POST /tasks/{public_id}/reassign  reassign with reason

Errors are returned as structured bodies via the OpsibError handler.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError, OpsibError
from app.core.timeutils import display_local
from app.db.init_db import init_db
from app.db.models import (
    ExceptionCase,
    GoodsReceiptLine,
    ImportBatch,
    ImportRow,
    PurchaseOrderLine,
    RowOutcome,
    Task,
)
from app.db.session import get_session
from app.detection.service import DetectionService
from app.ingestion.csv_reader import read_csv_bytes
from app.ingestion.schemas import DATASET_SCHEMAS
from app.ingestion.service import ImportService
from app.api import schemas as S
from app.workflows.service import TaskService

logger = logging.getLogger("opsib")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="OPSiB Backend Slice",
    version="0.1.0",
    description="Retail data integration, exception detection and task reliability.",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    logger.info("schema ready; frozen_eval_time=%s", get_settings().frozen_eval_time.isoformat())


@app.exception_handler(OpsibError)
async def _opsib_error_handler(_: Request, exc: OpsibError):
    # Structured, useful errors — never a bare 500 for an expected business failure.
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
@app.post("/imports/{dataset}", response_model=S.ImportResultOut)
def import_dataset(dataset: str, file: UploadFile = File(...), session: Session = Depends(get_session)):
    if dataset not in DATASET_SCHEMAS:
        raise HTTPException(status_code=404, detail=f"unknown dataset {dataset!r}")
    content = file.file.read()
    rows = read_csv_bytes(content)
    svc = ImportService(session)
    result = svc.import_dataset(dataset, rows, source_filename=file.filename)
    session.commit()
    logger.info(
        "import dataset=%s id=%s accepted=%s dup=%s rejected=%s conflict=%s",
        dataset, result.import_id, result.rows_accepted, result.rows_duplicate,
        result.rows_rejected, result.rows_conflict,
    )
    return result.as_dict()


@app.get("/imports/{import_id}", response_model=S.ImportResultOut)
def get_import(import_id: int, session: Session = Depends(get_session)):
    batch = session.get(ImportBatch, import_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="import not found")
    rows = session.scalars(
        select(ImportRow).where(ImportRow.import_id == import_id).order_by(ImportRow.row_number)
    ).all()
    return {
        "import_id": batch.id,
        "dataset": batch.dataset,
        "status": batch.status.value,
        "counts": {
            "total": batch.rows_total,
            "accepted": batch.rows_accepted,
            "duplicate": batch.rows_duplicate,
            "rejected": batch.rows_rejected,
            "conflict": batch.rows_conflict,
        },
        "rows": [
            {
                "row_number": r.row_number,
                "source_record_id": r.source_record_id,
                "outcome": r.outcome.value,
                "reason": r.reason,
            }
            for r in rows
        ],
    }


@app.get("/data-quality", response_model=S.DataQualityOut)
def data_quality(session: Session = Depends(get_session)):
    """Everything that needs a human: rejected rows and conflicting source ids."""
    pairs = session.execute(
        select(ImportRow, ImportBatch)
        .join(ImportBatch, ImportRow.import_id == ImportBatch.id)
        .where(ImportRow.outcome.in_([RowOutcome.REJECTED, RowOutcome.CONFLICT]))
        .order_by(ImportRow.id)
    ).all()
    items = [
        {
            "import_id": batch.id,
            "dataset": batch.dataset,
            "row_number": row.row_number,
            "source_record_id": row.source_record_id,
            "outcome": row.outcome.value,
            "reason": row.reason,
        }
        for row, batch in pairs
    ]
    return {"unresolved": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Detection + task generation
# ---------------------------------------------------------------------------
@app.post("/detection/run")
def run_detection(session: Session = Depends(get_session)):
    summary = DetectionService(session).run_detection()
    session.commit()
    logger.info("detection run: %s", summary)
    return summary


@app.post("/tasks/generate")
def generate_tasks(session: Session = Depends(get_session)):
    svc = TaskService(session)
    exceptions = session.scalars(select(ExceptionCase).where(
        ExceptionCase.tenant_id == get_settings().default_tenant)).all()
    out = []
    for exc in exceptions:
        task = svc.create_task_for_exception(exc)
        out.append({"exception": exc.public_id, "task": task.public_id,
                    "status": task.status.value, "assignee": task.assignee_employee_id})
    session.commit()
    return {"tasks": out}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
@app.get("/exceptions", response_model=list[S.ExceptionOut])
def list_exceptions(
    status: Optional[str] = None,
    location: Optional[str] = Query(None, alias="location"),
    session: Session = Depends(get_session),
):
    stmt = select(ExceptionCase).where(
        ExceptionCase.tenant_id == get_settings().default_tenant)
    if status:
        stmt = stmt.where(ExceptionCase.status == status)
    if location:
        stmt = stmt.where(ExceptionCase.canonical_location_id == location)
    rows = session.scalars(stmt.order_by(ExceptionCase.po_number)).all()
    return [_exception_to_dict(r) for r in rows]


@app.get("/exceptions/{public_id}", response_model=S.ExceptionDetailOut)
def get_exception(public_id: str, session: Session = Depends(get_session)):
    exc = session.scalars(
        select(ExceptionCase).where(ExceptionCase.public_id == public_id)
    ).one_or_none()
    if exc is None:
        raise HTTPException(status_code=404, detail="exception not found")

    evidence = json.loads(exc.evidence_refs)
    source_records = []
    for sid in evidence:
        po = session.scalars(
            select(PurchaseOrderLine).where(PurchaseOrderLine.source_record_id == sid)
        ).one_or_none()
        if po:
            source_records.append({
                "kind": "po_line", "source_record_id": sid,
                "detail": {
                    "po_number": po.po_number, "line_number": po.line_number,
                    "canonical_product_id": po.canonical_product_id,
                    "ordered_qty_base": po.ordered_qty_base, "base_uom": po.base_uom,
                    "status": po.status,
                    "expected_receipt_by": display_local(po.expected_receipt_by),
                    "source_system": po.source_system,
                },
            })
            continue
        rc = session.scalars(
            select(GoodsReceiptLine).where(GoodsReceiptLine.source_record_id == sid)
        ).one_or_none()
        if rc:
            source_records.append({
                "kind": "receipt_line", "source_record_id": sid,
                "detail": {
                    "receipt_number": rc.receipt_number, "po_number": rc.po_number,
                    "canonical_product_id": rc.canonical_product_id,
                    "accepted_qty_base": rc.accepted_qty_base, "base_uom": rc.base_uom,
                    "posting_status": rc.posting_status,
                    "received_at": display_local(rc.received_at),
                    "source_system": rc.source_system,
                },
            })

    detail = _exception_to_dict(exc)
    detail["source_records"] = source_records
    detail["tasks"] = [_task_to_dict(t, exc.public_id) for t in exc.tasks]
    return detail


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@app.get("/tasks", response_model=list[S.TaskOut])
def list_tasks(
    assignee_id: Optional[str] = None,
    location: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    stmt = select(Task).where(Task.tenant_id == get_settings().default_tenant)
    if assignee_id:
        stmt = stmt.where(Task.assignee_employee_id == assignee_id)
    if location:
        stmt = stmt.where(Task.canonical_location_id == location)
    if status:
        stmt = stmt.where(Task.status == status)
    rows = session.scalars(stmt.order_by(Task.public_id)).all()
    return [_task_to_dict(t) for t in rows]


@app.get("/tasks/{public_id}", response_model=S.TaskDetailOut)
def get_task(public_id: str, session: Session = Depends(get_session)):
    task = _require_task(session, public_id)
    out = _task_to_dict(task)
    out["events"] = [
        {
            "event_type": e.event_type, "from_status": e.from_status,
            "to_status": e.to_status, "actor": e.actor, "reason": e.reason,
            "detail": e.detail, "created_at": display_local(e.created_at),
        }
        for e in task.events
    ]
    return out


@app.post("/tasks/{public_id}/complete", response_model=S.TaskDetailOut)
def complete_task(public_id: str, body: S.CompleteIn, session: Session = Depends(get_session)):
    task = _require_task(session, public_id)
    svc = TaskService(session)
    svc.transition(
        task, "complete", actor=body.actor, reason=body.reason,
        idempotency_key=body.idempotency_key,
        resolution_code=body.resolution_code,
        resolution_evidence_ref=body.resolution_evidence_ref,
    )
    session.commit()
    return get_task(public_id, session)


@app.post("/tasks/{public_id}/reassign", response_model=S.TaskDetailOut)
def reassign_task(public_id: str, body: S.ReassignIn, session: Session = Depends(get_session)):
    task = _require_task(session, public_id)
    svc = TaskService(session)
    svc.reassign(task, new_assignee_id=body.new_assignee_id, reason=body.reason, actor=body.actor)
    session.commit()
    return get_task(public_id, session)


@app.post("/tasks/{public_id}/{action}", response_model=S.TaskDetailOut)
def task_action(public_id: str, action: str, body: S.TransitionIn, session: Session = Depends(get_session)):
    """Generic lifecycle action: acknowledge, start, submit_verification, verify,
    reject, reopen, block, unblock. (complete/reassign have dedicated routes.)"""
    if action in {"complete", "reassign"}:
        raise HTTPException(status_code=400, detail=f"use POST /tasks/{{id}}/{action}")
    task = _require_task(session, public_id)
    svc = TaskService(session)
    svc.transition(task, action, actor=body.actor, reason=body.reason,
                   idempotency_key=body.idempotency_key)
    session.commit()
    return get_task(public_id, session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require_task(session: Session, public_id: str) -> Task:
    task = session.scalars(select(Task).where(Task.public_id == public_id)).one_or_none()
    if task is None:
        raise NotFoundError(f"task {public_id} not found")
    return task


def _exception_to_dict(exc: ExceptionCase) -> dict:
    return {
        "public_id": exc.public_id,
        "case_key": exc.case_key,
        "type": exc.type.value,
        "status": exc.status.value,
        "rule_version": exc.rule_version,
        "mapping_version": exc.mapping_version,
        "po_number": exc.po_number,
        "po_line_number": exc.po_line_number,
        "canonical_product_id": exc.canonical_product_id,
        "canonical_location_id": exc.canonical_location_id,
        "expected_qty_base": exc.expected_qty_base,
        "received_qty_base": exc.received_qty_base,
        "variance_qty_base": exc.variance_qty_base,
        "base_uom": exc.base_uom,
        "evidence_refs": json.loads(exc.evidence_refs),
        "explanation": exc.explanation,
        "resolution_code": exc.resolution_code,
        "detected_at": display_local(exc.detected_at),
    }


def _task_to_dict(task: Task, exception_public_id: Optional[str] = None) -> dict:
    return {
        "public_id": task.public_id,
        "exception_public_id": exception_public_id,
        "status": task.status.value,
        "canonical_location_id": task.canonical_location_id,
        "required_role": task.required_role,
        "assignee_employee_id": task.assignee_employee_id,
        "assignment_reason": task.assignment_reason,
        "due_at": display_local(task.due_at),
        "resolution_code": task.resolution_code,
    }
