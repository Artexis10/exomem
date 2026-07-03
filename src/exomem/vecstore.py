"""SQL-native vector KNN: sqlite-vec vec0 shadow tables inside the embedding sidecars.

One `SqliteVecStore` per blob table (`chunks` in `.embeddings.sqlite`, `images` in
`.clip.sqlite`) owns everything vec0: extension loading, schema, blob↔vec sync,
dual-write SQL, and KNN (full-precision cosine, or binary Hamming with an exact
full-precision rescore). The blob tables remain the source of truth — every vec0 row
is rebuildable from stored blobs with pure SQL, so sync never involves a model.

Mapping contract: vec0 rowid == blob-table rowid (both blob tables are ordinary
rowid tables). Callers join KNN rowids back to the blob table for metadata.

Availability is a ladder, decided per process:
- `EXOMEM_VEC_BACKEND` = `auto` (default) | `sqlite-vec` | `numpy` (kill switch).
  Policy (who consults it) lives in the index classes; this module is mechanism.
- `try_load()` soft-fails once per process (`_LOAD_FAILED` memo, mirroring
  `embeddings._IMPORT_FAILED`): sqlite-vec not installed (lean deploy), or this
  Python's sqlite3 compiled without loadable-extension support
  (`AttributeError`), or the extension refusing to load (`sqlite3.Error`). All
  of these leave vector search on the in-memory numpy scan with zero behavior
  change.
- `EXOMEM_VEC_QUANT` = `off` (default) | `binary` — strictly opt-in: quantization
  can affect recall, so the golden retrieval floors are its promotion gate.

This module is import-safe without sqlite_vec installed: the import is lazy,
inside the load probe.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

import numpy as np

log = logging.getLogger(__name__)

# Binary-mode over-fetch: Hamming KNN retrieves k * this many candidates, which are
# then rescored exactly against their float32 blobs. 8x is generous for 768-bit
# signatures (per-query overlap is checked by the benchmark harness; recall by the
# golden gate).
RESCORE_MULTIPLIER = 8

# vec0 hard-caps KNN k at 4096 and ERRORS above it. find()'s candidate over-fetch
# legitimately reaches ~4000 (CLIP) and binary mode multiplies by RESCORE_MULTIPLIER,
# so every MATCH clamps to this — an unclamped k would trip the failure ladder and
# silently retire the backend for the process (the worst kind of regression: search
# still "works", just slower). Top-4096 is far beyond anything fusion consumes.
VEC0_MAX_K = 4096

_LOAD_FAILED = False  # process-global one-time soft-fail (embeddings._IMPORT_FAILED idiom)
_LOAD_LOCK = threading.Lock()


def backend() -> str:
    """`EXOMEM_VEC_BACKEND`: `auto` (default) | `sqlite-vec` | `numpy` (kill switch).

    Unrecognized values fall back to `auto` — a typo must not silently disable the
    exact numpy escape hatch someone reached for, nor hard-fail search.
    """
    raw = (os.environ.get("EXOMEM_VEC_BACKEND") or "").strip().lower()
    return raw if raw in ("sqlite-vec", "numpy") else "auto"


def quant_mode() -> str:
    """`EXOMEM_VEC_QUANT`: `off` (default) | `binary`. Unrecognized → `off`."""
    raw = (os.environ.get("EXOMEM_VEC_QUANT") or "").strip().lower()
    return "binary" if raw == "binary" else "off"


def load_failed() -> bool:
    """True once this process has failed to import/load the extension."""
    return _LOAD_FAILED


def reset_load_memo() -> None:
    """Test seam: forget a memoized load failure."""
    global _LOAD_FAILED
    _LOAD_FAILED = False


def _import_sqlite_vec():
    """Lazy import seam (monkeypatched by lean-suite tests to simulate absence)."""
    import sqlite_vec  # heavy-free, but optional: embeddings extra only

    return sqlite_vec


class SqliteVecStore:
    """vec0 shadow tables for ONE blob table: schema, sync, dual-write, KNN.

    Stateless apart from configuration — connections are owned by the caller
    (the index classes open one per operation), and per-instance sync memoization
    lives in the index, which knows its own lifecycle.
    """

    def __init__(self, source_table: str, vector_column: str, dim: int, vec_table: str):
        self.source_table = source_table
        self.vector_column = vector_column
        self.dim = dim
        self.vec_table = vec_table
        self.bin_table = f"{vec_table}_bin"

    # ------------------------------------------------------------ availability

    def try_load(self, conn: sqlite3.Connection) -> bool:
        """Load the extension on this connection; memoize failure process-wide.

        Mechanical only — backend policy (kill switch) is the caller's decision.
        """
        global _LOAD_FAILED
        if _LOAD_FAILED:
            return False
        try:
            sqlite_vec = _import_sqlite_vec()
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
            return True
        except (ImportError, AttributeError, sqlite3.Error) as e:
            with _LOAD_LOCK:
                _LOAD_FAILED = True
            log.info(
                "sqlite-vec unavailable (%s); vector search stays on the numpy scan", e
            )
            return False

    # ------------------------------------------------------------ schema + sync

    def _bin_exists(self, conn: sqlite3.Connection) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?", (self.bin_table,)
            ).fetchone()
            is not None
        )

    def ensure_synced(self, conn: sqlite3.Connection, *, quant: bool = False) -> None:
        """Create vec tables if missing; heal any blob↔vec count drift from blobs.

        This single check is both the MIGRATION for pre-existing sidecars (fresh
        vec table: 0 ≠ N → backfill) and the DRIFT HEALER for sidecars advanced by
        non-vec-aware writers. Pure SQL — never re-embeds. The caller must have
        loaded the extension on `conn`.
        """
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.vec_table} "
            f"USING vec0(embedding float[{self.dim}] distance_metric=cosine)"
        )
        if quant:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.bin_table} "
                f"USING vec0(embedding bit[{self.dim}])"
            )
        n_src = conn.execute(f"SELECT count(*) FROM {self.source_table}").fetchone()[0]
        for table, quantize in ((self.vec_table, False), (self.bin_table, True)):
            if quantize and not self._bin_exists(conn):
                continue
            n_vec = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if n_vec == n_src:
                continue
            log.info(
                "vec sync: rebuilding %s from %s blobs (%d vec rows vs %d source rows)",
                table, self.source_table, n_vec, n_src,
            )
            with conn:
                conn.execute(f"DELETE FROM {table}")
                conn.execute(self._insert_select(table, quantize, where=None))

    def _insert_select(self, table: str, quantize: bool, where: str | None) -> str:
        expr = (
            f"vec_quantize_binary({self.vector_column})" if quantize else self.vector_column
        )
        sql = (
            f"INSERT INTO {table}(rowid, embedding) "
            f"SELECT rowid, {expr} FROM {self.source_table}"
        )
        if where:
            sql += f" WHERE {where}"
        return sql

    # ------------------------------------------------------------ dual-write

    def dual_delete(self, conn: sqlite3.Connection, where: str, params: tuple) -> None:
        """Delete vec rows for blob rows matching `where`. MUST run BEFORE the blob
        delete — the subquery needs the blob rows' rowids."""
        tables = [self.vec_table]
        if self._bin_exists(conn):
            tables.append(self.bin_table)
        for table in tables:
            conn.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {self.source_table} WHERE {where})",
                params,
            )

    def dual_insert(self, conn: sqlite3.Connection, where: str, params: tuple) -> None:
        """Insert vec rows for blob rows matching `where` (after the blob insert)."""
        conn.execute(self._insert_select(self.vec_table, False, where), params)
        if self._bin_exists(conn):
            conn.execute(self._insert_select(self.bin_table, True, where), params)

    def wipe(self, conn: sqlite3.Connection) -> None:
        """Empty the vec tables (rebuild_all's initial wipe)."""
        conn.execute(f"DELETE FROM {self.vec_table}")
        if self._bin_exists(conn):
            conn.execute(f"DELETE FROM {self.bin_table}")

    def repopulate_all(self, conn: sqlite3.Connection) -> None:
        """Whole-table INSERT..SELECT after a bulk blob rebuild."""
        conn.execute(self._insert_select(self.vec_table, False, where=None))
        if self._bin_exists(conn):
            conn.execute(self._insert_select(self.bin_table, True, where=None))

    # ------------------------------------------------------------ KNN

    def knn(
        self,
        conn: sqlite3.Connection,
        query_vec: np.ndarray,
        k: int,
        *,
        quant: bool = False,
    ) -> list[tuple[int, float]]:
        """Top-k `(rowid, cosine_score)` by KNN over the vec tables.

        Full-precision mode is EXACT (same ranking as the numpy scan, modulo fp
        ties). Binary mode over-fetches `k * RESCORE_MULTIPLIER` by Hamming
        distance, then rescores those candidates against their float32 blobs — so
        returned scores are true cosine in every mode.
        """
        if k <= 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        if q.shape != (self.dim,):
            raise ValueError(f"query_vec shape {q.shape} != ({self.dim},)")
        blob = q.tobytes()
        if not quant:
            rows = conn.execute(
                f"SELECT rowid, distance FROM {self.vec_table} "
                f"WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (blob, min(k, VEC0_MAX_K)),
            ).fetchall()
            return [(rid, 1.0 - float(dist)) for rid, dist in rows]

        rows = conn.execute(
            f"SELECT rowid FROM {self.bin_table} "
            f"WHERE embedding MATCH vec_quantize_binary(?) AND k = ? ORDER BY distance",
            (blob, min(k * RESCORE_MULTIPLIER, VEC0_MAX_K)),
        ).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        candidates = conn.execute(
            f"SELECT rowid, {self.vector_column} FROM {self.source_table} "
            f"WHERE rowid IN ({placeholders})",
            ids,
        ).fetchall()
        scored = [
            (rid, float(np.frombuffer(vec_blob, dtype=np.float32) @ q))
            for rid, vec_blob in candidates
        ]
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored[:k]
