# OPSiB Backend Slice

A small, well-tested backend service that imports inconsistent retail source
records, reconciles them into a canonical model, detects **valid receipt
shortfalls**, routes work to eligible employees, and tracks task progress with an
auditable history.

Built for the OPSiB Backend Engineering Assessment. Stack: **Python / FastAPI /
SQLAlchemy**, SQLite by default (zero infra), Postgres-ready for a pilot.

---

## TL;DR — run it

```bash
# 1. create a virtualenv and install deps
python -m venv .venv
.venv/Scripts/activate        # Windows (PowerShell/Git Bash)
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt

# 2. run the whole slice against the supplied fixtures (load -> detect -> route)
python -m app.cli run-all --dir fixtures

# 3. run the tests
pytest
```

`run-all` prints the detected exceptions and the routed tasks. On a clean
database it reproduces exactly the six required scenario outcomes.

### Run the API

```bash
uvicorn app.api.main:app --reload
# interactive docs:   http://127.0.0.1:8000/docs
# OpenAPI JSON:        http://127.0.0.1:8000/openapi.json  (also exported to openapi.json)
```

Typical API flow:

```bash
# upload each dataset (reference data first)
curl -X POST localhost:8000/imports/product_aliases     -F file=@fixtures/product_aliases.csv
curl -X POST localhost:8000/imports/location_aliases    -F file=@fixtures/location_aliases.csv
curl -X POST localhost:8000/imports/employees           -F file=@fixtures/employees.csv
curl -X POST localhost:8000/imports/workforce_status    -F file=@fixtures/workforce_status.csv
curl -X POST localhost:8000/imports/purchase_order_lines -F file=@fixtures/purchase_order_lines.csv
curl -X POST localhost:8000/imports/goods_receipt_lines  -F file=@fixtures/goods_receipt_lines.csv

curl -X POST localhost:8000/detection/run      # deterministic shortfall detection
curl -X POST localhost:8000/tasks/generate     # create + route one task per exception

curl localhost:8000/exceptions                 # list findings
curl localhost:8000/tasks?assignee_id=HR-102   # work queue for a person
```

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OPSIB_DATABASE_URL` | `sqlite:///./opsib.db` | Any SQLAlchemy URL. Use `postgresql+psycopg://…` for a pilot. |
| `OPSIB_FROZEN_EVAL_TIME` | `2026-10-16T15:00:00+05:30` | Frozen evaluation time from the fixture README. **Scenario logic uses this, never the machine clock.** Must be tz-aware. |
| `OPSIB_DEFAULT_TENANT` | `retailer-demo` | Single-tenant default; every row still carries `tenant_id`. |
| `OPSIB_SHORTFALL_TOLERANCE` | `0` | Variance (base units) below which no shortfall is raised. |
| `OPSIB_WORKFORCE_FRESHNESS_MINUTES` | `120` | How old a shift sample may be and still count toward on-shift eligibility. |
| `OPSIB_REQUIRE_ON_SHIFT` | `true` | Whether on-shift status is required for assignment (employment always is). |

---

## Migrations

The ORM models in [`app/db/models.py`](app/db/models.py) are the schema of record.

- **Dev / test (SQLite default):** schema is created with `create_all()` via
  `python -m app.cli init-db` (the API also does this on startup). No migration
  tool needed to try the slice.
- **Pilot (Postgres):** [`migrations/0001_initial.sql`](migrations/0001_initial.sql)
  is the equivalent DDL (Postgres dialect, `TIMESTAMPTZ`, the same UNIQUE/CHECK
  constraints). In production this would be managed by Alembic rather than
  `create_all`; see *Trade-offs* below.

---

## Commands

```bash
python -m app.cli init-db                 # create schema
python -m app.cli load-fixtures --dir fixtures
python -m app.cli detect                  # run the detector (idempotent)
python -m app.cli generate-tasks          # create + route tasks (idempotent)
python -m app.cli run-all --dir fixtures  # all of the above
python -m app.cli show-exceptions         # human-readable summary
```

---

## Running the tests

```bash
pytest            # 44 tests; isolated temp SQLite DB per run
pytest -v
```

Tests are organised by the assessment's minimum-test list:

| File | Covers |
|---|---|
| `tests/test_detection_unit.py` | UOM conversion, partial/split receipts, deadline boundary, cancellation, duplicate-safe aggregation, unposted receipts, data-quality block |
| `tests/test_integration_scenarios.py` | Loads the **real fixture pack** and asserts all six required scenario outcomes + traceability + neutral narrative |
| `tests/test_idempotency.py` | Re-import doesn't double-count; identical duplicate row; stable re-detection; **conflicting** duplicate id flagged not overwritten |
| `tests/test_routing.py` | HRMS source-location mapping, off-shift employee, inactive employee, stale sample, store-manager fallback, end-to-end routing |
| `tests/test_workflow.py` | Invalid transitions, repeated completion (retry safety), audit history, verification gate |
| `tests/test_failure_paths.py` | Unknown product mapping, invalid timestamp, negative qty, AI boundary guards |
| `tests/test_api.py` | Full HTTP flow, structured error bodies, `/data-quality` |

Latest run:

```
44 passed in ~6s
```

---

## The six required scenarios (as produced by this service)

| PO | Ordered | Received | Deadline vs 2026-10-16 15:00 IST | Result |
|---|---|---|---|---|
| PO-501 | 100 EA | 60 + 32 = 92 EA | passed | **Shortfall 8 EA** |
| PO-502 | 40 EA | 40 EA | passed | No exception (met in full) |
| PO-503 | 50 EA | 20 EA | **future** (10-17 18:00) | No exception (not actionable yet) |
| PO-504 | 20 EA | — | passed | No exception (CANCELLED) |
| PO-505 | 10 BOX → **120 EA** | 96 EA | passed | **Shortfall 24 EA** (UOM converted) |
| PO-506 | 20 EA | 10 EA (dup source id → counted once) | passed | **Shortfall 10 EA** |

Routing: PO-501 & PO-506 (STORE-014) → **HR-102**; PO-505 (STORE-015, no receiver
on shift) → fallback store manager **HR-401**.

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — data model, layering, and how
  ingestion → mapping → detection → routing → persistence fit together, plus the
  three design-question answers.
- [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) — assumptions, trade-offs, what is
  intentionally omitted, and what would be hardened for a live retailer.
- [`docs/AI_DISCLOSURE.md`](docs/AI_DISCLOSURE.md) — AI tooling used and what was
  independently reviewed, plus the designed (not live) AI investigation boundary.
- `openapi.json` — exported API contract.

---

## Repository layout

```
app/
  api/         FastAPI app, request/response schemas (HTTP contract)
  core/        config, typed errors, tz helpers, id generation
  ingestion/   typed row schemas, CSV reader, import service (idempotency/conflict)
  mapping/     identity + UOM resolver (the only place that knows crosswalks)
  detection/   pure shortfall detector + persistence service
  routing/     employee eligibility + assignee selection (pure)
  workflows/   task lifecycle state machine + task service (routing/audit)
  ai/          optional AI investigation provider interface (boundary-enforced)
  db/          ORM models, session/engine, schema init
  pipeline.py  end-to-end orchestration shared by CLI and API
  cli.py       command-line entry point
tests/         unit + integration + API tests
migrations/    Postgres DDL for a pilot
fixtures/      the supplied synthetic data (unchanged) + fixture README
```
