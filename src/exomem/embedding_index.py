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
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import index_paths, semantic_index, sidecar_store, vecstore
from .vector_index_common import vec_gate

log = logging.getLogger(__name__)

VECTOR_DIM = 768
SEMANTIC_UNIT_SCHEMA_VERSION = 3

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
    metadata: list[tuple[str, int]]
    matrix: np.ndarray


class SemanticUnitVectorHit(NamedTuple):
    """One current semantic-unit vector candidate with its raw cosine."""

    unit_ref: str
    parent_path: str
    parent_generation: str
    parent_source_hash: str
    parser_version: int
    cosine: float


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
        # Guards in-memory cache mutation only (never held across a sqlite write).
        # Reentrant so rebuild_all()-style nesting can't self-deadlock.
        self._lock = threading.RLock()
        # vec0 backend state (see vec_gate): sync memo + per-instance retirement.
        self._vec = vecstore.SqliteVecStore("chunks", "vector", VECTOR_DIM, "vec_chunks")
        self._vec_ready: bool | None = None
        self._vec_quant_synced = False
        self._vec_failed = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
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
                f"chunks/vectors length mismatch for {rel_path}: "
                f"{len(chunks)} vs {len(vectors)}"
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
                # Bump the write generation INSIDE this txn, then read back the
                # FULL (epoch, generation, instance) token — stable under the
                # write lock. The cache keys on it, not the mtime.
                sidecar_store.bump_meta(conn, "generation")
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
        conn = self._connect()
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
                conn.execute(
                    "DELETE FROM semantic_unit_vectors WHERE parent_path = ?",
                    (rel_path,),
                )
                sidecar_store.bump_meta(conn, "generation")
                sidecar_store.bump_meta(conn, "semantic_unit_generation")
                own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        self._patch_cache(rel_path, [], None, own_epoch, own_gen, own_instance)

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
                return  # not contiguous with what THIS cache holds -> never splice
            try:
                lo, hi = sidecar_store.file_block([m[0] for m in c.metadata], rel_path)
                new_metadata = c.metadata[:lo] + list(new_meta) + c.metadata[hi:]
                parts = [c.matrix[:lo]]
                if new_vecs is not None and new_vecs.shape[0]:
                    parts.append(new_vecs)
                parts.append(c.matrix[hi:])
                parts = [p for p in parts if p.shape[0]]
                new_matrix = (
                    np.concatenate(parts, axis=0)
                    if parts
                    else np.zeros((0, VECTOR_DIM), dtype=np.float32)
                )
                if len(new_metadata) != new_matrix.shape[0]:
                    raise ValueError(
                        f"splice invariant broken for {rel_path}: "
                        f"{len(new_metadata)} meta rows vs {new_matrix.shape[0]} vectors"
                    )
                self._cache = _EmbCache(
                    c.epoch, own_gen, c.instance, c.mtime, new_metadata, new_matrix
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
        c = self._cache
        served = sidecar_store.try_serve_cached(c, self.path)
        if served is not None:
            return served.metadata, served.matrix
        with self._lock:
            # Re-check under the lock: another thread may have loaded while we
            # waited, or the fast-path token read may have failed transiently.
            c = self._cache
            served = sidecar_store.try_serve_cached(c, self.path)
            if served is not None:
                return served.metadata, served.matrix
            loaded = self._load_all_rows()
            log.info(
                "embedding matrix full load: reason=%s rows=%d gen=%d epoch=%d",
                sidecar_store.reload_reason(c, loaded.epoch, loaded.generation),
                len(loaded.metadata), loaded.generation, loaded.epoch,
            )
            self._cache = loaded
            return loaded.metadata, loaded.matrix

    def unload_cache(self) -> bool:
        """Drop the resident matrix cache without deleting sidecar rows."""
        with self._lock:
            loaded = self._cache is not None
            self._cache = None
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

    def _load_all_rows(self) -> _EmbCache:
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
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = sidecar_store.read_meta_token(conn)
                rows = conn.execute(
                    "SELECT file_path, chunk_idx, vector FROM chunks "
                    "ORDER BY file_path, chunk_idx"
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
                epoch, gen, instance, mtime, [], np.zeros((0, VECTOR_DIM), dtype=np.float32)
            )
        metadata: list[tuple[str, int]] = []
        vectors: list[np.ndarray] = []
        for fp, idx, blob in rows:
            metadata.append((sys.intern(fp), idx))
            vectors.append(np.frombuffer(blob, dtype=np.float32))
        return _EmbCache(epoch, gen, instance, mtime, metadata, np.stack(vectors, axis=0))

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
        """
        if allowed_paths is None:
            vec_hits = self._vec_search(query_vec, k)
            if vec_hits is not None:
                return vec_hits
        metadata, matrix = self.all_vectors()
        if not metadata:
            return []
        if allowed_paths is not None:
            keep = [index for index, (path, _chunk) in enumerate(metadata) if path in allowed_paths]
            if not keep:
                return []
            metadata = [metadata[index] for index in keep]
            matrix = matrix[keep]
        # query_vec is (768,) normalized; matrix is (N, 768) normalized.
        scores = matrix @ query_vec.astype(np.float32, copy=False)
        k_eff = min(k, len(scores))
        if k_eff <= 0:
            return []
        # argpartition is O(N), then sort the top-k slice.
        top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        top = [(metadata[i][0], metadata[i][1], float(scores[i])) for i in top_idx]
        # numpy-lite: hydrate only the winners' texts (PK point-lookups).
        try:
            texts = self._texts_for([(fp, ci) for fp, ci, _ in top])
        except Exception as e:  # noqa: BLE001 — text hydration must never break search
            log.warning("chunk-text fetch failed (%s); returning hits without text", e)
            texts = {}
        return [(fp, ci, texts.get((fp, ci), ""), score) for fp, ci, score in top]

    def search_semantic_units(
        self,
        query_vec: np.ndarray,
        k: int,
        *,
        allowed_unit_refs: set[str] | None = None,
    ) -> list[SemanticUnitVectorHit]:
        """Return current unit rows ranked by cosine after exact eligibility.

        Semantic-unit rows deliberately use the exact numpy rung even when the
        optional vec0 page backend is enabled. The allowlist and parent
        generation/source/parser validation are applied before scoring and
        top-k, so stale or ineligible high-cosine rows cannot consume the
        bounded candidate window.
        """
        if k <= 0 or not self.path.exists():
            return []
        if allowed_unit_refs is not None and not allowed_unit_refs:
            return []

        conn = self._connect()
        try:
            if allowed_unit_refs is None:
                rows = conn.execute(
                    "SELECT unit_ref, parent_path, parent_generation, "
                    "parent_source_hash, parser_version, vector "
                    "FROM semantic_unit_vectors"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT unit_ref, parent_path, parent_generation, "
                    "parent_source_hash, parser_version, vector "
                    "FROM semantic_unit_vectors "
                    "WHERE unit_ref IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(allowed_unit_refs), ensure_ascii=False),),
                ).fetchall()
        finally:
            conn.close()

        current_rows: list[tuple[str, str, str, str, int, np.ndarray]] = []
        freshness_by_stamp: dict[tuple[str, str, str, int], bool] = {}
        for unit_ref, parent_path, generation, source_hash, parser_version, blob in rows:
            stamp = (
                str(parent_path),
                str(generation),
                str(source_hash),
                int(parser_version),
            )
            accepted = freshness_by_stamp.get(stamp)
            if accepted is None:
                accepted = semantic_index.validate_parent_record(
                    self.vault_root,
                    parent_path=stamp[0],
                    parent_generation_value=stamp[1],
                    parent_source_hash=stamp[2],
                    parser_version=stamp[3],
                ).current
                freshness_by_stamp[stamp] = accepted
            if not accepted:
                continue
            vector = np.frombuffer(blob, dtype=np.float32)
            if vector.shape != (VECTOR_DIM,):
                continue
            current_rows.append(
                (
                    str(unit_ref),
                    stamp[0],
                    stamp[1],
                    stamp[2],
                    stamp[3],
                    vector,
                )
            )
        if not current_rows:
            return []

        query = query_vec.astype(np.float32, copy=False)
        ranked = sorted(
            (
                SemanticUnitVectorHit(
                    unit_ref,
                    parent_path,
                    generation,
                    source_hash,
                    parser_version,
                    float(vector @ query),
                )
                for (
                    unit_ref,
                    parent_path,
                    generation,
                    source_hash,
                    parser_version,
                    vector,
                ) in current_rows
            ),
            key=lambda hit: (-hit.cosine, hit.unit_ref),
        )
        return ranked[:k]

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
                batch = pairs[s:s + batch_size]
                where = " OR ".join(
                    "(file_path = ? AND chunk_idx = ?)" for _ in batch
                )
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
                self.path, e,
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
                "SELECT parent_path, parent_generation, unit_ref "
                "FROM semantic_unit_vectors"
            ).fetchall()
        finally:
            conn.close()
        grouped: dict[str, tuple[set[str], set[str]]] = {}
        for parent_path, generation, unit_ref in rows:
            generations, unit_refs = grouped.setdefault(
                str(parent_path), (set(), set())
            )
            generations.add(str(generation))
            unit_refs.add(str(unit_ref))
        return {
            parent_path: (frozenset(generations), frozenset(unit_refs))
            for parent_path, (generations, unit_refs) in grouped.items()
        }

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
        # Wipe whole table — easier than per-file diff for a one-shot rebuild.
        conn = self._connect()
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.wipe(conn)
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM semantic_unit_vectors")
        finally:
            conn.close()
        self._cache = None

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
                # file mtimes.
                sidecar_store.bump_meta(conn, "generation")
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
