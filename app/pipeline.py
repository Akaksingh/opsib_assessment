"""End-to-end orchestration shared by the CLI and the API.

load_fixtures -> run_detection -> create_tasks, all inside one unit of work each,
so a clean run reproduces the required scenario outcomes. Each step is idempotent,
so running the whole pipeline twice yields the same business state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import ExceptionCase
from app.detection.service import DetectionService
from app.ingestion.csv_reader import read_csv_rows
from app.ingestion.service import ImportService
from app.workflows.service import TaskService

# Reference data must load before transactional data that maps through it.
DATASET_ORDER = [
    "product_aliases",
    "location_aliases",
    "employees",
    "workforce_status",
    "purchase_order_lines",
    "goods_receipt_lines",
]


def load_fixtures(
    session: Session, fixtures_dir: str | Path, tenant_id: Optional[str] = None
) -> dict:
    fixtures_dir = Path(fixtures_dir)
    svc = ImportService(session, tenant_id=tenant_id)
    results = {}
    for dataset in DATASET_ORDER:
        path = fixtures_dir / f"{dataset}.csv"
        if not path.exists():
            continue
        rows = read_csv_rows(path)
        res = svc.import_dataset(dataset, rows, source_filename=path.name)
        results[dataset] = res.as_dict()
    return results


def run_detection(session: Session, tenant_id: Optional[str] = None) -> dict:
    return DetectionService(session, tenant_id=tenant_id).run_detection()


def create_tasks(session: Session, tenant_id: Optional[str] = None) -> list[dict]:
    settings = get_settings()
    tenant = tenant_id or settings.default_tenant
    svc = TaskService(session, tenant_id=tenant)
    created = []
    exceptions = session.scalars(
        select(ExceptionCase).where(ExceptionCase.tenant_id == tenant)
    ).all()
    for exc in exceptions:
        task = svc.create_task_for_exception(exc)
        created.append(
            {
                "exception": exc.public_id,
                "task": task.public_id,
                "status": task.status.value,
                "assignee": task.assignee_employee_id,
                "reason": task.assignment_reason,
            }
        )
    return created


def run_full_pipeline(
    session: Session, fixtures_dir: str | Path, tenant_id: Optional[str] = None
) -> dict:
    imports = load_fixtures(session, fixtures_dir, tenant_id)
    detection = run_detection(session, tenant_id)
    tasks = create_tasks(session, tenant_id)
    return {"imports": imports, "detection": detection, "tasks": tasks}
