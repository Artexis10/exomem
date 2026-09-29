## Context

`records.append_record` validates one candidate, resolves identity (declared natural key, else a UUID), takes the writer lease, re-reads the collection, checks `expected_container_hash`, detects a content-identical replay, plans one item file, a manifest audit-head update and one log entry, and publishes them with `vault.batch_atomic_write`. The container hash is over the whole item set, so it changes on every commit. That is correct for one item and is what made 24 rows a 24-round serial chain.

Already true and reused: natural-key identity (`collections.derived_item_key`, `_natural_key_twins`), content replay (`_payload_hash`, `_replay_audit_correlation`), all-or-nothing multi-file publication (`batch_atomic_write`, `transactional-vault-writes`), the audit chain (`_audit_body`, `_plan_required_audit`), and transport-level idempotency (`mutation_request_id` and the per-vault idempotency store, REST `Idempotency-Key`).

## Goals / Non-Goals

**Goals:** N rows, one guard, one write, one receipt; a complete per-row outcome report; safe retry; no weaker guarantee than a single append.

**Non-Goals:** streaming or resumable multi-request imports (N is bounded at 500); a bulk `update` of arbitrary existing items by `item_key` (this action upserts by natural key only); bulk deletion; server-side interpretation or normalisation of rows (the caller supplies normalised rows); the `dataset` storage strategy (still read-only); any change to hosted frozen candidates.

## Decisions

### 1. A new action, not a longer `append`

`append` has a one-item contract (`item`, `item_key`, `held`, a single result). Overloading it with a list would make every existing argument rule conditional. `bulk_upsert` gets its own field set: `collection`, `rows`, `why`, `expected_container_hash`, `source`, `on_reject`, `idempotency_key`. It is validated by the same `_ACTION_FIELDS` / `_REQUIRED_FIELDS` machinery, so surplus and missing fields refuse in one message as today.

### 2. Request shape

```
record_memory(
  action="bulk_upsert",
  collection="...",
  why="...",                       # one audit reason for the batch, <= 512 bytes
  expected_container_hash="...",   # required; ONE guard for the whole batch
  source="Evidence/....md",        # optional batch default provenance ref
  on_reject="abort" | "skip",      # default "abort"
  idempotency_key="...",           # optional, 8-128 chars of [A-Za-z0-9._:-]
  rows=[ { "item": {...}, "body": "...", "source": "Evidence/....md" }, ... ]  # 1..500
)
```

Each row is `{item, body?, source?}`. `item_key` is not accepted per row: identity is the declared natural key, so a collection without a complete natural key refuses `bulk_upsert` (`BULK_UPSERT_NEEDS_NATURAL_KEY`). That is what makes the action an upsert rather than a blind append, and it removes the UUID-guessing failure class. A row's provenance is `row.source` or, absent that, the batch `source`; a row with neither is rejected `SOURCE_REQUIRED`.

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

Two rows in one request that derive the same identity are a caller error: the later one is `rejected` `DUPLICATE_ROW_KEY` naming the earlier row's index, never a silent last-write-wins.

`updated` replaces the item's values wholesale with the row's values, and its body only when the row supplies one; it does not merge. This differs from single `append`, which refuses a different payload for a held identity (`RECORD_ID_CONFLICT`). That difference is the point of "upsert" and is stated in `describe`.

### 5. Provenance

`row.source` (or the batch `source`) must resolve, through the ordinary reader and release filter for the calling audience, to a preserved Evidence or Source page. An unresolvable reference and a withheld one produce the identical rejection (`SOURCE_NOT_FOUND`) with identical details, so the response cannot be used to probe for hidden pages. The reference is bound into the row: if the collection declares a link-array field named `sources` the server appends the reference to it when absent; if there is no such field the row is rejected `SOURCE_FIELD_UNDECLARED` at validation, before any write. A batch may not fabricate provenance: the reference is only ever a page that exists at commit time, and it is re-checked (path guard) at publication like a single append's delivery evidence.

### 6. Atomic by default; skip mode is still one atomic write

Planning finishes before any write. In `abort` mode a single rejected row means zero writes; the response carries every row's outcome (rows that would have been accepted read `inserted`/`updated`/`unchanged` with `committed: false` on the batch), so one round trip finds every problem. In `skip` mode the accepted rows go into ONE `batch_atomic_write` and the rejected rows are reported; a crash mid-publication rolls the whole batch back per `transactional-vault-writes`. The response has a top-level `committed` boolean and `counts` per outcome, so "partial" is never inferred. Unchanged rows never write. If every row is `unchanged` or rejected, nothing is written and the audit head does not advance.

### 7. One receipt

One audit transition (`operation: "bulk_upsert"`) is appended to the collection's audit chain and one log entry to `Knowledge Base/log.md`. The transition body carries the batch payload hash (over the ordered row payload hashes and the resolved provenance refs), the row count, counts per outcome, the batch idempotency key when one was given, and the before/after container hashes. It does NOT carry row values. Each written item file carries the transition id in its own audit marker, exactly as a single append's does, so item-level audit correlation and `_replay_audit_correlation` keep working unchanged. The per-item marker count and audit source size caps (`_MAX_AUDIT_MARKERS`, `_MAX_AUDIT_SOURCE_BYTES`) are checked once for the projected batch; a batch that would breach them refuses `BULK_UPSERT_TOO_LARGE` before planning.

### 8. Idempotency and exactly-once

Two independent layers, both required:

1. **Content replay.** Because identity is natural-key-derived and identical payloads are `unchanged`, replaying a committed batch writes nothing and reports every row `unchanged`. This holds with no key at all and after the idempotency store's TTL.
2. **Explicit key.** When `idempotency_key` is supplied it is bound, with the collection and the batch payload hash, into the receipt. A retry with the same key and same payload returns the recorded result with `replayed: true` and writes nothing, even when the container hash has moved since. The same key with a different payload refuses `IDEMPOTENCY_KEY_REUSED`. This composes with the transport idempotency store; it is the durable, vault-resident record of it, so it survives a store eviction.

Exactly-once: a batch either commits under `batch_atomic_write` (one transition, all planned files) or leaves no file, no audit entry and no manifest change. A crash between "commit" and "response" is resolved by either layer above.

### 9. Withheld-as-absent

The writer refuses a mutation the caller's release filter would not allow (`require_mutation_visibility`, `precommit_authorize_mutation`), planned over the union of every path the batch would touch. Existing items the caller cannot read are treated as absent for outcome purposes: a natural-key collision with a withheld item is reported `rejected` `RECORD_NATURAL_KEY_CONFLICT` with no `item_keys` (a single append does the same for a twin it may not disclose), never as `updated` or `unchanged`, so the outcome cannot reveal whether a hidden row exists or what it holds. The response's counts and row echoes never include values from any item the caller cannot read.

### 10. Limits

500 rows per call (a proposal; the constant is `BULK_UPSERT_MAX_ROWS`, exposed by `describe`). Each row obeys the existing per-value (32 KiB) and body limits; the request is also bounded in total bytes. The existing collection ceiling (`_MAX_ITEM_FILES`, 2,000) applies to the projected post-batch item count, so a batch that would cross it refuses whole with `COLLECTION_ITEM_LIMIT`. A larger source is sent as several batches, each chained from the previous response's container hash: still 1/500th of the round trips.

### 11. Held candidates

Bulk does not hold. A rejected row is reported with its diagnostics and is not written to the held-candidate store, because 500 held files from one call is exactly the noise that store was designed to avoid, and the caller already holds the complete rows. A caller wanting one row held or resumed uses `append` for it.

### 12. Tool surface

`bulk_upsert` joins the `record_memory` action enum and argument rules. Frozen hosted candidates (v1, v2, v3 Claude) keep their released schemas byte-for-byte; the local surface, the v5 candidate and the command binding are regenerated, along with the packaged guidance, plugin tree, hosted render and `docs/capabilities.md`, per `CONTRIBUTING.md`.

## Risks / Trade-offs

- **Blast radius of one wrong batch.** Upsert can overwrite. Mitigation: the container-hash guard, `abort` default, per-row outcomes with the updated identities named, and the audit transition. A dry-run is `validate`-shaped and deliberately deferred (see Open Questions).
- **Larger single transaction.** One `batch_atomic_write` of up to ~500 item files. Bounded by N and by the existing item ceiling; measured before the constant is ratified.
- **Behavioural difference from `append`.** `updated` for a changed payload. Named in `describe` and the spec so agents do not expect `RECORD_ID_CONFLICT`.

## Open Questions

Listed in the PR under "Needs ruling": API shape, `N`, provenance binding, upsert-replace semantics, explicit `idempotency_key` versus transport key only, dry-run.
