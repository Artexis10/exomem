"""Idle-unload subsystem: per-model guards, unload setters, accel seams, and the reaper.

Torch-free — models are plain sentinels; the torch-touching seams (accel.empty_cache /
gpu_mem) are exercised with a fake torch module. Proves the concurrency-safety contract
(skip-if-inflight, skip-if-warming), the reload-after-unload path, and the reaper decision.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from exomem import accel, embeddings, model_reaper, readiness


@pytest.fixture(autouse=True)
def _reset() -> None:
    embeddings._MODEL = embeddings._RERANKER = embeddings._CLIP_MODEL = None
    for g in (embeddings.BGE_GUARD, embeddings.RERANKER_GUARD, embeddings.CLIP_GUARD):
        g._inflight = 0
        g._last_activity = 0.0
    readiness.reset()
    yield
    model_reaper.stop()
    model_reaper._thread = None
    embeddings._MODEL = embeddings._RERANKER = embeddings._CLIP_MODEL = None
    readiness.reset()


# ---- _ModelGuard ----

def test_guard_active_tracks_inflight_and_activity() -> None:
    g = embeddings.BGE_GUARD
    assert g.inflight() == 0
    with g.active():
        assert g.inflight() == 1
        stamped = g.last_activity()
        assert stamped > 0
    assert g.inflight() == 0
    assert g.last_activity() >= stamped  # stamped again on exit


def test_guard_active_decrements_on_exception() -> None:
    g = embeddings.CLIP_GUARD
    with pytest.raises(ValueError):
        with g.active():
            assert g.inflight() == 1
            raise ValueError("boom")
    assert g.inflight() == 0  # finally always decrements


# ---- unload setters ----

def test_unload_model_nulls_and_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(accel, "empty_cache", lambda: calls.append(1))
    embeddings._MODEL = object()
    assert embeddings.unload_model() is True
    assert embeddings._MODEL is None
    assert calls == [1]


def test_unload_noop_when_not_loaded() -> None:
    assert embeddings._MODEL is None
    assert embeddings.unload_model() is False


def test_unload_skips_when_inflight() -> None:
    embeddings._MODEL = object()
    embeddings.BGE_GUARD._inflight = 1
    assert embeddings.unload_model() is False
    assert embeddings._MODEL is not None  # kept — a worker is mid-encode


def test_unload_reranker_and_clip() -> None:
    embeddings._RERANKER = object()
    embeddings._CLIP_MODEL = object()
    assert embeddings.unload_reranker() is True
    assert embeddings._RERANKER is None
    assert embeddings.unload_clip_model() is True
    assert embeddings._CLIP_MODEL is None


def test_reload_after_unload_reconstructs(monkeypatch: pytest.MonkeyPatch) -> None:
    constructions: list[int] = []

    class _Fake:
        def __init__(self, *a, **k) -> None:
            constructions.append(1)

    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = lambda name, device=None: _Fake()
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setattr(embeddings, "_maybe_half", lambda m, d: m)
    monkeypatch.setattr(accel, "select_device", lambda **k: "cpu")

    m1 = embeddings.get_model()
    assert len(constructions) == 1
    assert embeddings.BGE_GUARD.last_activity() > 0  # touched on load
    embeddings.unload_model()
    m2 = embeddings.get_model()
    assert len(constructions) == 2  # reconstructed on the next use
    assert m2 is not m1


# ---- accel.empty_cache / gpu_mem seams ----

def test_empty_cache_no_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch → ImportError
    accel.empty_cache()  # must not raise


def test_empty_cache_only_when_context_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        is_initialized=lambda: False,
        empty_cache=lambda: calls.append(1),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    accel.empty_cache()
    assert calls == []  # no context yet → must NOT create one
    torch.cuda.is_initialized = lambda: True
    accel.empty_cache()
    assert calls == [1]


def test_gpu_mem_none_without_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, is_initialized=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert accel.gpu_mem() is None


def test_gpu_mem_reports_when_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        is_initialized=lambda: True,
        memory_allocated=lambda: 100 * 2**20,
        memory_reserved=lambda: 200 * 2**20,
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert accel.gpu_mem() == {"allocated_mb": 100.0, "reserved_mb": 200.0}


# ---- reaper decision (pure) ----

def _slot(*, loaded: bool = True, inflight: int = 0, last: float = 0.0):
    unloaded: list[int] = []
    slot = model_reaper.ModelSlot(
        name="m",
        is_loaded=lambda: loaded and not unloaded,
        inflight=lambda: inflight,
        last_activity=lambda: last,
        unload=lambda: (unloaded.append(1), True)[1],
    )
    return slot, unloaded


def test_should_unload_true_when_idle() -> None:
    slot, _ = _slot(last=0.0)
    assert model_reaper._should_unload(slot, now=1000.0, threshold=900.0) is True


def test_should_unload_false_before_threshold() -> None:
    slot, _ = _slot(last=500.0)
    assert model_reaper._should_unload(slot, now=1000.0, threshold=900.0) is False


def test_should_unload_false_when_inflight() -> None:
    slot, _ = _slot(inflight=1, last=0.0)
    assert model_reaper._should_unload(slot, now=1e9, threshold=900.0) is False


def test_should_unload_false_when_not_loaded() -> None:
    slot, _ = _slot(loaded=False)
    assert model_reaper._should_unload(slot, now=1e9, threshold=900.0) is False


def test_should_unload_false_when_warming() -> None:
    readiness.begin_warm()
    slot, _ = _slot(last=0.0)
    assert model_reaper._should_unload(slot, now=1e9, threshold=900.0) is False
    readiness.finish_warm()
    assert model_reaper._should_unload(slot, now=1e9, threshold=900.0) is True


def test_reap_once_unloads_only_stale() -> None:
    stale, s_un = _slot(inflight=0, last=0.0)      # 1000-0 = 1000 >= 900 → reap
    active, a_un = _slot(inflight=1, last=0.0)      # in-flight → skip
    fresh, f_un = _slot(inflight=0, last=1000.0)    # 1000-1000 = 0 < 900 → skip
    reaped = model_reaper._reap_once([stale, active, fresh], now=1000.0, threshold=900.0)
    assert s_un == [1]
    assert a_un == []
    assert f_un == []
    assert len(reaped) == 1


# ---- reaper lifecycle ----

def test_reaper_start_fires_then_stops() -> None:
    slot, un = _slot(inflight=0, last=0.0)  # always stale; is_loaded flips False after unload
    model_reaper.start(threshold=0.0, tick=0.01, slots=[slot])
    time.sleep(0.08)
    model_reaper.stop()
    time.sleep(0.05)
    assert un == [1]  # unloaded exactly once (is_loaded False afterwards)
    assert not model_reaper.is_running()


def test_idle_seconds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_IDLE_MINUTES", raising=False)
    assert model_reaper.idle_seconds() == model_reaper.DEFAULT_IDLE_SECONDS
    monkeypatch.setenv("EXOMEM_IDLE_MINUTES", "5")
    assert model_reaper.idle_seconds() == 300.0
    monkeypatch.setenv("EXOMEM_IDLE_MINUTES", "garbage")
    assert model_reaper.idle_seconds() == model_reaper.DEFAULT_IDLE_SECONDS
