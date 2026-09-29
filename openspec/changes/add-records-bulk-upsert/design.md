## Context

`records.append_record` validates one candidate, resolves identity (declared natural key, else a UUID), takes the writer lease, re-reads the collection, checks `expected_container_hash`, detects a content-identical replay, plans one item file, a manifest audit-head update and one log entry, and publishes them with `vault.batch_atomic_write`. The container hash is over the whole item set, so it changes on every commit. That is correct for one item and is what made 24 rows a 24-round serial chain.

Already true and reused: natural-key identity (`collections.derived_item_key`, `_natural_key_twins`), content replay (`_payload_hash`, `_replay_audit_correlation`), all-or-nothing multi-file publication (`batch_atomic_write`, `transactional-vault-writes`), the audit chain (`_audit_body`, `_plan_required_audit`), and transport-level idempotency (`mutation_request_id` and the per-vault idempotency store, REST `Idempotency-Key`).

## Goals / Non-Goals

**Goals:** N rows, one guard, one write, one receipt; a complete per-row outcome report; safe retry; no weaker guarantee than a single append.

**Non-Goals:** streaming or resumable multi-request imports (N is bounded at 500); a bulk `update` of arbitrary existing items by `item_key` (this action upserts by natural key only); bulk deletion; server-side interpretation or normalisation of rows (the caller supplies normalised rows); the `dataset` storage strategy (still read-only); any change to hosted frozen candidates.

## Decisions

### 1. A new action, not a longer `append`

`append` has a one-item contract (`item`, `item_key`, `held`, a single result). Overloading it with a list would make every existing argument rule conditional. `bulk_upsert` gets its own field set: `collection`, `rows`, `why`, `expected_container_hash`, `source`, `on_reject`. It is validated by the same `_ACTION_FIELDS` / `_REQUIRED_FIELDS` machinery, so surplus and missing fields refuse in one message as today.

### 2. Request shape

```
record_memory(
  action="bulk_upsert",
  collection="...",
  why="...",                       # one audit reason for the batch, <= 512 bytes
  expected_container_hash="...",   # required; ONE guard for the whole batch
  source="Evidence/....md",        # optional batch default provenance ref
  on_reject="abort" | "skip",      # default "abort"
  rows=[ { "item": {...}, "body": "...", "source": "Evidence/....md" }, ... ]  # 1..500
)
```

Each row is `{item, body?, source?}`. `item_key` is not accepted per row: identity is the declared natural key when the collection has a complete one. A collection WITHOUT a complete natural key is not refused: bulk runs **insert-only** there, each row gets a fresh UUID identity, the outcome is `inserted` or `rejected` and never `updated` or `unchanged`, and re-running the same rows duplicates them. `describe` and the response (`identity: "natural-key" | "generated"`) say so. A row's provenance is `row.source` or, absent that, the batch `source`; a row with neither is rejected `SOURCE_REQUIRED`.

### 3. One guard, checked once, inside the lease

The whole action runs inside one `mutation_guard`. The collection is read once, `expected_container_hash` is compared once (`CONTAINER_HASH_MISMATCH` refuses the entire batch, nothing planned), and every row is planned against that one snapshot plus the rows already planned in the same batch. The hash after the batch is the ordinary container hash of the resulting item set, so the next guarded call chains from the response as it does today.

### 4. Row planning is the single-append plan, run in a loop

For each row, in input order: schema and representability validation, size limits, identity from the natural key, natural-key twin check, payload hash, then the outcome:

| Situation | Outcome |
| --- | --- |
| no item holds the identity | `inserted` |
| item holds identity, identical payload hash | `unchanged` (no write) |
| item holds identity, different payload | `updated` (item file replaced) |
| twin holds the natural key under another identity, ambiguous record, invalid values, provenance failure | `rejected` with `code` and field paths |

On a natural-keyed collection, two rows in one request that derive the same identity are a caller error: the later one is `rejected` `DUPLICATE_ROW_KEY` naming the earlier row's index, never a silent last-write-wins.

`updated` replaces the item's values wholesale with the row's values, and its body only when the row supplies one; it does not merge. This differs from single `append`, which refuses a different payload for a held identity (`RECORD_ID_CONFLICT`). That difference is the point of "upsert" and is stated in `describe`.

### 5. Provenance

`row.source` (or the batch `source`) must resolve, through the ordinary reader and release filter for the calling audience, to a preserved Evidence or Source page. An unresolvable reference and a withheld one produce the identical rejection (`SOURCE_NOT_FOUND`) with identical details, so the response cannot be used to probe for hidden pages. When the collection declares a link-array field named `sources`, the server appends the verified reference to it when absent. When it declares none the row is NOT rejected: the verified reference is recorded per row index in the audit receipt instead (decision 7). A batch may not fabricate provenance: the reference is only ever a page that exists at commit time, and it is re-checked (path guard) at publication like a single append's delivery evidence.

### 6. Atomic by default; skip mode is still one atomic write

Planning finishes before any write. In `abort` mode a single rejected row means zero writes; the response carries every row's outcome (rows that would have been accepted read `inserted`/`updated`/`unchanged` with `committed: false` on the batch), so one round trip finds every problem. In `skip` mode the accepted rows go into ONE `batch_atomic_write` and the rejected rows are reported; a crash mid-publication rolls the whole batch back per `transactional-vault-writes`. The response has a top-level `committed` boolean and `counts` per outcome, so "partial" is never inferred. Unchanged rows never write. If every row is `unchanged` or rejected, nothing is written and the audit head does not advance.

### 7. One batch receipt over per-item chained events (ruled: option A)

The audit protocol is unchanged. It is strictly one event per item (one `item_key`, `canonical_path` and `after_item_hash`; every item marker must match its event; the chain walk is capped at 2048 events; each event is one `log.md` entry), so a single bulk event would need a new event version and reader-compatibility work. That is deferred (option B, a follow-up only if depth or log size matters in practice).

Instead each written row gets an ordinary `append` or `update` event, chained in input order. Their intermediate manifest and container hashes are computed in memory in sequence, so every event's before/after hashes chain exactly as N serial calls' would, but only the final state is ever published: the item files, ONE manifest write whose audit head is the last event, and ONE combined `log.md` write carrying N entries (`vault.plan_log_writes_many`). Publication is a single `batch_atomic_write`, so the chain on disk is either the old chain or the whole new one.

Each event's rationale is `<why> | bulk <batch_id> <i>/<n>` plus, when the collection declares no `sources` link field, ` src <ref>`; it must fit the existing 512-byte rationale bound or that row is rejected `AUDIT_RATIONALE_TOO_LONG`. Events carry no row values.

The response carries ONE batch receipt: `batch_id`, `rows`, `counts`, `committed`, `first_transition`, `last_transition`, and per-row `{index, outcome, item_key, transition_id?, source?, code?, fields?}`.

**Chain-depth budget.** The chain walk refuses beyond 2048 events, so a bulk consumes one event per written row. Before planning writes, the batch computes the collection's used depth (events for this collection across the live log and archives, an upper bound of the reachable chain) and refuses `BULK_UPSERT_AUDIT_DEPTH` naming used, needed and budget when used + needed > 2048. It never half-writes. `describe` states the budget.

### 8. Idempotency and exactly-once

No new argument. Two existing layers compose:

1. **Transport idempotency.** The command dispatcher already binds `mutation_request_id` and the implicit retry scope to every mutation; a retry under the same transport identity (REST `Idempotency-Key`) returns the recorded result without re-executing. `bulk_upsert` is an ordinary mutating command and inherits this unchanged.
2. **Content replay.** On a natural-keyed collection identical payloads are `unchanged`, so replaying a committed batch writes nothing and reports every row `unchanged`, with no key and after the store's TTL. On an insert-only collection there is no content replay, so retry safety there rests on layer 1 alone (documented).

Exactly-once: a batch either commits under `batch_atomic_write` (all events, all planned files) or leaves no file, no audit event and no manifest change. A crash between "commit" and "response" is resolved by either layer above.

### 9. Withheld-as-absent

The writer already refuses any mutation unless the caller's release filter allows the manifest, the source, and EVERY item entry of the collection (`require_mutation_visibility`, `COLLECTION_NOT_FOUND`), and `precommit_authorize_mutation` re-checks the union of every path to be written. A mutating caller therefore never holds a partial view: a hidden item makes the whole collection read as absent. Bulk runs those same two gates once, before planning, and once over the union of all planned paths before publication. So a hidden natural-key twin cannot exist for a caller that reaches planning, and no outcome, count or echo can reveal one. The earlier draft's "rejected without `item_keys`" case is dropped as unreachable.

### 10. Limits

500 rows per call (a proposal; the constant is `BULK_UPSERT_MAX_ROWS`, exposed by `describe`). Each row obeys the existing per-value (32 KiB) and body limits; the request is also bounded in total bytes. The existing collection ceiling (`_MAX_ITEM_FILES`, 2,000) applies to the projected post-batch item count, so a batch that would cross it refuses whole with `COLLECTION_ITEM_LIMIT`, and the chain-depth budget (decision 7) refuses whole with `BULK_UPSERT_AUDIT_DEPTH`. A larger source is sent as several batches, each chained from the previous response's container hash: still 1/500th of the round trips.

### 11. Held candidates

Bulk does not hold (ruled). A rejected row is reported with its diagnostics and is not written to the held-candidate store, because 500 held files from one call is exactly the noise that store was designed to avoid, and the caller already holds the complete rows. A caller wanting one row held or resumed uses `append` for it.

### 12. Tool surface

`bulk_upsert` joins the `record_memory` action enum and argument rules. Frozen hosted candidates (v1, v2, v3 Claude) keep their released schemas byte-for-byte; the local surface, the v5 candidate and the command binding are regenerated, along with the packaged guidance, plugin tree, hosted render and `docs/capabilities.md`, per `CONTRIBUTING.md`.

## Risks / Trade-offs

- **Blast radius of one wrong batch.** Upsert can overwrite. Mitigation: the container-hash guard, `abort` default, per-row outcomes with the updated identities named, and the audit transition. No dry-run action (ruled).
- **Larger single transaction.** One `batch_atomic_write` of up to ~500 item files. Bounded by N and by the existing item ceiling; measured before the constant is ratified.
- **Behavioural difference from `append`.** `updated` for a changed payload. Named in `describe` and the spec so agents do not expect `RECORD_ID_CONFLICT`.

## Open Questions

None: ruled on PR #1452 (N = 500 ratified and measured after building; abort default, no dry-run; provenance falls back to the receipt; transport key plus content replay only; insert-only without a natural key).
