# Assumptions & trade-offs

## Assumptions made (and why)

- **Frozen evaluation time** is `2026-10-16T15:00:00+05:30` (fixture README), used
  for every deadline and shift-freshness check. The machine clock is never used
  for scenario logic.
- **Deadline boundary is inclusive**: a line is actionable when
  `frozen_now >= expected_receipt_by`. A receipt window whose deadline equals the
  evaluation instant is treated as closed. (Configurable in one place if a
  retailer defines it exclusively.)
- **Only POSTED receipts count** toward accepted quantity. DRAFT/REVERSED receipts
  are excluded from the comparison. This is explicit in the detector and tested.
- **Zero tolerance** by default. A configurable tolerance exists
  (`OPSIB_SHORTFALL_TOLERANCE`) but is off unless set.
- **On-shift is required** for assignment by default, and a workforce sample must
  be within 120 minutes of the evaluation time to count. Employment status
  (`ACTIVE`) is always required and always wins over the shift export.
- **Store-manager fallback** is the approved fallback when no primary-role
  receiver is eligible at a location. Anything beyond that stays
  `NEEDS_MANUAL_ASSIGNMENT`.
- **Workforce is a time series**: a *newer* observation for an employee updates the
  latest sample (not a conflict); an older/equal one is a duplicate. Other
  reference/transactional data treats a changed repeat as a CONFLICT.
- **Region-scoped employees may have no store location** (HR-501). They are loaded
  with a region and a null canonical location, and are simply not eligible for a
  store-scoped task — which is correct, not an error.
- **UOM conversion is a simple multiplicative factor** from the alias file
  (`to_base_factor`). Enough for the fixtures (BOX→EA ×12); real catalogs may need
  per-pair conversion tables.

## Intentionally omitted (documented, not implemented)

These are **not implemented** and must not be read as if they were:

- **Authentication / tenant authorization.** Every row carries `tenant_id` and
  queries filter on it, but there is no auth layer, no per-tenant access control,
  and no actor identity beyond the `actor` string callers supply. A pilot needs
  real authn/z and a server-trusted actor.
- **Secret management.** No secrets are used; a real integration (ERP/WMS
  credentials, an AI key) would need secure storage (a vault / managed secrets),
  never env files in the repo.
- **A migration tool.** `create_all()` + a hand-written `migrations/0001_initial.sql`
  stand in. Production should use Alembic with reviewed, reversible migrations.
- **A background worker / queue.** Detection and task creation run synchronously.
  The idempotent `case_key` / `idempotency_key` design is *ready* for a worker, but
  no Celery/RQ/Kafka is wired up (and the brief explicitly discourages adding it).
- **Live AI calls.** The AI provider interface is designed and a `NullAIProvider`
  exercises the path; no model is called and no key is required.
- **Monitoring, metrics, retention, PII handling policy.** Structured logs exist;
  dashboards, alerting, data-retention jobs and a formal PII policy do not.

## Trade-offs

- **SQLite by default.** Keeps the slice runnable on a clean checkout with zero
  infra, and the same SQLAlchemy models + constraints run on Postgres. Limits of
  SQLite for a live pilot: weaker concurrency (single writer; we set a busy
  timeout), no true `TIMESTAMPTZ` (we normalize to UTC in a `TypeDecorator` and
  re-attach UTC on load), and no `SELECT … FOR UPDATE`. For a pilot, point
  `OPSIB_DATABASE_URL` at Postgres and run the SQL migration; the invariants
  (UNIQUE keys, FKs, CHECKs) transfer unchanged.
- **Floats for quantities.** Simple and exact for the integer-valued fixtures. A
  live system handling fractional UOMs and money should use `Decimal` / numeric
  columns to avoid rounding surprises.
- **`create_all` on API startup.** Convenient for evaluation; a pilot would remove
  this and gate schema changes behind migrations.
- **Synchronous import of whole files.** Fine for these small fixtures; large feeds
  would want streaming/batched commits and a job abstraction.
- **One task per exception by default.** The model supports many tasks per
  exception (separate tables, idempotency key includes a `purpose`), but the
  default pipeline creates a single investigation task per finding.

## What would be hardened first for a live retailer

1. **Tenant isolation + authn/z** on every endpoint, with a server-trusted actor
   replacing the client-supplied `actor` string.
2. **Postgres + Alembic**, with `INSERT … ON CONFLICT`/row locks for the
   import and task-creation hot paths.
3. **Conflict-resolution workflow** for CONFLICT rows (today they are flagged in
   `/data-quality`; a pilot needs a review/approve/version UI and policy).
4. **Secrets + config management** for ERP/WMS/AI integration credentials.
5. **Observability**: request/trace ids in logs, metrics on import outcomes and
   detection counts, alerting on rejected-row spikes.
6. **Data retention & PII minimisation** for employee data.
