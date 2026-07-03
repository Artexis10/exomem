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
    assert vecstore.backend() == "auto"
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    assert vecstore.backend() == "numpy"
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
    assert vecstore.backend() == "sqlite-vec"
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "bogus")
    assert vecstore.backend() == "auto"
    monkeypatch.delenv("EXOMEM_VEC_BACKEND")
    monkeypatch.setenv("EXOMEM_VEC_QUANT", "binary")
    assert vecstore.quant_mode() == "binary"
    monkeypatch.setenv("EXOMEM_VEC_QUANT", "bogus")
    assert vecstore.quant_mode() == "off"


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
    """Under `auto` with the extension unavailable (the lean deployment shape, forced
    here deterministically), search serves the numpy scan with the same contract."""
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
