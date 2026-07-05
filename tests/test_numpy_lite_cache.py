"""numpy-lite: the matrix cache holds NO chunk text; search hydrates top-k.

The numpy rung's in-memory cache used to retain every chunk's text — most of
its memory bill at scale (~2GB of ~3.5GB RSS at 200k chunks). Now the cache is
`(file_path, chunk_idx)` + matrix only, and `search()` fetches the winners'
texts by PRIMARY KEY at query time (exactly how the vec0 path hydrates by
rowid). These tests pin: correct text hydration, rank parity with the raw
matrix, a text-free cache, and the write-splice keeping both properties.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem.embeddings import VECTOR_DIM, EmbeddingIndex


@pytest.fixture(autouse=True)
def _numpy_backend(monkeypatch: pytest.MonkeyPatch):
    """Force the numpy rung — the vec0 shadow path is out of scope here."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")


def _unit_rows(rng: np.random.Generator, n: int) -> np.ndarray:
    m = rng.standard_normal((n, VECTOR_DIM)).astype(np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def _build(tmp_path: Path, rng: np.random.Generator, files: int = 5, chunks: int = 3):
    idx = EmbeddingIndex(tmp_path)
    texts: dict[tuple[str, int], str] = {}
    for f in range(files):
        rel = f"Knowledge Base/Notes/note-{f:03d}.md"
        vecs = _unit_rows(rng, chunks)
        chunk_texts = [f"text of file {f} chunk {i}" for i in range(chunks)]
        idx.upsert_file(rel, chunk_texts, vecs, mtime=1000.0 + f)
        for i, txt in enumerate(chunk_texts):
            texts[(rel, i)] = txt
    return idx, texts


def test_search_hydrates_correct_texts_and_rank_parity(tmp_path: Path) -> None:
    rng = np.random.default_rng(11)
    idx, texts = _build(tmp_path, rng)
    q = _unit_rows(rng, 1)[0]

    hits = idx.search(q, k=7)
    assert len(hits) == 7
    # Texts are the exact upserted strings, hydrated from the sidecar.
    for fp, ci, txt, _score in hits:
        assert txt == texts[(fp, ci)]
    # Rank parity with the raw matrix math.
    metadata, matrix = idx.all_vectors()
    scores = matrix @ q
    expect = sorted(range(len(scores)), key=lambda i: -scores[i])[:7]
    assert [(h[0], h[1]) for h in hits] == [metadata[i] for i in expect]
    # Scores are the true cosines, descending.
    assert all(h1[3] >= h2[3] for h1, h2 in zip(hits, hits[1:]))


def test_cache_holds_no_chunk_text(tmp_path: Path) -> None:
    rng = np.random.default_rng(12)
    idx, _ = _build(tmp_path, rng)
    idx.search(_unit_rows(rng, 1)[0], k=3)  # loads the cache
    assert idx._cache is not None
    _epoch, _gen, _mtime, metadata, _matrix = idx._cache
    assert metadata and all(len(m) == 2 for m in metadata), (
        "numpy-lite cache must hold (file_path, chunk_idx) only — no text"
    )


def test_write_splice_keeps_hydration_and_shape(tmp_path: Path) -> None:
    rng = np.random.default_rng(13)
    idx, texts = _build(tmp_path, rng)
    idx.search(_unit_rows(rng, 1)[0], k=3)  # warm the cache so upsert splices
    rel = "Knowledge Base/Notes/note-001.md"
    new_vecs = _unit_rows(rng, 4)
    new_texts = [f"replaced chunk {i}" for i in range(4)]
    idx.upsert_file(rel, new_texts, new_vecs, mtime=2000.0)
    for i, txt in enumerate(new_texts):
        texts[(rel, i)] = txt
    texts = {k: v for k, v in texts.items() if not (k[0] == rel and k[1] >= 4)}

    hits = idx.search(new_vecs[2], k=5)
    assert hits[0][0] == rel and hits[0][1] == 2  # its own vector wins
    for fp, ci, txt, _score in hits:
        assert txt == texts[(fp, ci)]
    _epoch, _gen, _mtime, metadata, matrix = idx._cache
    assert len(metadata) == matrix.shape[0]
    assert all(len(m) == 2 for m in metadata)
