"""Voice embedding — speechbrain ECAPA voiceprints (deterministic transduction, not a brain).

A frozen ECAPA-TDNN model (`speechbrain/spkrec-ecapa-voxceleb`) turns an audio span into a
192-dim speaker-embedding vector — the same category of measurement as bge/CLIP/Whisper: a
fixed audio→vector function, no reasoning, no generation. `extract._diarize` uses it to embed
each anonymous diarization cluster so `speaker_attribution` can match it against enrolled
profiles by cosine. Pure-substrate "measure," not "judge."

Soft-import seam (mirrors `extract._load_diarization_pipeline`): a box without the
`[diarization]` extra's `speechbrain` raises ImportError inside the lazy loader, which
`embed_spans` catches → returns None. Every failure (missing dep, unloadable model, GPU/cuDNN
error, inference exception, undecodable audio) degrades to None — the caller then stays
anonymous. Never raises.

Device follows the CLIP precedent (`embeddings._clip_device`): ECAPA runs on torch, whose
cuDNN can be shadowed by faster-whisper's PATH-prepended CUDA-12 cuDNN when ASR is active —
the bug that broke CLIP's ViT. So `_voice_device()` returns CPU whenever ASR/media extraction
is enabled in this process, with a `EXOMEM_VOICE_DEVICE` override. TF32 is disabled before
inference so a voiceprint computed here matches one computed elsewhere (embedding parity).
"""
from __future__ import annotations

import logging
import os
import threading

import numpy as np

from . import accel

log = logging.getLogger(__name__)

# Frozen ECAPA speaker-embedding checkpoint; override for a pinned/local copy.
VOICE_EMBED_MODEL = os.environ.get(
    "EXOMEM_VOICE_EMBED_MODEL", "speechbrain/spkrec-ecapa-voxceleb"
)
# ECAPA produces 192-dim speaker embeddings.
VOICE_EMBED_DIM = 192
# ECAPA was trained on 16 kHz mono audio; spans are resampled to this before inference.
_TARGET_SR = 16000

_VOICE_MODEL = None  # speechbrain EncoderClassifier singleton
_VOICE_LOCK = threading.Lock()


def _voice_device() -> str:
    """Device for ECAPA. Honors EXOMEM_VOICE_DEVICE; otherwise CUDA when available
    (never under active ASR — the cuDNN-shadow clash), else CPU.

    Same CUDA rationale as `embeddings._clip_device()`: faster-whisper's CUDA-12 cuDNN
    wheels get PATH-prepended (extract._ensure_cuda_dll_path) so ctranslate2 can load,
    which then shadows torch-cu132's bundled cuDNN 13 and makes torch conv/inference die
    with CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH. So `avoid_cuda_when_asr` keeps ECAPA
    off CUDA while ASR is active.

    Unlike CLIP, ECAPA does **not** auto-select MPS (`auto_mps=False`): a voiceprint is
    matched by cosine against profiles that may have been computed on another machine,
    and MPS float32 kernels differ numerically from CPU — so the parity-safe default is
    CPU, mirroring `_disable_tf32`. Set `EXOMEM_VOICE_DEVICE=mps` to opt into the Apple
    GPU on a single-machine setup where parity across hosts doesn't matter.
    """
    return accel.select_device(
        override_env="EXOMEM_VOICE_DEVICE", avoid_cuda_when_asr=True, auto_mps=False
    )


def _disable_tf32() -> None:
    """Disable TF32 matmul before inference so voiceprints are bit-stable across machines.

    TF32 trades mantissa bits for speed on Ampere+ GPUs; with it on, the same audio + model can
    yield slightly different embeddings on different hardware, drifting the cosine scores that
    attribution thresholds depend on. Embedding parity matters more than the marginal speed.
    """
    import torch

    torch.backends.cuda.matmul.allow_tf32 = False
    torch.set_float32_matmul_precision("highest")


def _load_voice_model():
    """Lazy-import + load the pretrained ECAPA speaker-embedding model.

    Soft-import seam (patched in tests): a box without the `[diarization]` extra's `speechbrain`
    raises ImportError here, which `embed_spans` catches → None. The checkpoint is gated like
    pyannote's, so `HUGGINGFACE_TOKEN`/`HF_TOKEN` is honored if set.
    """
    from speechbrain.inference.speaker import EncoderClassifier  # soft dep — only when used

    device = _voice_device()
    log.info("loading voice-embedding model %s on %s", VOICE_EMBED_MODEL, device)
    return EncoderClassifier.from_hparams(source=VOICE_EMBED_MODEL, run_opts={"device": device})


def _get_voice_model():
    """Lazy singleton for the ECAPA model (one load per process)."""
    global _VOICE_MODEL
    if _VOICE_MODEL is not None:
        return _VOICE_MODEL
    with _VOICE_LOCK:
        if _VOICE_MODEL is None:
            _VOICE_MODEL = _load_voice_model()
    return _VOICE_MODEL


def _load_audio(audio_path):
    """Load `audio_path` as a mono 16 kHz float waveform `(1, N)` torch tensor + sample rate.

    Decodes via faster-whisper's PyAV-based `decode_audio` (handles wav/mp3/m4a/video; already a
    media dep, present whenever ASR/diarization runs) — NOT torchaudio, whose 2.x decode routes
    through `torchcodec`, whose native lib fails to load against torch-cu132 (the diarization
    blocker diagnosed 2026-06-28). `decode_audio` returns a mono float32 array already resampled
    to the target rate. absent/undecodable → the caller catches and returns None.
    """
    import torch
    from faster_whisper.audio import decode_audio  # soft dep — bundles PyAV/ffmpeg, no torchcodec

    samples = decode_audio(str(audio_path), sampling_rate=_TARGET_SR)
    waveform = torch.from_numpy(np.ascontiguousarray(samples, dtype=np.float32)).unsqueeze(0)
    return waveform, _TARGET_SR


def embed_spans(audio_path, spans) -> np.ndarray | None:
    """Embed the given `(start, end)` second-spans of `audio_path` → a mean 192-dim voiceprint.

    Loads the audio once, slices each span, ECAPA-embeds it, and returns the mean vector over all
    non-empty spans (float32, shape `(VOICE_EMBED_DIM,)`). Used to compute one centroid per
    anonymous diarization cluster.

    Soft-fail: returns None on ANY failure — missing `speechbrain`/torchaudio, an unloadable or
    gated model, a GPU/cuDNN error, an inference exception, undecodable audio, or no usable
    spans. Never raises; the caller then keeps the cluster anonymous.
    """
    if not spans:
        return None
    try:
        model = _get_voice_model()
    except Exception:  # noqa: BLE001 — missing dep / unloadable model → anonymous
        log.warning("voice-embedding model unavailable; clusters stay anonymous", exc_info=True)
        return None
    try:
        waveform, sr = _load_audio(audio_path)
    except Exception:  # noqa: BLE001 — undecodable audio / torchaudio absent → anonymous
        log.warning("voice-embedding audio load failed for %s", audio_path, exc_info=True)
        return None
    try:
        _disable_tf32()
        vectors: list[np.ndarray] = []
        total = waveform.shape[-1]
        # ECAPA's TDNN convolutions need a minimum input length; sub-~0.4s spans (common when a
        # diarizer over-splits on backchannels/crosstalk) either error in F.pad or carry no usable
        # voiceprint. Skip them — a real speaker has longer spans to embed; a speaker with only
        # tiny spans stays anonymous, which is the right degradation.
        min_samples = int(0.4 * sr)
        for start, end in spans:
            a = max(0, int(float(start) * sr))
            b = min(total, int(float(end) * sr))
            if b - a < min_samples:
                continue
            try:
                emb = model.encode_batch(waveform[:, a:b])
            except Exception:  # noqa: BLE001 — one bad span must not kill the whole cluster
                continue
            arr = emb.detach().cpu().numpy() if hasattr(emb, "detach") else np.asarray(emb)
            vectors.append(np.asarray(arr, dtype=np.float32).reshape(-1))
        if not vectors:
            return None
        return np.mean(np.stack(vectors), axis=0).astype(np.float32)
    except Exception:  # noqa: BLE001 — inference / cuDNN / OOM error → anonymous
        log.warning("voice-embedding inference failed for %s", audio_path, exc_info=True)
        return None
