"""Application configuration.

Everything that varies by environment lives here. Defaults keep the service
runnable on a clean checkout with zero infrastructure (SQLite + the frozen
evaluation time from the fixture README). A live pilot overrides these via
environment variables.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Rule / mapping versions.
#
# These are stamped onto every canonical fact and every finding so a reader can
# trace *which* logic produced a result. Bump them when the detection rule or the
# mapping semantics change; existing findings keep the version that computed them.
# ---------------------------------------------------------------------------
RULE_VERSION = "shortfall-v1"
MAPPING_VERSION = "mapping-v1"

# Single-tenant default for this slice. Every row still carries a tenant_id so the
# model is ready for the multi-tenant isolation described in the README.
DEFAULT_TENANT = "retailer-demo"

# The timezone the fixtures are authored in. Used for *display*; all storage is UTC.
SCENARIO_TZ = ZoneInfo("Asia/Kolkata")


class Settings:
    """Runtime settings resolved from the environment once at process start."""

    def __init__(self) -> None:
        # SQLite keeps the slice runnable with no infra. Point DATABASE_URL at a
        # Postgres DSN for the pilot (e.g. postgresql+psycopg://user:pw@host/db).
        self.database_url: str = os.getenv(
            "OPSIB_DATABASE_URL", "sqlite:///./opsib.db"
        )

        # Frozen evaluation time. Scenario logic (deadlines, shift freshness) MUST
        # use this rather than the wall clock, so results are reproducible.
        self.frozen_eval_time: datetime = _parse_frozen_time(
            os.getenv("OPSIB_FROZEN_EVAL_TIME", "2026-10-16T15:00:00+05:30")
        )

        self.default_tenant: str = os.getenv("OPSIB_DEFAULT_TENANT", DEFAULT_TENANT)

        # Shortfall tolerance in base units. Zero unless explicitly configured.
        self.shortfall_tolerance: float = float(
            os.getenv("OPSIB_SHORTFALL_TOLERANCE", "0")
        )

        # How stale a workforce sample may be (minutes) and still count toward
        # on-shift eligibility, measured from the frozen evaluation time.
        self.workforce_freshness_minutes: int = int(
            os.getenv("OPSIB_WORKFORCE_FRESHNESS_MINUTES", "120")
        )

        # Whether on-shift status is required for assignment (employment is always
        # required). Defaults on, matching the assessment's routing scenarios.
        self.require_on_shift: bool = _env_bool("OPSIB_REQUIRE_ON_SHIFT", True)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def _parse_frozen_time(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError("OPSIB_FROZEN_EVAL_TIME must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
