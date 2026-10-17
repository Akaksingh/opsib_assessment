# Architecture note

## One sentence

Source CSVs are validated and loaded as **raw, idempotent facts**, mapped into a
**canonical model** (identity + units), reconciled by a **pure deterministic
detector** into **exceptions**, which spawn **tasks** routed to eligible employees
and driven through a **server-enforced lifecycle** with a full **audit trail** —
and every finding traces back to the source rows and the rule/mapping version
that produced it.

```
CSV ─▶ ingestion ─▶ mapping ─▶ canonical facts ─▶ detection ─▶ exceptions ─▶ routing ─▶ tasks ─▶ lifecycle+audit
      (validate,   (identity,   (PO/receipt      (pure rule)   (idempotent   (eligibility) (state   (append-only
       idempotent)  UOM)         lines, UTC)                    case_key)                   machine)  events)
```

## Layering (and why business state is separate from raw input)

Four layers, deliberately separated (assessment Task A.6):

| Layer | Tables | Authoritative for |
|---|---|---|
| **Raw / audit** | `import_batches`, `import_rows` | exactly what arrived, per row, with outcome + reason |
| **Reference** | `product_aliases`, `location_aliases`, `employees`, `workforce_status` | crosswalks and roster |
| **Canonical** | `po_lines`, `receipt_lines` | business state after identity + UOM normalization |
| **Business** | `exceptions`, `tasks`, `task_events` | derived findings and the work to resolve them |

Raw import rows are never mutated into business state; canonical facts are built
*from* validated input and carry provenance back to it. Business findings are
derived and never hand-edited.

### Authoritative-source policy (per field)

- **Canonical product identity + UOM factor** → `product_aliases.csv`
- **Canonical location identity** → `location_aliases.csv`
- **Employee canonical location** → HRMS code resolved through `location_aliases.csv`
- **Ordered quantity / PO status / deadline** → ERP (`purchase_order_lines.csv`)
- **Accepted quantity / posting status / received time** → WMS (`goods_receipt_lines.csv`)

A **missing or ambiguous mapping is never guessed.** The resolver returns a typed
failure; the row becomes a visible data-quality issue (`REJECTED`, with a reason)
rather than a silently mis-joined business fact.

## Field mapping vs. entity identity vs. business meaning

The assessment stresses this distinction, and the design keeps the three apart:

- **Field mapping** (shape) lives in `app/ingestion/schemas.py` — types, enums,
  tz-aware timestamp parsing. A malformed field fails here.
- **Entity identity** (which canonical product/location/store a source id *is*)
  lives only in `app/mapping/resolver.py`, driven by the alias crosswalks. The
  detector never joins on raw ids or display names — only on canonical ids.
- **Business meaning** (is this a shortfall? who should act?) lives in
  `app/detection/` and `app/routing/`, operating purely on canonical facts.

So `ERP-SKU-001` and `WMS-BRC-001` are recognised as the *same* `SKU-001`, and
`10 BOX` is converted to `120 EA` **before** any comparison — never joined by name
or compared across units.

## Canonical data model & handling source ids that change or collide

- Every canonical fact has a natural identity `(tenant, source_system,
  source_record_id)` enforced **UNIQUE** in the database. This is what makes
  re-import idempotent and collapses the fixture's identical duplicate rows.
- Each canonical fact also stores a **`content_hash`** over its business-relevant
  fields. On re-import:
  - key absent → **ACCEPTED** (insert);
  - key present, hash equal → **DUPLICATE** (no-op);
  - key present, hash differs → **CONFLICT** — flagged in `/data-quality`, the
    existing row is **not overwritten**. A changed source id needs an explicit
    human/versioning decision, not a silent last-writer-wins.
- Exceptions use a **stable `case_key`** = `SHORTFALL::{po}::L{line}::{product}::
  {location}`, UNIQUE per tenant. Re-detection upserts on it, so a business issue
  is one case forever — its id never changes and it never forks.

## Detection: preserving the correct grain

`app/detection/shortfall.py` is pure (no I/O, no AI). It:

1. indexes **POSTED** receipts by `(po_number, po_line_number)`, keyed by
   `source_record_id` so a repeated receipt is stored **once** (PO-506);
2. skips CANCELLED lines (PO-504) and lines whose deadline is still in the future
   at the frozen time (PO-503);
3. sums accepted base-unit quantities for the line and compares to the ordered
   base-unit quantity (UOM already converted, PO-505);
4. raises a shortfall only when `expected − received > tolerance`;
5. blocks as a **data-quality** issue (not a business exception) any line whose
   receipts resolved to a different canonical product.

Aggregation is by **PO line**, never by row count or product name — partial,
split, and repeated receipts all compute correctly.

Every persisted finding records: stable `public_id` + `case_key`, type/status,
`expected/received/variance` in the base unit, the **linked source record ids**
(`evidence_refs`), the **rule and mapping versions**, and a **neutral narrative**
that states what is known and what is unknown and **asserts no cause/fault** —
because the pack has no supplier dispatch/ASN data.

## Routing & lifecycle

- **Eligibility** (`app/routing/eligibility.py`, pure): employment `ACTIVE` is
  mandatory and always wins over a conflicting shift export (so INACTIVE+ON_SHIFT
  HR-103 is excluded); on-shift + a fresh sample is required by default (so
  ACTIVE+OFF_SHIFT HR-101 is excluded); role + canonical location must match.
  If no primary-role receiver is eligible, fall back to an eligible store manager
  (HR-401 at STORE-015). If nothing is eligible, the task is left
  `NEEDS_MANUAL_ASSIGNMENT` — never silently assigned to an ineligible person.
  The full per-candidate trace (who was considered and why each was/ wasn't
  eligible) is persisted on the assignment event.
- **Lifecycle** (`app/workflows/lifecycle.py`): a single transition table is the
  server's source of truth. Clients request *actions*; the server maps them to
  transitions and refuses anything not in the table. Exceptions and tasks are
  separate tables — one exception can have many tasks, and reassignment changes a
  task without forking the exception.
- **Audit**: every transition and assignment change writes an append-only
  `task_events` row (from/to status, actor, reason, detail).
- **Verification gate**: an exception is only marked `RESOLVED` when its task
  carries a valid `resolution_code` **and** an evidence reference.

## Design-question answers

### 1. How does a second import / concurrent worker not produce a different result?

The invariant lives in the database, not just the app. `(tenant, source_system,
source_record_id)` is **UNIQUE** on every canonical table, and each import runs in
a single transaction (`session_scope`). Two workers importing the same row race to
insert; exactly one wins and the other is handled as DUPLICATE/CONFLICT — the
quantity can never be counted twice. SQLite enforces this with `PRAGMA
foreign_keys=ON` + the unique index; Postgres with the same constraint (and
`SELECT … FOR UPDATE` / `INSERT … ON CONFLICT` available for a pilot).

### 2. What if the API commits task completion but the client times out before the response, then retries?

Completion is **idempotent on its target state**. `TaskService.transition` first
checks whether the task already sits in the state the action targets; if so, the
retry is a no-op success returning the same task — no error, no duplicate event,
no double business effect (`test_repeated_completion_is_idempotent`). Callers may
also pass an `idempotency_key`, which is recorded on the transition event for
traceability. The `(tenant, idempotency_key)` UNIQUE on tasks likewise makes task
*creation* safe to retry.

### 3. Why a receipt shortfall rather than a proven supplier fault?

The fixtures contain **no supplier dispatch/ASN data**. We can prove that fewer
units were *accepted and posted* than were *ordered* after the window closed — a
receipt shortfall against the PO. We cannot prove the supplier dispatched less, a
carrier lost stock, or where a discrepancy occurred. The finding narrative is
therefore kept neutral and names the evidence still needed (ASN records, carrier
proof-of-delivery, unposted/in-transit receipts). The optional AI boundary
enforces the same: it may suggest investigation steps but may not assert cause.

## Adding another retailer export without touching the rule engine

Source-specific parsing/mapping is isolated from detection. To add a retailer:
add a row schema + a loader in `app/ingestion/` and the alias rows for their ids.
The canonical facts look identical afterwards, so `app/detection/` and
`app/routing/` are unchanged.
