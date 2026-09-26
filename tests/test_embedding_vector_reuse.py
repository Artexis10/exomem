"""A write re-encodes only the text the sidecar has no vector for.

Appending one observation to a long note used to encode every chunk and every
semantic unit of the page inside the write call. The stored rows already hold
the text each vector was computed from, so an unchanged text keeps its vector.
"""

from __future__ import annotations

import dataclasses
import itertools
import random
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from exomem import (
    call_spans,
    corpus_aware,
    embedding_backend,
    embeddings,
    readiness,
    runtime_resources,
    semantic_index,
)
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
    embeddings.remember_passage_vectors(
        ["alpha", "beta"], encoder(["alpha", "beta"]), stamp=embeddings.passage_memo_stamp()
    )
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
    """A substituted encoder -- the bench's, a test's -- is another vector space."""
    vault, target, encoder = live
    embeddings.remember_passage_vectors(
        ["alpha"], encoder(["alpha"]), stamp=embeddings.passage_memo_stamp()
    )
    other = type(encoder)()
    monkeypatch.setattr(embeddings, "embed_texts", other)

    assert embeddings.recall_passage_vectors(["alpha"], stamp=embeddings.passage_memo_stamp()) == {}
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_a, **_k: ["alpha"])
    target.write_text(_source(["- [config_rule] Keep WAL enabled #sqlite ^obs-aaaa1111"]), encoding="utf-8")
    embeddings.upsert_after_write_status(vault, [target])
    assert other.calls[0] == ["alpha"]


def test_handed_on_vectors_are_bounded_and_released_with_the_model(live, monkeypatch) -> None:
    _vault, _target, encoder = live
    limit = embeddings.PASSAGE_MEMO_MAX_TEXTS
    texts = [f"text {i}" for i in range(limit + 5)]
    stamp = embeddings.passage_memo_stamp()
    embeddings.remember_passage_vectors(texts, encoder(texts), stamp=stamp)

    assert embeddings.recall_passage_vectors(texts[:5], stamp=stamp) == {}
    assert set(embeddings.recall_passage_vectors(texts[-3:], stamp=stamp)) == set(texts[-3:])

    embeddings.unload_index_caches()
    assert embeddings.recall_passage_vectors(texts[-3:], stamp=stamp) == {}

    embeddings.remember_passage_vectors(texts[:2], encoder(texts[:2]), stamp=stamp)
    monkeypatch.setattr(embeddings, "_MODEL", object())
    assert embeddings.unload_model() is True
    assert embeddings.recall_passage_vectors(texts[:2], stamp=stamp) == {}
    assert embeddings.recall_passage_vectors(texts[:2], stamp=embeddings.passage_memo_stamp()) == {}


# ---------------- the hand-off follows the resident model ----------------
#
# These drive the real model lifecycle -- `get_model`, `unload_model`, the idle
# reaper's cache eviction -- with only the loader replaced, so every vector says
# which load produced it. A sweep's encode can finish just before the reaper
# unloads the model and something loads another; the sweep then files vectors
# of a model that is no longer resident.


def _model_tag(name: str) -> float:
    return float(sum(name.encode("utf-8")) % 997 + 1)


class _TaggedModel:
    """A loaded encoder whose vectors carry its own model tag in column 0."""

    backend = "fake"
    device = "cpu"
    delay = 0.0

    def __init__(self, name: str, log: list[tuple[str, str]]) -> None:
        self.name = name
        self.tag = _model_tag(name)
        self._log = log

    def encode(self, texts, **_kwargs) -> np.ndarray:
        if self.delay:
            time.sleep(self.delay)
        self._log.extend((self.name, text) for text in texts)
        out = np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32)
        out[:, 0] = self.tag
        out[:, 1] = [float(sum(text.encode("utf-8")) % 9973) for text in texts]
        return out

    def release(self) -> None:
        pass


@pytest.fixture
def lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    log: list[tuple[str, str]] = []
    monkeypatch.setattr(
        embedding_backend, "load_encoder", lambda name, **_k: _TaggedModel(name, log)
    )
    monkeypatch.setattr(embeddings, "MODEL_NAME", "model-a")
    monkeypatch.setattr(embeddings, "_MODEL", None)
    embeddings.clear_embedding_indexes()
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    yield vault, log
    embeddings.clear_embedding_indexes()
    readiness.reset()


def test_a_vector_encoded_before_a_model_reload_is_never_handed_on(
    lifecycle, monkeypatch
) -> None:
    """The sweep encodes on model A; A is unloaded and B loaded before it files them."""
    vault, log = lifecycle
    embeddings.get_model()
    real_record = call_spans.record_span
    reloaded: list[str] = []

    def reload_after_the_encode(name, elapsed_ms, fields=None):
        real_record(name, elapsed_ms, fields)
        if name == "embeddings.encode" and not reloaded:
            reloaded.append(name)
            assert embeddings.unload_model() is True
            monkeypatch.setattr(embeddings, "MODEL_NAME", "model-b")
            embeddings.get_model()

    monkeypatch.setattr(call_spans, "record_span", reload_after_the_encode)
    body = "First probe paragraph.\n\nSecond probe paragraph."
    corpus_aware._best_cosine_per_file(vault, title="Reload probe", body=body)
    monkeypatch.setattr(call_spans, "record_span", real_record)
    chunks = embeddings.chunk_text("Reload probe", body)
    assert reloaded and [name for name, _t in log] == ["model-a"] * len(chunks)

    vectors = embeddings._embed_live_chunks_reusing(chunks, {})

    assert vectors[:, 0].tolist() == [_model_tag("model-b")] * len(chunks), (
        "the upsert took vectors model-a produced while model-b is the resident model"
    )


def test_handed_on_vectors_follow_the_resident_model_under_concurrent_reloads(
    lifecycle, monkeypatch
) -> None:
    """Sweeps, the reaper's unload plus the next load, cache evictions and upserts race.

    Every sweep encodes fresh text, so it always files vectors; the reaper unloads
    whenever nothing is encoding and the model is loaded straight back under the
    other name. An upsert is judged only when one model was resident throughout.
    """
    from exomem import accel

    vault, _log = lifecycle
    monkeypatch.setattr(_TaggedModel, "delay", 0.0005)
    # An unload's full collection takes tens of milliseconds and would space the
    # reloads out; the race is about ordering, not about garbage.
    monkeypatch.setattr(embeddings.gc, "collect", lambda *_a: 0)
    accel.empty_cache()  # the first call imports the runtime; keep it out of the window
    embeddings.get_model()
    recent: list[list[str]] = []
    recent_lock = threading.Lock()
    stop = time.monotonic() + 3.0
    errors: list[str] = []
    judged = [0]
    wrong = [0]
    reloads = [0]

    def admitted(call):
        # The model gate refuses past its admission capacity; a real caller retries.
        try:
            return call()
        except runtime_resources.ModelBusyError:
            return None

    def guarded(fn):
        def run():
            try:
                fn()
            except Exception as error:  # noqa: BLE001 - collected and asserted below
                errors.append(repr(error))

        return run

    def sweeper(seed: int):
        local = random.Random(seed)

        def run():
            while time.monotonic() < stop:
                body = "\n\n".join(f"Race paragraph {local.random():.12f}." for _ in range(3))
                corpus_aware._best_cosine_per_file(vault, title="Race probe", body=body)
                with recent_lock:
                    recent.append(embeddings.chunk_text("Race probe", body))
                    del recent[:-32]

        return run

    def reaper():
        names = itertools.cycle(["model-b", "model-a"])
        local = random.Random(3)
        while time.monotonic() < stop:
            if embeddings.unload_model():
                reloads[0] += 1
                embeddings.MODEL_NAME = next(names)  # restored by the fixture
                admitted(embeddings.get_model)
            if local.random() < 0.05:
                embeddings.clear_embedding_indexes()
            if local.random() < 0.05:
                embeddings.unload_index_caches()
            time.sleep(0.0005)

    def consumer():
        local = random.Random(4)
        while time.monotonic() < stop:
            with recent_lock:
                chunks = local.choice(recent) if recent else None
            if chunks is None:
                continue
            model = embeddings._MODEL
            vectors = admitted(
                lambda chunks=chunks: embeddings._embed_live_chunks_reusing(chunks, {})
            )
            if vectors is None or model is None or model is not embeddings._MODEL:
                continue  # judge only an upsert the same model saw start to end
            judged[0] += len(chunks)
            wrong[0] += int(np.count_nonzero(vectors[:, 0] != model.tag))
            assert len(embeddings._PASSAGE_MEMO) <= embeddings.PASSAGE_MEMO_MAX_TEXTS

    threads = [
        threading.Thread(target=guarded(target))
        for target in (sweeper(1), sweeper(2), reaper, consumer)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert reloads[0] > 0 and judged[0] > 0
    assert wrong[0] == 0, (
        f"{wrong[0]} of {judged[0]} upsert vectors came from a model no longer resident "
        f"({reloads[0]} reloads)"
    )


def test_a_remembered_long_text_does_not_pin_its_own_bytes(live) -> None:
    """The hand-off holds a vector per text, never the text: chunking caps words,
    not characters, so one unbroken paragraph can be a megabyte."""
    import gc
    import tracemalloc

    _vault, _target, _encoder = live
    embeddings.clear_passage_vectors()
    stamp = embeddings.passage_memo_stamp()
    vector = np.ones((1, embeddings.VECTOR_DIM), dtype=np.float32)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for i in range(16):
            text = f"{i:02d}" + "y" * 1_000_000
            embeddings.remember_passage_vectors([text], vector, stamp=stamp)
            del text
        gc.collect()
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()

    assert held < 1_000_000, f"16 remembered 1 MB texts still hold {held / 2**20:.1f} MiB"
    probe = "07" + "y" * 1_000_000
    assert set(embeddings.recall_passage_vectors([probe], stamp=stamp)) == {probe}


def test_reused_counts_only_the_page_s_own_rows_on_both_spans(live) -> None:
    """`reused` is a page-row count on every span; the hand-off is `recalled`, disjoint."""
    _vault, _target, encoder = live
    stamp = embeddings.passage_memo_stamp()
    embeddings.remember_passage_vectors(["beta"], encoder(["beta"]), stamp=stamp)
    stored = {"alpha": encoder(["alpha"])[0]}
    handle = call_spans.MCP_CALL_TOKEN.set("u5d-reused-meaning")
    try:
        embeddings._embed_live_chunks_reusing(["alpha", "beta", "gamma"], stored)
        spans = {span["name"]: span for span in call_spans.pop_call_spans("u5d-reused-meaning")}
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)

    assert spans["index.embeddings.reuse"]["fields"] == {"texts": 3, "reused": 1, "recalled": 1}


# ---------------- the real invalidation paths ----------------
#
# Each fills the hand-off through a real sweep, fires one production event, and
# checks that the next upsert encodes every text itself and that the memory is
# released, not merely hidden.


def _fill_hand_off(vault: Path, log: list[tuple[str, str]]) -> list[str]:
    body = "Invalidation paragraph one.\n\nInvalidation paragraph two."
    corpus_aware._best_cosine_per_file(vault, title="Invalidation probe", body=body)
    chunks = embeddings.chunk_text("Invalidation probe", body)
    assert len(embeddings._PASSAGE_MEMO) == len(chunks), "the sweep filed nothing"
    log.clear()
    return chunks


def _upsert_encodes_everything(chunks: list[str], log: list[tuple[str, str]]) -> None:
    embeddings._embed_live_chunks_reusing(chunks, {})
    assert sorted(text for _name, text in log) == sorted(chunks)


def test_a_model_load_releases_the_hand_off(lifecycle, monkeypatch) -> None:
    """Even a reload of the same model: its vectors are equal, but nothing proves it."""
    vault, log = lifecycle
    embeddings.get_model()
    chunks = _fill_hand_off(vault, log)
    monkeypatch.setattr(embeddings, "_MODEL", None)  # a load that no unload preceded
    before_the_load = embeddings.passage_memo_stamp()

    embeddings.get_model()

    assert len(embeddings._PASSAGE_MEMO) == 0
    # A sweep that began before the load files nothing under the loaded model.
    embeddings.remember_passage_vectors(
        chunks, np.ones((len(chunks), embeddings.VECTOR_DIM), np.float32), stamp=before_the_load
    )
    assert len(embeddings._PASSAGE_MEMO) == 0
    _upsert_encodes_everything(chunks, log)


def test_clearing_the_shared_indexes_releases_the_hand_off(lifecycle) -> None:
    vault, log = lifecycle
    embeddings.get_model()
    chunks = _fill_hand_off(vault, log)

    embeddings.clear_embedding_indexes()

    assert len(embeddings._PASSAGE_MEMO) == 0
    _upsert_encodes_everything(chunks, log)


def test_the_reaper_s_cache_eviction_releases_the_hand_off(lifecycle) -> None:
    from exomem import model_reaper

    vault, log = lifecycle
    embeddings.get_model()
    chunks = _fill_hand_off(vault, log)
    embeddings.get_embedding_index(vault)  # the slot evicts only a shared index
    slot = next(s for s in model_reaper.default_slots() if s.name == "index-matrices")

    slot.unload()

    assert len(embeddings._PASSAGE_MEMO) == 0
    _upsert_encodes_everything(chunks, log)


def test_the_reaper_s_model_unload_releases_the_hand_off(lifecycle) -> None:
    from exomem import model_reaper

    vault, log = lifecycle
    embeddings.get_model()
    chunks = _fill_hand_off(vault, log)
    slot = next(s for s in model_reaper.default_slots() if s.name == "embeddings")

    assert slot.unload() is True

    assert len(embeddings._PASSAGE_MEMO) == 0
    _upsert_encodes_everything(chunks, log)


def test_a_vector_encoded_before_an_unload_is_not_served_to_the_next_load(
    lifecycle, monkeypatch
) -> None:
    """The reaper unloads between the sweep's encode and its filing, and the next
    load is another encoder under the SAME model name -- a shared remote encoder,
    changed weights -- so only the load generation tells the two apart."""
    vault, log = lifecycle
    loads = itertools.count(1)
    monkeypatch.setattr(
        embedding_backend,
        "load_encoder",
        lambda name, **_k: _TaggedModel(f"{name}#load{next(loads)}", log),
    )
    embeddings.get_model()
    real_record = call_spans.record_span
    unloaded: list[str] = []

    def unload_after_the_encode(name, elapsed_ms, fields=None):
        real_record(name, elapsed_ms, fields)
        if name == "embeddings.encode" and not unloaded:
            unloaded.append(name)
            assert embeddings.unload_model() is True

    monkeypatch.setattr(call_spans, "record_span", unload_after_the_encode)
    body = "Unload paragraph one.\n\nUnload paragraph two."
    corpus_aware._best_cosine_per_file(vault, title="Unload probe", body=body)
    monkeypatch.setattr(call_spans, "record_span", real_record)
    chunks = embeddings.chunk_text("Unload probe", body)
    assert unloaded and embeddings._MODEL is None

    vectors = embeddings._embed_live_chunks_reusing(chunks, {})

    assert vectors[:, 0].tolist() == [_model_tag("model-a#load2")] * len(chunks), (
        "the upsert took vectors the unloaded encoder produced"
    )
    assert embeddings._MODEL.name == "model-a#load2"


def test_the_stamp_names_the_resident_encoder_s_own_identity(lifecycle) -> None:
    """The vector space is the loaded encoder's own fingerprint, so a vector of
    another build of the same model -- other bytes under the same name -- is
    never handed on."""
    embeddings.get_model()
    profile = embedding_backend.EncoderProfile(
        model="model-a",
        pooling="cls",
        query_prefix="",
        passage_prefix="",
        max_seq=512,
        pad_token="<pad>",
        revision="a" * 40,
        quantization=embedding_backend.ORT_DYNAMIC_INT8,
        file_format=embedding_backend.ONNX_EXTERNAL_DATA,
        artifact_digest="0123456789abcdef",
    )
    embeddings._MODEL.profile = profile
    stamp = embeddings.passage_memo_stamp()
    embeddings.remember_passage_vectors(
        ["alpha"], np.ones((1, embeddings.VECTOR_DIM), np.float32), stamp=stamp
    )

    assert stamp.space == profile.fingerprint()
    assert set(embeddings.recall_passage_vectors(["alpha"], stamp=embeddings.passage_memo_stamp())) == {"alpha"}

    embeddings._MODEL.profile = dataclasses.replace(profile, artifact_digest="fedcba9876543210")

    assert embeddings.recall_passage_vectors(["alpha"], stamp=embeddings.passage_memo_stamp()) == {}
