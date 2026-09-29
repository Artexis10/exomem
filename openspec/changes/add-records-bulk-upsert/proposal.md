## Why

A 24-row, already-normalised, structured source had to be appended to a Records collection through 24 separate guarded `record_memory` appends. Every append changes the container hash, so each call needed the hash returned by the previous one: writes were forced serial, any transport retry had to re-read first, and the run took about 21 minutes. Querying the finished collection was fast. The cost is entirely the per-item guard round trip, not the storage. Anything shaped like a small import (a normalised export, a table lifted from a preserved source, a backfill of a ledger) hits the same wall, and the wall grows linearly with the row count.

## What Changes

- **One new `record_memory` action, `bulk_upsert`.** It takes up to 500 rows and commits them under ONE container-hash guard, ONE batch receipt and ONE `batch_atomic_write` (per-item chained audit events, unchanged protocol).
- **Per-row validation and identity.** Each row is validated exactly as a single append is (schema, representability, undeclared fields, size limits). Identity derives from the collection's declared natural key, so a re-stated row is `unchanged` and a changed row is `updated`. A row carrying only a natural-key value already held by another identity is `rejected`, as a single append refuses it. A collection without a complete declared natural key is not refused: bulk runs insert-only there (`inserted` or `rejected`, never `updated`), and re-running the same rows duplicates them.
- **Per-row provenance.** Every row points at a preserved Evidence or Source page. The reference is resolved through the same release filter as any read: a page the caller may not read is reported exactly as an absent one.
- **Per-row outcomes and an honest commit state.** The response lists one outcome per input row, in input order: `inserted`, `updated`, `unchanged` or `rejected` with a code and field paths. The batch is atomic by default (`on_reject: "abort"`: any rejection writes nothing). `on_reject: "skip"` commits every accepted row in one atomic batch and reports the rest as rejected. There is no state in which a row's outcome is unknown after a response.
- **One batch receipt, idempotent on retry.** One batch receipt covers the batch; the audit protocol is unchanged, so each written row still gets its ordinary chained event, all published in one atomic write with one combined log write. A retry under the same transport idempotency identity returns the recorded result without a second write, and on a natural-keyed collection a retry without one is still safe because every row replays as `unchanged`. No new argument is added.
- **Chain-depth budget.** A batch that would carry the audit chain past its 2048-event depth refuses up front (`BULK_UPSERT_AUDIT_DEPTH`) and never half-writes. A bulk audit event version is a follow-up, only if depth or log size matters in practice.
- **No new guarantees to invent.** Exactly-once and withheld-as-absent are inherited from the single append; the spec delta states them for the batch.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: a governed bulk append and upsert requirement, and its scenarios.

## Impact

Code touched at implementation: `record_memory` (action, argument rules, docstring), `records` (a bulk writer beside `append_record`, sharing its validation, replay, audit and batch-write helpers), `commands.op_record_memory` (parameters), and the Records `describe` contract. The action is an addition to an existing tool, so no new MCP tool is created. Released frozen hosted candidates (v1, v2, v3 Claude) do not change; the local surface, v5 and the command binding pick it up, and the derived artifacts are regenerated per `CONTRIBUTING.md`. Existing manifests stay valid: no schema-version bump. No new dependency.
