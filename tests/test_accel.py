"""Device-selection matrix for `exomem.accel` — CUDA > MPS > CPU with per-model policy.

Hardware-independent: `torch.cuda.is_available` and `accel._mps_available` are
monkeypatched so every branch (CUDA / MPS / CPU / overrides / ASR-avoidance) is
exercised deterministically on any host, including CI boxes with no GPU.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from exomem import accel

_ENV_KEYS = (
    "EXOMEM_DEVICE",
    "EXOMEM_TORCH_DEVICE",
    "EXOMEM_EMBED_DEVICE",
    "EXOMEM_CLIP_DEVICE",
    "EXOMEM_VOICE_DEVICE",
    "EXOMEM_MODE",
    "EXOMEM_QUIET_MODE",
    "EXOMEM_GPU_MIN_FREE_GB",
    "EXOMEM_DISABLE_MEDIA_EXTRACTION",
    "PYTORCH_ENABLE_MPS_FALLBACK",
    "EXOMEM_ASR_BACKEND",
    "EXOMEM_MLX_WHISPER_MODEL",
    "EXOMEM_MPS_FP16",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from ambient device/env config. Mode resolves to `normal`
    by default (conftest points EXOMEM_CONFIG_PATH at an empty tmp path)."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_hw(monkeypatch: pytest.MonkeyPatch, *, cuda: bool, mps: bool, free_gb: float = 8.0) -> None:
    """Advertise a fake `torch` with the given accelerators — no real torch needed, so the
    matrix runs on lean CI too (matching the suite's torch-optional design). `free_gb` feeds
    the `gpu_usable()` marginal-VRAM probe via a fake `torch.cuda.mem_get_info`."""
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: cuda,
        mem_get_info=lambda *a, **k: (int(free_gb * 1024**3), int(16 * 1024**3)),
    )
    torch.backends = types.SimpleNamespace()  # MPS is probed via accel._mps_available (patched)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(accel, "_mps_available", lambda _torch: mps)


# ---- steady-state default policy: CPU-first, CUDA never auto-selected ----

def test_normal_mode_stays_cpu_even_with_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core VRAM-kill: the default (normal) mode never grabs CUDA at idle."""
    _set_hw(monkeypatch, cuda=True, mps=False)
    assert accel.select_device() == "cpu"


def test_normal_mode_prefers_mps_on_apple_silicon(monkeypatch: pytest.MonkeyPatch) -> None:
    """MPS is unified memory (no discrete idle-VRAM cost), so normal mode keeps it."""
    _set_hw(monkeypatch, cuda=False, mps=True)
    assert accel.select_device() == "mps"
    assert os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1"


def test_cpu_when_no_accelerator(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=False)
    assert accel.select_device() == "cpu"


def test_no_torch_is_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    # `import torch` with None in sys.modules raises ImportError → CPU.
    monkeypatch.setitem(sys.modules, "torch", None)
    assert accel.select_device() == "cpu"


def test_quiet_mode_forces_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=True)  # even with both accelerators present
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    assert accel.select_device() == "cpu"


# ---- performance mode: the discoverable GPU opt-in, capability-gated ----

def test_performance_mode_selects_cuda_when_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=8.0)
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    assert accel.select_device() == "cuda"


def test_performance_mode_degrades_to_cpu_on_marginal_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    """Marginal-VRAM guard: a GPU busy with a game (little free VRAM) → CPU, not OOM."""
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=0.5)
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    assert accel.select_device() == "cpu"


def test_performance_mode_falls_back_to_mps_without_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=True)
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    assert accel.select_device() == "mps"


def test_mode_alias_gpu_maps_to_performance(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=8.0)
    monkeypatch.setenv("EXOMEM_MODE", "gpu")  # alias for performance
    assert accel.select_device() == "cuda"


# ---- auto_mps=False (ECAPA voiceprint parity default) ----

def test_auto_mps_false_skips_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=True)
    assert accel.select_device(auto_mps=False) == "cpu"


def test_auto_mps_false_still_honors_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=True)
    monkeypatch.setenv("EXOMEM_VOICE_DEVICE", "mps")
    assert accel.select_device(override_env="EXOMEM_VOICE_DEVICE", auto_mps=False) == "mps"


# ---- explicit device values: verbatim, with gpu/auto sentinels ----

def test_per_model_override_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_CLIP_DEVICE", "cpu")
    assert accel.select_device(override_env="EXOMEM_CLIP_DEVICE") == "cpu"


def test_global_override_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_DEVICE", "cpu")
    assert accel.select_device() == "cpu"


def test_legacy_torch_device_alias_forces_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat door: an explicit EXOMEM_TORCH_DEVICE=cuda keeps GPU verbatim,
    unconditionally (no headroom probe) — existing GPU users are undisturbed."""
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=0.1)  # verbatim ignores headroom
    monkeypatch.setenv("EXOMEM_TORCH_DEVICE", "cuda")
    assert accel.select_device() == "cuda"


def test_device_gpu_sentinel_is_probe_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`EXOMEM_DEVICE=gpu` opts in politely: uses CUDA only if capable, else degrades."""
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=8.0)
    monkeypatch.setenv("EXOMEM_DEVICE", "gpu")
    assert accel.select_device() == "cuda"
    monkeypatch.setenv("EXOMEM_GPU_MIN_FREE_GB", "16")  # now marginal
    assert accel.select_device() == "cpu"


def test_per_model_override_beats_global(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=False)
    monkeypatch.setenv("EXOMEM_DEVICE", "cpu")
    monkeypatch.setenv("EXOMEM_CLIP_DEVICE", "cuda")
    assert accel.select_device(override_env="EXOMEM_CLIP_DEVICE") == "cuda"


def test_explicit_mps_override_arms_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_DEVICE", "mps")
    assert accel.select_device() == "mps"
    assert os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1"


# ---- gpu_usable(): the capability / marginal-VRAM probe ----

def test_gpu_usable_true_with_ample_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=8.0)
    assert accel.gpu_usable() is True


def test_gpu_usable_false_without_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=False)
    assert accel.gpu_usable() is False


def test_gpu_usable_false_on_marginal_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=1.0)  # < 2 GB default
    assert accel.gpu_usable() is False


def test_gpu_usable_threshold_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=1.5)
    monkeypatch.setenv("EXOMEM_GPU_MIN_FREE_GB", "1")
    assert accel.gpu_usable() is True
    monkeypatch.setenv("EXOMEM_GPU_MIN_FREE_GB", "3")
    assert accel.gpu_usable() is False


def test_gpu_usable_no_raise_on_probe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = types.ModuleType("torch")

    def _boom(*a, **k):
        raise RuntimeError("driver mismatch")

    torch.cuda = types.SimpleNamespace(is_available=lambda: True, mem_get_info=_boom)
    torch.backends = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert accel.gpu_usable() is False


def test_gpu_usable_no_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)
    assert accel.gpu_usable() is False


# ---- avoid_cuda_when_asr: the cuDNN-shadow workaround is CUDA-only ----

def test_avoid_cuda_when_asr_active_falls_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_MODE", "performance")  # so CUDA is even considered
    # ASR active = EXOMEM_DISABLE_MEDIA_EXTRACTION unset (cleared by the fixture).
    assert accel.select_device(avoid_cuda_when_asr=True) == "cpu"


def test_avoid_cuda_when_asr_disabled_keeps_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    assert accel.select_device(avoid_cuda_when_asr=True) == "cuda"


def test_avoid_cuda_when_asr_does_not_penalize_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key Apple-Silicon win: CLIP/ECAPA keep MPS even with ASR active, because the
    cuDNN clash only exists on CUDA."""
    _set_hw(monkeypatch, cuda=False, mps=True)  # a Mac: MPS, no CUDA
    assert accel.select_device(avoid_cuda_when_asr=True) == "mps"


# ---- pipeline_device (HF transformers device form) ----

def test_pipeline_device_cpu_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)  # normal mode → CPU → -1
    assert accel.pipeline_device() == -1


def test_pipeline_device_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=True)
    assert accel.pipeline_device() == "mps"


def test_pipeline_device_cuda_in_performance_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False, free_gb=8.0)
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    assert accel.pipeline_device() == 0


def test_pipeline_device_no_accel(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=False)
    assert accel.pipeline_device() == -1


# ---- ASR transcription backend seam (Phase 2) ----

def test_get_transcriber_is_faster_whisper_and_keeps_engine_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam ships one backend today; it must preserve the `faster-whisper:<model>`
    provenance label that sidecars and tests depend on."""
    from exomem import extract

    class _Seg:
        text, start, end = "hi", 0.0, 1.0

    class _FakeWhisper:
        def transcribe(self, _path):  # faster-whisper returns (segments, info)
            return [_Seg()], object()

    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper())
    backend = extract.get_transcriber()
    assert isinstance(backend, extract.FasterWhisperBackend)
    from pathlib import Path

    segments, engine = backend.transcribe(Path("clip.wav"))
    assert engine == f"faster-whisper:{extract.WHISPER_MODEL}"
    assert [s.text for s in segments] == ["hi"]


# ---- MLX (Apple Silicon Metal) ASR backend ----

def _fake_mlx(monkeypatch: pytest.MonkeyPatch, segments: list[dict], capture: dict | None = None):
    """Inject a fake `mlx_whisper` module so the backend runs off Apple Silicon."""
    mlx = types.ModuleType("mlx_whisper")

    def transcribe(audio, path_or_hf_repo=None):
        if capture is not None:
            capture["audio"] = audio
            capture["repo"] = path_or_hf_repo
        return {"segments": segments, "text": "", "language": "en"}

    mlx.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", mlx)


def _fake_fw_decode(monkeypatch: pytest.MonkeyPatch, array) -> None:
    """Inject a fake `faster_whisper.audio.decode_audio` returning a canned array."""
    fw = types.ModuleType("faster_whisper")
    fw_audio = types.ModuleType("faster_whisper.audio")
    fw_audio.decode_audio = lambda _p, sampling_rate=16000: array
    fw.audio = fw_audio
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", fw_audio)


def test_mlx_backend_normalizes_segments_and_engine_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """MLX yields segment *dicts*; the backend adapts them to the .text/.start/.end objects
    the extractor consumes, and stamps an `mlx-whisper:<model>` provenance label."""
    from pathlib import Path

    from exomem import extract

    capture: dict = {}
    _fake_fw_decode(monkeypatch, [0.0, 0.0, 0.0])
    _fake_mlx(
        monkeypatch,
        [{"start": 0.0, "end": 1.5, "text": " hello"}, {"start": 1.5, "end": 2.0, "text": " world"}],
        capture,
    )
    segments, engine = extract.MlxWhisperBackend().transcribe(Path("clip.wav"))
    assert engine == f"mlx-whisper:{extract.MLX_WHISPER_MODEL}"
    assert [(round(s.start, 2), round(s.end, 2), s.text.strip()) for s in segments] == [
        (0.0, 1.5, "hello"),
        (1.5, 2.0, "world"),
    ]
    # PyAV-decoded array is passed to MLX (not the raw path) when faster-whisper is present.
    assert capture["audio"] == [0.0, 0.0, 0.0]
    assert capture["repo"] == extract.MLX_WHISPER_MODEL


def test_mlx_load_audio_falls_back_to_path_without_faster_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from exomem import extract

    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", None)
    assert extract.MlxWhisperBackend._load_audio(Path("a.wav")) == str(Path("a.wav"))


def test_mlx_backend_missing_dep_raises_extraction_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from exomem import extract

    monkeypatch.setitem(sys.modules, "mlx_whisper", None)  # import mlx_whisper → ImportError
    with pytest.raises(extract.ExtractionUnavailable):
        extract.MlxWhisperBackend().transcribe(Path("x.wav"))


def test_get_transcriber_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import extract

    # Explicit override wins over auto-detection.
    monkeypatch.setenv("EXOMEM_ASR_BACKEND", "mlx")
    assert isinstance(extract.get_transcriber(), extract.MlxWhisperBackend)
    monkeypatch.setenv("EXOMEM_ASR_BACKEND", "faster-whisper")
    assert isinstance(extract.get_transcriber(), extract.FasterWhisperBackend)

    # Auto: MLX on Apple Silicon (mlx-whisper importable), else faster-whisper.
    monkeypatch.delenv("EXOMEM_ASR_BACKEND", raising=False)
    monkeypatch.setattr(extract, "_mlx_available", lambda: True)
    assert isinstance(extract.get_transcriber(), extract.MlxWhisperBackend)
    monkeypatch.setattr(extract, "_mlx_available", lambda: False)
    assert isinstance(extract.get_transcriber(), extract.FasterWhisperBackend)


# ---- fp16 on MPS (embeddings._maybe_half) ----

def test_maybe_half_only_on_mps_and_respects_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """bge/CLIP run fp16 on MPS only (CPU fp16 is emulated; CUDA keeps fp32 for parity),
    and EXOMEM_MPS_FP16=0 opts out."""
    from exomem import embeddings

    class _Model:
        def __init__(self):
            self.halved = False

        def half(self):
            self.halved = True
            return self

    monkeypatch.delenv("EXOMEM_MPS_FP16", raising=False)
    assert embeddings._maybe_half(_Model(), "mps").halved is True
    assert embeddings._maybe_half(_Model(), "cpu").halved is False
    assert embeddings._maybe_half(_Model(), "cuda").halved is False

    monkeypatch.setenv("EXOMEM_MPS_FP16", "0")
    assert embeddings._maybe_half(_Model(), "mps").halved is False


def test_maybe_half_swallows_conversion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing .half() must never break model load — return the original model, fp32."""
    from exomem import embeddings

    class _Boom:
        def half(self):
            raise RuntimeError("mps fp16 unsupported for this op")

    monkeypatch.delenv("EXOMEM_MPS_FP16", raising=False)
    m = _Boom()
    assert embeddings._maybe_half(m, "mps") is m  # original returned, no raise
