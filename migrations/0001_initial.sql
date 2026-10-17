-- OPSiB backend slice — initial schema (Postgres dialect).
--
-- For the SQLite-default dev/test path the ORM's create_all() builds these tables.
-- This file documents the same DDL for a Postgres pilot and is the artifact a real
-- migration tool (Alembic) would manage. The UNIQUE constraints below are the
-- business invariants that make imports idempotent and task/exception creation
-- single-writer-safe under concurrency.

CREATE TABLE import_batches (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    dataset         VARCHAR(64) NOT NULL,
    source_filename VARCHAR(255),
    status          VARCHAR(32) NOT NULL,
    rows_total      INTEGER NOT NULL DEFAULT 0,
    rows_accepted   INTEGER NOT NULL DEFAULT 0,
    rows_duplicate  INTEGER NOT NULL DEFAULT 0,
    rows_rejected   INTEGER NOT NULL DEFAULT 0,
    rows_conflict   INTEGER NOT NULL DEFAULT 0,
    mapping_version VARCHAR(32) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_import_batches_tenant ON import_batches (tenant_id);

CREATE TABLE import_rows (
    id               BIGSERIAL PRIMARY KEY,
    import_id        BIGINT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    row_number       INTEGER NOT NULL,
    source_record_id VARCHAR(128),
    outcome          VARCHAR(16) NOT NULL,
    reason           TEXT,
    raw_payload      TEXT
);
CREATE INDEX ix_import_rows_import ON import_rows (import_id);

CREATE TABLE product_aliases (
    id                   BIGSERIAL PRIMARY KEY,
    tenant_id            VARCHAR(64) NOT NULL,
    source_system        VARCHAR(16) NOT NULL,
    external_item_id     VARCHAR(128) NOT NULL,
    canonical_product_id VARCHAR(64) NOT NULL,
    source_uom           VARCHAR(16) NOT NULL,
    base_uom             VARCHAR(16) NOT NULL,
    to_base_factor       DOUBLE PRECISION NOT NULL,
    import_id            BIGINT REFERENCES import_batches(id),
    CONSTRAINT uq_product_alias UNIQUE (tenant_id, source_system, external_item_id)
);

CREATE TABLE location_aliases (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(64) NOT NULL,
    source_system         VARCHAR(16) NOT NULL,
    external_location_id  VARCHAR(128) NOT NULL,
    canonical_location_id VARCHAR(64) NOT NULL,
    region_id             VARCHAR(64),
    import_id             BIGINT REFERENCES import_batches(id),
    CONSTRAINT uq_location_alias UNIQUE (tenant_id, source_system, external_location_id)
);

CREATE TABLE employees (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(64) NOT NULL,
    source_employee_id    VARCHAR(64) NOT NULL,
    display_name          VARCHAR(128),
    employment_status     VARCHAR(32) NOT NULL,
    role                  VARCHAR(64) NOT NULL,
    source_location_code  VARCHAR(64),
    canonical_location_id VARCHAR(64),
    region_id             VARCHAR(64),
    manager_employee_id   VARCHAR(64),
    import_id             BIGINT REFERENCES import_batches(id),
    CONSTRAINT uq_employee UNIQUE (tenant_id, source_employee_id)
);

CREATE TABLE workforce_status (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(64) NOT NULL,
    source_employee_id  VARCHAR(64) NOT NULL,
    availability_status VARCHAR(32) NOT NULL,
    shift_start         TIMESTAMPTZ,
    shift_end           TIMESTAMPTZ,
    observed_at         TIMESTAMPTZ NOT NULL,
    import_id           BIGINT REFERENCES import_batches(id),
    CONSTRAINT uq_workforce UNIQUE (tenant_id, source_employee_id)
);

CREATE TABLE po_lines (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(64) NOT NULL,
    source_system         VARCHAR(16) NOT NULL,
    source_record_id      VARCHAR(128) NOT NULL,
    import_id             BIGINT NOT NULL REFERENCES import_batches(id),
    mapping_version       VARCHAR(32) NOT NULL,
    content_hash          VARCHAR(64) NOT NULL,
    po_number             VARCHAR(64) NOT NULL,
    line_number           INTEGER NOT NULL,
    supplier_code         VARCHAR(64),
    canonical_product_id  VARCHAR(64) NOT NULL,
    canonical_location_id VARCHAR(64) NOT NULL,
    ordered_qty_base      DOUBLE PRECISION NOT NULL,
    base_uom              VARCHAR(16) NOT NULL,
    status                VARCHAR(32) NOT NULL,
    expected_receipt_by   TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_po_source UNIQUE (tenant_id, source_system, source_record_id),
    CONSTRAINT ck_po_qty_nonneg CHECK (ordered_qty_base >= 0)
);

CREATE TABLE receipt_lines (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(64) NOT NULL,
    source_system         VARCHAR(16) NOT NULL,
    source_record_id      VARCHAR(128) NOT NULL,
    import_id             BIGINT NOT NULL REFERENCES import_batches(id),
    mapping_version       VARCHAR(32) NOT NULL,
    content_hash          VARCHAR(64) NOT NULL,
    receipt_number        VARCHAR(64) NOT NULL,
    receipt_line          INTEGER NOT NULL,
    po_number             VARCHAR(64) NOT NULL,
    po_line_number        INTEGER NOT NULL,
    canonical_product_id  VARCHAR(64) NOT NULL,
    canonical_location_id VARCHAR(64) NOT NULL,
    accepted_qty_base     DOUBLE PRECISION NOT NULL,
    base_uom              VARCHAR(16) NOT NULL,
    posting_status        VARCHAR(32) NOT NULL,
    received_at           TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_receipt_source UNIQUE (tenant_id, source_system, source_record_id),
    CONSTRAINT ck_receipt_qty_nonneg CHECK (accepted_qty_base >= 0)
);

CREATE TABLE exceptions (
    id                      BIGSERIAL PRIMARY KEY,
    tenant_id               VARCHAR(64) NOT NULL,
    case_key                VARCHAR(128) NOT NULL,
    public_id               VARCHAR(64) NOT NULL UNIQUE,
    type                    VARCHAR(32) NOT NULL,
    status                  VARCHAR(32) NOT NULL,
    rule_version            VARCHAR(32) NOT NULL,
    mapping_version         VARCHAR(32) NOT NULL,
    po_number               VARCHAR(64) NOT NULL,
    po_line_number          INTEGER NOT NULL,
    canonical_product_id    VARCHAR(64) NOT NULL,
    canonical_location_id   VARCHAR(64) NOT NULL,
    expected_qty_base       DOUBLE PRECISION NOT NULL,
    received_qty_base       DOUBLE PRECISION NOT NULL,
    variance_qty_base       DOUBLE PRECISION NOT NULL,
    base_uom                VARCHAR(16) NOT NULL,
    evidence_refs           TEXT NOT NULL,
    explanation             TEXT NOT NULL,
    resolution_code         VARCHAR(64),
    resolution_evidence_ref VARCHAR(255),
    detected_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_exception_case_key UNIQUE (tenant_id, case_key)
);

CREATE TABLE tasks (
    id                      BIGSERIAL PRIMARY KEY,
    tenant_id               VARCHAR(64) NOT NULL,
    public_id               VARCHAR(64) NOT NULL UNIQUE,
    idempotency_key         VARCHAR(160) NOT NULL,
    exception_id            BIGINT NOT NULL REFERENCES exceptions(id),
    status                  VARCHAR(32) NOT NULL,
    canonical_location_id   VARCHAR(64) NOT NULL,
    required_role           VARCHAR(64) NOT NULL,
    assignee_employee_id    VARCHAR(64),
    assignment_reason       TEXT,
    due_at                  TIMESTAMPTZ,
    resolution_code         VARCHAR(64),
    resolution_evidence_ref VARCHAR(255),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_task_idem UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX ix_tasks_assignee ON tasks (assignee_employee_id);

CREATE TABLE task_events (
    id          BIGSERIAL PRIMARY KEY,
    task_id     BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type  VARCHAR(48) NOT NULL,
    from_status VARCHAR(32),
    to_status   VARCHAR(32),
    actor       VARCHAR(64) NOT NULL,
    reason      TEXT,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_task_events_task ON task_events (task_id);
