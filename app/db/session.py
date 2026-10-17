"""Engine and session factory.

The engine is built once from settings. SQLite needs a couple of pragmas to
behave like a real database for our invariants:
- foreign_keys=ON so FK constraints are actually enforced.
- a short busy_timeout so concurrent writers wait rather than failing instantly.

On Postgres these are no-ops; the same SQLAlchemy models and constraints apply.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

_connect_args = {}
if _settings.is_sqlite:
    # check_same_thread=False lets FastAPI's threadpool share the engine safely;
    # we still hand out one Session per request/unit-of-work.
    _connect_args = {"check_same_thread": False}

engine: Engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver glue
    if _settings.is_sqlite:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional unit of work: commit on success, roll back on any error.

    Business invariants (idempotency, single-task-per-case) are enforced by the
    constraints that commit inside this boundary, so a half-applied import can
    never be observed.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency. The endpoint commits via its service layer; we only
    guarantee the session is closed and rolled back on an unhandled error."""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
