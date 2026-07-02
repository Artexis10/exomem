"""voice_embed.py — device selection, TF32 parity, and soft-fail seams (model patched).

The real ECAPA model is never loaded here: a box without `speechbrain`/`torchaudio` must still
run the suite. We patch the lazy loader + audio loader and exercise the soft-fail contract
(every failure → None, never raises) plus the CLIP-precedent device gating.
"""
from __future__ import annotations

import numpy as np
import pytest

# The embeddings/voice extra (torch) isn't installed in lean CI — skip the whole module
# there instead of erroring on collection, which is what reddened the `tests` workflow on
# every PR. Mirrors the importorskip gate the other model-loading tests already use.
pytest.importorskip("torch")

import torch  # noqa: E402

from exomem import voice_embed  # noqa: E402


def test_voice_device_env_and_asr_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    """ECAPA device policy via `accel`: an explicit EXOMEM_VOICE_DEVICE override always wins;
    CUDA is avoided while ASR is active (the cuDNN-shadow clash); and MPS is never
    auto-selected — voiceprints are matched by cosine against profiles from other machines and
    MPS float32 kernels drift from CPU, so Apple Silicon is opt-in only."""
    import torch

    from exomem import accel

    # Explicit override wins regardless of hardware / ASR state.
    monkeypatch.setenv("EXOMEM_VOICE_DEVICE", "cuda")
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert voice_embed._voice_device() == "cuda"
    monkeypatch.setenv("EXOMEM_VOICE_DEVICE", "cpu")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    assert voice_embed._voice_device() == "cpu"

    monkeypatch.delenv("EXOMEM_VOICE_DEVICE", raising=False)
    monkeypatch.delenv("EXOMEM_TORCH_DEVICE", raising=False)

    # CUDA box + ASR active → CPU (dodges the whisper cuDNN clash).
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(accel, "_mps_available", lambda _t: False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert voice_embed._voice_device() == "cpu"

    # Apple Silicon (MPS, no CUDA): NOT auto-selected → CPU for cross-machine parity.
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(accel, "_mps_available", lambda _t: True)
    assert voice_embed._voice_device() == "cpu"

    # …but an explicit opt-in is honored.
    monkeypatch.setenv("EXOMEM_VOICE_DEVICE", "mps")
    assert voice_embed._voice_device() == "mps"


def test_disable_tf32_turns_tf32_off() -> None:
    torch.backends.cuda.matmul.allow_tf32 = True
    voice_embed._disable_tf32()
    assert torch.backends.cuda.matmul.allow_tf32 is False


class _FakeModel:
    """Stand-in for the ECAPA EncoderClassifier: returns a fixed (1,1,192) embedding."""

    def __init__(self, vector: np.ndarray) -> None:
        self._vec = vector

    def encode_batch(self, _waveform):
        return torch.tensor(self._vec).reshape(1, 1, -1)


def test_embed_spans_mean_over_spans_and_disables_tf32(monkeypatch: pytest.MonkeyPatch) -> None:
    vec = np.arange(voice_embed.VOICE_EMBED_DIM, dtype=np.float32)
    monkeypatch.setattr(voice_embed, "_get_voice_model", lambda: _FakeModel(vec))
    monkeypatch.setattr(voice_embed, "_load_audio", lambda p: (torch.zeros(1, 32000), 16000))
    torch.backends.cuda.matmul.allow_tf32 = True

    out = voice_embed.embed_spans("x.wav", [(0.0, 1.0), (1.0, 2.0)])

    assert out is not None
    assert out.shape == (voice_embed.VOICE_EMBED_DIM,)
    # Two spans of the same fixed embedding → the mean is that embedding.
    np.testing.assert_allclose(out, vec)
    # TF32 was disabled before inference (embedding parity).
    assert torch.backends.cuda.matmul.allow_tf32 is False


def test_embed_spans_empty_spans_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_embed, "_get_voice_model", lambda: pytest.fail("should not load"))
    assert voice_embed.embed_spans("x.wav", []) is None


def test_embed_spans_import_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_dep():
        raise ImportError("speechbrain not installed")

    # The lazy loader raises ImportError (no [diarization] extra) → soft-fail to None.
    monkeypatch.setattr(voice_embed, "_load_voice_model", _no_dep)
    monkeypatch.setattr(voice_embed, "_VOICE_MODEL", None)
    assert voice_embed.embed_spans("x.wav", [(0.0, 1.0)]) is None


def test_embed_spans_inference_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def encode_batch(self, _waveform):
            raise RuntimeError("cuDNN shadow / OOM")

    monkeypatch.setattr(voice_embed, "_get_voice_model", lambda: _Boom())
    monkeypatch.setattr(voice_embed, "_load_audio", lambda p: (torch.zeros(1, 32000), 16000))
    assert voice_embed.embed_spans("x.wav", [(0.0, 1.0)]) is None


def test_embed_spans_audio_load_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_audio(_p):
        raise RuntimeError("undecodable / torchaudio absent")

    monkeypatch.setattr(voice_embed, "_get_voice_model", lambda: _FakeModel(np.zeros(192)))
    monkeypatch.setattr(voice_embed, "_load_audio", _bad_audio)
    assert voice_embed.embed_spans("x.wav", [(0.0, 1.0)]) is None
