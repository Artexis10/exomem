"""Activation index vectors (step 4, T7; design §4.1).

Signature vectors are fingerprinted with the encoder that made them, re-embedded
only for the anchors whose signature changed, never loaded on a request thread,
and read as one cached matrix per write token. Torch-free: the encoder is a
deterministic fake keyed by text.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from test_working_set_index import _seed_structure, _write

from exomem import (
    embeddings,
    ranking_config,
    readiness,
    working_set,
    working_set_index,
    working_set_runtime,
)


class _Encoder:
    """A resident-or-cold activation encoder whose vectors are a function of text."""

    def __init__(self, fingerprint: str = "fake-model|cls|l2|aaaa", dim: int = 16, resident: bool = True) -> None:
        self.fingerprint = fingerprint
        self.dim = dim
        self.resident = resident
        self.encoded: list[list[str]] = []
        self.loads = 0

    def vector(self, text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(f"{self.fingerprint}|{text}".encode()).digest()[:8], "little")
        vector = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
        return vector / np.linalg.norm(vector)

    def load(self) -> _Encoder:
        if not self.resident:
            self.loads += 1
            self.resident = True
        return self

    def passages(self, texts: list[str]) -> np.ndarray:
        assert self.resident, "the index encodes only with a resident or just-loaded encoder"
        self.encoded.append(list(texts))
        return np.vstack([self.vector(text) for text in texts])


@pytest.fixture
def encoder(vault: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_structure(vault)
    fake = _Encoder()
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_activation_passages", fake.passages)
    monkeypatch.setattr(embeddings, "activation_fingerprint", lambda: fake.fingerprint if fake.resident else None)
    monkeypatch.setattr(embeddings, "get_activation_model", fake.load)
    monkeypatch.setattr(embeddings, "get_model", lambda: pytest.fail("the index never loads recall's model"))
    monkeypatch.setattr(embeddings, "embed_texts", lambda *_a, **_k: pytest.fail("the index encodes on the activation lane"))
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    working_set_runtime.reset_caches_for_tests()
    yield vault, fake
    working_set_runtime.reset_caches_for_tests()


def _encoded(fake: _Encoder) -> list[str]:
    return [text for batch in fake.encoded for text in batch]


def test_the_schema_records_fingerprinted_vectors() -> None:
    assert working_set_index.SCHEMA_VERSION == 9


def test_a_rollback_to_schema_8_and_back_keeps_the_index_working(encoder) -> None:
    """A host that rolls back one release opens this sidecar with the schema-8
    code, which writes vectors as `(anchor_id, vector)`. That insert must
    succeed on this table, and the next schema-9 open rebuilds from it."""
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    path = index.path
    index.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT OR REPLACE INTO anchor_vectors (anchor_id, vector) VALUES (?, ?)",
            ("schema-8-row", np.zeros(16, dtype=np.float32).tobytes()),
        )
        conn.commit()
    finally:
        conn.close()
    working_set_index._MATRIX_CACHE.clear()

    again = working_set_index.WorkingSetIndex(vault)
    again.rebuild(load_encoder=True)

    ids, matrix = again.vector_matrix(fake.fingerprint)
    assert matrix is not None and len(ids) == len(again.anchors())
    assert "schema-8-row" not in ids


def test_one_changed_anchor_encodes_exactly_one_signature(encoder) -> None:
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    first = _encoded(fake)
    ids, matrix = index.vector_matrix(fake.fingerprint)

    assert first and matrix is not None and matrix.shape == (len(ids), fake.dim)
    assert len(ids) == len(first) == len(index.anchors())

    fake.encoded.clear()
    _write(
        vault / "Knowledge Base/Products/Cargo Sled.md",
        "---\ntype: note\nstatus: active\n---\n# Cargo Sled\n\nA sled rebuilt for deep snow.\n",
    )
    index.update()

    assert len(_encoded(fake)) == 1
    assert "deep snow" in _encoded(fake)[0]
    ids_after, matrix_after = index.vector_matrix(fake.fingerprint)
    assert set(ids_after) == set(ids)


def test_an_unchanged_catalogue_encodes_nothing_and_keeps_its_generation(encoder) -> None:
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    generation = index.generation()
    fake.encoded.clear()

    result = index.update()

    assert result.get("unchanged") is True
    assert fake.encoded == []
    assert index.generation() == generation


def test_a_fingerprint_change_empties_the_matrix_until_a_background_pass(encoder) -> None:
    """Two vector spaces never meet: vectors made by another encoder are not
    read, and only a pass that may load the encoder re-embeds them all."""
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    fake.fingerprint = "fake-model|cls|l2|bbbb"
    fake.encoded.clear()

    ids, matrix = index.vector_matrix(fake.fingerprint)
    bands, state = working_set.signature_evidence(index, "Could the sled cope?")

    assert (ids, matrix) == ((), None)
    assert (bands, state) == ({}, "absent")

    index.update(load_encoder=True)

    assert len(_encoded(fake)) == len(index.anchors())
    ids, matrix = index.vector_matrix(fake.fingerprint)
    assert matrix is not None and len(ids) == len(index.anchors())
    assert index.vector_matrix("fake-model|cls|l2|aaaa") == ((), None)


def test_an_inline_update_never_loads_the_encoder(encoder) -> None:
    """A request thread carries forward what it can; a cold encoder is the
    background build's to load."""
    vault, fake = encoder
    fake.resident = False
    index = working_set_index.WorkingSetIndex(vault)

    index.rebuild()

    assert fake.loads == 0 and fake.encoded == []
    assert index.vector_matrix(fake.fingerprint) == ((), None)

    index.update(load_encoder=True)

    assert fake.loads == 1
    assert len(_encoded(fake)) == len(index.anchors())


def test_a_changed_signature_without_an_encoder_loses_only_its_own_vector(encoder) -> None:
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    before, _ = index.vector_matrix(fake.fingerprint)
    sled = next(row.anchor_id for row in index.anchors() if row.title == "Cargo Sled")
    fake.resident = False
    _write(
        vault / "Knowledge Base/Products/Cargo Sled.md",
        "---\ntype: note\nstatus: active\n---\n# Cargo Sled\n\nA sled rebuilt for deep snow.\n",
    )

    index.update()

    fake.resident = True
    after, _ = index.vector_matrix(fake.fingerprint)
    assert set(after) == set(before) - {sled}
    assert fake.encoded and len(_encoded(fake)) == len(before), "only the first build encoded"


def test_an_inline_pass_encodes_a_bounded_number_of_signatures(encoder, monkeypatch) -> None:
    vault, fake = encoder
    monkeypatch.setattr(working_set_index, "INLINE_VECTOR_ENCODE_LIMIT", 3)
    index = working_set_index.WorkingSetIndex(vault)

    index.rebuild()
    assert len(_encoded(fake)) == 3
    index.update()
    assert len(_encoded(fake)) == 6

    fake.encoded.clear()
    index.update(load_encoder=True)
    assert len(_encoded(fake)) == len(index.anchors()) - 6, "a background pass has no bound"


def test_the_matrix_is_read_once_per_write_token(encoder, monkeypatch) -> None:
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    reads: list[int] = []
    real = working_set_index.WorkingSetIndex._read_matrix
    monkeypatch.setattr(
        working_set_index.WorkingSetIndex,
        "_read_matrix",
        lambda self, conn: reads.append(1) or real(self, conn),
    )

    first = index.vector_matrix(fake.fingerprint)
    second = index.vector_matrix(fake.fingerprint)

    assert reads == [1]
    assert first[0] == second[0] and np.array_equal(first[1], second[1])
    assert not first[1].flags.writeable, "the cached matrix is shared, never mutated"
    _write(
        vault / "Knowledge Base/Products/Cargo Sled.md",
        "---\ntype: note\nstatus: active\n---\n# Cargo Sled\n\nA sled rebuilt for deep snow.\n",
    )
    index.update()
    index.vector_matrix(fake.fingerprint)
    assert reads == [1, 1]


def test_a_384_dimension_matrix_round_trips(encoder) -> None:
    vault, fake = encoder
    fake.dim = 384
    index = working_set_index.WorkingSetIndex(vault)

    index.rebuild()

    ids, matrix = index.vector_matrix(fake.fingerprint)
    assert matrix is not None and matrix.shape == (len(ids), 384) and matrix.dtype == np.float32
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    anchor = next(row for row in index.anchors() if row.title == "Cargo Sled")
    assert np.allclose(matrix[ids.index(anchor.anchor_id)], fake.vector(_signature_of(index, anchor.anchor_id)), atol=1e-6)


def _signature_of(index: working_set_index.WorkingSetIndex, anchor_id: str) -> str:
    conn = index._connect()
    return conn.execute("SELECT signature FROM anchors WHERE anchor_id = ?", (anchor_id,)).fetchone()[0]


def test_signature_evidence_uses_the_activation_query_lane(encoder, monkeypatch) -> None:
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(embeddings, "embed_query_if_loaded", lambda *_a, **_k: pytest.fail("recall's query lane"))
    turns: list[str] = []
    monkeypatch.setattr(
        embeddings, "embed_activation_query_if_loaded", lambda text: turns.append(text) or fake.vector(text)
    )
    # The seeded vault is a dozen anchors; the band's own population floor is
    # tested in test_semantic_band.py.
    monkeypatch.setattr(
        ranking_config,
        "DEFAULT_RANKING",
        dataclasses.replace(ranking_config.DEFAULT_RANKING, working_set_semantic_min_population=1),
    )

    bands, state = working_set.signature_evidence(index, "Could the sled cope?")

    assert turns == ["Could the sled cope?"]
    assert state in {"ready", "uncalibrated"}


def test_a_cold_encoder_reads_unavailable_without_loading(encoder, monkeypatch) -> None:
    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    fake.resident = False
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(
        embeddings, "embed_activation_query_if_loaded", lambda *_a: pytest.fail("no encode on a cold encoder")
    )

    assert working_set.signature_evidence(index, "Could the sled cope?") == ({}, "unavailable")
    assert fake.loads == 0
