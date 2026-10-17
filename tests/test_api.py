"""API smoke/contract test via FastAPI's TestClient.

Exercises the real HTTP surface: upload imports, run detection, list/inspect
exceptions and tasks, drive a lifecycle transition, and hit the structured-error
path. Uses the same isolated test database as the other tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db.init_db import drop_all, init_db
from app.api.main import app

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DATASETS = [
    "product_aliases", "location_aliases", "employees", "workforce_status",
    "purchase_order_lines", "goods_receipt_lines",
]


@pytest.fixture()
def client():
    drop_all()
    init_db()
    with TestClient(app) as c:
        yield c


def _upload_all(client):
    for ds in DATASETS:
        path = FIXTURES / f"{ds}.csv"
        with path.open("rb") as fh:
            resp = client.post(f"/imports/{ds}", files={"file": (path.name, fh, "text/csv")})
        assert resp.status_code == 200, resp.text


def test_full_flow_over_http(client):
    _upload_all(client)

    # Importing PO lines reports the duplicate row explicitly.
    with (FIXTURES / "purchase_order_lines.csv").open("rb") as fh:
        resp = client.post("/imports/purchase_order_lines",
                           files={"file": ("po.csv", fh, "text/csv")})
    # Re-upload => all duplicates (idempotent).
    counts = resp.json()["counts"]
    assert counts["duplicate"] >= 1

    # Detection + tasks.
    assert client.post("/detection/run").json()["created"] == 3
    gen = client.post("/tasks/generate").json()
    assert len(gen["tasks"]) == 3

    # List exceptions.
    exceptions = client.get("/exceptions").json()
    assert {e["po_number"] for e in exceptions} == {"PO-501", "PO-505", "PO-506"}

    # Inspect one with its source records.
    exc_501 = next(e for e in exceptions if e["po_number"] == "PO-501")
    detail = client.get(f"/exceptions/{exc_501['public_id']}").json()
    assert detail["variance_qty_base"] == 8
    assert len(detail["source_records"]) == 3
    assert len(detail["tasks"]) == 1

    # Tasks for the STORE-014 receiver.
    tasks = client.get("/tasks", params={"assignee_id": "HR-102"}).json()
    assert len(tasks) == 2  # PO-501 and PO-506 both at STORE-014

    # Drive a lifecycle transition over HTTP.
    task_id = detail["tasks"][0]["public_id"]
    assert client.post(f"/tasks/{task_id}/acknowledge",
                       json={"actor": "HR-102"}).status_code == 200
    r = client.post(f"/tasks/{task_id}/start", json={"actor": "HR-102"})
    assert r.json()["status"] == "IN_PROGRESS"


def test_invalid_transition_returns_structured_error(client):
    _upload_all(client)
    client.post("/detection/run")
    client.post("/tasks/generate")
    tasks = client.get("/tasks").json()
    task_id = tasks[0]["public_id"]

    # Completing a freshly-assigned task is illegal.
    resp = client.post(f"/tasks/{task_id}/complete", json={"actor": "HR-102"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "invalid_transition"
    assert "allowed_from" in body["error"]["details"]


def test_data_quality_endpoint_lists_rejections(client):
    _upload_all(client)
    # Upload a PO row with an unmappable product.
    bad = (
        "source_record_id,po_number,line_number,supplier_code,location_code,"
        "item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
        "ERP-BAD,PO-BAD,1,SUP-07,MUM-014,ERP-NOPE,10,EA,OPEN,2026-10-15T12:00:00+05:30\n"
    )
    resp = client.post("/imports/purchase_order_lines",
                      files={"file": ("bad.csv", bad.encode(), "text/csv")})
    assert resp.json()["counts"]["rejected"] == 1

    dq = client.get("/data-quality").json()
    assert dq["total"] >= 1
    assert any("unmapped_product" in (i["reason"] or "") for i in dq["unresolved"])


def test_unknown_dataset_is_404(client):
    resp = client.post("/imports/not_a_dataset",
                      files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert resp.status_code == 404
