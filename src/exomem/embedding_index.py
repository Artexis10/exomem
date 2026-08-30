"""Text embedding vector sidecar store.

This module owns the `.embeddings.sqlite` lifecycle: chunk vectors, matrix
caching, and sqlite-vec fallback behavior. Model loading and encoding stay in
`embeddings.py`; this file is storage only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from . import index_paths, reserved_paths, semantic_index, sidecar_store, vecstore
from .vector_index_common import vec_gate

log = logging.getLogger(__name__)


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("embedding_index"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)

VECTOR_DIM = 768
SEMANTIC_UNIT_SCHEMA_VERSION = 3

# Per-path change log for the read-side bounded catch-up (see sidecar_store's
# "bounded catch-up" note). The table records the generation at which each
# `chunks.file_path` was last mutated; the two meta keys bound the contiguous run
# of generations that log fully covers, so a bump from any writer that does not
# maintain it — an older generation-AWARE binary sharing this vault, a manual
# repair, a future in-tree writer — forces the full reload instead of reading as
# "this write changed nothing".
CHUNK_PATH_LOG = sidecar_store.PathChangeLog(
    "chunk_path_log", "chunk_path_log_from", "chunk_path_log_upto"
)

# Catch-up bounds. PATHS is the real cost knob, not generations: one bump can
# retire a whole batch of paths (a purge) and many bumps can rewrite one path
# (repeated upserts), so the changed-path count — not the generation delta — is
# what the splice actually pays for. Per path it costs one `file_block` walk over
# the key list plus that path's rows; the whole delta then costs ONE concatenate.
# A full reload instead fetches and re-materializes every vector blob (a ~3 KB
# BLOB per row against a bare string compare per key), so the per-path unit is
# more than an order of magnitude cheaper than the per-row unit it replaces. 32
# is set an order of magnitude below where those two could plausibly meet, and
# comfortably above the 1-11 path deltas seen in production; it is a safety bound
# on a fallback, not a tuned break-even, and the fallback is always correct.
# GENERATIONS is only a cheap pre-filter: a cache that far behind belongs to an
# idle process rejoining a busy vault, where one clean reload is the simpler
# answer.
CATCHUP_MAX_PATHS = 32
CATCHUP_MAX_GENERATIONS = 64

# --------------------------------------------------------------- generation meta
#
# The matrix caches key on an in-band WRITE GENERATION, not the sidecar file's
# mtime. The sidecars are WAL sqlite: a commit does NOT move the main file's
# mtime — a CHECKPOINT does, and under concurrent connections the checkpoint
# fires whenever the last connection (often a pure reader) closes, at a moment no
# writer runs. So mtime-keyed invalidation BOTH spuriously misses (a checkpoint
# with no content change) AND goes stale (an uncheckpointed commit leaves the
# mtime unmoved). A `meta(key, value)` row bumped inside each write's own
# transaction changes iff the content did. Third occurrence of this class in the
# repo; precedent + rationale: lexstore.cache_token.
#
# One-way legacy fallback: once a sidecar's generation reaches >= 1, the cache
# trusts (epoch, generation, instance) EXCLUSIVELY and stops checking mtime. A
# write from a PRE-generation binary (one that predates this whole mechanism)
# past that point is invisible to invalidation — old and new binaries writing
# the SAME sidecar concurrently is unsupported. Fine for this single-user,
# single-machine-per-sidecar deployment; would need re-litigating for multi-writer.


class _EmbCache(NamedTuple):
    """EmbeddingIndex's in-memory matrix cache. `(epoch, generation, instance)` is
    the write token (F1-F3); `mtime` is retained only for the gen==0 legacy
    fallback. `metadata[i] = (file_path, chunk_idx)`; `matrix[i]` = its vector."""

    epoch: int
    generation: int
    instance: int
    mtime: float
    recall_policy_identity: tuple[str, str]
    metadata: list[tuple[str, int]]
    matrix: np.ndarray


class _MaskCache(NamedTuple):
    """One memoized eligibility row mask for `search`'s allowed-paths filter.

    `metadata` is held by STRONG reference and matched with `is`, NOT by the
    write token `all_vectors()` keys on — deliberately the stronger of the two
    invariants. Every producer of the matrix cache is copy-on-write
    (`_load_all_rows`, `_splice_path_blocks` under `_patch_cache` /
    `_catch_up_cache`, and `_purge_cache_paths` all build fresh containers), so
    a different row set is ALWAYS a different list object; the converse does not
    hold, because an equal `(epoch, generation, instance)` triple still falls
    back to mtime on gen==0 legacy sidecars, where mtime both spuriously misses
    and goes stale (see the generation-meta note). Reading the token separately
    would also race: another thread may swap `self._cache` between
    `all_vectors()` returning and the token being read, keying a mask against a
    generation its rows never came from. Holding the reference additionally
    stops the list being freed and its `id()` reused by an unrelated one, so a
    mask can never outlive the matrix it was computed against.

    `allowed_paths` is matched by CONTENT (a frozenset), never by identity: the
    set belongs to the caller, which may mutate it in place between two queries
    at the same scope, and a stale mask would return rows the caller excluded —
    far worse than the slowness this cache exists to remove. `frozenset(x)`
    returns `x` itself when it is already a frozenset, so an immutable caller
    pays nothing for that safety.
    """

    metadata: list[tuple[str, int]]
    allowed_paths: frozenset[str]
    mask: np.ndarray
    eligible: int


def _splice_path_blocks(
    metadata: list[tuple[str, int]],
    matrix: np.ndarray,
    replacements: dict[str, tuple[list[tuple[str, int]], np.ndarray | None]],
) -> tuple[list[tuple[str, int]], np.ndarray]:
    """Replace whole per-path row blocks in a `(metadata, matrix)` pair.

    THE splice: the write-side single-path patch and the read-side bounded
    catch-up both go through here, so there is exactly one implementation of
    "swap a file's contiguous block for its current rows, keeping the pair sorted
    by file_path and the two halves the same length".

    `metadata` is sorted by file_path (both producers keep it that way), so
    `sidecar_store.file_block` locates each path's block — or, for a path with no
    rows yet, its insertion point. It scans from index 0 every time, so this is
    O(paths x rows), not a single pass; that is affordable only because the
    caller bounds the path count (CATCHUP_MAX_PATHS). The replacements are
    processed in sorted order and `cursor` only ever moves forward, so blocks
    that go backwards mean the caller handed in unsorted metadata and raise
    rather than silently scrambling the matrix. An empty replacement (`new_vecs`
    None or zero rows) deletes the block.

    Copy-on-write: builds fresh containers and never mutates the arrays a
    concurrent reader may be holding. Raises ValueError on any inconsistency;
    both callers treat that as "drop the cache and take the full reload".
    """
    keys = [m[0] for m in metadata]
    out_meta: list[tuple[str, int]] = []
    parts: list[np.ndarray] = []
    cursor = 0
    for path in sorted(replacements):
        lo, hi = sidecar_store.file_block(keys, path)
        if lo < cursor:
            raise ValueError(
                f"unsorted matrix metadata at {path}: block starts at {lo}, "
                f"behind cursor {cursor}"
            )
        out_meta.extend(metadata[cursor:lo])
        parts.append(matrix[cursor:lo])
        new_meta, new_vecs = replacements[path]
        out_meta.extend(new_meta)
        if new_vecs is not None and new_vecs.shape[0]:
            parts.append(new_vecs)
        cursor = hi
    out_meta.extend(metadata[cursor:])
    parts.append(matrix[cursor:])
    parts = [p for p in parts if p.shape[0]]
    new_matrix = (
        np.concatenate(parts, axis=0)
        if parts
        else np.zeros((0, VECTOR_DIM), dtype=np.float32)
    )
    if len(out_meta) != new_matrix.shape[0]:
        raise ValueError(
            f"splice invariant broken for {sorted(replacements)}: "
            f"{len(out_meta)} meta rows vs {new_matrix.shape[0]} vectors"
        )
    return out_meta, new_matrix


class SemanticUnitVectorHit(NamedTuple):
    """One current semantic-unit vector candidate with its raw cosine."""

    unit_ref: str
    parent_path: str
    parent_generation: str
    parent_source_hash: str
    parser_version: int
    cosine: float


class SemanticUnitVectorRow(NamedTuple):
    """One stored unit vector as the corpus-level read returns it.

    `parent_generation` travels with the geometry on purpose: a consumer that
    reads these vectors against a NEWER parse of the same page would be reading
    deleted text, and an anchored `unit_ref` is content-independent so the join
    alone cannot detect that.
    """

    unit_ref: str
    source_order: int
    vector: np.ndarray
    parent_generation: str


#: Rows per batch in the corpus-level unit-vector read. Bounds the result set a
#: single SELECT materialises; it is not a limit on what the read returns.
SEMANTIC_UNIT_READ_BATCH = 2_000


class EmbeddingIndex:
    """Per-vault sqlite sidecar holding chunk-level vectors.

    The matrix returned by `all_vectors()` is cached per-process and
    invalidated by an in-band WRITE GENERATION (a `meta` row bumped inside every
    write's own transaction), NOT the sidecar mtime — see the generation-meta
    note above for why WAL-checkpoint timing makes mtime keying both spuriously
    miss and go stale. When the vec0 backend is active (`vecstore`), `search()` is served by a
    SQL-native KNN over shadow tables in the same sidecar instead, and this
    matrix stays cold — `all_vectors()` remains for audit's all-pairs sweep
    and the numpy fallback.

    numpy-lite (2026-07-04): the cache holds ONLY `(file_path, chunk_idx)`
    metadata + the float32 matrix — chunk TEXT is never resident. Text was
    most of the numpy backend's memory bill at scale (~2GB of a ~3.5GB RSS at
    200k chunks); the top-k winners' texts are point-lookups on the
    `(file_path, chunk_idx)` PRIMARY KEY at search time, exactly how the vec0
    path already hydrates metadata by rowid.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.path = index_paths.sidecar_path(vault_root)
        self._cache: _EmbCache | None = None
        # One-slot memo for search()'s allowed-paths row mask (see _MaskCache).
        self._mask_cache: _MaskCache | None = None
        # Guards in-memory cache mutation only (never held across a sqlite write).
        # Reentrant so rebuild_all()-style nesting can't self-deadlock.
        self._lock = threading.RLock()
        # vec0 backend state (see vec_gate): sync memo + per-instance retirement.
        self._vec = vecstore.SqliteVecStore("chunks", "vector", VECTOR_DIM, "vec_chunks")
        self._vec_ready: bool | None = None
        self._vec_quant_synced = False
        self._vec_failed = False

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("embedding_index"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("embeddings-store",),
            ):
                return self._connect_owned(path)

    def _connect_owned(self, path: Path | None = None) -> sqlite3.Connection:
        target = path if path is not None else self.path
        if target == self.path:
            with reserved_paths._sqlite_owner_target_scope(
                self.vault_root,
                target,
                "embeddings-store",
                create=True,
            ) as retained_target:
                return self._connect_retained(retained_target)
        return self._connect_retained(target)

    def _connect_retained(self, target: Path) -> sqlite3.Connection:
        sidecar_store.ensure_sidecar_parent(target)
        conn = _sqlite_connect_owned(target)
        sidecar_store.apply_sidecar_pragmas(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                file_path TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                vector BLOB NOT NULL,
                file_mtime REAL NOT NULL,
                PRIMARY KEY (file_path, chunk_idx)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_unit_vectors (
                unit_key TEXT NOT NULL,
                record_type TEXT NOT NULL CHECK(record_type = 'semantic_unit'),
                unit_ref TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                parent_ref TEXT,
                parent_generation TEXT NOT NULL,
                parent_source_hash TEXT NOT NULL,
                parser_version INTEGER NOT NULL,
                form TEXT NOT NULL,
                category TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                unit_source_hash TEXT NOT NULL,
                source_order INTEGER NOT NULL,
                vector BLOB NOT NULL,
                file_mtime REAL NOT NULL,
                PRIMARY KEY (parent_path, unit_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS semantic_unit_vectors_parent "
            "ON semantic_unit_vectors(parent_path, parent_generation)"
        )
        sidecar_store.ensure_meta_table(conn, "chunks", self.path.name)
        # Before any write on this connection: every writer must have the log, so
        # that a writer which bumps the generation without logging its paths is
        # DETECTED rather than silently caught up (see sidecar_store).
        sidecar_store.ensure_path_change_log(conn, CHUNK_PATH_LOG)
        stored_unit_schema = conn.execute(
            "SELECT value FROM meta WHERE key = 'semantic_unit_schema_version'"
        ).fetchone()
        if stored_unit_schema != (SEMANTIC_UNIT_SCHEMA_VERSION,):
            with conn:
                conn.execute("DELETE FROM semantic_unit_vectors")
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ("semantic_unit_schema_version", SEMANTIC_UNIT_SCHEMA_VERSION),
                )
                sidecar_store.bump_meta(conn, "semantic_unit_generation")
        if target == self.path:
            try:
                reserved_paths._publish_sqlite_owner_family(
                    self.vault_root,
                    target,
                    "embeddings-store",
                    conn,
                )
            except BaseException:
                conn.close()
                raise
        return conn

    def upsert_file(
        self,
        rel_path: str,
        chunks: list[str],
        vectors: np.ndarray,
        mtime: float,
    ) -> None:
        """Replace all rows for `rel_path` in a single transaction."""
        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunks/vectors length mismatch for {rel_path}: {len(chunks)} vs {len(vectors)}"
            )
        conn = self._connect()
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                if vec_on:
                    # BEFORE the blob delete — the subquery needs the old rowids.
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
                if chunks:
                    rows = [
                        (rel_path, i, chunks[i], vectors[i].astype(np.float32).tobytes(), mtime)
                        for i in range(len(chunks))
                    ]
                    conn.executemany(
                        "INSERT INTO chunks "
                        "(file_path, chunk_idx, chunk_text, vector, file_mtime) "
                        "VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    if vec_on:
                        self._vec.dual_insert(conn, "file_path = ?", (rel_path,))
                # Bump the write generation INSIDE this txn — declaring the path
                # it changed, so a reader one generation behind can catch up from
                # the log instead of full-loading — then read back the FULL
                # (epoch, generation, instance) token, stable under the write
                # lock. The cache keys on it, not the mtime.
                sidecar_store.bump_generation_for_paths(
                    conn, CHUNK_PATH_LOG, [rel_path]
                )
                own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        # Patch the shared in-memory matrix in place instead of nulling it, so a
        # concurrent find() doesn't pay a full O(vault) reload for this one write.
        # numpy-lite: metadata rows carry no chunk text (see class docstring).
        new_meta = [(rel_path, i) for i in range(len(chunks))]
        new_vecs = np.asarray(vectors, dtype=np.float32) if chunks else None
        self._patch_cache(rel_path, new_meta, new_vecs, own_epoch, own_gen, own_instance)

    def delete_file(self, rel_path: str) -> None:
        """Remove one parent's page and semantic-unit rows if the sidecar exists."""
        self.purge_paths_if_present([rel_path])

    def purge_paths_if_present(
        self, rel_paths: list[str], *, connection_path: Path | None = None
    ) -> int:
        """Model-free, idempotent removal for paths no longer admitted to recall.

        This intentionally never calls :meth:`_connect` for an absent sidecar:
        policy changes and disabled-model writes must be able to clean stale rows
        without creating derived state.  The chunk and semantic-unit tables (and
        vec0's chunk mirror) move together under one transaction.
        """
        paths = sorted({path for path in rel_paths if path})
        target = connection_path if connection_path is not None else self.path
        if not paths or not target.exists():
            return 0
        conn = self._connect(target)
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                removed = 0
                removed_paths: list[str] = []
                for rel_path in paths:
                    chunk_count = conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE file_path = ?", (rel_path,)
                    ).fetchone()[0]
                    unit_count = conn.execute(
                        "SELECT COUNT(*) FROM semantic_unit_vectors WHERE parent_path = ?",
                        (rel_path,),
                    ).fetchone()[0]
                    if not chunk_count and not unit_count:
                        continue
                    if vec_on and chunk_count:
                        self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                    conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
                    conn.execute(
                        "DELETE FROM semantic_unit_vectors WHERE parent_path = ?",
                        (rel_path,),
                    )
                    removed += 1
                    removed_paths.append(rel_path)
                if removed:
                    # ONE bump for the whole batch — hence the explicit path list:
                    # the generation delta alone could never say how many, or
                    # which, paths this retired.
                    sidecar_store.bump_generation_for_paths(
                        conn, CHUNK_PATH_LOG, removed_paths
                    )
                    sidecar_store.bump_meta(conn, "semantic_unit_generation")
                    own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        if removed:
            self._purge_cache_paths(set(paths), own_epoch, own_gen, own_instance)
        return removed

    def purge_exact_persisted_rows(
        self, values: list[str], *, connection_path: Path | None = None
    ) -> int:
        """Remove quarantined stored identities without filesystem routing."""
        return self.purge_paths_if_present(values, connection_path=connection_path)

    def _purge_cache_paths(
        self,
        paths: set[str],
        own_epoch: int,
        own_gen: int,
        own_instance: int,
    ) -> None:
        """Drop a multi-path block from a warm matrix only when contiguous."""
        with self._lock:
            cached = self._cache
            if cached is None:
                return
            if (
                own_epoch != cached.epoch
                or own_instance != cached.instance
                or own_gen != cached.generation + 1
            ):
                log.info(
                    "embedding matrix purge refused: paths=%s "
                    "own=(epoch=%d, gen=%d, instance=%d) "
                    "cached=(epoch=%d, gen=%d, instance=%d) delta=%d",
                    sorted(paths),
                    own_epoch,
                    own_gen,
                    own_instance,
                    cached.epoch,
                    cached.generation,
                    cached.instance,
                    own_gen - cached.generation,
                )
                self._cache = None
                return
            keep = [i for i, (path, _chunk) in enumerate(cached.metadata) if path not in paths]
            self._cache = _EmbCache(
                cached.epoch,
                own_gen,
                cached.instance,
                cached.mtime,
                cached.recall_policy_identity,
                [cached.metadata[i] for i in keep],
                cached.matrix[keep]
                if keep
                else np.zeros((0, VECTOR_DIM), dtype=np.float32),
            )

    def upsert_semantic_units(
        self,
        state: semantic_index.SemanticParentIndexState,
        vectors: np.ndarray,
        mtime: float,
    ) -> None:
        """Replace one parent's unit vectors in a single sidecar transaction."""
        rows = self._semantic_unit_rows(state, vectors, mtime)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM semantic_unit_vectors WHERE parent_path = ?",
                    (state.path,),
                )
                if rows:
                    conn.executemany(
                        "INSERT INTO semantic_unit_vectors("
                        "unit_key, record_type, unit_ref, parent_path, parent_ref, "
                        "parent_generation, parent_source_hash, parser_version, form, "
                        "category, kind, content, unit_source_hash, source_order, vector, "
                        "file_mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                sidecar_store.bump_meta(conn, "semantic_unit_generation")
        finally:
            conn.close()

    def delete_semantic_units(self, parent_path: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM semantic_unit_vectors WHERE parent_path = ?",
                    (parent_path,),
                )
                sidecar_store.bump_meta(conn, "semantic_unit_generation")
        finally:
            conn.close()

    @staticmethod
    def _semantic_unit_rows(
        state: semantic_index.SemanticParentIndexState,
        vectors: np.ndarray,
        mtime: float,
    ) -> list[tuple]:
        units = [
            (source_order, unit)
            for source_order, unit in enumerate(state.document.units)
            if unit.unit_ref is not None
        ]
        if len(units) != len(vectors):
            raise ValueError(
                f"semantic-unit/vector length mismatch for {state.path}: "
                f"{len(units)} vs {len(vectors)}"
            )
        return [
            (
                unit.unit_ref,
                "semantic_unit",
                unit.unit_ref,
                state.path,
                state.parent_ref,
                state.parent_generation,
                state.parent_source_hash,
                state.parser_version,
                unit.form,
                unit.category,
                unit.kind,
                unit.content,
                unit.source_hash,
                source_order,
                vectors[vector_order].astype(np.float32).tobytes(),
                mtime,
            )
            for vector_order, (source_order, unit) in enumerate(units)
        ]

    def _patch_cache(
        self,
        rel_path: str,
        new_meta: list[tuple[str, int]],
        new_vecs: np.ndarray | None,
        own_epoch: int,
        own_gen: int,
        own_instance: int,
    ) -> None:
        """Splice one file's rows into the cached matrix (copy-on-write) — ONLY
        when this write is contiguous with the CURRENT cache: `own_epoch ==
        cached.epoch AND own_instance == cached.instance AND own_gen ==
        cached.generation + 1`. On ANY mismatch, the splice is skipped ENTIRELY —
        content is NOT spliced and the label does NOT advance — leaving the cache
        exactly as it was; the resulting token mismatch heals via a full reload on
        the next `all_vectors()` (cheap enough — Phase 1 semantics).

        This gates content and label TOGETHER because splicing content whose
        label can't (yet) advance is unsafe on its own (a corrected design point:
        an earlier version of this cache spliced content unconditionally and only
        gated the label, which does not prevent corruption). Proven trace: writer
        A upserts file F (capturing generation 5) then stalls before calling this;
        writer B upserts the SAME file F (generation 6) and patches immediately —
        contiguous, so B's rows land and the label advances to 6; A then resumes
        and calls this with its OWN (now stale) generation 5 and its OLDER rows —
        if content were spliced unconditionally (as before), A's stale rows would
        overwrite B's current ones while the label still reads a plausible value,
        risking B's genuinely-current rows being replaced by A's stale ones. Never
        use `max()` on the generation either, for the same reason: it would let
        the cache claim a generation whose rows it never received.

        Builds fresh `metadata`/`matrix` and atomically swaps `self._cache`; never
        mutates the arrays a concurrent reader may be holding. Best-effort: any
        inconsistency (post-gate) drops the cache to None so the next
        `all_vectors()` does a safe full reload. Leaves a cold (`None`) cache
        cold — the next read loads.
        """
        with self._lock:
            c = self._cache
            if c is None:
                return
            if own_epoch != c.epoch or own_instance != c.instance or own_gen != c.generation + 1:
                log.info(
                    "embedding matrix patch refused: rel_path=%s "
                    "own=(epoch=%d, gen=%d, instance=%d) "
                    "cached=(epoch=%d, gen=%d, instance=%d) delta=%d",
                    rel_path,
                    own_epoch,
                    own_gen,
                    own_instance,
                    c.epoch,
                    c.generation,
                    c.instance,
                    own_gen - c.generation,
                )
                return  # not contiguous with what THIS cache holds -> never splice
            try:
                new_metadata, new_matrix = _splice_path_blocks(
                    c.metadata, c.matrix, {rel_path: (list(new_meta), new_vecs)}
                )
                self._cache = _EmbCache(
                    c.epoch,
                    own_gen,
                    c.instance,
                    c.mtime,
                    c.recall_policy_identity,
                    new_metadata,
                    new_matrix,
                )
            except Exception as e:  # noqa: BLE001 — self-heal, never break a write
                log.warning("embedding matrix splice failed (%s); dropping cache", e)
                self._cache = None

    def all_vectors(self) -> tuple[list[tuple[str, int]], np.ndarray]:
        """Return `(metadata, matrix)` cached until the sidecar's write generation
        (or epoch) advances — NOT its mtime (see the class + generation-meta notes).

        metadata[i] = (file_path, chunk_idx); matrix[i] = vector. Chunk text
        is deliberately NOT here (numpy-lite — see class docstring); fetch the
        winners' texts via `_texts_for` when needed.
        """
        if not self.path.exists():
            return [], np.zeros((0, VECTOR_DIM), dtype=np.float32)
        # Snapshot the cache tuple ONCE: another thread may swap or null it between
        # reads. This fast path takes no lock — the common case.
        from . import recall_policy

        policy_identity = recall_policy.recall_policy_identity(self.vault_root)
        c = self._cache
        served = (
            sidecar_store.try_serve_cached(c, self.path)
            if c is not None and c.recall_policy_identity == policy_identity
            else None
        )
        if served is not None:
            return served.metadata, served.matrix
        with self._lock:
            # Re-check under the lock: another thread may have loaded while we
            # waited, or the fast-path token read may have failed transiently.
            c = self._cache
            served = (
                sidecar_store.try_serve_cached(c, self.path)
                if c is not None and c.recall_policy_identity == policy_identity
                else None
            )
            if served is not None:
                return served.metadata, served.matrix
            # Bounded catch-up BEFORE the full reload: a cache a couple of
            # generations behind (the common case — another instance wrote, or a
            # patch was refused as non-contiguous) is patched from the changed
            # paths' rows alone, instead of paying the O(vault) SELECT + stack.
            if c is not None and c.recall_policy_identity == policy_identity:
                try:
                    patched = self._catch_up_cache(c)
                except Exception as e:  # noqa: BLE001 — always fall back, never raise
                    log.warning(
                        "embedding matrix catch-up failed (%s); taking the full load", e
                    )
                    patched = None
                if patched is not None:
                    self._cache = patched
                    return patched.metadata, patched.matrix
            # Keep this call zero-argument: cache tests and production probes
            # deliberately wrap the named full-reload seam.
            loaded = self._load_all_rows()
            log.info(
                "embedding matrix full load: reason=%s rows=%d gen=%d epoch=%d cached_gen=%d",
                sidecar_store.reload_reason(c, loaded.epoch, loaded.generation),
                len(loaded.metadata),
                loaded.generation,
                loaded.epoch,
                c.generation if c is not None else -1,
            )
            self._cache = loaded
            return loaded.metadata, loaded.matrix

    def unload_cache(self) -> bool:
        """Drop the resident matrix cache without deleting sidecar rows."""
        with self._lock:
            loaded = self._cache is not None
            self._cache = None
            # The mask memo pins the metadata list by strong reference, so an
            # unload that left it behind would keep those rows resident after
            # the caller asked for the memory back. Correctness never depended
            # on this — a reload builds a new list and simply misses.
            self._mask_cache = None
            return loaded

    def cache_status(self) -> dict:
        """Best-effort residency status for this in-memory matrix only."""
        c = self._cache
        if c is None:
            return {"loaded": False, "rows": 0, "bytes": 0}
        return {
            "loaded": True,
            "rows": len(c.metadata),
            "bytes": int(c.matrix.nbytes),
            "epoch": c.epoch,
            "generation": c.generation,
        }

    def _catch_up_cache(self, c: _EmbCache) -> _EmbCache | None:
        """Patch a slightly-stale warm cache forward from the changed paths only.

        Returns the patched cache, or None when this delta is not eligible (the
        caller then takes the full reload). Call under `self._lock` with `c` the
        cache that was just found stale and already known to match the current
        recall-policy identity.

        Everything the decision rests on — the meta token, the change log, the
        replacement rows, and the row count that cross-checks them — is read
        inside ONE explicit `BEGIN`, for exactly the reason `_load_all_rows`
        does it: python sqlite3 in autocommit gives every bare SELECT its own
        snapshot, so a split read could pair one write's generation with another
        write's rows and then label the result as current.

        What keeps an unlogged generation bump from being read as "this write
        changed nothing" is `catchup_is_eligible`'s run check, NOT the `COUNT(*)`
        below: a count cannot see an in-place re-embed of an existing chunk,
        which preserves the row count exactly. The count is a cheap secondary
        cross-check for insert/delete-shaped divergence only.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = sidecar_store.read_meta_token(conn)
                run = sidecar_store.read_logged_run(conn, CHUNK_PATH_LOG)
                if not sidecar_store.catchup_is_eligible(
                    c,
                    epoch,
                    gen,
                    instance,
                    run=run,
                    max_generations=CATCHUP_MAX_GENERATIONS,
                ):
                    return None
                changed = sidecar_store.changed_paths_since(
                    conn, CHUNK_PATH_LOG, c.generation, limit=CATCHUP_MAX_PATHS
                )
                if len(changed) > CATCHUP_MAX_PATHS:
                    return None  # wider than the bound — one clean reload is cheaper
                replacements: dict[
                    str, tuple[list[tuple[str, int]], np.ndarray | None]
                ] = {}
                for path in changed:
                    rows = conn.execute(
                        "SELECT chunk_idx, vector FROM chunks "
                        "WHERE file_path = ? ORDER BY chunk_idx",
                        (path,),
                    ).fetchall()
                    key = sys.intern(path)
                    replacements[key] = (
                        [(key, idx) for idx, _blob in rows],
                        np.stack(
                            [np.frombuffer(blob, dtype=np.float32) for _idx, blob in rows],
                            axis=0,
                        )
                        if rows
                        else None,
                    )
                total_rows = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            finally:
                conn.rollback()  # read-only txn — release the snapshot
        finally:
            conn.close()
        metadata, matrix = _splice_path_blocks(c.metadata, c.matrix, replacements)
        if len(metadata) != total_rows:
            raise ValueError(
                f"catch-up row count disagrees with the sidecar: {len(metadata)} "
                f"spliced vs {total_rows} stored (gen {c.generation} -> {gen})"
            )
        log.info(
            "embedding matrix catch-up: reason=%s paths=%d rows=%d gen=%d epoch=%d "
            "cached_gen=%d delta=%d",
            sidecar_store.CATCHUP_REASON,
            len(replacements),
            len(metadata),
            gen,
            epoch,
            c.generation,
            gen - c.generation,
        )
        return _EmbCache(
            epoch, gen, instance, c.mtime, c.recall_policy_identity, metadata, matrix
        )

    def _load_all_rows(self, policy_identity: tuple[str, str] | None = None) -> _EmbCache:
        """Full reload from the sidecar → an `_EmbCache`.

        Reads the meta token AND the rows inside ONE explicit `BEGIN` so they
        are a single consistent snapshot — python sqlite3 in autocommit runs each
        bare SELECT in its OWN snapshot, so a naive two-statement read could pair a
        generation with rows from a different write. This is the O(vault) `SELECT`
        + `np.stack` the incremental cache exists to avoid paying per find; kept a
        named method so tests can count genuine full reloads. numpy-lite: chunk
        text is neither SELECTed nor retained; file_path strings are interned so N
        rows of one file share a single str object.
        """
        if policy_identity is None:
            from . import recall_policy

            policy_identity = recall_policy.recall_policy_identity(self.vault_root)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = sidecar_store.read_meta_token(conn)
                rows = conn.execute(
                    "SELECT file_path, chunk_idx, vector FROM chunks ORDER BY file_path, chunk_idx"
                ).fetchall()
            finally:
                conn.rollback()  # read-only txn — release the snapshot
        finally:
            conn.close()
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not rows:
            return _EmbCache(
                epoch,
                gen,
                instance,
                mtime,
                policy_identity,
                [],
                np.zeros((0, VECTOR_DIM), dtype=np.float32),
            )
        metadata: list[tuple[str, int]] = []
        vectors: list[np.ndarray] = []
        for fp, idx, blob in rows:
            metadata.append((sys.intern(fp), idx))
            vectors.append(np.frombuffer(blob, dtype=np.float32))
        return _EmbCache(
            epoch, gen, instance, mtime, policy_identity, metadata, np.stack(vectors, axis=0)
        )

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        *,
        allowed_paths: set[str] | None = None,
    ) -> list[tuple[str, int, str, float]]:
        """Top-k chunk hits: list of `(file_path, chunk_idx, chunk_text, score)`.

        Backend ladder: vec0 KNN in the sidecar when available (full-precision by
        default — exact, rank-identical to the scan below; binary+rescore when
        `EXOMEM_VEC_QUANT=binary`), otherwise the in-memory numpy scan. Every vec
        failure mode falls through to the scan — search never breaks on vec0.

        Under an allowed-paths filter the scan SCORES FIRST AND MASKS SECOND:
        one BLAS pass over the whole cached matrix, then `-inf` onto the
        ineligible rows' scores. Slicing the matrix instead (`matrix[keep]`)
        fancy-indexed a fresh copy of most of a ~200 MB matrix on every single
        recall, which the matmul then had to read back — the memcpy issue #951
        measured, and the reason a semantic recall cost ~10x a keyword one with
        the GPU idle. Masking is behaviour-identical rather than approximate: an
        ineligible row scores `-inf` and `k_eff` is clamped to the eligible
        count, so `argpartition` provably cannot reach a masked row.
        """
        if allowed_paths is None:
            vec_hits = self._vec_search(query_vec, k)
            if vec_hits is not None:
                return vec_hits
        metadata, matrix = self.all_vectors()
        if not metadata:
            return []
        mask: np.ndarray | None = None
        # The ceiling on how many rows may be returned. Under a filter it is the
        # ELIGIBLE count, never the row count: `argpartition` over the full score
        # array will return `k` rows whatever the mask says, so an unclamped
        # `k_eff` pads the answer with masked `-inf` rows as soon as fewer than
        # `k` survive the filter — and returns a whole top-k when none do.
        k_ceiling = len(metadata)
        if allowed_paths is not None:
            mask, k_ceiling = self._eligibility_mask(metadata, allowed_paths)
        k_eff = min(k, k_ceiling)
        if k_eff <= 0:
            return []
        # query_vec is (768,) normalized; matrix is (N, 768) normalized.
        scores = matrix @ query_vec.astype(np.float32, copy=False)
        if mask is not None:
            scores = np.where(mask, scores, -np.inf)
        # argpartition is O(N), then sort the top-k slice.
        top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
        if mask is not None and not bool(mask[top_idx].all()):
            # An ineligible row won a slot, which masking alone cannot prevent:
            # `-(-inf)` is `+inf`, and numpy orders NaN ABOVE `+inf`, so when the
            # query embeds to NaN (a zero-norm or broken vector) every eligible
            # score is NaN and the masked rows partition first. Reachable only
            # with non-finite scores, but eligibility is a governance boundary
            # rather than a ranking preference, so it must not depend on
            # arithmetic holding. Fall back to selecting among the eligible rows
            # only — the pre-#951 computation exactly, on the rows it would have
            # had — which restores both the row and its true score.
            eligible_idx = np.flatnonzero(mask)
            sub = scores[eligible_idx]
            top_idx = eligible_idx[np.argpartition(-sub, k_eff - 1)[:k_eff]]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        top = [(metadata[i][0], metadata[i][1], float(scores[i])) for i in top_idx]
        # numpy-lite: hydrate only the winners' texts (PK point-lookups).
        try:
            texts = self._texts_for([(fp, ci) for fp, ci, _ in top])
        except Exception as e:  # noqa: BLE001 — text hydration must never break search
            log.warning("chunk-text fetch failed (%s); returning hits without text", e)
            texts = {}
        return [(fp, ci, texts.get((fp, ci), ""), score) for fp, ci, score in top]

    def _eligibility_mask(
        self, metadata: list[tuple[str, int]], allowed_paths: AbstractSet[str]
    ) -> tuple[np.ndarray, int]:
        """`(row_mask, eligible_count)` for `allowed_paths` over `metadata`'s rows.

        Memoized in a single slot (see `_MaskCache` for why the key is the
        metadata list's identity plus the allowed set's CONTENT). `allowed_paths`
        is stable across a session at a given scope, so only the first query
        after a matrix rebuild or a scope change pays the membership pass; the
        rest pay one frozenset compare instead of a `len(metadata)`-iteration
        Python loop that ran on every recall.

        Lock-free on purpose, like `all_vectors()`'s fast path. The slot holds
        one immutable NamedTuple, so a concurrent reader sees either the old
        entry or the new one and never a torn pair, and it re-validates whatever
        it read before trusting it. Two threads racing at different scopes both
        compute a correct mask and one simply wins the slot; the only loss is a
        recomputation. An `unload_cache()` that interleaves with the assignment
        below can leave one memo pinned — the next unload or rebuild clears it,
        and no answer is affected, which is not worth putting a lock on the hot
        path for.
        """
        key = frozenset(allowed_paths)
        cached = self._mask_cache
        if (
            cached is not None
            and cached.metadata is metadata
            and cached.allowed_paths == key
        ):
            return cached.mask, cached.eligible
        # Build from `key`, NOT from `allowed_paths`. The caller's set is mutable
        # and callers do mutate it, so reading it a second time here can cache a
        # mask under a key that does not describe it: snapshot {a}, another thread
        # adds b, the mask admits b, and the entry is filed under {a}. Restoring
        # the set to {a} then serves that stale mask for the rest of the session.
        # Membership on the frozenset costs the same and closes the window.
        mask = np.fromiter(
            (path in key for path, _chunk in metadata),
            dtype=bool,
            count=len(metadata),
        )
        eligible = int(np.count_nonzero(mask))
        self._mask_cache = _MaskCache(metadata, key, mask, eligible)
        return mask, eligible

    def search_semantic_units(
        self,
        query_vec: np.ndarray,
        k: int,
        *,
        allowed_unit_refs: set[str] | None = None,
        allowed_parent_paths: set[str] | None = None,
        validate: bool = True,
    ) -> list[SemanticUnitVectorHit]:
        """Score unit rows first, then validate only a bounded winner window.

        Vector scoring is an in-memory/numpy scan of rebuildable blobs. Markdown
        freshness validation is the expensive part, so an unfiltered query
        overfetches a bounded ranked window instead of reopening every parent.
        An explicit allowlist retains its exact validation contract for audit
        and repair callers.
        """
        if k <= 0 or not self.path.exists():
            return []
        if allowed_unit_refs is not None and not allowed_unit_refs:
            return []
        if allowed_parent_paths is not None and not allowed_parent_paths:
            return []

        conn = self._connect()
        try:
            if allowed_unit_refs is None and allowed_parent_paths is None:
                rows = conn.execute(
                    "SELECT unit_ref, parent_path, parent_generation, "
                    "parent_source_hash, parser_version, vector "
                    "FROM semantic_unit_vectors"
                ).fetchall()
            elif allowed_unit_refs is not None and allowed_parent_paths is None:
                rows = conn.execute(
                    "SELECT unit_ref, parent_path, parent_generation, "
                    "parent_source_hash, parser_version, vector "
                    "FROM semantic_unit_vectors "
                    "WHERE unit_ref IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(allowed_unit_refs), ensure_ascii=False),),
                ).fetchall()
            elif allowed_unit_refs is None:
                rows = conn.execute(
                    "SELECT unit_ref, parent_path, parent_generation, "
                    "parent_source_hash, parser_version, vector "
                    "FROM semantic_unit_vectors "
                    "WHERE parent_path IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(allowed_parent_paths), ensure_ascii=False),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT unit_ref, parent_path, parent_generation, "
                    "parent_source_hash, parser_version, vector "
                    "FROM semantic_unit_vectors "
                    "WHERE unit_ref IN (SELECT value FROM json_each(?)) "
                    "AND parent_path IN (SELECT value FROM json_each(?))",
                    (
                        json.dumps(sorted(allowed_unit_refs), ensure_ascii=False),
                        json.dumps(sorted(allowed_parent_paths), ensure_ascii=False),
                    ),
                ).fetchall()
        finally:
            conn.close()

        candidates: list[tuple[str, str, str, str, int, np.ndarray]] = []
        for unit_ref, parent_path, generation, source_hash, parser_version, blob in rows:
            vector = np.frombuffer(blob, dtype=np.float32)
            if vector.shape != (VECTOR_DIM,):
                continue
            candidates.append(
                (
                    str(unit_ref),
                    str(parent_path),
                    str(generation),
                    str(source_hash),
                    int(parser_version),
                    vector,
                )
            )
        if not candidates:
            return []

        query = query_vec.astype(np.float32, copy=False)
        matrix = np.stack([candidate[5] for candidate in candidates])
        scores = matrix @ query
        order = sorted(
            range(len(candidates)),
            key=lambda index: (-float(scores[index]), candidates[index][0]),
        )
        validation_limit = (
            len(order)
            if allowed_unit_refs is not None or allowed_parent_paths is not None
            else min(len(order), max(k * 4, k + 32))
        )
        if not validate:
            return [
                SemanticUnitVectorHit(
                    candidates[index][0],
                    candidates[index][1],
                    candidates[index][2],
                    candidates[index][3],
                    candidates[index][4],
                    float(scores[index]),
                )
                for index in order[:k]
            ]

        freshness_by_stamp: dict[tuple[str, str, str, int], bool] = {}
        ranked: list[SemanticUnitVectorHit] = []
        for index in order[:validation_limit]:
            unit_ref, parent_path, generation, source_hash, parser_version, _vector = candidates[
                index
            ]
            stamp = (parent_path, generation, source_hash, parser_version)
            accepted = freshness_by_stamp.get(stamp)
            if accepted is None:
                accepted = semantic_index.validate_parent_record(
                    self.vault_root,
                    parent_path=parent_path,
                    parent_generation_value=generation,
                    parent_source_hash=source_hash,
                    parser_version=parser_version,
                ).current
                freshness_by_stamp[stamp] = accepted
            if not accepted:
                continue
            ranked.append(
                SemanticUnitVectorHit(
                    unit_ref,
                    parent_path,
                    generation,
                    source_hash,
                    parser_version,
                    float(scores[index]),
                )
            )
            if len(ranked) == k:
                break
        return ranked

    def _texts_for(self, pairs: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        """chunk_text for `(file_path, chunk_idx)` pairs — search's top-k only.

        The in-memory cache holds no chunk text (numpy-lite), so the numpy
        rung hydrates its winners here: point-lookups on the table's
        `(file_path, chunk_idx)` PRIMARY KEY, batched to stay far under
        SQLite's bound-variable cap.
        """
        out: dict[tuple[str, int], str] = {}
        if not pairs:
            return out
        conn = self._connect()
        try:
            batch_size = 150  # 2 bound params per pair
            for s in range(0, len(pairs), batch_size):
                batch = pairs[s : s + batch_size]
                where = " OR ".join("(file_path = ? AND chunk_idx = ?)" for _ in batch)
                params: list = []
                for fp, ci in batch:
                    params.extend((fp, ci))
                rows = conn.execute(
                    f"SELECT file_path, chunk_idx, chunk_text FROM chunks WHERE {where}",
                    params,
                ).fetchall()
                for fp, ci, txt in rows:
                    out[(fp, ci)] = txt
        finally:
            conn.close()
        return out

    def _vec_search(
        self, query_vec: np.ndarray, k: int
    ) -> list[tuple[str, int, str, float]] | None:
        """vec0 KNN, or None when the backend can't serve (the scan takes over).

        Never creates the sidecar file on a read path (a missing sidecar keeps the
        historical `[]`-via-scan semantics), and never raises: a runtime vec
        failure logs, retires vec for this instance, and returns None.
        """
        if self._vec_failed or vecstore.backend() == "numpy" or vecstore.load_failed():
            return None
        if not self.path.exists():
            return None
        try:
            conn = self._connect()
            try:
                if not vec_gate(self, conn):
                    return None
                quant = vecstore.quant_mode() == "binary"
                pairs = self._vec.knn(conn, query_vec, k, quant=quant)
                if not pairs:
                    return []
                ids = [rid for rid, _ in pairs]
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    "SELECT rowid, file_path, chunk_idx, chunk_text FROM chunks "
                    f"WHERE rowid IN ({placeholders})",
                    ids,
                ).fetchall()
                by_id = {r[0]: r for r in rows}
                return [
                    (by_id[rid][1], by_id[rid][2], by_id[rid][3], score)
                    for rid, score in pairs
                    if rid in by_id
                ]
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — vec failure must never break search
            log.warning(
                "vec search failed for %s (%s); falling back to the in-memory scan",
                self.path,
                e,
            )
            self._vec_failed = True
            return None

    def file_mtimes(self) -> dict[str, float]:
        """Map each indexed `file_path` → its max stored `file_mtime` (one query).

        The idempotency oracle for `index_incremental`: a file whose on-disk mtime
        does not exceed this value is already current in the sidecar and is skipped.
        Empty dict when the sidecar has not been created yet.
        """
        if not self.path.exists():
            return {}
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT file_path, MAX(file_mtime) FROM chunks GROUP BY file_path"
            ).fetchall()
        finally:
            conn.close()
        return {r[0]: r[1] for r in rows if isinstance(r[0], str) and r[1] is not None}

    def semantic_unit_parent_states(
        self,
    ) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
        """Return stored generations and unit refs for incremental parity checks."""
        if not self.path.exists():
            return {}
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT parent_path, parent_generation, unit_ref FROM semantic_unit_vectors"
            ).fetchall()
        finally:
            conn.close()
        grouped: dict[str, tuple[set[str], set[str]]] = {}
        for parent_path, generation, unit_ref in rows:
            generations, unit_refs = grouped.setdefault(str(parent_path), (set(), set()))
            generations.add(str(generation))
            unit_refs.add(str(unit_ref))
        return {
            parent_path: (frozenset(generations), frozenset(unit_refs))
            for parent_path, (generations, unit_refs) in grouped.items()
        }

    def all_semantic_unit_vectors(
        self, *, batch_size: int = SEMANTIC_UNIT_READ_BATCH
    ) -> dict[str, list[SemanticUnitVectorRow]]:
        """Every stored unit vector, grouped by parent path, in ONE corpus read.

        The audit's semantic scope-divergence sensor needs a page's unit geometry,
        and nothing here could supply it: `all_vectors()` is the CHUNK matrix
        (`metadata[i] = (file_path, chunk_idx)`), and `search_semantic_units` is a
        kNN query whose hit type carries a cosine but no vector. This is the
        missing bulk read, and it is deliberately the ONLY one — a per-page
        `WHERE parent_path = ?` across a sweep is the shape this exists to prevent.

        Read-only, and NOT wired into `_cache`: that cache is keyed by
        `(file_path, chunk_idx)` and patched by chunk-path deltas, so admitting
        unit rows would either corrupt its splice arithmetic or silently serve a
        stale generation. A sweep pays one honest load instead.

        Paginated by the table's own primary key `(parent_path, unit_key)` so a
        large corpus never materialises one unbounded result set.

        A row whose blob is not a readable full-width vector is DROPPED, and that
        includes a blob whose length is not a whole number of float32s —
        `np.frombuffer` raises on those before any shape check could run, and an
        escaping raise would cost every OTHER page in the corpus its judgment
        rather than just the corrupt row. Rebuildable derived data never fails a
        sweep closed.
        """
        if not self.path.exists():
            return {}
        limit = max(1, int(batch_size))
        grouped: dict[str, list[SemanticUnitVectorRow]] = {}
        conn = self._connect()
        try:
            cursor = ("", "")
            while True:
                rows = conn.execute(
                    "SELECT parent_path, unit_key, unit_ref, source_order, vector, "
                    "parent_generation "
                    "FROM semantic_unit_vectors WHERE (parent_path, unit_key) > (?, ?) "
                    "ORDER BY parent_path, unit_key LIMIT ?",
                    (*cursor, limit),
                ).fetchall()
                if not rows:
                    break
                for parent_path, _unit_key, unit_ref, source_order, blob, generation in rows:
                    try:
                        vector = np.frombuffer(blob, dtype=np.float32)
                    except (ValueError, TypeError):
                        # Truncated or non-buffer blob: this row is unreadable, the
                        # rest of the corpus is not.
                        continue
                    if vector.shape != (VECTOR_DIM,):
                        continue
                    grouped.setdefault(str(parent_path), []).append(
                        SemanticUnitVectorRow(
                            str(unit_ref), int(source_order), vector, str(generation)
                        )
                    )
                cursor = (str(rows[-1][0]), str(rows[-1][1]))
                if len(rows) < limit:
                    break
        finally:
            conn.close()
        for unit_rows in grouped.values():
            unit_rows.sort(key=lambda row: (row.source_order, row.unit_ref))
        return grouped

    def _projected_source_snapshot(
        self,
    ) -> tuple[tuple[str, str], tuple[tuple[str, tuple[int, int, int]], ...]]:
        """Current ordinary-recall source identity for a rebuild publication.

        Rebuilds are staged from human-owned files.  This compact snapshot binds
        the staged vectors to exactly the projected corpus and local policy that
        was scanned, so a direct edit, create/delete, or access transition can
        refuse publication instead of replacing a still-valid sidecar with a
        mixed-generation corpus.
        """
        from . import freshness, recall_policy

        rows: list[tuple[str, tuple[int, int, int]]] = []
        for path in index_paths.iter_index_markdown(self.vault_root):
            rel = index_paths.rel_to_vault(self.vault_root, path)
            if rel is None:
                continue
            try:
                rows.append((rel, freshness.stat_signature(path)))
            except (OSError, ValueError):
                continue
        return recall_policy.recall_policy_identity(self.vault_root), tuple(sorted(rows))

    def rebuild_all(self) -> int:
        """Wipe + re-embed every compiled .md the index scope covers. Returns row count.

        Scope is `index_scope()` (`EXOMEM_INDEX_SCOPE`): `"kb"` (default) walks
        `Knowledge Base/` only — byte-identical to the historical behavior;
        `"vault"` walks the whole vault (`vault.walk_vault_md`) so notes outside
        `Knowledge Base/` become semantically searchable. Both honor
        `access.is_indexable` and the shared `_is_embeddable_path` /
        `_chunks_for_page` filtering, so only the walked file SET differs.
        """
        from . import access
        from . import embeddings as embeddings_module
        from . import find as find_module

        scope = index_paths.index_scope()
        # KB scope with no Knowledge Base/ is a no-op that must NOT wipe (historical
        # early return). Vault scope always proceeds — it indexes the wider tree.
        if scope == "kb" and not index_paths.kb_index_root(self.vault_root).is_dir():
            return 0
        source_snapshot = self._projected_source_snapshot()

        all_chunks: list[tuple[str, list[str], float]] = []
        all_unit_states: list[tuple[semantic_index.SemanticParentIndexState, float]] = []
        for md in index_paths.iter_index_markdown(self.vault_root):
            if not index_paths.is_embeddable_path(md):
                continue
            page = find_module._CACHE.get(md, self.vault_root)
            if page is None:
                continue
            if not access.is_indexable(self.vault_root, page.rel_path):
                continue  # excluded tree (_access.yaml) — keep it out of the index
            chunks = embeddings_module._chunks_for_page(self.vault_root, page)
            if chunks:
                all_chunks.append((page.rel_path, chunks, page.mtime))
            try:
                state = semantic_index.build_parent_index_state(self.vault_root, md)
            except (OSError, UnicodeError, ValueError):
                continue
            if any(unit.unit_ref is not None for unit in state.document.units):
                all_unit_states.append((state, page.mtime))

        if not all_chunks and not all_unit_states:
            return 0

        # Batch-embed across all files at once for GPU efficiency.
        flat_texts: list[str] = []
        for _, chunks, _ in all_chunks:
            flat_texts.extend(chunks)
        log.info(
            "rebuild_embeddings: embedding %d chunks from %d files",
            len(flat_texts),
            len(all_chunks),
        )
        vectors = (
            embeddings_module.embed_texts(flat_texts, is_query=False)
            if flat_texts
            else np.zeros((0, VECTOR_DIM), dtype=np.float32)
        )
        unit_texts = [
            unit.content
            for state, _mtime in all_unit_states
            for unit in state.document.units
            if unit.unit_ref is not None
        ]
        unit_vectors = (
            embeddings_module.embed_texts(unit_texts, is_query=False)
            if unit_texts
            else np.zeros((0, VECTOR_DIM), dtype=np.float32)
        )

        # Bulk write in ONE transaction. Per-file upsert_file() calls would each
        # open a connection, fsync, and splice the in-memory matrix — O(N²) copies
        # plus N fsyncs. Build every row, wipe + executemany once, then leave the
        # cache null (set at the top) so the next all_vectors() does ONE full load.
        insert_rows: list[tuple[str, int, str, bytes, float]] = []
        offset = 0
        total = 0
        for rel_path, chunks, mtime in all_chunks:
            for i, ch in enumerate(chunks):
                insert_rows.append(
                    (rel_path, i, ch, vectors[offset + i].astype(np.float32).tobytes(), mtime)
                )
            offset += len(chunks)
            total += len(chunks)
        unit_insert_rows: list[tuple] = []
        unit_offset = 0
        for state, mtime in all_unit_states:
            count = sum(unit.unit_ref is not None for unit in state.document.units)
            unit_insert_rows.extend(
                self._semantic_unit_rows(
                    state,
                    unit_vectors[unit_offset : unit_offset + count],
                    mtime,
                )
            )
            unit_offset += count
        conn = self._connect()
        try:
            # No initial wipe: retain the prior coherent sidecar until the staged
            # projected corpus proves it still matches immediately before commit.
            if self._projected_source_snapshot() != source_snapshot:
                log.info("rebuild_embeddings: projected source changed; publication refused")
                return 0
            vec_on = vec_gate(self, conn)
            with conn:
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM semantic_unit_vectors")
                conn.executemany(
                    "INSERT INTO chunks "
                    "(file_path, chunk_idx, chunk_text, vector, file_mtime) "
                    "VALUES (?, ?, ?, ?, ?)",
                    insert_rows,
                )
                conn.executemany(
                    "INSERT INTO semantic_unit_vectors("
                    "unit_key, record_type, unit_ref, parent_path, parent_ref, "
                    "parent_generation, parent_source_hash, parser_version, form, "
                    "category, kind, content, unit_source_hash, source_order, vector, "
                    "file_mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    unit_insert_rows,
                )
                if vec_on:
                    # One whole-table INSERT..SELECT from the fresh blobs — the
                    # bulk analog of the per-file dual-write.
                    self._vec.wipe(conn)
                    self._vec.repopulate_all(conn)
                # Bump generation (monotonic write counter) AND epoch (re-embed
                # marker) in the FINAL txn only — never the wipe txn above. A WARM
                # reader whose cache still matches the PRE-bump token keeps serving
                # its correct pre-rebuild snapshot through the wipe→final-txn gap
                # (the whole point of gating patch-cache on contiguity, F1). A COLD
                # reader (or any cache miss) racing that same gap instead loads the
                # wipe's EMPTY table under that pre-bump token, and would keep
                # serving empty until this commit moves the token — the same
                # exposure a full reload always had racing a wipe/rebuild window,
                # unchanged by this PR. epoch catches re-embeds that changed no
                # file mtimes. The per-path change log cannot describe a
                # whole-table rewrite, so it resets and a fresh logged run starts
                # here — no cache may be caught up across this write.
                sidecar_store.bump_generation_for_reset(conn, CHUNK_PATH_LOG)
                sidecar_store.bump_meta(conn, "epoch")
                sidecar_store.bump_meta(conn, "semantic_unit_generation")
        finally:
            conn.close()
        with self._lock:
            self._cache = None
        return total

    @staticmethod
    def cache_token(vault_root: Path) -> tuple[int, int, int]:
        """`(epoch, generation, instance)` for this vault's embedding sidecar —
        the freshness signal find keys its hot cache on. `(0, 0, 0)` when the
        sidecar is absent or pre-meta (legacy); find's walk triples cover
        invalidation meanwhile.

        Deliberately NOT the sidecar file's mtime: WAL-checkpoint timing moves the
        mtime independent of content (spurious misses) and an uncheckpointed commit
        leaves it unmoved (stale hits). The in-band generation is bumped inside
        every write's transaction, so it changes iff the content did; `instance`
        additionally guards the ABA case where the sidecar was deleted and
        recreated from scratch (see `sidecar_store.ensure_meta_table`). Precedent and
        rationale: lexstore.cache_token. Read-only: never creates the sidecar.
        """
        path = index_paths.sidecar_path(vault_root)
        return sidecar_store.sidecar_cache_token(path)
