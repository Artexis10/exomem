"""Allocation-light policy for the CTranslate2 ASR runtime.

This deliberately does not depend on torch: faster-whisper owns a separate
CTranslate2 CUDA stack, so torch readiness says nothing about ASR readiness.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from . import cuda_containment, resource_status
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


class ASRGpuBusy(RuntimeError):
    """An explicitly requested GPU is healthy but another process holds its memory right now.

    That is a passing state and not a broken runtime. It deliberately is not an
    `ASRComputeRuntimeError`: that type blocks the job until the operator repairs
    something, while the caller of this one keeps the job pending and tries again later.
    """


@dataclass(frozen=True)
class ASRProbe:
    cuda_available: bool
    cuda_types: frozenset[str]
    cpu_types: frozenset[str]
    compute_capability: tuple[int, int] | None
    reason: str | None = None
    #: The device works but another process holds its memory now; `cuda_available` is False meanwhile.
    busy: bool = False


@dataclass(frozen=True)
class ASRSelection:
    device: str
    compute_type: str
    probe: ASRProbe


def _requested_device() -> tuple[str, bool]:
    if mode_module.service_profile_enabled():
        return "cpu", True
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
            if probe.busy:
                raise ASRGpuBusy(probe.reason or "the GPU is busy")
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
            # A failed preflight hid the devices, so the supervisor's reason says more than "none".
            reason = cuda_containment.preflight_failure() or "no CTranslate2 CUDA device"
            return ASRProbe(False, frozenset(), cpu_types, None, reason)
        cuda_types = frozenset(ctranslate2.get_supported_compute_types("cuda", 0))
    except Exception as exc:  # noqa: BLE001 - broken CT2 is a soft CPU posture unless explicit CUDA
        return ASRProbe(False, frozenset(), frozenset({CPU_DEFAULT_COMPUTE_TYPE}), None, str(exc))
    try:
        capability, free_mib, total_mib = _nvidia_headroom()
    except Exception as exc:  # noqa: BLE001 - nvidia-smi is an optional admission source
        return ASRProbe(False, cuda_types, cpu_types, None, f"NVIDIA headroom probe failed: {exc}")
    needed_mib = resource_status.min_free_vram_mb()
    if total_mib < needed_mib:
        # No amount of waiting frees memory the card does not have, so this stays a visible refusal.
        reason = f"the GPU has {total_mib} MiB of memory in total and ASR needs {needed_mib} MiB free"
        return ASRProbe(False, cuda_types, cpu_types, capability, reason)
    if free_mib < needed_mib:
        reason = f"insufficient free GPU memory for ASR: {free_mib} MiB free, {needed_mib} MiB needed"
        return ASRProbe(False, cuda_types, cpu_types, capability, reason, busy=True)
    return ASRProbe(True, cuda_types, cpu_types, capability)


def _nvidia_headroom() -> tuple[tuple[int, int], int, int]:
    """Read compute capability, free MiB and total MiB through nvidia-smi; it never creates a CUDA context.

    Raises when nvidia-smi is absent or cannot answer, which is a broken probe and not a busy GPU.
    """
    exe = resource_status.find_nvidia_smi()
    if exe is None:
        raise FileNotFoundError("nvidia-smi not found")
    result = subprocess.run(
        [exe, "--query-gpu=compute_cap,memory.free,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=True,
    )
    row = result.stdout.splitlines()[0].split(",")
    major, minor = (int(part) for part in row[0].strip().split(".", 1))
    return (major, minor), int(row[1].strip()), int(row[2].strip())


def is_compute_runtime_failure(error: BaseException | str) -> bool:
    """Conservatively identify native CUDA runtime failures, never bad media."""
    message = (
        f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    )
    return _COMPUTE_RUNTIME_ERROR_RE.search(message) is not None


def is_cuda_out_of_memory(error: BaseException) -> bool:
    """A failed CUDA allocation inside CTranslate2.

    CTranslate2 raises a plain RuntimeError for it and has no type of its own, so its documented
    message ("CUDA failed with error out of memory"; cuBLAS "CUBLAS_STATUS_ALLOC_FAILED") is the signal.
    """
    message = str(error)
    return isinstance(error, RuntimeError) and (
        "out of memory" in message.lower() or "CUBLAS_STATUS_ALLOC_FAILED" in message
    )


def as_gpu_busy(error: BaseException) -> ASRGpuBusy | None:
    """A failed CUDA allocation on an explicit CUDA device means another process took the memory since the probe.

    The free-memory probe is a cheap pre-check: it cannot know what loading the model needs, and the
    card can fill between the probe and the load. Automatic mode is not covered: it already
    chose CUDA only for a card that admitted ASR, and an explicit device is the one that must wait.
    """
    if not is_cuda_out_of_memory(error) or _requested_device()[0] != "cuda":
        return None
    return ASRGpuBusy(f"the GPU ran out of memory while loading the speech model: {error}")


def cpu_selection_after_cuda_oom(selection: ASRSelection, error: BaseException) -> ASRSelection | None:
    """The CPU selection for a job whose automatic CUDA choice ran out of memory while loading, else None.

    Automatic mode chose CUDA from the free memory at the probe, and another process took it since.
    The job can run on the CPU, so it does not wait. An explicit CUDA request waits instead (`as_gpu_busy`).
    """
    if selection.device != "cuda" or not is_cuda_out_of_memory(error) or _requested_device()[0] != "auto":
        return None
    return select_asr_runtime(probe=replace(selection.probe, cuda_available=False))


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
    """The media child's environment: its CUDA device list back, and CUDA-12 wheel directories first."""
    env = cuda_containment.give_devices_to_child(os.environ if parent is None else parent)
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


#: Set in the environment of a process that `relaunch_with_cuda_runtime` started, so that it relaunches once.
CUDA_RUNTIME_READY_ENV = "EXOMEM_CUDA_RUNTIME_READY"


def relaunch_with_cuda_runtime(arguments: list[str]) -> None:
    """Run `python -m exomem <arguments>` in place of this process, with the CUDA 12 wheel directories loadable.

    The dynamic loader reads `LD_LIBRARY_PATH` when a process starts, so a path set inside the process comes
    too late and `libcublas.so.12` stays unfound. The media supervisor starts its child with
    `cuda_runtime_child_env` for that reason; a command that no supervisor started does the same to itself,
    after the GPU preflight that the supervisor runs, and `CUDA_RUNTIME_READY_ENV` marks the second run.
    Returns at once in a process that was relaunched already.
    """
    if os.environ.get(CUDA_RUNTIME_READY_ENV):
        return
    env = cuda_runtime_child_env(os.environ)
    verdict = cuda_containment.check_gpu(env, driver_present=resource_status.find_nvidia_smi() is not None)
    env = cuda_containment.apply_verdict(env, verdict, until="the worker restarts")
    env[CUDA_RUNTIME_READY_ENV] = "1"
    os.execve(sys.executable, [sys.executable, "-m", "exomem", *arguments], env)  # noqa: S606 - fixed interpreter and module


def _nvidia_roots() -> list[Path]:
    """Find namespace-package roots without importing native CUDA libraries."""
    import importlib.util

    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        return []
    return [Path(location) for location in spec.submodule_search_locations]
