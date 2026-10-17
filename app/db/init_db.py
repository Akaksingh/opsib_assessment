"""Create schema.

For this slice, `create_all` builds the tables from the ORM models (the models ARE
the schema of record). A hand-written Alembic migration is provided under
migrations/ documenting the same DDL for a Postgres pilot; see README for how a
live deployment would run migrations instead of create_all.
"""
from __future__ import annotations

from app.db.models import Base
from app.db.session import engine


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    Base.metadata.drop_all(bind=engine)


if __name__ == "__main__":  # pragma: no cover
    init_db()
    print("schema created")
