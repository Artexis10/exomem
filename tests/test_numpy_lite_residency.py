"""Regression coverage for the numpy backend's text-free resident cache."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from exomem.embeddings import VECTOR_DIM, EmbeddingIndex


@pytest.fixture(autouse=True)
def _numpy_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")


def _vector(*values: float) -> np.ndarray:
    vector = np.zeros(VECTOR_DIM, dtype=np.float32)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


def _uncached_top_k(index: EmbeddingIndex, query: np.ndarray, k: int) -> list[tuple[str, int, str, float]]:
    """Reference the former tuple-with-text scan straight from the sidecar."""
    conn = sqlite3.connect(index.path)
    try:
        rows = conn.execute(
            "SELECT file_path, chunk_idx, chunk_text, vector FROM chunks "
            "ORDER BY file_path, chunk_idx"
        ).fetchall()
    finally:
        conn.close()
    scored = [
        (path, chunk_idx, text, float(np.frombuffer(blob, dtype=np.float32) @ query))
        for path, chunk_idx, text, blob in rows
    ]
    return sorted(scored, key=lambda hit: -hit[3])[:k]


def test_numpy_query_hydrates_top_k_without_retaining_chunk_text(tmp_path: Path) -> None:
    index = EmbeddingIndex(tmp_path)
    secret_texts = [
        "resident-text-probe-alpha",
        "resident-text-probe-beta",
        "resident-text-probe-gamma",
    ]
    index.upsert_file(
        "Knowledge Base/Notes/probe.md",
        secret_texts,
        np.stack([_vector(1, 0), _vector(0.9, 0.1), _vector(0, 1)]),
        mtime=1.0,
    )
    query = _vector(1, 0)

    before = _uncached_top_k(index, query, k=2)
    after = index.search(query, k=2)

    # Same ordered top-k, text, and scores as the pre-cache tuple scan.
    assert [(path, chunk, text) for path, chunk, text, _ in after] == [
        (path, chunk, text) for path, chunk, text, _ in before
    ]
    np.testing.assert_allclose(
        [score for *_rest, score in after],
        [score for *_rest, score in before],
    )

    assert index._cache is not None
    metadata = index._cache.metadata
    assert metadata == [("Knowledge Base/Notes/probe.md", 0), ("Knowledge Base/Notes/probe.md", 1), ("Knowledge Base/Notes/probe.md", 2)]
    assert all(
        text not in value
        for text in secret_texts
        for path, _chunk_idx in metadata
        for value in (path,)
    )
