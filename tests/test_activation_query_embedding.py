"""Resident-only query embeddings never make activation wait or load a model."""

from __future__ import annotations

import contextlib
import threading
import time

import numpy as np
import pytest

from exomem import embedding_backend, embeddings, runtime_resources


def _install_gate(monkeypatch: pytest.MonkeyPatch) -> runtime_resources.ModelAdmissionGate:
    gate = runtime_resources.ModelAdmissionGate(4)
    monkeypatch.setattr(runtime_resources, "_gate", gate)
    monkeypatch.setattr(runtime_resources, "_gate_capacity", 4)
    return gate


def test_embed_query_if_loaded_returns_none_cold_without_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(
        embedding_backend,
        "load_encoder",
        lambda _name: pytest.fail("resident query embedding must not load a model"),
    )
    monkeypatch.setattr(
        embeddings, "get_model", lambda: pytest.fail("resident query embedding must not get a model")
    )

    assert embeddings.embed_query_if_loaded("activation") is None


def test_embed_query_if_loaded_honors_embedding_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(
        embeddings, "_MODEL", object()
    )

    assert embeddings.embed_query_if_loaded("activation") is None


def test_activation_query_honors_embedding_kill_switch_in_both_topologies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "_MODEL", object())
    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", object())

    monkeypatch.delenv(embeddings.ACTIVATION_MODEL_ENV, raising=False)
    assert embeddings.embed_activation_query_if_loaded("activation") is None
    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, "intfloat/multilingual-e5-small")
    assert embeddings.embed_activation_query_if_loaded("activation") is None


def test_embed_query_if_loaded_uses_resident_query_encoding_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    _install_gate(monkeypatch)
    # This test describes the English recall model; a personal server now
    # encodes recall with the multilingual one (tests/test_recall_switch.py).
    monkeypatch.setattr(embeddings, "MODEL_NAME", "BAAI/bge-base-en-v1.5")
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Model:
        device = "cpu"

        def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
            calls.append((texts, kwargs))
            return np.asarray([[1.0, 2.0, 3.0]], dtype=np.float64)

    monkeypatch.setattr(embeddings, "_MODEL", Model())

    actual = embeddings.embed_query_if_loaded("activation readiness")

    assert actual is not None
    assert actual.shape == (3,)
    assert actual.dtype == np.float32
    assert calls == [
        (
            [embeddings.QUERY_PREFIX + "activation readiness"],
            {
                "batch_size": 8,
                "convert_to_numpy": True,
                "normalize_embeddings": True,
                "show_progress_bar": False,
            },
        )
    ]


def test_embed_query_if_loaded_is_reentrant_for_admitted_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    gate = _install_gate(monkeypatch)

    class Model:
        device = "cpu"

        def encode(self, _texts: list[str], **_kwargs: object) -> np.ndarray:
            assert gate.admitted_count() == 1
            return np.asarray([[1.0]], dtype=np.float32)

    monkeypatch.setattr(embeddings, "_MODEL", Model())

    with runtime_resources.model_execution():
        result = embeddings.embed_query_if_loaded("activation")
        assert gate.admitted_count() == 1

    assert result is not None
    assert gate.admitted_count() == 0


def test_embed_query_if_loaded_refuses_busy_execution_without_leaking_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    gate = _install_gate(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    class Model:
        device = "cpu"

        def encode(self, _texts: list[str], **_kwargs: object) -> np.ndarray:
            return np.asarray([[1.0]], dtype=np.float32)

    monkeypatch.setattr(embeddings, "_MODEL", Model())

    def hold_execution() -> None:
        with runtime_resources.model_execution():
            entered.set()
            assert release.wait(timeout=1)

    worker = threading.Thread(target=hold_execution)
    worker.start()
    try:
        assert entered.wait(timeout=1)
        started = time.monotonic()
        with pytest.raises(runtime_resources.ModelBusyError):
            embeddings.embed_query_if_loaded("activation")
        assert time.monotonic() - started < 0.25
        assert gate.admitted_count() == 1
    finally:
        release.set()
        worker.join(timeout=1)
    assert not worker.is_alive()
    assert gate.admitted_count() == 0


def test_embed_query_if_loaded_refuses_busy_model_singleton_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    entered = threading.Event()
    release = threading.Event()
    encoded = False

    class Model:
        device = "cpu"

        def encode(self, _texts: list[str], **_kwargs: object) -> np.ndarray:
            nonlocal encoded
            encoded = True
            return np.asarray([[1.0]], dtype=np.float32)

    monkeypatch.setattr(embeddings, "_MODEL", Model())

    def hold_model_lock() -> None:
        with embeddings._MODEL_LOCK:
            entered.set()
            release.wait(timeout=5)

    worker = threading.Thread(target=hold_model_lock)
    worker.start()
    try:
        assert entered.wait(timeout=1)
        started = time.monotonic()
        with pytest.raises(runtime_resources.ModelBusyError):
            embeddings.embed_query_if_loaded("activation")
        assert time.monotonic() - started < 0.25
        assert encoded is False
    finally:
        release.set()
        worker.join(timeout=1)
    assert not worker.is_alive()


def test_embed_query_if_loaded_pins_resident_model_before_unload_can_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    captured = threading.Event()
    allow_guard = threading.Event()
    encode_started = threading.Event()
    finish_encode = threading.Event()
    released = threading.Event()

    class Model:
        device = "cpu"

        def encode(self, _texts: list[str], **_kwargs: object) -> np.ndarray:
            encode_started.set()
            assert finish_encode.wait(timeout=1)
            return np.asarray([[1.0]], dtype=np.float32)

        def release(self) -> None:
            released.set()

    class CoordinatedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._first_release = True

        def acquire(self, *args, **kwargs) -> bool:
            return self._lock.acquire(*args, **kwargs)

        def release(self) -> None:
            self._lock.release()
            if self._first_release:
                self._first_release = False
                captured.set()
                assert allow_guard.wait(timeout=1)

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, *_args) -> None:
            self.release()

    monkeypatch.setattr(embeddings, "_MODEL", Model())
    monkeypatch.setattr(embeddings, "_MODEL_LOCK", CoordinatedLock())
    outcome: list[np.ndarray | None] = []

    worker = threading.Thread(
        target=lambda: outcome.append(embeddings.embed_query_if_loaded("activation"))
    )
    worker.start()
    try:
        assert captured.wait(timeout=1)
        assert embeddings.unload_model() is False
        assert released.is_set() is False
        allow_guard.set()
        assert encode_started.wait(timeout=1)
        assert embeddings.unload_model() is False
        assert released.is_set() is False
        finish_encode.set()
    finally:
        allow_guard.set()
        finish_encode.set()
        worker.join(timeout=1)
    assert not worker.is_alive()
    assert outcome[0] is not None
    assert embeddings.BGE_GUARD.inflight() == 0
    assert embeddings.unload_model() is True
    assert released.is_set()


def test_embed_query_if_loaded_releases_guard_and_admission_after_encode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    gate = _install_gate(monkeypatch)

    class Model:
        device = "cpu"

        def encode(self, _texts: list[str], **_kwargs: object) -> np.ndarray:
            raise ValueError("encoder failed")

    monkeypatch.setattr(embeddings, "_MODEL", Model())

    with pytest.raises(ValueError, match="encoder failed"):
        embeddings.embed_query_if_loaded("activation")

    assert embeddings.BGE_GUARD.inflight() == 0
    assert gate.admitted_count() == 0


# --------------------------------------------------------------------------- #
# One model, two lanes: interactive query encodes never wait for a bulk batch
# --------------------------------------------------------------------------- #


class _ConcurrentModel:
    """An encoder that may run concurrently, as an ONNX Runtime session does."""

    device = "cpu"
    concurrent_encodes = True

    def __init__(self, park_on: str) -> None:
        self.park_on = park_on
        self.parked = threading.Event()
        self.resume = threading.Event()
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        self.calls.append((list(texts), kwargs))
        if any(self.park_on in text for text in texts):
            self.parked.set()
            assert self.resume.wait(timeout=2)
        return np.ones((len(texts), 3), dtype=np.float32)


@pytest.mark.parametrize("topology", ["shared", "separate"])
def test_an_interactive_query_is_served_while_a_bulk_batch_runs(
    monkeypatch: pytest.MonkeyPatch, topology: str
) -> None:
    """A bulk batch holds its execution slot for seconds on a real model; a query
    encode on a model that can run concurrently takes a lane of its own."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    gate = _install_gate(monkeypatch)
    monkeypatch.setattr(embeddings, "_INTERACTIVE_GATE", None)
    monkeypatch.setattr(embeddings, "_ACTIVATION_GATE", None)
    model = _ConcurrentModel(park_on="bulk one")
    if topology == "shared":
        monkeypatch.delenv(embeddings.ACTIVATION_MODEL_ENV, raising=False)
        monkeypatch.setattr(embeddings, "_MODEL", model)
        bulk = lambda: embeddings.embed_texts(["bulk one", "bulk two"])  # noqa: E731
    else:
        monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, "intfloat/multilingual-e5-small")
        monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", model)
        bulk = lambda: embeddings.embed_activation_passages(["bulk one", "bulk two"])  # noqa: E731
    worker = threading.Thread(target=bulk)
    worker.start()
    try:
        assert model.parked.wait(timeout=1)
        started = time.monotonic()
        assert embeddings.embed_activation_query_if_loaded("continue") is not None
        assert time.monotonic() - started < 0.25
        if topology == "shared":
            assert embeddings.embed_query_if_loaded("recall query") is not None
            assert gate.admitted_count() == 1, "the interactive lane is not the bulk lane's admission"
    finally:
        model.resume.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert gate.admitted_count() == 0


def test_a_bulk_encode_releases_the_model_gate_between_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """One admission spans the whole encode; the execution slot is taken per
    batch, so nothing waits behind the whole of it. Batches go longest first, as
    sentence-transformers forms them, and rows come back in input order."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    gate = _install_gate(monkeypatch)
    monkeypatch.setattr(embedding_backend, "batch_size_for", lambda _device: 2)
    log: list[object] = []

    class Model:
        device = "cpu"

        def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
            log.append(("encode", list(texts), kwargs["batch_size"], gate.admitted_count()))
            return np.asarray([[float(len(text)), 0.0] for text in texts], dtype=np.float32)

    model = Model()
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", lambda: model)
    real_execution = runtime_resources.model_execution

    @contextlib.contextmanager
    def logged_execution(*, wait: bool = True):
        with real_execution(wait=wait):
            log.append("enter")
            yield
        log.append(("exit", gate.admitted_count()))

    monkeypatch.setattr(runtime_resources, "model_execution", logged_execution)

    vectors = embeddings.embed_texts(["a", "bbb", "cc"])

    assert log == [
        "enter",
        ("encode", ["bbb", "cc"], 2, 1),
        ("exit", 1),
        "enter",
        ("encode", ["a"], 2, 1),
        ("exit", 1),
    ]
    assert vectors[:, 0].tolist() == [1.0, 3.0, 2.0]
    assert gate.admitted_count() == 0


def test_the_activation_turn_is_read_at_40_tokens_in_both_topologies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    _install_gate(monkeypatch)
    monkeypatch.setattr(embeddings, "_ACTIVATION_GATE", None)
    caps: list[object] = []

    class Model:
        device = "cpu"

        def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
            caps.append(kwargs.get("max_tokens"))
            return np.ones((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(embeddings, "_MODEL", Model())
    monkeypatch.delenv(embeddings.ACTIVATION_MODEL_ENV, raising=False)
    embeddings.embed_activation_query_if_loaded("continue")
    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, "intfloat/multilingual-e5-small")
    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", Model())
    embeddings.embed_activation_query_if_loaded("continue")
    embeddings.embed_query_if_loaded("a recall query")

    assert embeddings.ACTIVATION_TURN_MAX_TOKENS == 40
    assert caps == [40, 40, None], "only the activation turn is capped; a recall query is not"


def test_query_and_passage_prefixes_come_from_the_resident_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recall's own encodes read their prefixes off the loaded model's profile, so
    a model trained without bge's query instruction is never given it."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    _install_gate(monkeypatch)
    seen: list[list[str]] = []

    class Model:
        device = "cpu"
        profile = embedding_backend.EncoderProfile(
            model="BAAI/bge-m3", pooling="cls", query_prefix="", passage_prefix="", max_seq=512, pad_token="<pad>"
        )

        def encode(self, texts: list[str], **_kwargs: object) -> np.ndarray:
            seen.append(list(texts))
            return np.ones((len(texts), 3), dtype=np.float32)

    model = Model()
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", lambda: model)

    embeddings.embed_query_if_loaded("wie war das")
    embeddings.embed_texts(["a question"], is_query=True)
    embeddings.embed_texts(["a passage"])

    assert seen == [["wie war das"], ["a question"], ["a passage"]]
