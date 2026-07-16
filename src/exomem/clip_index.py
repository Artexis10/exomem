"""CLIP visual vector sidecar store.

This module owns the `.clip.sqlite` lifecycle: image vectors, video keyframe
vectors, matrix caching, and sqlite-vec fallback behavior. CLIP model loading and
encoding stay in `embeddings.py`; this file is storage only.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import index_paths, sidecar_store, vecstore
from .vector_index_common import vec_gate

log = logging.getLogger(__name__)

CLIP_DIM = 512


class _ClipCache(NamedTuple):
    """ClipIndex's in-memory matrix cache.

    `frame_ts[i]` is None for an image row, seconds for a video keyframe row;
    `paths`/`frame_ts`/`matrix` are parallel arrays.
    """

    epoch: int
    generation: int
    instance: int
    mtime: float
    paths: list[str]
    frame_ts: list[float | None]
    matrix: np.ndarray


class ClipIndex:
    """Per-vault sqlite sidecar of CLIP image/video-frame vectors."""

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.path = index_paths.clip_sidecar_path(vault_root)
        self._cache: _ClipCache | None = None
        self._lock = threading.RLock()
        self._vec = vecstore.SqliteVecStore("images", "vector", CLIP_DIM, "vec_images")
        self._vec_ready: bool | None = None
        self._vec_quant_synced = False
        self._vec_failed = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        sidecar_store.apply_sidecar_pragmas(conn)
        # Multi-vector schema: one row per image (frame_ts NULL) OR one row per
        # video keyframe (frame_ts = seconds). Composite PK keys frames within a
        # file. SQLite treats NULL as DISTINCT in a PRIMARY KEY/UNIQUE index, so
        # image writes use delete-then-insert rather than INSERT OR REPLACE.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                file_path  TEXT NOT NULL,
                frame_ts   REAL,
                vector     BLOB NOT NULL,
                file_mtime REAL NOT NULL,
                PRIMARY KEY (file_path, frame_ts)
            )
            """
        )
        self._migrate_add_frame_ts(conn)
        sidecar_store.ensure_meta_table(conn, "images", self.path.name)
        return conn

    @staticmethod
    def _migrate_add_frame_ts(conn: sqlite3.Connection) -> None:
        """Upgrade a pre-existing single-vector `images` table in place."""
        cols = [r[1] for r in conn.execute("PRAGMA table_info(images)").fetchall()]
        if "frame_ts" in cols:
            return
        with conn:
            conn.execute(
                """
                CREATE TABLE images_new (
                    file_path  TEXT NOT NULL,
                    frame_ts   REAL,
                    vector     BLOB NOT NULL,
                    file_mtime REAL NOT NULL,
                    PRIMARY KEY (file_path, frame_ts)
                )
                """
            )
            conn.execute(
                "INSERT INTO images_new (file_path, frame_ts, vector, file_mtime) "
                "SELECT file_path, NULL, vector, file_mtime FROM images"
            )
            conn.execute("DROP TABLE images")
            conn.execute("ALTER TABLE images_new RENAME TO images")
        log.info("ClipIndex: migrated images table to multi-vector schema (frame_ts)")

    def upsert(self, rel_path: str, vector: np.ndarray, mtime: float) -> None:
        """Store one image vector, preserving any video keyframe rows."""
        conn = self._connect()
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.dual_delete(
                        conn, "file_path = ? AND frame_ts IS NULL", (rel_path,)
                    )
                conn.execute(
                    "DELETE FROM images WHERE file_path = ? AND frame_ts IS NULL",
                    (rel_path,),
                )
                conn.execute(
                    "INSERT INTO images (file_path, frame_ts, vector, file_mtime) "
                    "VALUES (?, NULL, ?, ?)",
                    (rel_path, vector.astype(np.float32).tobytes(), mtime),
                )
                if vec_on:
                    self._vec.dual_insert(
                        conn, "file_path = ? AND frame_ts IS NULL", (rel_path,)
                    )
                sidecar_store.bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        self._patch_cache(
            rel_path,
            [rel_path],
            [None],
            np.asarray(vector, dtype=np.float32).reshape(1, -1),
            own_epoch,
            own_gen,
            own_instance,
            images_only=True,
        )

    def upsert_frames(
        self, rel_path: str, frames: list[tuple[float, np.ndarray]], mtime: float
    ) -> None:
        """Replace all vectors for a video with one row per keyframe."""
        if not frames:
            return
        conn = self._connect()
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM images WHERE file_path = ?", (rel_path,))
                conn.executemany(
                    "INSERT INTO images (file_path, frame_ts, vector, file_mtime) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (rel_path, float(ts), vec.astype(np.float32).tobytes(), mtime)
                        for ts, vec in frames
                    ],
                )
                if vec_on:
                    self._vec.dual_insert(conn, "file_path = ?", (rel_path,))
                sidecar_store.bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        ordered = sorted(frames, key=lambda f: f[0])
        self._patch_cache(
            rel_path,
            [rel_path] * len(ordered),
            [float(ts) for ts, _ in ordered],
            np.stack([np.asarray(vec, dtype=np.float32) for _, vec in ordered], axis=0),
            own_epoch,
            own_gen,
            own_instance,
        )

    def delete(self, rel_path: str) -> None:
        conn = self._connect()
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM images WHERE file_path = ?", (rel_path,))
                sidecar_store.bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        self._patch_cache(rel_path, [], [], None, own_epoch, own_gen, own_instance)

    def _patch_cache(
        self,
        rel_path: str,
        new_paths: list[str],
        new_frame_ts: list[float | None],
        new_vecs: np.ndarray | None,
        own_epoch: int,
        own_gen: int,
        own_instance: int,
        *,
        images_only: bool = False,
    ) -> None:
        """Splice one file's rows into the cached matrix when generation-contiguous."""
        with self._lock:
            c = self._cache
            if c is None:
                return
            if own_epoch != c.epoch or own_instance != c.instance or own_gen != c.generation + 1:
                return
            try:
                paths, frame_ts, matrix = c.paths, c.frame_ts, c.matrix
                lo, hi = sidecar_store.file_block(paths, rel_path)
                if images_only and hi > lo:
                    keep = [j for j in range(lo, hi) if frame_ts[j] is not None]
                    block_paths = [paths[j] for j in keep] + list(new_paths)
                    block_ts = [frame_ts[j] for j in keep] + list(new_frame_ts)
                    keep_mat = (
                        matrix[keep] if keep else np.zeros((0, CLIP_DIM), dtype=np.float32)
                    )
                    block_parts = [keep_mat]
                    if new_vecs is not None and new_vecs.shape[0]:
                        block_parts.append(new_vecs)
                else:
                    block_paths = list(new_paths)
                    block_ts = list(new_frame_ts)
                    block_parts = (
                        [new_vecs] if (new_vecs is not None and new_vecs.shape[0]) else []
                    )
                out_paths = paths[:lo] + block_paths + paths[hi:]
                out_ts = frame_ts[:lo] + block_ts + frame_ts[hi:]
                parts = [matrix[:lo], *block_parts, matrix[hi:]]
                parts = [p for p in parts if p.shape[0]]
                out_matrix = (
                    np.concatenate(parts, axis=0)
                    if parts
                    else np.zeros((0, CLIP_DIM), dtype=np.float32)
                )
                if not (len(out_paths) == len(out_ts) == out_matrix.shape[0]):
                    raise ValueError(
                        f"CLIP splice invariant broken for {rel_path}: "
                        f"{len(out_paths)} paths / {len(out_ts)} ts / "
                        f"{out_matrix.shape[0]} vecs"
                    )
                self._cache = _ClipCache(
                    c.epoch, own_gen, c.instance, c.mtime, out_paths, out_ts, out_matrix
                )
            except Exception as e:  # noqa: BLE001
                log.warning("CLIP matrix splice failed (%s); dropping cache", e)
                self._cache = None

    def has(self, rel_path: str) -> bool:
        """True if this file has any visual vector row."""
        if not self.path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM images WHERE file_path = ?", (rel_path,)
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def has_frames(self, rel_path: str) -> bool:
        """True if this file has at least one per-keyframe vector row."""
        if not self.path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM images WHERE file_path = ? AND frame_ts IS NOT NULL",
                (rel_path,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def all_vectors(self) -> tuple[list[str], list[float | None], np.ndarray]:
        """Return parallel `(file_paths, frame_ts, matrix)` arrays."""
        if not self.path.exists():
            return [], [], np.zeros((0, CLIP_DIM), dtype=np.float32)
        c = self._cache
        served = sidecar_store.try_serve_cached(c, self.path)
        if served is not None:
            return served.paths, served.frame_ts, served.matrix
        with self._lock:
            c = self._cache
            served = sidecar_store.try_serve_cached(c, self.path)
            if served is not None:
                return served.paths, served.frame_ts, served.matrix
            loaded = self._load_all_rows()
            log.info(
                "CLIP matrix full load: reason=%s rows=%d gen=%d epoch=%d",
                sidecar_store.reload_reason(c, loaded.epoch, loaded.generation),
                len(loaded.paths),
                loaded.generation,
                loaded.epoch,
            )
            self._cache = loaded
            return loaded.paths, loaded.frame_ts, loaded.matrix

    def unload_cache(self) -> bool:
        """Drop the resident CLIP matrix cache without deleting sidecar rows."""
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
            "rows": len(c.paths),
            "bytes": int(c.matrix.nbytes),
            "epoch": c.epoch,
            "generation": c.generation,
        }

    def _load_all_rows(self) -> _ClipCache:
        """Full reload from `.clip.sqlite` under one read transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = sidecar_store.read_meta_token(conn)
                rows = conn.execute(
                    "SELECT file_path, frame_ts, vector FROM images "
                    "ORDER BY file_path, frame_ts"
                ).fetchall()
            finally:
                conn.rollback()
        finally:
            conn.close()
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not rows:
            return _ClipCache(
                epoch, gen, instance, mtime, [], [], np.zeros((0, CLIP_DIM), dtype=np.float32)
            )
        paths = [r[0] for r in rows]
        frame_ts = [r[1] for r in rows]
        matrix = np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows], axis=0)
        return _ClipCache(epoch, gen, instance, mtime, paths, frame_ts, matrix)

    @staticmethod
    def cache_token(vault_root: Path) -> tuple[int, int, int]:
        """Read-only `(epoch, generation, instance)` token for `.clip.sqlite`."""
        return sidecar_store.sidecar_cache_token(index_paths.clip_sidecar_path(vault_root))

    def _vec_search(
        self, query_vec: np.ndarray, k: int
    ) -> list[tuple[str, float | None, float]] | None:
        """sqlite-vec KNN, or None when the backend cannot serve."""
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
                    "SELECT rowid, file_path, frame_ts FROM images "
                    f"WHERE rowid IN ({placeholders})",
                    ids,
                ).fetchall()
                by_id = {r[0]: r for r in rows}
                return [
                    (by_id[rid][1], by_id[rid][2], score)
                    for rid, score in pairs
                    if rid in by_id
                ]
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "vec search failed for %s (%s); falling back to the in-memory scan",
                self.path,
                e,
            )
            self._vec_failed = True
            return None

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        *,
        allowed_paths: set[str] | None = None,
    ) -> list[tuple[str, float | None, float]]:
        """Top-k visual hits: `(file_path, frame_ts, score)` by cosine similarity."""
        if allowed_paths is None:
            vec_hits = self._vec_search(query_vec, k)
            if vec_hits is not None:
                return vec_hits
        paths, frame_ts, matrix = self.all_vectors()
        if not paths:
            return []
        if allowed_paths is not None:
            keep = [index for index, path in enumerate(paths) if path in allowed_paths]
            if not keep:
                return []
            paths = [paths[index] for index in keep]
            frame_ts = [frame_ts[index] for index in keep]
            matrix = matrix[keep]
        scores = matrix @ query_vec.astype(np.float32, copy=False)
        k_eff = min(k, len(scores))
        if k_eff <= 0:
            return []
        top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        return [(paths[i], frame_ts[i], float(scores[i])) for i in top_idx]
