"""Vector-backend fallback ladder — LEAN-SAFE (no sqlite-vec, no torch required).

These tests must pass on the lean CI matrix where sqlite-vec is genuinely absent:
they prove the kill switch and every unavailability path serve correct results
through the numpy scan with the unchanged return contract. `vecstore` itself must
be importable without sqlite_vec installed (its import is lazy).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from exomem import vecstore
from exomem.embeddings import VECTOR_DIM, EmbeddingIndex


def _unit_rows(rng: np.random.Generator, n: int, dim: int) -> np.ndarray:
    m = rng.standard_normal((n, dim)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return m


def _has_table(path: Path, table: str) -> bool:
    conn = sqlite3.connect(path)  # plain connection — vec tables show in sqlite_master
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _fresh_vec_state(monkeypatch: pytest.MonkeyPatch):
    vecstore.reset_load_memo()
    monkeypatch.delenv("EXOMEM_VEC_BACKEND", raising=False)
    monkeypatch.delenv("EXOMEM_VEC_QUANT", raising=False)
    yield
    vecstore.reset_load_memo()


def test_backend_env_reader_defaults(monkeypatch):
    # numpy is the default backend; sqlite-vec is strictly opt-in. Unset, the
    # legacy `auto`, and any unrecognized value all resolve to numpy — a typo (or
    # a stale `auto`) must never silently activate vec0.
    assert vecstore.backend() == "numpy"
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    assert vecstore.backend() == "numpy"
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
    assert vecstore.backend() == "sqlite-vec"
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "auto")  # legacy value → no longer special
    assert vecstore.backend() == "numpy"
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "bogus")
    assert vecstore.backend() == "numpy"
    monkeypatch.delenv("EXOMEM_VEC_BACKEND")
    monkeypatch.setenv("EXOMEM_VEC_QUANT", "binary")
    assert vecstore.quant_mode() == "binary"
    monkeypatch.setenv("EXOMEM_VEC_QUANT", "bogus")
    assert vecstore.quant_mode() == "off"


def test_default_serves_numpy_and_never_probes_extension(tmp_path, monkeypatch):
    """The new default (EXOMEM_VEC_BACKEND unset): search serves the numpy scan and
    the sqlite-vec extension is NEVER probed or loaded. This is the whole point of
    the opt-in flip — no silent probe-and-activate. Both the write path (dual-write
    gate) and the read path (search) must short-circuit before touching vec code."""
    probes = {"n": 0}

    def _spy_import():
        probes["n"] += 1
        raise ImportError("vec extension must not be probed under the numpy default")

    monkeypatch.setattr(vecstore, "_import_sqlite_vec", _spy_import)
    loads = {"n": 0}
    real_try_load = vecstore.SqliteVecStore.try_load

    def _count_try_load(self, conn):
        loads["n"] += 1
        return real_try_load(self, conn)

    monkeypatch.setattr(vecstore.SqliteVecStore, "try_load", _count_try_load)

    rng = np.random.default_rng(42)
    idx = EmbeddingIndex(tmp_path)
    vecs = _unit_rows(rng, 3, VECTOR_DIM)
    idx.upsert_file("Knowledge Base/a.md", ["c0", "c1", "c2"], vecs, 1.0)  # write path

    hits = idx.search(vecs[1], k=2)  # read path
    assert hits[0] == ("Knowledge Base/a.md", 1, "c1", pytest.approx(1.0, abs=1e-5))
    assert not _has_table(idx.path, "vec_chunks")  # no vec tables ever written
    assert loads["n"] == 0  # the gate short-circuits on the numpy default
    assert probes["n"] == 0  # the extension import is never attempted


def test_kill_switch_forces_numpy_and_skips_vec_writes(tmp_path, monkeypatch):
    """EXOMEM_VEC_BACKEND=numpy is full old behavior: numpy serves search AND the
    writers never touch vec tables (the escape hatch must not depend on vec code)."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    rng = np.random.default_rng(21)
    idx = EmbeddingIndex(tmp_path)
    vecs = _unit_rows(rng, 3, VECTOR_DIM)
    idx.upsert_file("Knowledge Base/a.md", ["c0", "c1", "c2"], vecs, 1.0)

    hits = idx.search(vecs[1], k=2)
    assert hits[0] == ("Knowledge Base/a.md", 1, "c1", pytest.approx(1.0, abs=1e-5))
    assert not _has_table(idx.path, "vec_chunks")


def test_unavailable_extension_serves_numpy_results(tmp_path, monkeypatch):
    """With `sqlite-vec` opted in but the extension unavailable (the lean deployment
    shape, forced here deterministically), search soft-fails to the numpy scan with
    the same contract."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
    monkeypatch.setattr(
        vecstore, "_import_sqlite_vec", lambda: (_ for _ in ()).throw(ImportError("lean"))
    )
    rng = np.random.default_rng(22)
    idx = EmbeddingIndex(tmp_path)
    for f in range(3):
        idx.upsert_file(
            f"Knowledge Base/n{f}.md",
            [f"t{f}-{i}" for i in range(2)],
            _unit_rows(rng, 2, VECTOR_DIM),
            1.0 + f,
        )
    query = _unit_rows(rng, 1, VECTOR_DIM)[0]
    hits = idx.search(query, k=3)
    assert len(hits) == 3
    for h in hits:
        assert isinstance(h[0], str) and isinstance(h[1], int)
        assert isinstance(h[2], str) and isinstance(h[3], float)
    # Ranking equals a direct numpy computation over the same rows.
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    assert hits == EmbeddingIndex(tmp_path).search(query, k=3)


def test_vecstore_importable_and_inert_without_sqlite_vec():
    """The module never imports sqlite_vec at import time; availability probing is
    lazy and a probe failure is final for the process until reset."""
    store = vecstore.SqliteVecStore("chunks", "vector", VECTOR_DIM, "vec_chunks")
    assert store.vec_table == "vec_chunks"
    assert vecstore.load_failed() is False  # memo starts clean; nothing probed yet
