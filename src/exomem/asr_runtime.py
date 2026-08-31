"""Allocation-light policy for the CTranslate2 ASR runtime.

This deliberately does not depend on torch: faster-whisper owns a separate
CTranslate2 CUDA stack, so torch readiness says nothing about ASR readiness.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import mode as mode_module

ASR_DEVICE_ENV = "EXOMEM_ASR_DEVICE"
ASR_COMPUTE_TYPE_ENV = "EXOMEM_ASR_COMPUTE_TYPE"
CPU_DEFAULT_COMPUTE_TYPE = "int8"
CUDA_DEFAULT_COMPUTE_TYPE = "float16"
_INT8_TYPES = frozenset({"int8", "int8_float16", "int8_float32", "int8_bfloat16"})
COMPUTE_RUNTIME_MARKERS = (
    "cublas",
    "cudnn",
    "cuda driver",
    "cuda error",
    "cudart",
    "failed to initialize cuda",
    "failed to load cuda",
    "no cuda-capable device",
)
_COMPUTE_RUNTIME_ERROR_RE = re.compile(
    r"^(?:ASR(?:ComputeRuntimeError|RuntimeRefusal)|RuntimeError|OSError):.*"
    r"(?:\bcublas(?:\s|_| failed)|\bcudnn(?:\s|_| failed)|"
    r"\bcuda (?:driver (?:version|is insufficient)|error)|\bcudart(?:\s|_| failed)|"
    r"failed to (?:initialize|load) cuda|no cuda-capable device)",
    re.IGNORECASE,
)


class ASRComputeRuntimeError(RuntimeError):
    """CTranslate2 failed to initialize or execute its CUDA runtime."""


class ASRRuntimeRefusal(ASRComputeRuntimeError):
    """An explicitly requested or unsafe ASR runtime cannot be selected."""


@dataclass(frozen=True)
class ASRProbe:
    cuda_available: bool
    cuda_types: frozenset[str]
    cpu_types: frozenset[str]
    compute_capability: tuple[int, int] | None
    reason: str | None = None


@dataclass(frozen=True)
class ASRSelection:
    device: str
    compute_type: str
    probe: ASRProbe


def _requested_device() -> tuple[str, bool]:
    raw = os.environ.get(ASR_DEVICE_ENV, "").strip().lower()
    if raw in {"cpu", "cuda"}:
        return raw, True
    # Historical auto/gpu means automatic; unknown values are invalid instead of
    # silently turning a typo into a different accelerator choice.
    if raw in {"", "auto", "gpu"}:
        return "auto", False
    raise ASRRuntimeRefusal(f"unsupported {ASR_DEVICE_ENV}={raw!r}; choose cpu or cuda")


def _requested_compute_type() -> str | None:
    raw = os.environ.get(ASR_COMPUTE_TYPE_ENV, "").strip().lower()
    if not raw:
        return None
    if raw == "auto":
        raise ASRRuntimeRefusal(
            f"{ASR_COMPUTE_TYPE_ENV}=auto is unsafe; choose a concrete CTranslate2 type"
        )
    return raw


def _cuda_type_allowed(value: str, probe: ASRProbe) -> bool:
    capability = probe.compute_capability
    if capability is not None and capability[0] >= 12 and value in _INT8_TYPES:
        return False
    return value in probe.cuda_types


def _refuse_type(value: str, device: str, probe: ASRProbe) -> ASRRuntimeRefusal:
    capability = probe.compute_capability
    if device == "cuda" and capability is not None and capability[0] >= 12 and value in _INT8_TYPES:
        return ASRRuntimeRefusal(
            f"{value} is unsafe on sm_{capability[0]}{capability[1]} despite CTranslate2 capability reporting; use float16"
        )
    supported = sorted(probe.cuda_types if device == "cuda" else probe.cpu_types)
    return ASRRuntimeRefusal(
        f"{value!r} is not supported for ASR on {device}; choose one of {', '.join(supported) or 'none'}"
    )


def select_asr_runtime(*, probe: ASRProbe | None = None) -> ASRSelection:
    """Resolve a concrete faster-whisper device/type without consulting torch."""
    probe = probe or probe_asr_runtime()
    requested_device, _ = _requested_device()
    override = _requested_compute_type()
    mode = mode_module.resolve_mode()

    if requested_device == "cpu" or (requested_device == "auto" and mode == "quiet"):
        selected = override or CPU_DEFAULT_COMPUTE_TYPE
        if selected not in probe.cpu_types:
            raise _refuse_type(selected, "cpu", probe)
        return ASRSelection("cpu", selected, probe)

    cuda_admitted = probe.cuda_available and CUDA_DEFAULT_COMPUTE_TYPE in probe.cuda_types
    if requested_device == "cuda":
        if not probe.cuda_available:
            raise ASRRuntimeRefusal(probe.reason or "CUDA ASR is not admitted")
        selected = override or CUDA_DEFAULT_COMPUTE_TYPE
        if not _cuda_type_allowed(selected, probe):
            raise _refuse_type(selected, "cuda", probe)
        return ASRSelection("cuda", selected, probe)

    # Normal/performance automatic routing may use CUDA only after ASR-specific
    # admission. A requested concrete type can fall back only if it is exactly
    # CPU-supported; never silently replace an operator choice.
    if cuda_admitted and (override is None or _cuda_type_allowed(override, probe)):
        return ASRSelection("cuda", override or CUDA_DEFAULT_COMPUTE_TYPE, probe)
    if override is not None:
        if override not in probe.cpu_types:
            raise _refuse_type(override, "cpu", probe)
        return ASRSelection("cpu", override, probe)
    if CPU_DEFAULT_COMPUTE_TYPE not in probe.cpu_types:
        raise _refuse_type(CPU_DEFAULT_COMPUTE_TYPE, "cpu", probe)
    return ASRSelection("cpu", CPU_DEFAULT_COMPUTE_TYPE, probe)


def probe_asr_runtime() -> ASRProbe:
    """Allocation-free CTranslate2 capability/headroom admission probe.

    This function is called only from the disposable media worker. Status and
    doctor report cached policy and must not invoke it.
    """
    try:
        import ctranslate2

        cpu_types = frozenset(ctranslate2.get_supported_compute_types("cpu"))
        count = int(ctranslate2.get_cuda_device_count())
        if count < 1:
            return ASRProbe(False, frozenset(), cpu_types, None, "no CTranslate2 CUDA device")
        cuda_types = frozenset(ctranslate2.get_supported_compute_types("cuda", 0))
    except Exception as exc:  # noqa: BLE001 - broken CT2 is a soft CPU posture unless explicit CUDA
        return ASRProbe(False, frozenset(), frozenset({CPU_DEFAULT_COMPUTE_TYPE}), None, str(exc))
    capability, headroom_reason = _nvidia_headroom()
    if headroom_reason is not None:
        return ASRProbe(False, cuda_types, cpu_types, capability, headroom_reason)
    return ASRProbe(True, cuda_types, cpu_types, capability)


def _nvidia_headroom() -> tuple[tuple[int, int] | None, str | None]:
    """Read NVIDIA state through nvidia-smi; it never creates a CUDA context."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        row = result.stdout.splitlines()[0].split(",")
        major, minor = (int(part) for part in row[0].strip().split(".", 1))
        if int(row[1].strip()) < 2048:
            return (major, minor), "insufficient free GPU memory for ASR"
        return (major, minor), None
    except Exception as exc:  # noqa: BLE001 - nvidia-smi is an optional admission source
        return None, f"NVIDIA headroom probe failed: {exc}"


def is_compute_runtime_failure(error: BaseException | str) -> bool:
    """Conservatively identify native CUDA runtime failures, never bad media."""
    message = (
        f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    )
    return _COMPUTE_RUNTIME_ERROR_RE.search(message) is not None


def as_compute_runtime_error(error: BaseException) -> ASRComputeRuntimeError | None:
    if not is_compute_runtime_failure(error):
        return None
    return ASRComputeRuntimeError(f"ASR CUDA runtime failed: {type(error).__name__}: {error}")


def cuda_runtime_child_env(
    parent: dict[str, str] | None = None,
    *,
    roots: list[Path] | None = None,
    platform_name: str | None = None,
) -> dict[str, str]:
    """Prepend CUDA-12 wheel directories before the media child interpreter starts."""
    env = dict(os.environ if parent is None else parent)
    platform_name = platform_name or sys.platform
    if platform_name not in {"linux", "win32"}:
        return env
    leaf = "bin" if platform_name == "win32" else "lib"
    roots = roots if roots is not None else _nvidia_roots()
    owned: list[str] = []
    for root in roots:
        for package in ("cublas", "cuda_runtime", "cudnn"):
            candidate = root / package / leaf
            if candidate.is_dir():
                owned.append(str(candidate))
    if not owned:
        return env
    key = "PATH" if platform_name == "win32" else "LD_LIBRARY_PATH"
    prior = env.get(key, "")
    env[key] = os.pathsep.join([*owned, *([prior] if prior else [])])
    return env


def _nvidia_roots() -> list[Path]:
    """Find namespace-package roots without importing native CUDA libraries."""
    import importlib.util

    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        return []
    return [Path(location) for location in spec.submodule_search_locations]
