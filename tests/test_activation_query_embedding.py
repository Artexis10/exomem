"""Resident-only query embeddings never make activation wait or load a model."""

from __future__ import annotations

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


def test_embed_query_if_loaded_uses_resident_query_encoding_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    _install_gate(monkeypatch)
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
