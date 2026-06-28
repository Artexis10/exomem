"""voice_embed.py — device selection, TF32 parity, and soft-fail seams (model patched).

The real ECAPA model is never loaded here: a box without `speechbrain`/`torchaudio` must still
run the suite. We patch the lazy loader + audio loader and exercise the soft-fail contract
(every failure → None, never raises) plus the CLIP-precedent device gating.
"""
from __future__ import annotations

import numpy as np
import pytest
import torch

from kb_mcp import voice_embed


def test_voice_device_env_and_asr_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors embeddings._clip_device: GPU only when ASR (media extraction) is disabled;
    an explicit KB_MCP_VOICE_DEVICE override always wins."""
    # Explicit override wins regardless of ASR state.
    monkeypatch.setenv("KB_MCP_VOICE_DEVICE", "cuda")
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert voice_embed._voice_device() == "cuda"
    monkeypatch.setenv("KB_MCP_VOICE_DEVICE", "cpu")
    monkeypatch.setenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", "1")
    assert voice_embed._voice_device() == "cpu"
    # No override + ASR enabled (the live default) → CPU, dodging the whisper cuDNN clash.
    monkeypatch.delenv("KB_MCP_VOICE_DEVICE", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert voice_embed._voice_device() == "cpu"


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
