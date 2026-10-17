"""CSV -> list[dict] reader.

Kept tiny and separate so ingestion logic is testable without files, and so a
different source format (JSON export, API payload) can feed the same loaders.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def read_csv_rows(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def read_csv_bytes(content: bytes) -> list[dict[str, Any]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    return [dict(row) for row in reader]
