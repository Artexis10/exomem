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
    monkeypatch.setattr(
        embeddings, "embed_activation_passages_if_loaded", lambda texts: fake.passages(texts) if fake.resident else None
    )
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


def test_a_catalogue_without_vectors_reads_absent_not_unavailable(encoder) -> None:
    """An install without the embeddings extra has no encoder and never had
    vectors: that is `absent`, a settled state a packet may be cached under,
    exactly as before step 4. `unavailable` is for vectors that exist while
    their encoder is cold."""
    vault, fake = encoder
    fake.resident = False
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()

    assert index.vector_matrix(fake.fingerprint) == ((), None)
    assert working_set.signature_evidence(index, "Could the sled cope?") == ({}, "absent")


def test_the_fingerprint_and_the_rows_are_read_in_one_snapshot(encoder, monkeypatch) -> None:
    """A writer in another process that re-embeds under another encoder between
    the fingerprint read and the rows read must not hand this reader the other
    encoder's vectors under its own fingerprint."""
    vault, fake = encoder
    reader = working_set_index.WorkingSetIndex(vault)
    reader.rebuild(load_encoder=True)
    first = fake.fingerprint
    working_set_index._MATRIX_CACHE.clear()

    class Interleaved:
        def __init__(self, conn) -> None:
            self._conn = conn
            self.fired = False

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args):
            cursor = self._conn.execute(sql, *args)
            if not self.fired and "activation_encoder_fingerprint" in sql and sql.lstrip().upper().startswith("SELECT"):
                self.fired = True
                row = cursor.fetchone()
                fake.fingerprint, fake.dim = "fake-model|cls|l2|bbbb", 16
                writer = working_set_index.WorkingSetIndex(vault)
                writer.update(load_encoder=True)
                writer.close()
                fake.fingerprint = first

                class Done:
                    def fetchone(self_inner):
                        return row

                return Done()
            return cursor

    reader._conn = Interleaved(reader._connect())
    ids, matrix = reader.vector_matrix(first)

    assert matrix is not None
    own = {anchor_id: fake.vector(text) for anchor_id, text in _signatures(reader).items()}
    for anchor_id, row in zip(ids, matrix, strict=True):
        assert np.allclose(row, own[anchor_id], atol=1e-5), "a row made by another encoder"


def _signatures(index: working_set_index.WorkingSetIndex) -> dict[str, str]:
    return {row.anchor_id: _signature_of(index, row.anchor_id) for row in index.anchors()}


def test_an_inline_pass_never_starts_another_encoders_vector_space(encoder, monkeypatch) -> None:
    """In a managed runtime, after the resident encoder changes, an inline pass
    encodes nothing: the stored rows stay under the fingerprint they were made
    with, and semantic evidence reads `absent` until the background pass it
    schedules re-embeds them all. A partial population under the new encoder
    would calibrate the band on a sample."""
    vault, fake = encoder
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    first = fake.fingerprint
    stored, _ = index.vector_matrix(first)
    fake.fingerprint = "fake-model|cls|l2|bbbb"
    fake.encoded.clear()
    _write(vault / "Knowledge Base/Products/Tern Wagon.md", "---\ntype: note\nstatus: active\n---\n# Tern Wagon\n\nNew.\n")

    for _ in range(3):
        index.update()

    assert fake.encoded == []
    assert set(index.vector_matrix(first)[0]) == set(stored)
    assert index.vector_matrix(fake.fingerprint) == ((), None)
    assert working_set.signature_evidence(index, "Could the sled cope?") == ({}, "absent")


def test_an_unmanaged_runtime_refills_a_changed_encoder_s_vectors_inline(encoder, monkeypatch) -> None:
    """With no managed runtime nothing schedules the background re-embed (the
    reviewer's r5_unmanaged probe: `absent` forever). So there a changed
    encoder is a first population: the stale rows go, and each inline pass
    fills at most `INLINE_VECTOR_ENCODE_LIMIT` signatures from the resident
    model, never loading one."""
    vault, fake = encoder
    monkeypatch.setattr(readiness, "runtime_managed", lambda: False)
    for i in range(70):
        _write(
            vault / f"Knowledge Base/Products/Zorvath {i:03d}.md",
            f"---\ntype: note\nstatus: active\n---\n# Zorvath {i:03d}\n\nPlanted {i}.\n",
        )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild(load_encoder=True)
    first = fake.fingerprint
    total = len(index.anchors())
    assert total > working_set_index.INLINE_VECTOR_ENCODE_LIMIT
    fake.fingerprint = "fake-model|cls|l2|bbbb"
    fake.encoded.clear()
    monkeypatch.setattr(embeddings, "get_activation_model", lambda: pytest.fail("a request-thread pass loaded a model"))

    passes = 0
    while len(index.vector_matrix(fake.fingerprint)[0]) < len(index.anchors()) and passes < 5:
        _write(
            vault / f"Knowledge Base/Products/Tern Wagon {passes}.md",
            "---\ntype: note\nstatus: active\n---\n# Tern Wagon\n\nNew.\n",
        )
        index.update()
        passes += 1
        assert index.vector_matrix(first) == ((), None), "no row of the old encoder survives"

    assert passes == 2
    assert all(len(batch) <= working_set_index.INLINE_VECTOR_ENCODE_LIMIT for batch in fake.encoded)
    assert index.vector_fingerprint() == fake.fingerprint
    assert set(index.vector_matrix(fake.fingerprint)[0]) == {anchor.anchor_id for anchor in index.anchors()}
    assert working_set.signature_evidence(index, "Could the sled cope?")[1] != "absent"


def test_a_managed_runtime_re_embeds_once_after_the_encoder_changes(encoder, monkeypatch) -> None:
    """A vault that does not change still gets its vectors re-embedded when the
    resident encoder changes: the managed runtime schedules one background
    build, and after it every row is under the new fingerprint. No request
    thread ever encoded a signature."""
    from exomem import reserved_paths

    vault, fake = encoder
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild(freshness_stamp="stamp-1", load_encoder=True)
    index.close()
    monkeypatch.setattr(working_set_runtime, "_managed", lambda: True)
    monkeypatch.setattr(reserved_paths, "identity_catalogue_ready", lambda root: True)
    scheduled: list[str] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root, freshness_stamp="": scheduled.append(freshness_stamp)
    )
    fake.fingerprint = "fake-model|cls|l2|bbbb"
    fake.encoded.clear()

    for _ in range(5):
        status, served, stale = working_set_runtime.ensure_index(vault, freshness_stamp="stamp-1")
        assert (status, stale) == (working_set_runtime.READY, False)
        assert working_set.signature_evidence(served, "Could the sled cope?")[1] == "absent"

    assert scheduled == ["stamp-1"], "exactly one background build"
    assert fake.encoded == [], "no request thread encoded a signature"

    working_set_index.WorkingSetIndex(vault).update(freshness_stamp="stamp-1", load_encoder=True)

    after = working_set_index.WorkingSetIndex(vault)
    ids, matrix = after.vector_matrix(fake.fingerprint)
    assert matrix is not None and len(ids) == len(after.anchors())
    status, served, _stale = working_set_runtime.ensure_index(vault, freshness_stamp="stamp-1")
    assert scheduled == ["stamp-1"], "nothing more to schedule once the vectors match"


def test_an_inline_update_never_loads_a_model_when_the_reaper_wins_the_race(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reaper may unload the model between the pass's residency check and
    its encode. The inline pass then encodes nothing: it only ever uses a model
    already resident, and never loads one on the request thread."""
    from exomem import embedding_backend

    _seed_structure(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv(embeddings.ACTIVATION_MODEL_ENV, raising=False)

    class Resident:
        backend, device, concurrent_encodes = "onnx", "cpu", True
        profile = embedding_backend.EncoderProfile(
            model=embeddings.MODEL_NAME, pooling="cls", query_prefix="", passage_prefix="", max_seq=512, pad_token="[PAD]"
        )

        def encode(self, texts, **_kwargs):
            return np.vstack([_Encoder().vector(text) for text in texts])

        def release(self) -> None:
            pass

    loads: list[str] = []
    monkeypatch.setattr(embedding_backend, "load_encoder", lambda name, **_k: loads.append(name) or Resident())
    monkeypatch.setattr(embeddings, "_MODEL", Resident())
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    fingerprint = embeddings.activation_fingerprint()
    assert len(index.vector_matrix(fingerprint)[0]) == len(index.anchors())
    _write(vault / "Knowledge Base/Products/Tern Wagon.md", "---\ntype: note\nstatus: active\n---\n# Tern Wagon\n\nNew.\n")
    real = embeddings.activation_fingerprint
    reaped: list[bool] = []

    def residency_check_then_reap():
        value = real()
        if value is not None and not reaped:
            reaped.append(embeddings.unload_model())
        return value

    monkeypatch.setattr(embeddings, "activation_fingerprint", residency_check_then_reap)

    index.update()

    assert reaped == [True]
    assert loads == [], "a request-thread index update loaded a model"
