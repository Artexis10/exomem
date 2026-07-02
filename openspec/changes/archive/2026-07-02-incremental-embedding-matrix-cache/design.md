# Design — incremental-embedding-matrix-cache

## 1. Why the cache never worked, and the two-part fix

The `EmbeddingIndex` docstring claimed the matrix was "cached per-process," but
nothing memoized the instance — `find._find_semantic` (and warm-up, and every
writer) constructed a fresh `EmbeddingIndex(vault_root)` whose `_cache` began
`None`. So `search()` → `all_vectors()` always missed and did the full O(vault)
load. Two things are therefore needed, and neither suffices alone:

1. **A process-lifetime shared instance** so the matrix outlives a single call.
2. **Incremental patching** so that, while the sidecar is being written, keeping
   the cache current costs the size of the *delta*, not the *vault* — otherwise a
   full-tilt writer bumps the sidecar mtime constantly and every find reloads.

## 2. Concurrency model

MCP sync tool handlers run on an anyio worker-thread pool; the file watcher and
media worker are their own daemon threads. Sharing one index across them is
genuinely concurrent, so:

- **Per-index `threading.RLock`** guards *only* in-memory cache mutation. It is
  never held across a SQLite write (that would re-couple find latency to the slow,
  contended write). Reentrant so any nested cache touch can't self-deadlock.
- **Copy-on-write, atomic swap.** A writer builds fresh `metadata`/`matrix` arrays
  and replaces `self._cache` in one assignment; it never mutates arrays a reader
  may be holding. Readers take the fast path with **no lock**: they snapshot
  `self._cache` into a local once (the reader-snapshot fix) and use it. A snapshot
  is either the pre- or post-swap tuple, never a torn mix.
- **Double-checked load.** On a miss the reader takes the lock and re-checks before
  loading, so a splice that landed while it waited is picked up without a reload.
- **Per-call `sqlite3.connect`** stays as-is (connections are not shared across
  threads).

## 3. Splice mechanics

Rows for one file are contiguous under the load ordering (`ORDER BY file_path,
chunk_idx`). `_file_block(keys, rel_path)` returns that block's `[lo, hi)`, or the
sorted insertion point when the file is new. The splice is
`np.concatenate([matrix[:lo], new_vecs?, matrix[hi:]])` + the parallel metadata
slice — which naturally handles a changed chunk count, a new file, and a delete
(empty new block; zero-row shape restored if the matrix empties). An
`len(metadata) == matrix.shape[0]` invariant guards every swap.

**Self-heal.** The whole splice is best-effort: any exception (or invariant
violation) drops `self._cache` to `None`, so the next `all_vectors()` does a
correct full reload. A splice bug can never surface a wrong or torn result — only,
at worst, one extra reload.

**Freshness gate stays authoritative.** `all_vectors()` still keys on the sidecar
file mtime. An in-process splice sets the cache mtime to the post-write stat, so a
subsequent read hits without reloading. An out-of-process / out-of-instance write
(a CLI `audit_fix --rebuild-embeddings`) advances the on-disk mtime past the cache
mtime, so the shared instance detects it and reloads exactly once — correctness
before speed.

**CLIP twin.** `ClipIndex` mirrors the same shape. Its `upsert()` does a *partial*
delete (image/NULL-ts rows only), so its splice keeps existing video-keyframe rows
and replaces only the image row (`images_only=True`); `upsert_frames`/`delete`
block-replace the whole file.

**`rebuild_all`.** Routing it through per-file `upsert_file` would now cost O(N²)
splices plus N transactions, so it is special-cased: build every row, wipe +
`executemany` in one transaction, leave the cache cold for one clean reload.

## 4. WAL

`journal_mode=WAL` decouples readers from the writer (the direct cause of the 13s
read spikes) and, with `synchronous=NORMAL`, collapses the writer fsync cost behind
the 37-42s note-writes. Applied in both `_connect`s via `_apply_sidecar_pragmas`,
which soft-fails to the default journal if WAL is unavailable. Safe because the
sidecar is a local per-machine dotfile (Sync-ignored, never on a network FS).
SQLite's automatic checkpoint bounds the `-wal` file under a long backfill.

## 5. Scope boundary

In-scope: the in-process write-churn incident (file watcher / media worker / tool
writes on the worker-thread pool) — fully covered. Out-of-scope, deferred as
measured follow-ups: a metadata-diff delta reload for out-of-process CLI writers
(WAL already degrades that case to the ~289ms baseline), and a monotonic `rev`
column for the same-source-mtime re-embed detection hole. Consistent with the
capability's "retrieval architecture changes are deferred until measured"
requirement — this change adds no ANN/LSH, only cache reuse and freshness-safe
invalidation.
