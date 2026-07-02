"""Compute-backend selection for torch models: CUDA > MPS > CPU.

One place decides where torch models run so macOS (Apple Silicon / Metal via the
MPS backend), Linux + Windows NVIDIA (CUDA), and CPU-only boxes all follow the
same rules. Torch is imported lazily inside `select_device` — keyword-mode and a
lean install without the `[embeddings]` extra must never pay the import cost.

Scope — this governs **torch** models only: the bge text embedder, bge reranker,
CLIP, ECAPA voiceprints, and the optional caption pipeline. The ASR engine is
faster-whisper (CTranslate2), which has **no Metal backend** and owns its own
cuda/cpu choice in `extract`; it does NOT route through here. On Apple Silicon,
transcription therefore still runs on CPU until a Metal-capable backend (e.g.
mlx-whisper) is added behind the `extract` transcription seam.

Overrides are checked first, in order: a per-model env var (e.g.
`EXOMEM_CLIP_DEVICE`, `EXOMEM_VOICE_DEVICE`), then the global
`EXOMEM_TORCH_DEVICE`. An override value is returned verbatim, so
`EXOMEM_TORCH_DEVICE=cpu` forces every torch model to CPU (a handy escape hatch
for debugging or thermal control on a fanless Mac).

Linux note: AMD ROCm builds of torch report `torch.cuda.is_available() == True`
(HIP masquerades as CUDA), so the existing `"cuda"` path already covers ROCm —
enabling it is a wheel/index packaging matter, not a code branch. The only
genuinely new hardware path here is macOS MPS.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Global torch-device override, consulted after any per-model override.
GLOBAL_OVERRIDE_ENV = "EXOMEM_TORCH_DEVICE"
# Lets ops the MPS backend hasn't implemented fall back to CPU instead of raising.
_MPS_FALLBACK_ENV = "PYTORCH_ENABLE_MPS_FALLBACK"


def _mps_available(torch) -> bool:
    """True when torch exposes a usable Apple-Silicon MPS (Metal) backend.

    Guarded with getattr so an older torch without `backends.mps` is simply
    treated as MPS-absent, and any probe error degrades to unavailable.
    """
    backend = getattr(torch.backends, "mps", None)
    if backend is None:
        return False
    try:
        return bool(backend.is_available() and backend.is_built())
    except Exception:  # noqa: BLE001 — any probe failure → treat as unavailable
        return False


def _asr_active() -> bool:
    """Mirror `extract.extraction_enabled()` via env alone (no heavy import edge).

    Media extraction (ASR) being active is the signal for the CUDA-only
    cuDNN-shadow workaround; see `select_device(avoid_cuda_when_asr=...)`.
    """
    return not os.environ.get("EXOMEM_DISABLE_MEDIA_EXTRACTION")


def _enable_mps_fallback() -> None:
    """Set PYTORCH_ENABLE_MPS_FALLBACK=1 before any model runs its first op.

    Setting it here (before a model is constructed or run) is sufficient — torch
    reads it at op-dispatch time. `setdefault` respects an explicit operator
    choice (e.g. someone who set it to 0 on purpose).
    """
    os.environ.setdefault(_MPS_FALLBACK_ENV, "1")


def select_device(
    *,
    override_env: str | None = None,
    avoid_cuda_when_asr: bool = False,
    auto_mps: bool = True,
) -> str:
    """Pick a torch device string: ``"cuda"`` | ``"mps"`` | ``"cpu"``.

    Priority: explicit override (``override_env`` then ``EXOMEM_TORCH_DEVICE``)
    → CUDA → MPS → CPU.

    Args:
        override_env: name of a per-model device override checked first. An
            override is returned verbatim (e.g. ``"cuda"``, ``"cpu"``, ``"mps"``,
            ``"cuda:1"``).
        avoid_cuda_when_asr: when the auto-pick would be CUDA *and* media
            extraction (ASR) is active in this process, return CPU instead. This
            is the faster-whisper cuDNN-shadow workaround and is **CUDA-only** —
            it never forces CPU on an MPS host, so CLIP/ECAPA keep the Apple GPU
            even with ASR running (an improvement over the previous
            unconditional force-to-CPU). See `embeddings._clip_device`.
        auto_mps: whether auto-detection may select MPS. False keeps the default
            on CPU (e.g. ECAPA voiceprints, for cross-machine numeric parity)
            while an explicit override to ``"mps"`` is still honored.
    """
    for env in (override_env, GLOBAL_OVERRIDE_ENV):
        if not env:
            continue
        value = os.environ.get(env)
        if value:
            value = value.strip()
            if value == "mps":
                _enable_mps_fallback()
            return value

    try:
        import torch
    except Exception:  # noqa: BLE001 — torch absent on a lean box → CPU
        return "cpu"

    if torch.cuda.is_available():
        if avoid_cuda_when_asr and _asr_active():
            return "cpu"
        return "cuda"
    if auto_mps and _mps_available(torch):
        _enable_mps_fallback()
        return "mps"
    return "cpu"


def pipeline_device(**kwargs):
    """`transformers.pipeline(device=...)` value for the selected torch device.

    HF's pipeline takes an int GPU index or a device string: ``0`` for the first
    CUDA GPU, ``"mps"`` for Apple Silicon, ``-1`` for CPU. Accepts the same
    keyword policy as `select_device`.
    """
    device = select_device(**kwargs)
    if device == "cuda":
        return 0
    if device == "cpu":
        return -1
    return device  # "mps", or an explicit override like "cuda:1"
