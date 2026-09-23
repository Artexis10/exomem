"""A write re-encodes only the text the sidecar has no vector for.

Appending one observation to a long note used to encode every chunk and every
semantic unit of the page inside the write call. The stored rows already hold
the text each vector was computed from, so an unchanged text keeps its vector.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from exomem import embeddings, readiness, semantic_index
from exomem import find as find_module

PAGE = "Knowledge Base/Notes/Insights/reuse.md"
PAGE_ID = "00000000-0000-4000-8000-0000000000a1"


def _source(observations: list[str]) -> str:
    lines = "\n".join(observations)
    return (
        "---\n"
        "title: Reuse\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {PAGE_ID}\n"
        "updated: 2026-09-18\n"
        "---\n\n"
        "# Reuse\n\nExisting prose.\n\n## Observations\n\n"
        f"{lines}\n"
    )


class _Encoder:
    """Stamps every vector with the call that produced it, so a reused vector is
    distinguishable from one recomputed from the same text."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.encodes = 0

    def __call__(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        self.calls.append(list(texts))
        self.encodes += 1
        out = np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            out[row, 0] = float(self.encodes)
            out[row, 1] = float(sum(text.encode("utf-8")) % 9973)
        return out


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    readiness.reset()
    encoder = _Encoder()
    monkeypatch.setattr(embeddings, "embed_texts", encoder)
    vault = tmp_path / "vault"
    target = vault / PAGE
    target.parent.mkdir(parents=True)
    yield vault, target, encoder
    find_module._CACHE.clear() if hasattr(find_module._CACHE, "clear") else None
    readiness.reset()


def _chunk_rows(vault: Path) -> list[tuple[int, str, float]]:
    conn = sqlite3.connect(embeddings.get_embedding_index(vault).path)
    try:
        rows = conn.execute(
            "SELECT chunk_idx, chunk_text, vector FROM chunks WHERE file_path = ? "
            "ORDER BY chunk_idx",
            (PAGE,),
        ).fetchall()
    finally:
        conn.close()
    return [(int(i), str(t), float(np.frombuffer(v, dtype=np.float32)[0])) for i, t, v in rows]


def _unit_rows(vault: Path) -> dict[str, float]:
    conn = sqlite3.connect(embeddings.get_embedding_index(vault).path)
    try:
        rows = conn.execute(
            "SELECT content, vector FROM semantic_unit_vectors WHERE parent_path = ?",
            (PAGE,),
        ).fetchall()
    finally:
        conn.close()
    return {str(c): float(np.frombuffer(v, dtype=np.float32)[0]) for c, v in rows}


def test_an_append_encodes_only_the_new_chunk_and_the_new_unit(live, monkeypatch) -> None:
    vault, target, encoder = live
    first = "- [config_rule] Keep WAL enabled #sqlite ^obs-aaaa1111"
    second = "- [config_rule] Bound every retry #reliability ^obs-bbbb2222"
    chunkings = iter([["alpha", "beta"], ["alpha", "beta", "gamma"]])
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_a, **_k: next(chunkings))

    target.write_text(_source([first]), encoding="utf-8")
    assert embeddings.upsert_after_write_status(vault, [target]).status == "completed"
    units_before = [
        unit.content
        for unit in semantic_index.current_parent_index_state(vault, target).document.units
        if unit.unit_ref is not None
    ]
    assert len(units_before) == 1, "fixture must parse to one addressable unit"
    assert encoder.calls == [["alpha", "beta"], units_before]

    target.write_text(_source([first, second]), encoding="utf-8")
    encoder.calls.clear()
    assert embeddings.upsert_after_write_status(vault, [target]).status == "completed"

    units_after = [
        unit.content
        for unit in semantic_index.current_parent_index_state(vault, target).document.units
        if unit.unit_ref is not None
    ]
    new_units = [content for content in units_after if content not in units_before]
    assert len(new_units) == 1
    assert encoder.calls == [["gamma"], new_units]

    # Unchanged texts kept the vector the first write computed (stamp 1); the
    # new ones carry the stamp of the call that encoded them. Order is the
    # page's chunk order, not encode order.
    assert [(i, t) for i, t, _ in _chunk_rows(vault)] == [(0, "alpha"), (1, "beta"), (2, "gamma")]
    assert [stamp for _, _, stamp in _chunk_rows(vault)] == [1.0, 1.0, 3.0]
    stamps = _unit_rows(vault)
    assert stamps[units_before[0]] == 2.0
    assert stamps[new_units[0]] == 4.0


def test_a_rewrite_with_no_new_text_encodes_nothing(live, monkeypatch) -> None:
    vault, target, encoder = live
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_a, **_k: ["alpha", "beta"])
    target.write_text(_source(["- [config_rule] Keep WAL enabled #sqlite ^obs-aaaa1111"]), encoding="utf-8")
    embeddings.upsert_after_write_status(vault, [target])
    encoder.calls.clear()

    # A reordered page reuses both vectors and publishes them in the new order.
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_a, **_k: ["beta", "alpha", "beta"])
    assert embeddings.upsert_after_write_status(vault, [target]).status == "completed"
    assert encoder.calls == []
    assert [(i, t) for i, t, _ in _chunk_rows(vault)] == [(0, "beta"), (1, "alpha"), (2, "beta")]


def test_a_stored_vector_of_another_width_is_not_reused(live, monkeypatch) -> None:
    vault, target, encoder = live
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_a, **_k: ["alpha", "beta"])
    target.write_text(_source(["- [config_rule] Keep WAL enabled #sqlite ^obs-aaaa1111"]), encoding="utf-8")
    embeddings.upsert_after_write_status(vault, [target])
    index = embeddings.get_embedding_index(vault)
    conn = sqlite3.connect(index.path)
    with conn:
        conn.execute(
            "UPDATE chunks SET vector = ? WHERE file_path = ? AND chunk_text = 'alpha'",
            (np.ones(3, dtype=np.float32).tobytes(), PAGE),
        )
    conn.close()
    encoder.calls.clear()

    embeddings.upsert_after_write_status(vault, [target])
    assert encoder.calls[0] == ["alpha"]


def test_stored_text_vectors_never_creates_the_sidecar(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    index = embeddings.EmbeddingIndex(vault)
    assert index.stored_text_vectors("a.md") == ({}, {})
    assert not index.path.exists()


# A sweep that scores a draft before (add) or beside (edit) the page's own rows
# encodes texts no sidecar row holds. It hands those vectors on for a short
# while, so the same write's commit, or the page's next edit, reuses them
# instead of encoding the same text again. The hand-off is bounded, and it is
# never trusted across a change of encoder or model.


def test_an_upsert_reuses_a_text_a_sweep_just_encoded(live, monkeypatch) -> None:
    vault, target, encoder = live
    embeddings.remember_passage_vectors(["alpha", "beta"], encoder(["alpha", "beta"]))
    encoder.calls.clear()
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_a, **_k: ["alpha", "beta", "gamma"])
    target.write_text(_source(["- [config_rule] Keep WAL enabled #sqlite ^obs-aaaa1111"]), encoding="utf-8")

    assert embeddings.upsert_after_write_status(vault, [target]).status == "completed"

    assert encoder.calls[0] == ["gamma"]
    # alpha/beta keep the vector the sweep computed (stamp 1); gamma is fresh.
    assert [(t, stamp) for _, t, stamp in _chunk_rows(vault)] == [
        ("alpha", 1.0),
        ("beta", 1.0),
        ("gamma", 2.0),
    ]


def test_a_handed_on_vector_is_never_served_to_another_encoder(live, monkeypatch) -> None:
    vault, target, encoder = live
    embeddings.remember_passage_vectors(["alpha"], encoder(["alpha"]))
    other = type(encoder)()
    monkeypatch.setattr(embeddings, "embed_texts", other)

    assert embeddings.recall_passage_vectors(["alpha"]) == {}
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_a, **_k: ["alpha"])
    target.write_text(_source(["- [config_rule] Keep WAL enabled #sqlite ^obs-aaaa1111"]), encoding="utf-8")
    embeddings.upsert_after_write_status(vault, [target])
    assert other.calls[0] == ["alpha"]


def test_handed_on_vectors_are_bounded_and_released_with_the_model(live, monkeypatch) -> None:
    _vault, _target, encoder = live
    limit = embeddings.PASSAGE_MEMO_MAX_TEXTS
    texts = [f"text {i}" for i in range(limit + 5)]
    embeddings.remember_passage_vectors(texts, encoder(texts))

    assert embeddings.recall_passage_vectors(texts[:5]) == {}
    assert set(embeddings.recall_passage_vectors(texts[-3:])) == set(texts[-3:])

    embeddings.unload_index_caches()
    assert embeddings.recall_passage_vectors(texts[-3:]) == {}

    embeddings.remember_passage_vectors(texts[:2], encoder(texts[:2]))
    monkeypatch.setattr(embeddings, "_MODEL", object())
    assert embeddings.unload_model() is True
    assert embeddings.recall_passage_vectors(texts[:2]) == {}
