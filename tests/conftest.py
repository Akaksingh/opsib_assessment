"""Test fixtures.

Each test runs against a fresh file-backed SQLite database in a temp dir, so the
schema and all constraints (including UNIQUE invariants) are exercised exactly as
they would be on Postgres. We set the env BEFORE importing app modules, because
the engine is constructed once from settings at import time.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point the whole app at an isolated temp database before any app import.
_TMPDIR = tempfile.mkdtemp(prefix="opsib-test-")
os.environ.setdefault("OPSIB_DATABASE_URL", f"sqlite:///{Path(_TMPDIR) / 'test.db'}")
# Lock the frozen evaluation time to the fixture README value for deterministic tests.
os.environ.setdefault("OPSIB_FROZEN_EVAL_TIME", "2026-10-16T15:00:00+05:30")

from app.db.init_db import drop_all, init_db  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture()
def db():
    """A clean schema per test."""
    drop_all()
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES_DIR
