"""`corpus_aware.pairwise_best_cosine_from_sidecar`: proximity among packed pages
from the sidecar's own rows, never from an encode. Torch-free: a fake index stands
in for the sidecar and the encoder is patched to refuse."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from exomem import corpus_aware, embeddings

A = "Knowledge Base/Notes/A.md"
B = "Knowledge Base/Notes/B.md"
C = "Knowledge Base/Notes/C.md"
D = "Knowledge Base/Notes/D.md"


class _FakeIndex:
    def __init__(self, rows: list[tuple[str, int, str, list[float]]]):
        self._rows = rows

    def all_vectors(self):
        metadata = [(fp, ci) for fp, ci, _text, _vec in self._rows]
        matrix = np.asarray([vec for _fp, _ci, _text, vec in self._rows], dtype=np.float32)
        return metadata, matrix

    def _texts_for(self, pairs):
        wanted = set(pairs)
        return {(fp, ci): text for fp, ci, text, _vec in self._rows if (fp, ci) in wanted}


@pytest.fixture
def no_encoder(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the sidecar path must not encode")

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_texts", refuse)
    monkeypatch.setattr(embeddings, "_embed_live_chunks", refuse)
    monkeypatch.setattr(embeddings, "get_model", refuse)


def _use(monkeypatch: pytest.MonkeyPatch, index: _FakeIndex) -> None:
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda vault_root: index)


def test_pairwise_scores_come_from_stored_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_encoder
) -> None:
    b_y = math.sqrt(1 - 0.85**2)
    index = _FakeIndex(
        [
            (A, 0, "A\n\nfirst", [1.0, 0.0]),
            (A, 1, "A\n\nsecond", [0.0, 1.0]),
            (B, 0, "B\n\nonly", [0.85, b_y]),
            (C, 0, "C\n\nonly", [2.0, 0.0]),  # unnormalised on disk; still cosine 1.0 with A[0]
        ]
    )
    _use(monkeypatch, index)
    scores, covered = corpus_aware.pairwise_best_cosine_from_sidecar(
        tmp_path,
        [(A, ["A\n\nfirst", "A\n\nsecond"]), (B, ["B\n\nonly"]), (C, ["C\n\nonly"])],
    )
    assert covered == {A, B, C}
    assert scores[frozenset((A, B))] == pytest.approx(max(0.85, b_y), abs=1e-6)
    assert scores[frozenset((A, C))] == pytest.approx(1.0, abs=1e-6)
    assert scores[frozenset((B, C))] == pytest.approx(0.85, abs=1e-6)


def test_a_page_whose_rows_are_not_exact_contributes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_encoder
) -> None:
    index = _FakeIndex(
        [
            (A, 0, "A\n\nfirst", [1.0, 0.0]),
            (B, 0, "B\n\nstale text", [1.0, 0.0]),  # page moved since it was embedded
            (C, 0, "C\n\nonly", [1.0, 0.0]),
            (C, 1, "C\n\nsecond", [0.0, 1.0]),  # one row too many for its current chunking
        ]
    )
    _use(monkeypatch, index)
    scores, covered = corpus_aware.pairwise_best_cosine_from_sidecar(
        tmp_path,
        [
            (A, ["A\n\nfirst"]),
            (B, ["B\n\nfresh text"]),
            (C, ["C\n\nonly"]),
            (D, ["D\n\nnever embedded"]),
        ],
    )
    assert covered == {A}
    assert scores == {}


def test_disabled_embeddings_and_a_broken_sidecar_are_no_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    assert corpus_aware.pairwise_best_cosine_from_sidecar(tmp_path, [(A, ["x"])]) == ({}, set())
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")

    def broken(vault_root):
        raise RuntimeError("sidecar locked")

    monkeypatch.setattr(embeddings, "get_embedding_index", broken)
    assert corpus_aware.pairwise_best_cosine_from_sidecar(tmp_path, [(A, ["x"])]) == ({}, set())
    assert corpus_aware.pairwise_best_cosine_from_sidecar(tmp_path, []) == ({}, set())
