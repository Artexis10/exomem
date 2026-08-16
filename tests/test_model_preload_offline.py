"""The two terms behind a ~35s first-write-after-restart: cold loads, and networked loads.

Both are pinned on seams, never on wall-clock. The offline group asserts on the
kwargs the loader is CALLED with (so "did it reach the network" is decidable
without a network), and the preload group asserts on the reaper's pure decision
function (so "would this model be dropped" is decidable without a clock).

Torch-free: `sentence_transformers` / `huggingface_hub` are injected as fakes, and
the models are plain sentinels.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from exomem import accel, embedding_backend, embeddings, mode, model_cache, model_reaper, warmup


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutral policy: no inherited mode, preload, release, or HF offline setting."""
    for var in (
        "EXOMEM_MODE",
        "EXOMEM_QUIET_MODE",
        "EXOMEM_PRELOAD_MODELS",
        "EXOMEM_RELEASE_GPU_WHEN_IDLE",
        "EXOMEM_MODEL_OFFLINE",
        "HF_HUB_OFFLINE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(Path(__file__).parent / "_no_such_config.json"))


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    embeddings._MODEL = embeddings._RERANKER = embeddings._CLIP_MODEL = None
    yield
    embeddings._MODEL = embeddings._RERANKER = embeddings._CLIP_MODEL = None


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty local HF hub cache; `_cache` populates a model into it."""
    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    return cache


def _cache(hub: Path, model_name: str) -> None:
    """Materialize a plausible snapshot for `model_name` in the fake hub cache."""
    snap = hub / model_cache.snapshot_dirname(model_name) / "snapshots" / "deadbeef"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")


class _Spy:
    """Records the kwargs of each load attempt; optionally fails the offline one."""

    def __init__(self, *, fail_offline: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail_offline = fail_offline

    def __call__(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        if self.fail_offline and kwargs.get("local_files_only"):
            raise OSError("snapshot incomplete")
        return "MODEL"

    @property
    def offline_flags(self) -> list[bool]:
        return [bool(c.get("local_files_only")) for c in self.calls]


def _fake_sentence_transformers(spy: _Spy) -> types.ModuleType:
    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = spy
    mod.CrossEncoder = spy
    return mod


# ---------------------------------------------- offline-first weight resolution


def test_snapshot_dirname_matches_the_hub_layout() -> None:
    assert model_cache.snapshot_dirname("BAAI/bge-base-en-v1.5") == "models--BAAI--bge-base-en-v1.5"
    # sentence-transformers resolves a bare name under its own org.
    assert (
        model_cache.snapshot_dirname("clip-ViT-B-32")
        == "models--sentence-transformers--clip-ViT-B-32"
    )


def test_is_cached_is_a_pure_directory_check(hub: Path) -> None:
    assert model_cache.is_cached("BAAI/bge-base-en-v1.5") is False
    _cache(hub, "BAAI/bge-base-en-v1.5")
    assert model_cache.is_cached("BAAI/bge-base-en-v1.5") is True


def test_cached_weights_load_without_touching_the_network(hub: Path) -> None:
    _cache(hub, "BAAI/bge-base-en-v1.5")
    spy = _Spy()

    assert model_cache.load_offline_first("BAAI/bge-base-en-v1.5", spy) == "MODEL"

    assert spy.offline_flags == [True], "a cache-resident model must not revalidate over HTTP"


def test_missing_weights_still_download(hub: Path) -> None:
    spy = _Spy()

    assert model_cache.load_offline_first("BAAI/bge-base-en-v1.5", spy) == "MODEL"

    # Nothing in the cache: go straight to the ordinary networked load. A
    # first-run user with an empty cache must still be able to fetch weights.
    assert spy.offline_flags == [False]
    assert spy.calls == [{}]


def test_a_broken_snapshot_falls_back_to_the_network(hub: Path) -> None:
    _cache(hub, "BAAI/bge-base-en-v1.5")
    spy = _Spy(fail_offline=True)

    assert model_cache.load_offline_first("BAAI/bge-base-en-v1.5", spy) == "MODEL"

    # Directory present but unusable (partial snapshot, or a runtime that does
    # not accept the kwarg at all) -> retry with network access, kwarg omitted.
    assert spy.offline_flags == [True, False]
    assert spy.calls[1] == {}


def test_offline_load_can_be_switched_off(hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(hub, "BAAI/bge-base-en-v1.5")
    monkeypatch.setenv("EXOMEM_MODEL_OFFLINE", "0")
    spy = _Spy()

    model_cache.load_offline_first("BAAI/bge-base-en-v1.5", spy)

    assert spy.offline_flags == [False]


def test_hf_hub_offline_forces_offline_even_with_a_cold_cache(
    hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    spy = _Spy()

    model_cache.load_offline_first("BAAI/bge-base-en-v1.5", spy)

    assert spy.offline_flags[0] is True


def test_torch_encoder_loads_cached_weights_offline(
    hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache(hub, embeddings.MODEL_NAME)
    spy = _Spy()
    monkeypatch.setitem(sys.modules, "sentence_transformers", _fake_sentence_transformers(spy))
    monkeypatch.setattr(accel, "select_device", lambda **_: "cpu")

    embedding_backend.load_encoder(embeddings.MODEL_NAME, backend=embedding_backend.TORCH)

    assert spy.offline_flags == [True], "the write path must not HEAD the hub for cached weights"


def test_onnx_file_resolution_prefers_the_local_snapshot(
    hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache(hub, embeddings.MODEL_NAME)
    spy = _Spy()
    fake = types.ModuleType("huggingface_hub")
    fake.hf_hub_download = spy
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    embedding_backend._resolve(embeddings.MODEL_NAME, "onnx/model.onnx")

    assert spy.offline_flags == [True]


def test_reranker_loads_cached_weights_offline(
    hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cache(hub, embeddings.RERANKER_NAME)
    spy = _Spy()
    monkeypatch.setitem(sys.modules, "sentence_transformers", _fake_sentence_transformers(spy))
    monkeypatch.setattr(accel, "select_device", lambda **_: "cpu")

    embeddings.get_reranker()

    assert spy.offline_flags == [True]


def test_clip_loads_cached_weights_offline(hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(hub, embeddings.CLIP_MODEL_NAME)
    spy = _Spy()
    monkeypatch.setitem(sys.modules, "sentence_transformers", _fake_sentence_transformers(spy))
    monkeypatch.setattr(accel, "select_device", lambda **_: "cpu")

    embeddings.get_clip_model()

    assert spy.offline_flags == [True]


# ------------------------------------------------- preload vs the idle reaper


def test_preload_policy_reports_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`resolved()` must report what warm-up will actually do, override included."""
    assert mode.preload_models() is False
    assert mode.resolved()["preload_models"] is False

    monkeypatch.setenv("EXOMEM_PRELOAD_MODELS", "1")

    assert mode.preload_models() is True
    assert mode.resolved()["preload_models"] is True
    assert warmup.model_preload_allowed(mode.resolve_mode()) is True


def test_preload_pins_the_models_it_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reaping a preloaded model hands the reload to the next request — the bug."""
    monkeypatch.setenv("EXOMEM_MODE", "performance")

    assert mode.preload_models() is True
    assert mode.reap_models_when_idle() is False
    assert mode.resolved()["reap_models_when_idle"] is False


def test_lazy_modes_still_reap_idle_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")

    assert mode.preload_models() is False
    assert mode.reap_models_when_idle() is True


def test_explicit_release_opt_in_beats_preload(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who asked for the VRAM back gets it, and pays the reload."""
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    monkeypatch.setenv("EXOMEM_RELEASE_GPU_WHEN_IDLE", "1")

    assert mode.reap_models_when_idle() is True


def test_release_off_disables_model_reaping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_RELEASE_GPU_WHEN_IDLE", "0")

    assert mode.reap_models_when_idle() is False


def _slot(name: str, *, is_model: bool) -> model_reaper.ResourceSlot:
    state = {"loaded": True}
    return model_reaper.ResourceSlot(
        name,
        lambda: state["loaded"],
        lambda: 0,
        lambda: 0.0,
        lambda: state.update(loaded=False) or True,
        is_model=is_model,
    )


def test_reaper_skips_model_slots_under_preload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    model = _slot("embeddings", is_model=True)
    cache = _slot("bm25-cache", is_model=False)

    assert model_reaper._should_unload(model, now=1e6, threshold=1.0) is False
    # Caches are cheap to rebuild and were never promised to be preloaded.
    assert model_reaper._should_unload(cache, now=1e6, threshold=1.0) is True


def test_reaper_unloads_model_slots_without_preload() -> None:
    model = _slot("embeddings", is_model=True)

    assert model_reaper._should_unload(model, now=1e6, threshold=1.0) is True


def test_default_slots_label_the_model_singletons() -> None:
    by_name = {s.name: s for s in model_reaper.default_slots()}

    assert [n for n, s in by_name.items() if s.is_model] == ["embeddings", "reranker", "clip"]


def test_preloaded_model_survives_a_reap_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end over the real slots: preload on, and bge is still resident."""
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    monkeypatch.setattr(accel, "empty_cache", lambda: None)
    embeddings._MODEL = object()

    reaped = model_reaper._reap_once(model_reaper.default_slots(), now=1e6, threshold=1.0)

    assert "embeddings" not in reaped
    assert embeddings._MODEL is not None


# ------------------------------------------------------------------- reporting


def test_status_models_exposes_the_preload_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """`status` must show the policy next to residency, so the gap needs no log."""
    from exomem import resource_status

    monkeypatch.setenv("EXOMEM_MODE", "performance")

    models = resource_status.collect()["models"]

    assert models["preload_policy"] is True
    assert models["reap_when_idle"] is False


def test_doctor_reports_model_residency(monkeypatch: pytest.MonkeyPatch, hub: Path) -> None:
    from exomem import doctor

    _cache(hub, embeddings.MODEL_NAME)
    check = doctor._check_model_residency()

    assert check.id == "models.residency"
    assert check.details["cache_dir"] == str(hub)
    assert check.details["bi_encoder_cached"] is True
    assert check.details["offline_load"] is True
    assert check.details["loaded"] is False
    assert check.details["preload_policy"] is False
    assert "not loaded" in check.message


def test_doctor_flags_an_uncached_model_as_an_inline_download(hub: Path) -> None:
    from exomem import doctor

    check = doctor._check_model_residency()

    assert check.details["bi_encoder_cached"] is False
    assert check.details["offline_load"] is False
    assert "downloads it inline" in check.message
