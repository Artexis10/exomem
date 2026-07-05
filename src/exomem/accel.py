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

CPU is the steady-state default (idle VRAM ~0); CUDA is never auto-selected —
it is reached only via `performance` mode (see `exomem.mode`) or an explicit
device value. Overrides are checked first, in order: a per-model env var (e.g.
`EXOMEM_CLIP_DEVICE`, `EXOMEM_EMBED_DEVICE`, `EXOMEM_VOICE_DEVICE`), then the
global `EXOMEM_DEVICE` (legacy alias `EXOMEM_TORCH_DEVICE`). A device value is
returned verbatim, so `EXOMEM_DEVICE=cpu` forces every torch model to CPU and
`EXOMEM_DEVICE=cuda` forces the GPU (the back-compat door) — except `gpu`/`auto`,
which mean "use a GPU if one is capable" and run the headroom-checked probe.

Linux note: AMD ROCm builds of torch report `torch.cuda.is_available() == True`
(HIP masquerades as CUDA), so the existing `"cuda"` path already covers ROCm —
enabling it is a wheel/index packaging matter, not a code branch. The only
genuinely new hardware path here is macOS MPS.
"""

from __future__ import annotations

import logging
import os

from . import mode as mode_module

log = logging.getLogger(__name__)

# Friendly global torch-device override, consulted after any per-model override.
GLOBAL_OVERRIDE_ENV = "EXOMEM_DEVICE"
# Legacy alias for the global override (pre-rename configs), checked after the
# friendly name. `=cuda`/`=gpu` here is the back-compat door for users who want the
# old GPU-first behavior after the CPU-default flip.
LEGACY_OVERRIDE_ENV = "EXOMEM_TORCH_DEVICE"
# Min free VRAM (GB) to accept a GPU — the marginal-VRAM guard. Overridable.
GPU_MIN_FREE_ENV = "EXOMEM_GPU_MIN_FREE_GB"
_DEFAULT_GPU_MIN_FREE_GB = 2.0
# Device-env values that mean "pick a GPU if you can" rather than a literal device:
# probe-gated (degrade politely to CPU) instead of returned verbatim.
_GPU_SENTINELS = frozenset({"auto", "gpu"})
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


def gpu_usable(min_free_gb: float | None = None) -> bool:
    """True iff torch has a working CUDA device with free VRAM above a threshold.

    The marginal-VRAM guard: a small card or a GPU already busy with a game reports
    little free VRAM → we decline it and stay on CPU rather than OOM the host or the
    user's other app. **Never raises** — a CPU-only build, absent driver, init
    crash, or `CUDA_VISIBLE_DEVICES=""` (which `mem_get_info` honors) all degrade to
    False. This is what makes a no/weak-GPU host a zero-error, CPU-by-default config.
    """
    if min_free_gb is None:
        try:
            min_free_gb = float(os.environ.get(GPU_MIN_FREE_ENV) or _DEFAULT_GPU_MIN_FREE_GB)
        except ValueError:
            min_free_gb = _DEFAULT_GPU_MIN_FREE_GB
    try:
        import torch

        if not torch.cuda.is_available():
            return False
        free, _total = torch.cuda.mem_get_info()  # bytes, current device; respects CVD
        return free >= min_free_gb * 1024**3
    except Exception:  # noqa: BLE001 — any probe failure degrades to CPU
        return False


def _auto_device(*, auto_mps: bool, avoid_cuda_when_asr: bool, want_cuda: bool) -> str:
    """Resolve a non-verbatim device: CUDA (if wanted + capable) → MPS → CPU.

    `want_cuda` gates whether CUDA is even considered — normal mode never wants it,
    performance mode and the explicit `gpu`/`auto` sentinels do. CUDA is accepted
    only through `gpu_usable()` (headroom-checked) so a marginal GPU degrades to CPU.
    """
    try:
        import torch
    except Exception:  # noqa: BLE001 — torch absent on a lean box → CPU
        return "cpu"

    if want_cuda and gpu_usable():
        if avoid_cuda_when_asr and _asr_active():
            return "cpu"
        return "cuda"
    if auto_mps and _mps_available(torch):
        _enable_mps_fallback()
        return "mps"
    return "cpu"


def select_device(
    *,
    override_env: str | None = None,
    avoid_cuda_when_asr: bool = False,
    auto_mps: bool = True,
) -> str:
    """Pick a torch device string: ``"cuda"`` | ``"mps"`` | ``"cpu"``.

    Precedence (highest first): explicit per-model override (``override_env``) →
    ``EXOMEM_DEVICE`` (alias ``EXOMEM_TORCH_DEVICE``) → the resolved compute *mode*
    → CPU. CPU is the steady-state default; CUDA is **never** auto-selected — it is
    reached only via performance mode or an explicit device value.

    Device-env values are returned verbatim (``"cpu"``, ``"cuda"``, ``"cuda:1"``,
    ``"mps"``) **except** the sentinels ``"gpu"``/``"auto"``, which mean "use a GPU
    if one is capable" and run the headroom-checked probe (degrading to MPS/CPU).
    So ``EXOMEM_DEVICE=cuda`` forces CUDA unconditionally (the back-compat door),
    while ``EXOMEM_DEVICE=gpu`` opts in politely.

    Args:
        override_env: name of a per-model device override checked first (e.g.
            ``EXOMEM_CLIP_DEVICE``, or ``EXOMEM_EMBED_DEVICE`` for the text path).
        avoid_cuda_when_asr: when the pick would be CUDA *and* media extraction
            (ASR) is active in this process, return CPU instead — the CUDA-only
            faster-whisper cuDNN-shadow workaround (never penalizes MPS). See
            `embeddings._clip_device`.
        auto_mps: whether auto-detection may select MPS. False keeps the default on
            CPU (e.g. ECAPA voiceprints, for cross-machine numeric parity) while an
            explicit override to ``"mps"`` is still honored.
    """
    for env in (override_env, GLOBAL_OVERRIDE_ENV, LEGACY_OVERRIDE_ENV):
        if not env:
            continue
        raw = os.environ.get(env)
        if not raw or not raw.strip():
            continue
        value = raw.strip()
        if value.lower() in _GPU_SENTINELS:
            return _auto_device(
                auto_mps=auto_mps, avoid_cuda_when_asr=avoid_cuda_when_asr, want_cuda=True
            )
        if value.lower() == "mps":
            _enable_mps_fallback()
            return "mps"
        return value  # verbatim: cpu / cuda / cuda:1 / ...

    m = mode_module.resolve_mode()
    if m == "quiet":
        return "cpu"
    return _auto_device(
        auto_mps=auto_mps,
        avoid_cuda_when_asr=avoid_cuda_when_asr,
        want_cuda=(m == "performance"),
    )


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
