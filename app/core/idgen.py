"""Public, human-friendly, collision-resistant ids for exceptions and tasks.

These are *external* identifiers shown in the API. They are distinct from the
stable business `case_key` (which drives idempotency) and the surrogate PK.
"""
from __future__ import annotations

import uuid


def new_public_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
