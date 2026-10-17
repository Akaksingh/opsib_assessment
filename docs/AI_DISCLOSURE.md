# AI tooling disclosure

The assessment allows AI coding tools and requires disclosure of what was used and
what the author independently reviewed. This is that disclosure.

## Tools used

- **Claude Code (Anthropic)** was used to scaffold the project structure, draft the
  modules, write the test suite, and produce this documentation.

## What was independently reviewed / verified (not taken on trust)

Every submitted line is the author's responsibility. The following were checked
directly rather than assumed correct:

- **The six scenario outcomes** were verified by running the pipeline against the
  **unmodified** fixtures and inspecting the numbers (8 / 24 / 10 EA shortfalls;
  PO-502/503/504 raising nothing). They are asserted in
  `tests/test_integration_scenarios.py`, not hard-coded anywhere in the app.
- **Idempotency** was verified by importing the fixtures twice and confirming row
  counts and exception/task counts do not change
  (`tests/test_idempotency.py`), plus a conflicting-duplicate case that must be
  flagged, not overwritten.
- **A real retry-safety bug was found and fixed during review**: the first draft
  validated the lifecycle transition before the idempotent-no-op check, so a
  retried `complete` raised `InvalidTransitionError` instead of returning the
  completed task. This was caught by `test_repeated_completion_is_idempotent`,
  then fixed by adding `target_state()` and checking the no-op case first. (This
  is a concrete example of the author reviewing and changing AI-generated code.)
- **UOM and grain correctness** were checked by hand against the detector
  (10 BOX × 12 = 120 EA; duplicate receipt source id summed once) and covered by
  unit tests that call the pure detector directly.
- **Timezone handling** was verified: all timestamps are parsed tz-aware, stored
  UTC, and the frozen evaluation time drives deadlines. Windows lacked the IANA tz
  database, which surfaced during a test run and was fixed by adding `tzdata` to
  requirements — verified by a green suite afterwards.

## The designed AI boundary (optional feature, not live)

`app/ai/provider.py` defines an optional investigation-assistant interface that is
**deliberately not calling any model** (no key required, no spend):

- The **deterministic detector** produces the finding and the authoritative
  numbers. The AI service receives only a bounded, already-computed evidence
  package.
- The AI may return a **narrative** summary and suggested next steps. It must not
  calculate authoritative quantities, invent records, decide permissions, perform
  ERP writes, or mark anything verified.
- Two guards enforce this regardless of model output:
  1. output is **schema-validated** (pydantic); malformed output is discarded;
  2. every cited source id is **checked against the supplied evidence** — a
     hallucinated id is rejected and the summary is never shown; a summary that
     asserts a cause/fault is rejected.
- `safe_summarize()` returns `None` on any failure (timeout, bad schema, boundary
  violation), so the application stays fully functional without the model.

These guards are tested in `tests/test_failure_paths.py`.
