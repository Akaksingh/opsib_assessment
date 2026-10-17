"""Timezone-aware time helpers.

Rules of the house:
- Every timestamp parsed from a source MUST be timezone-aware. A naive timestamp
  is a validation error, never silently assumed to be UTC or local.
- Everything is stored in UTC.
- Scenario logic reads the frozen evaluation time, never the machine clock.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import SCENARIO_TZ


def parse_aware(raw: str) -> datetime:
    """Parse an ISO-8601 string into a UTC-normalized, tz-aware datetime.

    Raises ValueError if the input lacks an offset, because guessing a timezone
    for operational deadlines is exactly the kind of silent assumption that
    produces wrong findings.
    """
    value = raw.strip()
    # Accept a trailing 'Z' (ISO UTC designator) that fromisoformat historically
    # rejected on older runtimes.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {raw!r}")
    return dt.astimezone(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("refusing to coerce a naive datetime to UTC")
    return dt.astimezone(timezone.utc)


def display_local(dt: datetime | None) -> str | None:
    """Render a UTC timestamp in the fixtures' scenario timezone, for humans."""
    if dt is None:
        return None
    return dt.astimezone(SCENARIO_TZ).isoformat()
