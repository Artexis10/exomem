## Why

> **Scope note (2026-07-02):** this change originally covered three costs (P1 freshness, P2 matrix,
> P3 inbound). **P2 (the embedding-matrix reload) shipped separately and first** as
> `incremental-embedding-matrix-cache` (process-lifetime shared index + WAL + in-place patching) —
> so it is REMOVED here to avoid duplicating a merged feature. This change now delivers **P1
> (event-maintained freshness) and P3 (event-maintained inbound-link index)** only, which the
> matrix change does not address. P2 is retained below for context only.

Measured on production (2026-07-02, ~1900-file vault):

- **P1** — every `find` pays ~494ms of freshness stat-walk (`_walk_freshness_key` via
  `FreshnessSnapshot`, `find.py:639-695`), even hot-cache **hits** pay it because the freshness key
  IS the cache key (`find.py:876-881`); a `scope="kb"` query pays **both** the KB walk and the
  vault walk (auto-widen triggers the second one on every non-empty query).
- **P2** — `find._find_semantic` instantiates `EmbeddingIndex(vault_root)` fresh per call
  (`find.py:1252`), so its per-instance matrix cache never survives between finds — the full
  matrix reloads from `.embeddings.sqlite` on **every** hybrid find (~250-300ms warm), inflating to
  13-27s under a concurrent out-of-process sidecar writer (backfill). `ClipIndex` is identical
  (`find.py:1298`). The docstring's "cached per-process" claim (`embeddings.py:774`) is false on
  the request path.
- **P3** — the inbound-link index (`vault._INBOUND_INDEX`, `vault.py:505-553`) does a full-vault
  re-read whenever the freshness digest moves. Consumers: `find_inbound_wikilinks` →
  `list_inbound_links`, `context_pack._neighborhood` (the ≈5s pack), and the
  `move_file`/`delete_file`/`delete_directory` safety checks.

None of this changes ranking or results — it is the same freshness/matrix/inbound work done by a
walk or a full reload on every call instead of being maintained incrementally from the write/watch
events the server already observes.

## What Changes

- Add an event-maintained freshness registry (`src/exomem/freshness.py`) keyed by
  `(vault_root, scope)`, holding an in-memory `{rel_path: mtime_ns}` map plus a derived
  digest-strength triple. `FreshnessSnapshot.kb()`/`.vault()` consult it when live (sub-ms, zero
  syscalls) instead of walking, falling back to today's walk byte-identically when not live. The
  file watcher extends to watch the vault root (one observer); embedding re-index dispatch stays
  KB-filtered. Watcher startup decouples from embeddings (it is gated only by
  `EXOMEM_DISABLE_FILE_WATCHER` now). A periodic reconcile re-walk (every 300s) bounds missed-event
  staleness.
- Make the `EmbeddingIndex`/`ClipIndex` matrix cache module-level, shared per vault, instead of
  per-instance. Cross-process changes (an out-of-process sidecar writer) are detected via a
  metadata-only delta scan and only the changed files' vectors are re-fetched and re-stacked — no
  full reload, no sqlite schema migration.
- Give the inbound-link index a per-file patch API (`on_files_changed`) so a write updates only the
  changed file's edges instead of re-reading the whole vault, with output identical to a full
  rebuild.
- Extend the two existing truth channels (the file watcher's debounced flush, and the in-process
  writer hooks that already call `register_self_write`/`upsert_after_write`) to also publish into
  the freshness and inbound registries — no new event bus.
- Add `EXOMEM_DISABLE_EVENT_INDEXES` as a rollback lever that reverts all three registries to
  today's polling/walk/full-rebuild behavior wholesale.

No server-side reasoning model is added. This is deterministic bookkeeping over filesystem/sidecar
mutations the server already observes through its own writer hooks and file watcher.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `live-index-freshness`: the file watcher now watches the vault root (not just `Knowledge Base/`)
  and maintains event-driven freshness and inbound-link registries alongside its existing
  embedding-reindex/self-write-suppression behavior; watcher startup no longer depends on
  embeddings being enabled.
- `find-recall-efficiency`: `find`'s per-request freshness snapshot reuses the live registry
  instead of walking when available; the embedding/CLIP matrix is shared and incrementally
  reloaded across finds instead of rebuilt per call; a new latency acceptance bound covers
  concurrent-writer conditions.

## Impact

- Code: new `src/exomem/freshness.py`; `find.py` (FreshnessSnapshot consult path, `clear_cache`);
  `embeddings.py` (shared matrix state, delta reload, counters, publish points); `vault.py`
  (inbound patch API); `file_watcher.py` (vault-root watch, dual-scope publish); `server.py`
  (watcher gate decouple); `warmup.py` (seed registries); `reconcile.py` (invalidate registries);
  `tests/conftest.py` (`EXOMEM_DISABLE_FILE_WATCHER=1`).
- Surfaces: none — no new `find`/command-registry parameters, no MCP/REST/CLI/OpenAPI schema
  change.
- Tests: registry-vs-walk triple equality (create/modify/delete/move/rename-same-mtime), reconcile
  healing a dropped event, not-live fallback byte-identical, matrix shared-across-finds (no
  reload), single-file upsert reloading exactly one file (with counters), a second sqlite
  connection simulating an out-of-process writer, delete removing rows, a CLIP twin of the matrix
  tests, inbound patch-vs-full-rebuild equivalence, watcher publishing to all three registries,
  self-write updating freshness while still suppressing the embedding echo, watcher starting with
  embeddings disabled, and a synthetic ~1900-file vault + background sidecar-writer perf test
  (`find` p50 < 300ms / p95 < 500ms).
- Dependencies: none expected. Existing optional embedding/CLIP/rerank lanes continue to soft-fail
  as today.
