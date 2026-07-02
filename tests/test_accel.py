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
    "EXOMEM_TORCH_DEVICE",
    "EXOMEM_CLIP_DEVICE",
    "EXOMEM_VOICE_DEVICE",
    "EXOMEM_DISABLE_MEDIA_EXTRACTION",
    "PYTORCH_ENABLE_MPS_FALLBACK",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from ambient device/env config."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_hw(monkeypatch: pytest.MonkeyPatch, *, cuda: bool, mps: bool) -> None:
    """Advertise a fake `torch` with the given accelerators — no real torch needed, so the
    matrix runs on lean CI too (matching the suite's torch-optional design)."""
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    torch.backends = types.SimpleNamespace()  # MPS is probed via accel._mps_available (patched)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(accel, "_mps_available", lambda _torch: mps)


# ---- auto-detection priority: CUDA > MPS > CPU ----

def test_cuda_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=True)
    assert accel.select_device() == "cuda"


def test_mps_when_no_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=True)
    assert accel.select_device() == "mps"
    # Selecting MPS must arm the op-fallback so unimplemented ops don't crash.
    assert os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1"


def test_cpu_when_no_accelerator(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=False)
    assert accel.select_device() == "cpu"


def test_no_torch_is_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    # `import torch` with None in sys.modules raises ImportError → CPU.
    monkeypatch.setitem(sys.modules, "torch", None)
    assert accel.select_device() == "cpu"


# ---- auto_mps=False (ECAPA voiceprint parity default) ----

def test_auto_mps_false_skips_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=True)
    assert accel.select_device(auto_mps=False) == "cpu"


def test_auto_mps_false_still_honors_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=True)
    monkeypatch.setenv("EXOMEM_VOICE_DEVICE", "mps")
    assert accel.select_device(override_env="EXOMEM_VOICE_DEVICE", auto_mps=False) == "mps"


# ---- overrides ----

def test_per_model_override_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)  # would auto-pick cuda
    monkeypatch.setenv("EXOMEM_CLIP_DEVICE", "cpu")
    assert accel.select_device(override_env="EXOMEM_CLIP_DEVICE") == "cpu"


def test_global_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_TORCH_DEVICE", "cpu")
    assert accel.select_device() == "cpu"


def test_per_model_override_beats_global(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=False, mps=False)
    monkeypatch.setenv("EXOMEM_TORCH_DEVICE", "cpu")
    monkeypatch.setenv("EXOMEM_CLIP_DEVICE", "cuda")
    assert accel.select_device(override_env="EXOMEM_CLIP_DEVICE") == "cuda"


def test_explicit_mps_override_arms_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_TORCH_DEVICE", "mps")
    assert accel.select_device() == "mps"
    assert os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK") == "1"


# ---- avoid_cuda_when_asr: the cuDNN-shadow workaround is CUDA-only ----

def test_avoid_cuda_when_asr_active_falls_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    # ASR active = EXOMEM_DISABLE_MEDIA_EXTRACTION unset (cleared by the fixture).
    assert accel.select_device(avoid_cuda_when_asr=True) == "cpu"


def test_avoid_cuda_when_asr_disabled_keeps_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_hw(monkeypatch, cuda=True, mps=False)
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    assert accel.select_device(avoid_cuda_when_asr=True) == "cuda"


def test_avoid_cuda_when_asr_does_not_penalize_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key Apple-Silicon win: CLIP/ECAPA keep MPS even with ASR active, because the
    cuDNN clash only exists on CUDA."""
    _set_hw(monkeypatch, cuda=False, mps=True)  # a Mac: MPS, no CUDA
    # ASR active. On the old code this returned "cpu"; now it stays on the GPU.
    assert accel.select_device(avoid_cuda_when_asr=True) == "mps"


# ---- pipeline_device (HF transformers device form) ----

@pytest.mark.parametrize(
    "cuda, mps, expected",
    [(True, False, 0), (False, True, "mps"), (False, False, -1)],
)
def test_pipeline_device(monkeypatch: pytest.MonkeyPatch, cuda, mps, expected) -> None:
    _set_hw(monkeypatch, cuda=cuda, mps=mps)
    assert accel.pipeline_device() == expected


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
