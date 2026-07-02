# incremental-embedding-matrix-cache

## Why

Under a concurrent sidecar writer (a backfill re-embedding the vault), `find`'s
vector lane degrades from ~1s to 10-25s and note-writes to 37-42s — observed live
on 2026-07-02, isolated with `find(include_timings=true)`: two identical smoke
finds ~30s apart, the first 13.9s with the **vector lane alone at 13.0s**, the
second 1.14s (vector 289ms), everything else healthy in both.

Root cause: the vector-matrix cache was never actually process-lifetime. Every
call site built a throwaway `EmbeddingIndex`, whose mtime-keyed `_cache` starts
empty, so `all_vectors()` did a full `SELECT … FROM chunks` + per-row
`np.frombuffer` + `np.stack` of the whole `(N, 768)` matrix on **every** find —
O(vault) each call. Quiescent + warm OS page cache that's ~289ms; contending with
a concurrent SQLite writer (rollback journal → readers block the writer) it's the
13s spike. Warm-up even populated a throwaway instance's cache and discarded it,
so it helped find nothing. The 37-42s note-writes are a separate, adjacent
mechanism: `rebuild_all` committed one transaction per file (N fsyncs) and a
concurrent note-write queued behind it — writer↔writer contention a read cache
can't touch.

## What Changes

- **Process-lifetime shared index.** A per-vault memo returns ONE shared
  `EmbeddingIndex`/`ClipIndex`; all production call sites (find, warm-up, writers,
  audit, backfill, media worker) go through `get_embedding_index()` /
  `get_clip_index()`. The in-memory matrix now survives across finds and warm-up
  finally primes it — the first find pays the load, the rest reuse it.
- **Incremental in-place cache updates.** `upsert_file`/`delete_file` (and the
  CLIP `upsert`/`upsert_frames`/`delete`) patch the changed file's rows into the
  cached matrix copy-on-write instead of nulling the whole cache, so an in-process
  write keeps the shared matrix current and a concurrent find pays **no** reload.
- **WAL on the sidecar.** `journal_mode=WAL`, `synchronous=NORMAL`,
  `busy_timeout=5000`: readers stop blocking the writer (and vice-versa), and the
  per-write fsync cost that produced the 37-42s note-writes collapses.
- **Reader-snapshot fix.** `all_vectors()` now snapshots `self._cache` once — a
  latent torn read (`TypeError` on a mid-flight `None`) that only becomes reachable
  once the instance is shared across threads.
- **`rebuild_all` single-transaction.** Wipe + one `executemany` (was N
  transactions + would now be N O(vault) splices); leaves the cache cold for one
  clean reload.

Default-on and lean-safe. Soft-fail everywhere: a splice inconsistency drops the
cache to `None` so the next read does a correct full reload (never a wrong result);
the WAL pragmas soft-fail to the default journal on an odd filesystem. WAL is safe
because the sidecar is a **local, per-machine dotfile Obsidian Sync ignores** — its
`-wal`/`-shm` siblings inherit the leading dot and stay ignored too; WAL's
shared-memory requirement rules out the network filesystems this sidecar is
designed never to live on. No model and no retrieval-architecture change — this is
cache reuse + freshness-safe invalidation only, inside the existing
`find-recall-efficiency` "defer architecture until measured" guardrail (pure
substrate: measurement, not reasoning).

Out of scope (deferred, measured follow-ups): a delta-reload path for
**out-of-process** CLI writers against a live server (under WAL those degrade
gracefully to the ~289ms baseline, not the 13s spike), and a monotonic `rev`
column to close the same-source-mtime re-embed detection hole.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `find-recall-efficiency`: adds a process-lifetime, incrementally-patched vector
  matrix cache and WAL sidecar concurrency, making `find` latency independent of
  concurrent in-process sidecar writes.

## Impact

- `src/exomem/embeddings.py`: shared memo + `get_embedding_index`/`get_clip_index`/
  `clear_embedding_indexes`; per-index `RLock`; COW splice in the writers;
  `all_vectors` reader-snapshot + `_load_all_rows` extraction; `_apply_sidecar_pragmas`
  (WAL) in both `_connect`s; single-transaction `rebuild_all`.
- Construction sites routed through the getters: `find.py`, `warmup.py`,
  `audit.py`, `audit_fix.py`, `corpus_aware.py`, `media_worker.py`, `backfill.py`.
- Tests: new `tests/test_embedding_matrix_cache.py` (reload-count, in-place patch,
  splice correctness, external-write gate, WAL, thread-safety); `tests/conftest.py`
  clears the memo in the `vault` fixture. No API/dependency changes; the hot-find
  cache freshness key (`find._freshness_key`) is untouched.
