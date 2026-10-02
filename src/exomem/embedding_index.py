"""Text embedding vector sidecar store.

This module owns the `.embeddings.sqlite` lifecycle: chunk vectors, matrix
caching, and sqlite-vec fallback behavior. Model loading and encoding stay in
`embeddings.py`; this file is storage only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from collections.abc import Set as AbstractSet
from itertools import chain
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from . import (
    call_spans,
    index_paths,
    recall_space,
    reserved_paths,
    semantic_index,
    sidecar_store,
    vecstore,
)
from .vector_index_common import vec_gate

log = logging.getLogger(__name__)


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("embedding_index"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)

#: The width of the legacy English space, kept for callers that name it. A
#: sidecar's own width is `EmbeddingIndex.dim`, read from its vector-space record.
VECTOR_DIM = recall_space.LEGACY_DIM
SEMANTIC_UNIT_SCHEMA_VERSION = 3
#: The tables holding this sidecar's vectors; the first row read for a legacy width.
_VECTOR_TABLES = ("chunks", "semantic_unit_vectors")

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
        else np.zeros((0, matrix.shape[1]), dtype=np.float32)
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


#: Query rows `EmbeddingIndex.search_many` scores per matrix product. Scoring a
#: whole draft at once holds `queries x rows` float32 scores: 293 MiB for a
#: 1,000-chunk note against 76,000 rows, where one `search` per chunk peaked at
#: a single row. A block of 64 bounds that at one block (~19 MiB at 76,000 rows)
#: and keeps most of the batched speed: for 1,000 chunks against 58,000 rows under
#: a 2-CPU quota, 5.5 s at 17 MiB peak, against 4.5 s at 225 MiB for one product
#: and 100 s for one `search` per chunk.
SEARCH_MANY_BLOCK = 64


def _top_admitted(
    scores: np.ndarray,
    k: int,
    total: int,
    admitted: Callable[[int], bool],
    metadata: list[tuple[str, int]],
) -> list[tuple[str, int, float]]:
    """One query's `k` best admitted rows from its full score row, best first.

    The candidate window holds the query's highest-scoring rows, so the first
    `k` admitted rows in window order are its top `k` admitted rows overall; the
    window widens until `k` are found or every row has been considered.
    """
    order = -scores
    window = min(total, max(4 * k, 64))
    while True:
        if window >= total:
            ranked = np.argsort(order, kind="stable")
        else:
            candidates = np.argpartition(order, window - 1)[:window]
            ranked = candidates[np.argsort(order[candidates], kind="stable")]
        picked: list[int] = []
        for row in ranked.tolist():
            if admitted(row):
                picked.append(row)
                if len(picked) == k:
                    break
        if len(picked) == k or window >= total:
            break
        window = min(total, window * 4)
    return [(metadata[row][0], metadata[row][1], float(scores[row])) for row in picked]


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

    def __init__(self, vault_root: Path, *, path: Path | None = None):
        self.vault_root = vault_root
        #: The sidecar this instance reads and writes: the serving one unless
        #: `path` names another (a new vector space being built beside it).
        self.path = path if path is not None else index_paths.sidecar_path(vault_root)
        #: The vector space the sidecar holds, as last read from it; None while
        #: it holds no vectors and no record. Refreshed on every connection.
        self._identity: recall_space.SpaceIdentity | None = None
        self._identity_read = False
        self._identity_token: tuple[int, int, int, int] | None = None
        self._cache: _EmbCache | None = None
        # One-slot memo for search()'s allowed-paths row mask (see _MaskCache).
        self._mask_cache: _MaskCache | None = None
        # Guards in-memory cache mutation only (never held across a sqlite write).
        # Reentrant so rebuild_all()-style nesting can't self-deadlock.
        self._lock = threading.RLock()
        #: Matrix served or loaded: the use signal the idle reaper watches.
        self._hits = 0
        # vec0 backend state (see vec_gate): sync memo + per-instance retirement.
        # The vec0 column is declared at the sidecar's own width; see `_vec_prepare`.
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
            conn.execute("BEGIN")
            try:
                identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                token = self._build_token(conn)
            finally:
                conn.rollback()
            self._observe_identity(identity, token)
            self._identity_read = True
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

    # ------------------------------------------------------------ vector space

    @property
    def identity(self) -> recall_space.SpaceIdentity | None:
        """The vector space this sidecar holds; None when it holds none.

        Read from the sidecar by this instance's first connection and again by
        every later one, so it is as current as the last operation. Never
        creates the sidecar.
        """
        if not self._identity_read and self.path.exists():
            conn = self._connect()
            conn.close()
        return self._identity

    @property
    def dim(self) -> int:
        """The width of this sidecar's vectors: its record's, or the encoder's in use."""
        identity = self.identity
        return identity.dim if identity is not None else recall_space.current_dim()

    def _set_identity(self, identity: recall_space.SpaceIdentity | None) -> None:
        previous, self._identity = self._identity, identity
        if identity is None:
            return
        if self._vec.dim != identity.dim:
            self._vec = vecstore.SqliteVecStore("chunks", "vector", identity.dim, "vec_chunks")
        if previous != identity:
            # A new record re-runs the vec0 sync check, which redeclares a
            # column of another width.
            self._vec_ready = None
            self._vec_quant_synced = False

    def _observe_identity(
        self, identity: recall_space.SpaceIdentity | None, token: tuple[int, int, int, int]
    ) -> bool:
        """Remember a serving snapshot unless a newer observation already won."""
        with self._lock:
            observed = self._identity_token
            if observed is not None and observed[2] == token[2] and (
                observed[0], observed[1], observed[3]
            ) > (token[0], token[1], token[3]):
                return False
            self._set_identity(identity)
            self._identity_token = token
            return True

    def _complete_publication(
        self, identity: recall_space.SpaceIdentity | None, token: tuple[int, int, int, int]
    ) -> None:
        """Fence identity and retained caches together after the SQL commit."""
        with self._lock:
            observed = self._identity_token
            if observed is None or observed[2] == token[2]:
                self._observe_identity(identity, token)
            self._invalidate_before((self._identity_token or token)[:3])

    def _vec_prepare(self, conn: sqlite3.Connection) -> bool:
        """Whether vec0 may be synced now: only once the sidecar has a width.

        A vec0 column is declared at a fixed width, so a sidecar holding no
        vectors yet must not declare one; its first write records the width
        and then syncs.
        """
        del conn
        return self._identity is not None

    def _admit(self, conn: sqlite3.Connection, dim: int) -> None:
        """Record or check the vector space before rows of width `dim` are written."""
        with conn:
            identity = recall_space.admit(conn, self._identity, dim)
            token = self._build_token(conn)
        self._observe_identity(identity, token)

    @contextlib.contextmanager
    def encoding(self, *, load: bool = False) -> Iterator[None]:
        """Encode for this sidecar inside the block, or refuse before encoding.

        Selects the encoder that serves this sidecar's vector space and checks
        it against the sidecar's record before anything is encoded, so a query
        vector or a row made inside the block belongs to this sidecar. A
        sidecar written by another model than the recall encoder's (one a
        re-embed has not replaced yet) is served by that model, which warm-up
        keeps resident and a request never loads: `recall_space.ServingEncoderCold`
        when it is not resident. `recall_space.VectorSpaceMismatch` when the
        encoder here is another build than the one the sidecar records. A
        sidecar holding no vectors takes whatever the recall encoder produces.
        """
        identity = self.identity
        if identity is None:
            yield
            return
        model = recall_space.recall_model()
        if identity.model != model:
            if recall_space.cell_mode():
                # A cell runs no second encoder and never re-embeds in place.
                raise recall_space.VectorSpaceMismatch(
                    f"the sidecar holds {identity.model} vectors; this cell encodes with {model}"
                )
            if load:
                recall_space.previous_encoder(identity.model)
            if recall_space.previous_resident(identity.model) is None:
                raise recall_space.ServingEncoderCold(
                    f"{identity.model}, which serves this sidecar, is not resident"
                )
            fingerprint = recall_space.resident_fingerprint(identity.model)
            if not identity.accepts(identity.model, fingerprint):
                raise recall_space.VectorSpaceMismatch(
                    f"the sidecar was written by another build of {identity.model}"
                )
            with recall_space.selecting(identity.model):
                yield
            return
        if identity.fingerprint is not None and recall_space.resident_fingerprint(model) is None:
            from . import embeddings

            embeddings.get_model()
        if not identity.accepts(model, recall_space.resident_fingerprint(model)):
            raise recall_space.VectorSpaceMismatch(
                f"the sidecar was written by another build of {model}"
            )
        with recall_space.selecting(None):
            yield

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
            if chunks:
                self._admit(conn, np.asarray(vectors).shape[1])
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

    def _admit_producer(
        self, conn: sqlite3.Connection, producer: recall_space.SpaceIdentity
    ) -> recall_space.SpaceIdentity:
        """Compare the actual producer with the serving space under the write lock."""
        stored = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
        if stored is not None and (
            stored.dim != producer.dim or not stored.accepts(producer.model, producer.fingerprint)
        ):
            raise recall_space.VectorSpaceMismatch(
                f"vectors from {producer} cannot join the serving space {stored}"
            )
        identity = stored or producer
        if stored is not None and stored.fingerprint is None and producer.fingerprint:
            identity = producer
        recall_space.write_identity(conn, identity)
        return identity

    def _publication_vec(
        self, conn: sqlite3.Connection, identity: recall_space.SpaceIdentity | None
    ) -> vecstore.SqliteVecStore | None:
        """Prepare a local mirror without committing or publishing process state."""
        if identity is None or self._vec_failed or vecstore.backend() == "numpy":
            return None
        mirror = vecstore.SqliteVecStore("chunks", "vector", identity.dim, "vec_chunks")
        if not mirror.try_load(conn):
            return None
        mirror.ensure_synced_in_transaction(conn, quant=vecstore.quant_mode() == "binary")
        return mirror

    def _invalidate_before(self, token: tuple[int, int, int]) -> None:
        """Release only caches older than this completed publication."""
        epoch, generation, instance = token
        with self._lock:
            cached = self._cache
            current = (
                cached is not None
                and cached.instance == instance
                and (
                    cached.epoch > epoch
                    or (cached.epoch == epoch and cached.generation >= generation)
                )
            )
            if not current:
                self._cache = None
            mask = self._mask_cache
            if mask is not None and (not current or mask.metadata is not cached.metadata):
                self._mask_cache = None

    def upsert_batch(
        self,
        replacements: list[tuple[str, list[str], np.ndarray, float]],
        unit_replacements: list[tuple[semantic_index.SemanticParentIndexState, np.ndarray, float]]
        | None = None,
        *,
        identity: recall_space.SpaceIdentity,
        validate: Callable[[], bool] | None = None,
    ) -> tuple[int, int, int, int] | None:
        """Publish one bounded group of prepared parents, then release old caches."""
        unit_replacements = unit_replacements or []
        if not replacements and not unit_replacements:
            return
        for path, chunks, vectors, _mtime in replacements:
            if len(chunks) != len(vectors) or (
                len(vectors) and np.asarray(vectors).shape != (len(chunks), identity.dim)
            ):
                raise ValueError(f"chunk/vector shape mismatch for {path}")
        unit_rows = []
        for state, vectors, mtime in unit_replacements:
            if len(vectors) and np.asarray(vectors).shape[1] != identity.dim:
                raise ValueError(f"semantic-unit vector width mismatch for {state.path}")
            unit_rows.append((state.path, self._semantic_unit_rows(state, vectors, mtime)))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if validate is not None and not validate():
                    raise ValueError("embedding input drifted before publication")
                stored = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                if any(chunks for _path, chunks, _vectors, _mtime in replacements) or any(
                    rows for _path, rows in unit_rows
                ):
                    stored = self._admit_producer(conn, identity)
                mirror = self._publication_vec(conn, stored)
                for path, chunks, vectors, mtime in replacements:
                    if mirror is not None:
                        mirror.dual_delete(conn, "file_path = ?", (path,))
                    conn.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
                    conn.executemany(
                        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                        (
                            (
                                path,
                                i,
                                chunk,
                                np.asarray(vectors[i], dtype=np.float32).tobytes(),
                                mtime,
                            )
                            for i, chunk in enumerate(chunks)
                        ),
                    )
                    if mirror is not None:
                        mirror.dual_insert(conn, "file_path = ?", (path,))
                for path, rows in unit_rows:
                    conn.execute("DELETE FROM semantic_unit_vectors WHERE parent_path = ?", (path,))
                    conn.executemany(
                        "INSERT INTO semantic_unit_vectors VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                if replacements:
                    sidecar_store.bump_generation_for_paths(
                        conn, CHUNK_PATH_LOG, [path for path, *_rest in replacements]
                    )
                if unit_replacements:
                    sidecar_store.bump_meta(conn, "semantic_unit_generation")
                if validate is not None and not validate():
                    raise ValueError("embedding input drifted during publication")
                own_token = self._build_token(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
        self._complete_publication(stored, own_token)
        return own_token

    def parent_publication_current(
        self,
        rel_path: str,
        *,
        token: tuple[int, int, int, int],
        identity: recall_space.SpaceIdentity,
        chunk_count: int,
        unit_count: int,
        parent_generation: str,
        parent_source_hash: str,
    ) -> bool:
        """Check a known exact publication without reading text or vector blobs.

        The chunk owner's existing contiguous change log proves that this
        parent's chunks have not moved since its transaction. Unit rows carry
        their deterministic generation/source hash. An absent/gapped proof is
        refused and replayed; unrelated publications do not invalidate it.
        """
        if not self.path.exists() or self.path != index_paths.sidecar_path(self.vault_root):
            return False
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            current = self._build_token(conn)
            if current[0] != token[0] or current[2] != token[2] or current[1] < token[1]:
                return False
            stored = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
            if stored != identity and (chunk_count or unit_count or stored is not None):
                return False
            if current[1] != token[1]:
                start, upto = sidecar_store.read_logged_run(conn, CHUNK_PATH_LOG)
                if start is None or start > token[1] or upto != current[1]:
                    return False
                changed = conn.execute(
                    "SELECT generation FROM chunk_path_log WHERE file_path = ?",
                    (rel_path,),
                ).fetchone()
                if changed is not None and int(changed[0]) > token[1]:
                    return False
            width = identity.dim * 4
            chunks = conn.execute(
                "SELECT COUNT(*), MIN(chunk_idx), MAX(chunk_idx), "
                "SUM(length(vector) != ?) FROM chunks WHERE file_path = ?",
                (width, rel_path),
            ).fetchone()
            if chunks[0] != chunk_count or (chunks[3] or 0) != 0:
                return False
            if chunk_count and (chunks[1], chunks[2]) != (0, chunk_count - 1):
                return False
            units = conn.execute(
                "SELECT COUNT(*), SUM(parent_generation != ? OR parent_source_hash != ? "
                "OR length(vector) != ?) FROM semantic_unit_vectors WHERE parent_path = ?",
                (parent_generation, parent_source_hash, width, rel_path),
            ).fetchone()
            return units[0] == unit_count and (units[1] or 0) == 0
        finally:
            conn.close()

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
                else np.zeros((0, cached.matrix.shape[1]), dtype=np.float32),
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
            if rows:
                self._admit(conn, np.asarray(vectors).shape[1])
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
            return [], np.zeros((0, self.dim), dtype=np.float32)
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
            self._hits += 1
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
                self._hits += 1
                return served.metadata, served.matrix
            # Bounded catch-up BEFORE the full reload: a cache a couple of
            # generations behind (the common case — another instance wrote, or a
            # patch was refused as non-contiguous) is patched from the changed
            # paths' rows alone, instead of paying the O(vault) SELECT + stack.
            if c is not None and c.recall_policy_identity == policy_identity:
                try:
                    with call_spans.span("embeddings.matrix_catch_up"):
                        patched = self._catch_up_cache(c)
                except Exception as e:  # noqa: BLE001 — always fall back, never raise
                    log.warning(
                        "embedding matrix catch-up failed (%s); taking the full load", e
                    )
                    patched = None
                if patched is not None:
                    self._cache = patched
                    self._hits += 1
                    return patched.metadata, patched.matrix
            # Keep this call zero-argument: cache tests and production probes
            # deliberately wrap the named full-reload seam.
            with call_spans.span("embeddings.matrix_load"):
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
            self._hits += 1
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
            return {"loaded": False, "rows": 0, "bytes": 0, "hits": self._hits}
        return {
            "loaded": True,
            "hits": self._hits,
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
        """Load one serving snapshot directly into its final float32 matrix."""
        if policy_identity is None:
            from . import recall_policy

            policy_identity = recall_policy.recall_policy_identity(self.vault_root)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                snapshot_token = self._build_token(conn)
                epoch, gen, instance = snapshot_token[:3]
                identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                width = identity.dim if identity is not None else recall_space.current_dim()
                count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                matrix = np.empty((count, width), dtype=np.float32)
                metadata: list[tuple[str, int]] = []
                cursor = conn.execute(
                    "SELECT file_path, chunk_idx, vector FROM chunks ORDER BY file_path, chunk_idx"
                )
                for row, (fp, idx, blob) in enumerate(cursor):
                    if len(blob) != width * np.dtype(np.float32).itemsize:
                        raise ValueError("embedding row width differs from stored space")
                    if row >= count:
                        raise ValueError("embedding row count differs from snapshot")
                    matrix[row] = np.frombuffer(blob, dtype=np.float32)
                    metadata.append((sys.intern(fp), idx))
                if len(metadata) != count:
                    raise ValueError("embedding row count differs from snapshot")
            finally:
                conn.rollback()
        finally:
            conn.close()
        self._observe_identity(identity, snapshot_token)
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return _EmbCache(epoch, gen, instance, mtime, policy_identity, metadata, matrix)

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

    def search_many(
        self,
        query_vecs: np.ndarray,
        k: int,
        *,
        admits: Callable[[str], bool],
    ) -> list[list[tuple[str, int, float]]]:
        """Top-k eligible chunk rows for each query row: `(file_path, chunk_idx, score)`.

        Each list is what `search(query, k, allowed_paths=A)` returns for that
        row, up to the choice and order among exactly tied scores, where
        `admits(file_path)` is membership in `A`: the `k` best rows whose file
        is admitted, best first, fewer only when fewer are admitted. Two things
        make it cheaper for a caller holding many queries, which is the write
        advisory scoring every chunk of a draft. The matrix is read once per
        block of `SEARCH_MANY_BLOCK` queries, in one product for the block,
        rather than once per query, and no chunk text is hydrated. And
        eligibility is asked only of files whose rows reach a query's candidate
        window, each file once, so the caller need not enumerate its whole
        eligible set to probe a few dozen of them.

        Exact, not approximate: a query's window holds its highest-scoring rows,
        so the first `k` admitted rows in window order are its top `k` admitted
        rows overall; the window widens until `k` are found or every row has
        been considered. Scores agree with `search` to the BLAS kernel wobble
        the #951 note measured, and a non-finite score sorts last exactly as
        `search`'s guarded selection leaves it.
        """
        metadata, matrix = self.all_vectors()
        queries = np.asarray(query_vecs, dtype=np.float32).reshape(-1, matrix.shape[1])
        if not len(queries):
            return []
        if not metadata or k <= 0:
            return [[] for _ in range(len(queries))]
        verdicts: dict[str, bool] = {}

        def admitted(row: int) -> bool:
            file_path = metadata[row][0]
            verdict = verdicts.get(file_path)
            if verdict is None:
                verdict = verdicts[file_path] = bool(admits(file_path))
            return verdict

        answers: list[list[tuple[str, int, float]]] = []
        total = len(metadata)
        for start in range(0, len(queries), SEARCH_MANY_BLOCK):
            # `matrix @ q.T`, not `q @ matrix.T`: the same scores, but with the
            # tall matrix leading OpenBLAS ran a 64-query block ~1.8x faster
            # (measured at 58k rows under a 2-CPU quota). Rows of the transposed
            # view are strided; `_top_admitted` reads each one once.
            block = (matrix @ queries[start : start + SEARCH_MANY_BLOCK].T).T
            answers.extend(_top_admitted(row, k, total, admitted, metadata) for row in block)
            # Released before the next product, so two blocks are never alive at once.
            del block
        return answers

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
            if vector.shape != (self.dim,):
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

    def stored_chunks_for(self, rel_path: str) -> tuple[list[str], float | None]:
        """One page's published chunk texts in index order, and the file mtime
        the embedding pass stamped on them.

        `([], None)` when the sidecar or the page's rows are absent, or the rows
        are not a contiguous `0..n-1` run (a partially replaced generation is not
        a chunking anyone cut). A reader that must not re-derive a page's
        chunking -- the context pack, for a media transcript whose chunking is
        an encode -- compares the mtime with the file it holds and takes these
        texts as the page's chunking when they match. One primary-key range
        read; never creates the sidecar.
        """
        if not self.path.exists():
            return [], None
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT chunk_idx, chunk_text, file_mtime FROM chunks "
                "WHERE file_path = ? ORDER BY chunk_idx",
                (rel_path,),
            ).fetchall()
        finally:
            conn.close()
        if not rows or [int(idx) for idx, _text, _mtime in rows] != list(range(len(rows))):
            return [], None
        mtimes = [float(mtime) for _idx, _text, mtime in rows if mtime is not None]
        if len(mtimes) != len(rows):
            return [], None
        return [str(text) for _idx, text, _mtime in rows], max(mtimes)

    def stored_text_vectors(
        self, rel_path: str, *, max_bytes: int | None = None
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        """One page's published chunk and unit vectors keyed by exact text."""
        chunks, units, _space = self.stored_text_vectors_with_space(rel_path, max_bytes=max_bytes)
        return chunks, units

    def stored_text_vectors_with_space(
        self, rel_path: str, *, max_bytes: int | None = None
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], recall_space.SpaceIdentity | None]:
        """One page's published vectors keyed by the exact text each was encoded
        from, and their space identity, all from one read transaction.

        A write that changes one chunk of a long page takes the rest from here
        instead of encoding them again. A row is offered only when its blob is
        one full float32 vector at the sidecar's width; anything else is left
        out, so the caller encodes that text. Reuse assumes what every search
        over this sidecar already assumes -- one encoder wrote all of it, which
        the sidecar's vector-space record enforces. Two primary-key range reads
        on one connection; never creates the sidecar.
        """
        if not self.path.exists():
            return {}, {}, None
        conn = self._connect()
        try:
            conn.execute("BEGIN", ())
            space = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
            if max_bytes is not None:
                # Blob + copied array, decoded strings and per-row containers.
                # Check both projections in the same snapshot before fetchall;
                # a formerly huge parent can now be a tiny replacement.
                reuse_bytes = conn.execute(
                    "SELECT COALESCE(SUM(2 * length(vector) + "
                    "4 * length(CAST(chunk_text AS BLOB)) + 512), 0) "
                    "FROM chunks WHERE file_path = ?",
                    (rel_path,),
                ).fetchone()[0]
                reuse_bytes += conn.execute(
                    "SELECT COALESCE(SUM(2 * length(vector) + "
                    "4 * length(CAST(content AS BLOB)) + 512), 0) "
                    "FROM semantic_unit_vectors WHERE parent_path = ?",
                    (rel_path,),
                ).fetchone()[0]
                if reuse_bytes > max_bytes:
                    return {}, {}, space
            chunk_rows = conn.execute(
                "SELECT chunk_text, vector FROM chunks WHERE file_path = ?",
                (rel_path,),
            ).fetchall()
            unit_rows = conn.execute(
                "SELECT content, vector FROM semantic_unit_vectors WHERE parent_path = ?",
                (rel_path,),
            ).fetchall()
        finally:
            conn.close()
        dim = space.dim if space is not None else recall_space.current_dim()
        width = dim * np.dtype(np.float32).itemsize

        def keyed(rows: list[tuple[Any, Any]]) -> dict[str, np.ndarray]:
            return {
                str(text): np.frombuffer(blob, dtype=np.float32).copy()
                for text, blob in rows
                if isinstance(blob, (bytes, memoryview)) and len(blob) == width
            }

        return keyed(chunk_rows), keyed(unit_rows), space

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
                    if vector.shape != (self.dim,):
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

    @staticmethod
    def _build_token(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
        unit_generation = conn.execute(
            "SELECT value FROM meta WHERE key = 'semantic_unit_generation'"
        ).fetchone()
        return (*sidecar_store.read_meta_token(conn), int(unit_generation[0]) if unit_generation else 0)

    def rebuild_all(self, *, batch_size: int = 256) -> int:
        """Stage bounded batches in this sidecar and atomically replace serving rows.

        A serving publication or source/policy change during encoding refuses this
        candidate. Staging is invisible to readers and cleanup touches this run only.
        """
        from . import access
        from . import embeddings as embeddings_module
        from . import find as find_module

        scope = index_paths.index_scope()
        if scope == "kb" and not index_paths.kb_index_root(self.vault_root).is_dir():
            return 0
        source_snapshot = self._projected_source_snapshot()
        limit = max(1, int(batch_size))

        def pages() -> Iterator[
            tuple[Any, list[str], semantic_index.SemanticParentIndexState | None]
        ]:
            for md in index_paths.iter_index_markdown(self.vault_root):
                if not index_paths.is_embeddable_path(md):
                    continue
                page = find_module._CACHE.get(md, self.vault_root)
                if page is None or not access.is_indexable(self.vault_root, page.rel_path):
                    continue
                chunks = embeddings_module._chunks_for_page(self.vault_root, page) or []
                try:
                    state = semantic_index.build_parent_index_state(self.vault_root, md)
                except (OSError, UnicodeError, ValueError):
                    state = None
                if chunks or (
                    state is not None
                    and any(unit.unit_ref is not None for unit in state.document.units)
                ):
                    yield page, chunks, state

        inputs = pages()
        first = next(inputs, None)
        if first is None:
            return 0
        run_key = uuid.uuid4().hex
        conn = self._connect()
        total = 0
        producer: recall_space.SpaceIdentity | None = None
        producer_model = recall_space.encoding_model()
        try:
            conn.execute("BEGIN")
            captured_token = self._build_token(conn)
            conn.rollback()
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embedding_build_runs "
                    "(run_key TEXT PRIMARY KEY, serving_token TEXT NOT NULL, space_identity TEXT)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embedding_build_chunks AS "
                    "SELECT CAST(NULL AS TEXT) AS run_key, chunks.* FROM chunks WHERE 0"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embedding_build_units AS "
                    "SELECT CAST(NULL AS TEXT) AS run_key, semantic_unit_vectors.* "
                    "FROM semantic_unit_vectors WHERE 0"
                )
                conn.execute(
                    "INSERT INTO embedding_build_runs(run_key, serving_token) VALUES (?, ?)",
                    (run_key, json.dumps(captured_token)),
                )
            pending_chunks: list[tuple[str, int, str, float]] = []
            pending_units: list[tuple] = []

            def flush(rows: list, *, units: bool = False) -> None:
                nonlocal producer, total
                if not rows:
                    return
                if recall_space.encoding_model() != producer_model:
                    raise recall_space.VectorSpaceMismatch(
                        "rebuild producer changed during staging"
                    )
                texts = [row[11] if units else row[2] for row in rows]
                vectors, encoded = embeddings_module._encode_prepared(
                    lambda: embeddings_module.embed_texts(texts, is_query=False)
                )
                if len(vectors) != len(rows):
                    raise ValueError("rebuild encoder returned an invalid vector shape")
                if recall_space.encoding_model() != producer_model or (
                    producer is not None and encoded != producer
                ):
                    raise recall_space.VectorSpaceMismatch(
                        "rebuild producer changed during staging"
                    )
                producer = encoded
                with conn:
                    conn.execute(
                        "UPDATE embedding_build_runs SET space_identity = ? WHERE run_key = ?",
                        (json.dumps([producer.model, producer.fingerprint, producer.dim]), run_key),
                    )
                    if units:
                        conn.executemany(
                            "INSERT INTO embedding_build_units VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                (run_key, *row[:14], vector.tobytes(), row[15])
                                for row, vector in zip(rows, vectors, strict=True)
                            ),
                        )
                    else:
                        conn.executemany(
                            "INSERT INTO embedding_build_chunks VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                (run_key, path, idx, text, vector.tobytes(), mtime)
                                for (path, idx, text, mtime), vector in zip(
                                    rows, vectors, strict=True
                                )
                            ),
                        )
                        total += len(rows)
                rows.clear()

            for page, chunks, state in chain((first,), inputs):
                for idx, text in enumerate(chunks):
                    pending_chunks.append((page.rel_path, idx, text, page.mtime))
                    if len(pending_chunks) >= limit:
                        flush(pending_chunks)
                if state is None:
                    continue
                # Build row metadata without allocating placeholder vectors for a page.
                for order, unit in enumerate(state.document.units):
                    if unit.unit_ref is None:
                        continue
                    pending_units.append(
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
                            order,
                            None,
                            page.mtime,
                        )
                    )
                    if len(pending_units) >= limit:
                        flush(pending_units, units=True)
            flush(pending_chunks)
            flush(pending_units, units=True)
            if producer is None:
                return 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                if (
                    self._build_token(conn) != captured_token
                    or self._projected_source_snapshot() != source_snapshot
                ):
                    conn.rollback()
                    log.info(
                        "rebuild_embeddings: serving or source snapshot changed; publication refused"
                    )
                    return 0
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM semantic_unit_vectors")
                recall_space.write_identity(conn, producer)
                conn.execute(
                    "INSERT INTO chunks SELECT file_path, chunk_idx, chunk_text, vector, file_mtime "
                    "FROM embedding_build_chunks WHERE run_key = ?",
                    (run_key,),
                )
                conn.execute(
                    "INSERT INTO semantic_unit_vectors SELECT "
                    "unit_key, record_type, unit_ref, parent_path, parent_ref, "
                    "parent_generation, parent_source_hash, parser_version, form, category, "
                    "kind, content, unit_source_hash, source_order, vector, file_mtime "
                    "FROM embedding_build_units WHERE run_key = ?",
                    (run_key,),
                )
                mirror = self._publication_vec(conn, producer)
                if mirror is not None:
                    mirror.wipe(conn)
                    mirror.repopulate_all(conn)
                sidecar_store.bump_generation_for_reset(conn, CHUNK_PATH_LOG)
                sidecar_store.bump_meta(conn, "epoch")
                sidecar_store.bump_meta(conn, "semantic_unit_generation")
                if self._projected_source_snapshot() != source_snapshot:
                    conn.rollback()
                    return 0
                own_token = self._build_token(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._complete_publication(producer, own_token)
            return total
        finally:
            conn.rollback()
            try:
                with conn:
                    for table in (
                        "embedding_build_chunks",
                        "embedding_build_units",
                        "embedding_build_runs",
                    ):
                        if (
                            conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE name = ?", (table,)
                            ).fetchone()
                            is not None
                        ):
                            conn.execute(f"DELETE FROM {table} WHERE run_key = ?", (run_key,))
            finally:
                conn.close()

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
