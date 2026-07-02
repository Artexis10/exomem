# Design - event-maintained indexes

## Context

Three costs measured on production (2026-07-02, ~1900-file vault) share one root cause: freshness,
the embedding/CLIP matrix, and the inbound-link index are each recomputed from scratch (a walk, a
full sqlite reload, a full-vault read) on the request path, instead of being maintained
incrementally from the write/watch events the server already observes.

**P1 — freshness stat-walk.** `_walk_freshness_key` (`find.py:648-676`) walks a markdown tree and
returns `(count, max_mtime_ns, digest)`. `FreshnessSnapshot` (`find.py:677-699`) memoizes `.kb()`
and `.vault()` per `find()` call, but a fresh `FreshnessSnapshot` is built per call, so every call
pays the walk again. Worse: this triple *is* the hot-find-cache key (`find.py:876-881`), so even a
cache **hit** pays the walk first, to decide whether the cached entry is still valid, before it can
answer from cache. A `scope="kb"` query with a non-empty `query_norm` also triggers auto-widen's
`scope="vault"` BM25 search, which needs `.vault()` too — two walks per request, not one.

**P2 — per-call matrix reload.** `EmbeddingIndex` and `ClipIndex` (`embeddings.py:771-782`,
`946-959`) hold a **per-instance** `self._cache` — `(sidecar_mtime, metadata, matrix)`, invalidated
by comparing the sidecar's mtime on every `all_vectors()` call (`embeddings.py:842-871`). The
docstring's claim that this is "cached per-process" (`embeddings.py:774`) is false on the request
path, because `find._find_semantic` (and the CLIP lane) construct a **new** `EmbeddingIndex(
vault_root)` / `ClipIndex(vault_root)` on every call (`find.py:1252`, `find.py:1298`) — a fresh
instance has an empty `self._cache`, so `all_vectors()` always re-reads and re-stacks every row
from `.embeddings.sqlite`, every hybrid find. Warm-process cost is ~250-300ms; under a concurrent
out-of-process sidecar writer (the `backfill` CLI), the sidecar mtime moves mid-request and the
cost inflates to 13-27s because every subsequent find in the same window also reloads the full
table.

**P3 — full-vault inbound re-read.** `_INBOUND_INDEX` (`vault.py:505-553`) is a module-level cache
keyed by a digest-strength freshness key (`_inbound_index`, `vault.py:542-553`), which is correctly
invalidation-safe — but on any invalidation, `_build_inbound_index` (`vault.py:508-539`) does a full
`walk_vault_md` read pass over every markdown file, re-parsing wikilinks from scratch. Consumers:
`find_inbound_wikilinks` (`vault.py:561+`) → `list_inbound_links` (the `list_inbound_links` tool),
`commands.py:233` (the `get` tool's inbound-links field), `context_pack._neighborhood`
(`context_pack.py:209`, called once per packed page — the pack's ≈5s cost), and the
`move_file`/`delete_file`/`delete_directory` pre-write safety checks (`move_file.py:142`,
`delete_file.py:173`, `delete_directory.py:155`) that must see current inbound links before
allowing a destructive operation.

The file watcher (`file_watcher.py`) already exists and already watches `<vault>/Knowledge Base/`
for `.md` changes, debounces them (~500ms), and dispatches through the same
`embeddings.upsert_after_write`/`delete_after_remove` paths writers use; it already has a
module-level self-write suppression registry so writer-authored mutations don't re-embed twice.
Writer paths (`vault.batch_atomic_write`, `delete_file`, `move_file`, `delete_directory`,
`reconcile`, `media_worker`, `scene_frames`, `backfill`, `audit_fix`'s `rebuild_all`) already call
`embeddings.upsert_after_write`/`delete_after_remove`/`rebuild_all` at well-defined points — this
change reuses those exact call sites to also publish into the new registries, rather than adding a
new event bus. `server.py`'s watcher startup gate is currently `not
EXOMEM_DISABLE_FILE_WATCHER and not EXOMEM_DISABLE_EMBEDDINGS` (`server.py:348-351`) — the watcher
is coupled to embeddings today only because there was no other reason to run it; this change gives
it one (freshness/inbound maintenance), so the embeddings condition is dropped.

`warmup.warm_all` (`warmup.py:93-153`) already runs lexical warm-up (parsed pages, both BM25
scopes, the wikilink resolver, the embedding/CLIP matrix warm) before model preloads, then calls
`readiness.mark_ready("lexical")` — the natural seed point for the new registries, since the vault
is already being walked at that point for other reasons.

## Goals / Non-Goals

**Goals:**

- Make `find`'s per-request freshness check sub-millisecond and syscall-free whenever the registry
  is live, with results byte-identical to today's walk-based computation.
- Make the embedding/CLIP matrix persist across finds in the same process, with cross-process
  writer changes (backfill, a second sidecar connection) detected and applied incrementally
  instead of triggering a full reload.
- Make the inbound-link index update per-file instead of re-reading the whole vault, with output
  identical (content and, where documented, order) to a full rebuild.
- Give every one of the above a not-live fallback that reproduces today's walk/reload/rebuild
  behavior exactly, and a single kill switch that reverts all three at once.
- Do this without a new event bus: extend the file watcher's existing debounced flush and the
  writer paths' existing embedding-hook call sites.

**Non-Goals:**

- No sqlite schema migration for the embedding/CLIP sidecars (no generation column, no
  `PRAGMA data_version` dependency).
- No change to `find` ranking, ordering, or return shape — this is purely an internal efficiency
  and correctness change to freshness/matrix/inbound bookkeeping.
- No coverage of the one documented residual edge case in the matrix delta reload (see Decisions,
  D2) — it cannot arise from real write flows.
- No new command-registry parameters, no MCP/REST/CLI/OpenAPI schema change.

## Decisions

### D1 — Event-maintained freshness registry (`src/exomem/freshness.py`, new, ~150 lines)

A module-level registry keyed by `(vault_root, scope)` holds an in-memory `{rel_path: mtime_ns}`
map per scope, plus a derived digest-strength triple `(count, max_mtime_ns, digest)` recomputed
from the map on read (sub-ms, zero syscalls — no stat calls, since the map already holds every
file's mtime). `FreshnessSnapshot.kb()`/`.vault()` (`find.py:677-699`) consult the registry first;
when the registry for that `(vault_root, scope)` is live, they return the derived triple directly.
When it is not live (registry never seeded, or explicitly disabled), they fall back to today's walk
byte-for-byte, so every caller that does not know about the registry keeps working unchanged.

**Watcher scope.** `FileWatcher` extends to watch the vault root (one `watchdog` observer for the
whole tree) instead of only `Knowledge Base/`. Embedding re-index dispatch (`upsert_after_write`/
`delete_after_remove`) stays KB-filtered exactly as today — only the *freshness* observation widens,
not what gets embedded. Watcher startup decouples from embeddings: `server.py`'s gate
(`server.py:348-351`) becomes `not EXOMEM_DISABLE_FILE_WATCHER` only. The embed-dispatch path
already no-ops harmlessly when embeddings are disabled (`upsert_after_write` degrades to a no-op /
logged skip), so decoupling the gate does not risk calling into a disabled embeddings subsystem.

**Seed.** One full walk per scope at watcher start, run from `warm_all` (`warmup.py:104-105`)
immediately after `readiness.mark_ready("lexical")` — the vault is already being walked there for
BM25/resolver warm-up, so seeding the registry from the same pass is free.

**Reconcile bound.** A periodic reconcile re-walk runs every 300s on the watcher's own thread,
independent of file events, and re-derives the map from a fresh walk. A mismatch between the
event-maintained map and the fresh walk is logged as `freshness_reconcile_drift` (stats only — no
change is silently dropped, the walk's result wins and replaces the map) and bounds how long a
missed filesystem event (a `watchdog` drop, which does happen under high-volume bursts) can leave
the registry stale.

**Parity rule (critical).** The event-maintained maps MUST apply the exact same inclusion rules as
the walks they replace: `VAULT_SCAN_SKIP_DIRS`, `.md`-only, the same tree roots (`Knowledge Base/`
for kb scope, the vault root for vault scope). This is what makes the live triple and the fallback
walk's triple equal on an identical tree — a registry that included one extra directory or a
non-`.md` file would silently diverge from the walk it is supposed to be interchangeable with.

**Rename-with-same-mtime.** Because `rel_path` is part of what the digest is computed over
(mirroring `_walk_freshness_key`'s existing digest-of-sorted-rel-paths design), a rename still
changes the digest even when the file's mtime is preserved across the rename — the registry
inherits this property for free because it stores exactly the map the digest is derived from.

**Self-write ordering (subtlety, ties into D4).** When the file watcher observes a self-authored
write, its embedding-echo suppression check drops the `upsert_after_write`/`delete_after_remove`
call — but the freshness (and inbound) registries still get published to for that same event. A
self-write changes the file on disk, so it MUST change the file's entry in the freshness map even
though its embedding re-index is (correctly) suppressed as redundant. This is not a new event path:
`_record`/`_flush` already sees every event before the suppression check short-circuits the
embedding dispatch; the freshness/inbound publish call sits alongside that dispatch, not gated by
the suppression.

**Sidecar-mtime portion of the hot-cache key.** The embedding/CLIP sidecar mtime component of
`find`'s hot-cache freshness key stays stat-based (2 syscalls: one per sidecar) — it is not moved
into the event-maintained registry, since the sidecar files are not markdown and are already cheap
to stat directly.

**Kill switch.** `EXOMEM_DISABLE_EVENT_INDEXES` reverts all three registries (freshness, matrix,
inbound) to their pre-this-change behavior wholesale — a single rollback lever rather than three
independent flags, since the three subsystems are designed to be live/not-live together (the same
watcher publish call feeds all three).

### D2 — Process-shared embedding/CLIP matrix, cross-process delta reload

`EmbeddingIndex`/`ClipIndex`'s matrix cache moves from a per-instance attribute (`self._cache`,
`embeddings.py:782`, `958`) to module-level state shared per vault: a `dict[str, MatrixState]`
keyed by resolved vault root, each guarded by its own `threading.Lock`. The lock protects
build-then-swap only — a writer builds the new immutable `(metadata, matrix)` pair off-lock, then
swaps the shared reference under the lock; readers capture a reference and never hold the lock
during the `matrix @ query_vec` matmul, so concurrent finds never block on each other for the read
path.

**Cross-process delta reload (no schema migration).** When a search or upsert notices the sidecar's
(mtime, size) has changed since the last swap — the signal that an out-of-process writer (backfill,
a second sidecar connection) touched the table — it runs a metadata-only scan:
`SELECT file_path, chunk_idx, file_mtime` (no `vector` BLOBs), diffs the result against the
per-file breakdown of the currently-held cache, and only for files whose `(file_mtime)` (or absence
in the metadata-only scan) changed does it fetch that file's vector rows. It then rebuilds the
stacked matrix by concatenating the unchanged files' existing submatrices with the newly-fetched
changed files' submatrices. Deletes fall out naturally — a file absent from the metadata scan is
simply not included in the rebuilt stack. New `last_files_reloaded`/`last_files_reused` counters
(module-level, per vault) give tests (and future observability) a way to assert "only N files were
actually re-fetched," mirroring the existing `bm25.last_tokenized` observability precedent.

**In-process writes** (`upsert_file`, `delete_file`, `upsert` (ClipIndex), `upsert_frames`) publish
directly into the shared state instead of nulling a private `self._cache` — a single-file write
updates just that file's submatrix in the shared stack rather than invalidating the whole thing.

**Rejected alternatives:**

| Alternative | Why rejected |
|---|---|
| Generation-column sqlite migration (a monotonic counter column bumped per write, compared to detect any change) | Correctly detects all changes, but requires a schema migration for an existing, already-deployed sidecar format, for a case (see residual edge below) that does not occur in real write flows. Complexity not justified by the case it would additionally cover. |
| `PRAGMA data_version` | Needs a long-lived connection per reader to observe transitions reliably, and even when observed, still can't identify *which* files changed — every reader would still need the full metadata scan to compute the delta, so it doesn't remove the need for the (mtime, size) + metadata-scan mechanism, only adds a second signal on top of it. |

**Accepted residual edge (documented, not fixed):** a backdated re-embed that produces an identical
`file_mtime` *and* identical chunk count as the previous version, but with different vectors, is
missed by the (mtime, size) delta signal — the file looks unchanged. This is explicitly accepted
because it cannot arise from any real write flow in this codebase: every writer (`upsert_file`,
`rebuild_all`, `reconcile`, `backfill`, `media_worker`) stamps the *current* filesystem stat mtime
when it writes, never a backdated one, on both the markdown source and the sidecar row. It is a
theoretical gap in the detection mechanism, not a case any code path here produces.

**Memory:** during a delta reload's rebuild step, both the old stacked matrix and the new one exist
simultaneously (a transient 2× memory footprint) until the swap completes and the old array is
garbage-collected. Accepted — the matrices are per-vault chunk-embedding tables (megabytes, not
gigabytes, at the ~1900-file measured scale), and the transient overlap is sub-second.

### D3 — Inbound-link index per-file patch API

`vault._INBOUND_INDEX` (`vault.py:505-553`) gains `on_files_changed(changed_rels, deleted_rels)`:
for each affected path, remove that file's existing edges from `buckets` and its contribution to
`stem_counts`, then re-read only that file (if it still exists) and re-add its edges and stem-count
contribution. The digest-keyed full rebuild (`_build_inbound_index`, `vault.py:508-539`) stays as
the not-live fallback, used whenever the registry isn't live (mirrors D1's fallback pattern).

Output-set equivalence: a patched index and a full rebuild produce the identical set of
`InboundLink` entries per target. The one documented difference is ordering: `_InboundEntry.seq`
(the global scan-order tiebreak, `vault.py:490-496`) is assigned in full-walk order during a full
rebuild, but a patched file's new entries get sequence numbers from insertion order (appended after
the existing max `seq`), not from where that file would fall in a fresh full walk. This is called
out explicitly because `tests/test_inbound_index.py`'s existing assertions must be checked for
order-sensitivity — where they assert content equality (which entries exist) that continues to
hold; where they'd assert exact `seq`-derived ordering across a patch, the test needs a content-set
guard instead (or must not exercise the patch path in a way that depends on relative order across
files touched at different times).

`_INBOUND_INDEX` stays private to `vault.py`, is not persisted to disk, and is cleared by the
existing `clear_inbound_index()` test hook (already called from `find.clear_cache()`,
`find.py:2537-2538`).

### D4 — No new event bus; extend the two existing truth channels

**(a) The file watcher's debounced flush.** `FileWatcher._flush()` (`file_watcher.py:233-245`)
already computes `(ups, del_rels)` from `_drain()` and dispatches them through
`embeddings.upsert_after_write`/`delete_after_remove`. This change adds freshness+inbound publish
calls alongside those two, using the same drained lists — no new debounce, no new thread, no new
queue. Ordering rule (restated from D1): freshness/inbound publish happens for **every** drained
path, including ones whose embedding dispatch gets suppressed by the self-write check in `_record`
(`file_watcher.py:196-212`) — suppression in `_record` only prevents a path from entering
`_pending_upsert`/`_pending_delete` in the first place for a *matching* self-write, so a suppressed
self-write never reaches `_flush` at all; genuinely external events (the only ones that do reach
`_flush`) always publish to both channels together. This means the "self-write still updates
freshness" case is naturally handled: a self-write publishes to freshness/inbound not through the
watcher's suppressed path, but through channel (b) below, at the moment the writer itself performs
the mutation.

**(b) In-process writer hooks.** Every call site that already calls
`register_self_write`/`upsert_after_write`/`delete_after_remove` today additionally publishes to
freshness+inbound at the same point:

- `vault.batch_atomic_write` (`vault.py:207-226`) — after `register_self_write`, alongside
  `upsert_after_write`.
- `delete_file`/`move_file`/`delete_directory`'s delete paths (`delete_file.py:235-251`,
  `move_file.py:190-207`, `delete_directory.py:216-231`) — alongside their
  `register_self_delete`/`delete_after_remove` calls.
- `reconcile.reconcile` (`reconcile.py:113`) — alongside its `upsert_after_write` call for
  drifted files.
- `media_worker.MediaWorker` (`media_worker.py:129`) and `scene_frames` (`scene_frames.py:91`,
  writes/clears) — alongside their existing embedding calls.
- `audit_fix`'s `rebuild_embeddings` path (`audit_fix.py:416`, which calls
  `EmbeddingIndex.rebuild_all()`) — the rebuild already re-touches every file; publish the full set.

Out-of-process writers (a second process running `backfill` or a raw sqlite connection) are NOT
covered by either channel above for freshness/inbound (those channels are in-process only) — they
are covered by (a) FS events for freshness/inbound (the watcher observes the resulting file writes
on disk exactly like an external edit) and (b) the matrix's own delta-reload signal (D2) for the
embedding matrix specifically, since the matrix's staleness signal is the sidecar's own
(mtime, size), independent of the watcher.

### D5 — Safety, tests, acceptance

**Invalidation surfaces.**

- `reconcile.reconcile` invalidates all three registries at the end of its run (an immediate clean
  slate after a reconcile pass, in addition to its existing per-file publish from D4b) — reconcile
  is the "I edited around the system, heal it" command, so it should never leave a registry
  trusting pre-reconcile state.
- `find.clear_cache()` (`find.py:2529-2538`) extends to also clear the freshness registry and the
  shared matrix state, alongside its existing `_CACHE`/`_RESOLVER_CACHE`/`_FIND_CACHE`/
  `clear_inbound_index()` clears.
- `vault.clear_inbound_index()` continues to be the inbound-index reset hook; no signature change.

**Kill switch.** `EXOMEM_DISABLE_EVENT_INDEXES` (new env var) makes all three registries behave as
permanently not-live — `FreshnessSnapshot` always walks, `EmbeddingIndex`/`ClipIndex` always
recompute per-instance (today's behavior), `_inbound_index` always does the full digest-keyed
rebuild. This is the rollback lever if the event-maintained path is ever suspected of drift in
production; it does not require restarting with a different watcher configuration, just setting the
env var and restarting.

**Tests (this is the TDD backbone for `tasks.md`):**

- Registry-vs-walk triple equality across create/modify/delete/move + the rename-same-mtime case.
- Reconcile heals a dropped event (simulate a missed `watchdog` callback, then let the 300s
  reconcile — or a directly-invoked reconcile-check helper in tests — repair the map).
- Not-live fallback is byte-identical to today's walk (registry never seeded / kill switch set).
- Matrix is shared across finds — two sequential `find()` calls in one process do not re-read the
  sqlite table a second time (assert via the new counters).
- A single-file upsert reloads exactly one file (counters assert `last_files_reloaded == 1`,
  `last_files_reused == N-1`).
- A second sqlite connection (simulating an out-of-process writer) triggers a delta reload that
  picks up only the newly-written files.
- A delete removes the corresponding rows from the shared matrix.
- A CLIP twin of every matrix test above.
- Inbound patch equality vs. a full rebuild (content-set equality; ordering caveat per D3 covered
  explicitly, not silently ignored).
- The watcher publishes to all three registries on an external event.
- A self-write updates freshness (and inbound, where applicable) while still suppressing the
  embedding echo — the D4 ordering case, tested directly rather than inferred.
- The watcher starts with embeddings disabled (verifying the decoupled gate) and does NOT start
  under `EXOMEM_DISABLE_FILE_WATCHER` (verifying the flag still works standalone).
- `tests/conftest.py` sets `EXOMEM_DISABLE_FILE_WATCHER=1` globally so the suite never starts a
  real `watchdog` observer as a side effect of the gate decoupling — mirrors the existing
  `EXOMEM_DISABLE_WARMUP` precedent already in `conftest.py`. Without this, every test that builds
  a server (or otherwise reaches the `server.py:348` gate) with embeddings disabled would newly
  start a real filesystem observer, which is exactly the behavior change this decoupling
  introduces and exactly why the suite needs the same kind of explicit opt-out `EXOMEM_DISABLE_
  WARMUP` already established.

**Perf acceptance.** A synthetic ~1900-file vault plus a background thread acting as a concurrent
sidecar writer (mirroring the measured backfill-concurrency scenario) must produce `find` p50 <
300ms and p95 < 500ms. The freshness stage specifically must be < 5ms when the registry is live —
reusing the existing `FindTimings` "freshness" span (already instrumented per the
`improve-find-latency-token-cost` change) as the signal, so this is measurable without adding a new
timing surface.

## Risks / Trade-offs

- Event-maintained map silently drifts from disk truth (a missed `watchdog` event) → bounded by the
  300s periodic reconcile re-walk, which detects and logs (`freshness_reconcile_drift`) and repairs
  any mismatch; worst case is 300s of staleness, not unbounded drift.
- Parity rule violated (registry includes/excludes something the walk wouldn't) → the registry
  silently diverges from a value every other consumer assumes is walk-equivalent. Mitigated by the
  explicit registry-vs-walk equality test suite (D5) run across create/modify/delete/move/rename.
- Shared matrix state introduces cross-request mutable state where there was none → mitigated by
  the build-then-swap-under-lock pattern (D2): readers never observe a partially-rebuilt array, and
  never hold the lock during the matmul, so a slow reader can't block a concurrent writer's swap.
- Delta reload's residual edge (backdated re-embed, same mtime, same chunk count, different
  vectors) → accepted and documented (D2); does not arise from any real writer in this codebase.
- Inbound patch's `seq`-ordering divergence from a full rebuild → explicitly documented (D3);
  existing `test_inbound_index.py` assertions audited for order-sensitivity as part of this
  change's tests rather than left to fail silently later.
- Watcher now runs whenever `EXOMEM_DISABLE_FILE_WATCHER` is unset, even with embeddings disabled →
  the embed-dispatch path already no-ops on a disabled-embeddings vault, so the only new cost is the
  freshness/inbound publish work, which is the point of this change; `tests/conftest.py` gets the
  explicit opt-out so the test suite's behavior is unchanged.
- Single kill switch (`EXOMEM_DISABLE_EVENT_INDEXES`) couples the rollback of three subsystems →
  intentional: the three registries are fed by the same publish points, so a partial rollback
  (e.g. freshness live but inbound not) would be a state combination nothing in this design
  produces or tests; one flag avoids a combinatorial rollback surface.

## Migration Plan

No data migration — the freshness and inbound registries are in-memory only, and the matrix
sharing is a cache-lifetime change with no sqlite schema change. Existing deployments get the
event-maintained behavior on their next deploy with no required config change (the kill switch is
opt-in-to-disable, not opt-in-to-enable). If drift or a matrix-staleness regression is suspected in
production, set `EXOMEM_DISABLE_EVENT_INDEXES=1` to isolate whether the event-maintained path is
responsible, independent of restarting with a different watcher/embeddings configuration.

## Open Questions

None for implementation. The one known limitation (D2's residual edge) is documented and accepted
rather than deferred as an open question, since it does not arise from any write flow this codebase
produces.
