"""Command-line entry point.

  python -m app.cli init-db
  python -m app.cli load-fixtures [--dir fixtures]
  python -m app.cli detect
  python -m app.cli generate-tasks
  python -m app.cli run-all [--dir fixtures]     # load + detect + create tasks
  python -m app.cli show-exceptions

Each command runs in its own transaction. `run-all` reproduces the full required
scenario slice on a clean database and prints a summary.
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app.db.init_db import init_db
from app.db.models import ExceptionCase
from app.db.session import session_scope
from app.pipeline import create_tasks, load_fixtures, run_detection, run_full_pipeline


def _cmd_init_db(_args):
    init_db()
    print("schema created")


def _cmd_load_fixtures(args):
    init_db()
    with session_scope() as s:
        res = load_fixtures(s, args.dir)
    print(json.dumps(res, indent=2))


def _cmd_detect(_args):
    with session_scope() as s:
        res = run_detection(s)
    print(json.dumps(res, indent=2))


def _cmd_generate_tasks(_args):
    with session_scope() as s:
        res = create_tasks(s)
    print(json.dumps(res, indent=2))


def _cmd_run_all(args):
    init_db()
    with session_scope() as s:
        res = run_full_pipeline(s, args.dir)
    # Print a compact, reviewer-friendly summary of exceptions.
    print(json.dumps({"detection": res["detection"], "tasks": res["tasks"]}, indent=2))


def _cmd_show_exceptions(_args):
    with session_scope() as s:
        rows = s.scalars(select(ExceptionCase).order_by(ExceptionCase.po_number)).all()
        for e in rows:
            print(
                f"{e.public_id}  {e.po_number}  {e.type.value}  {e.status.value}  "
                f"expected={e.expected_qty_base:g} received={e.received_qty_base:g} "
                f"variance={e.variance_qty_base:g} {e.base_uom}  @ {e.canonical_location_id}"
            )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="opsib")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=_cmd_init_db)

    p_load = sub.add_parser("load-fixtures")
    p_load.add_argument("--dir", default="fixtures")
    p_load.set_defaults(func=_cmd_load_fixtures)

    sub.add_parser("detect").set_defaults(func=_cmd_detect)
    sub.add_parser("generate-tasks").set_defaults(func=_cmd_generate_tasks)

    p_all = sub.add_parser("run-all")
    p_all.add_argument("--dir", default="fixtures")
    p_all.set_defaults(func=_cmd_run_all)

    sub.add_parser("show-exceptions").set_defaults(func=_cmd_show_exceptions)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
