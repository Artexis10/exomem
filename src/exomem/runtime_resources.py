"""Portable limits for active native model compute.

This module stays dependency-light so process entrypoints can establish the
native environment before optional model runtimes import.
"""

from __future__ import annotations

import contextlib
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CPU_THREADS_ENV = "EXOMEM_CPU_THREADS"
SYNC_WORKERS_ENV = "EXOMEM_SYNC_WORKERS"
ALLOW_NATIVE_OVERRIDES_ENV = "EXOMEM_ALLOW_NATIVE_THREAD_OVERRIDES"
SYSTEMD_CPU_WEIGHT = 20
_NATIVE_ENV = {
    "OMP_NUM_THREADS": None,
    "MKL_NUM_THREADS": None,
    "OPENBLAS_NUM_THREADS": None,
    "BLIS_NUM_THREADS": None,
    "NUMEXPR_NUM_THREADS": None,
    "RAYON_NUM_THREADS": None,
    "TOKENIZERS_PARALLELISM": "false",
}
_background_priority_applied: bool | None = None
_RESOURCE_POLICY_ENV = (
    CPU_THREADS_ENV,
    SYNC_WORKERS_ENV,
    ALLOW_NATIVE_OVERRIDES_ENV,
)


@dataclass(frozen=True)
class ComputePolicy:
    cpu_threads: int
    cpu_source: str
    sync_workers: int
    sync_source: str
    model_admission: int
    native_overrides_unsafe: bool


def _positive_env(name: str, default: int, *, minimum: int) -> tuple[int, str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default, "default"
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer >= {minimum}") from error
    if value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value, "env"


def resolve_policy() -> ComputePolicy:
    """Read the compute envelope without importing model runtimes."""
    cpu_threads, cpu_source = _positive_env(CPU_THREADS_ENV, 1, minimum=1)
    sync_workers, sync_source = _positive_env(SYNC_WORKERS_ENV, 8, minimum=2)
    return ComputePolicy(
        cpu_threads=cpu_threads,
        cpu_source=cpu_source,
        sync_workers=sync_workers,
        sync_source=sync_source,
        model_admission=min(4, sync_workers // 2),
        native_overrides_unsafe=os.environ.get(ALLOW_NATIVE_OVERRIDES_ENV) == "1",
    )


def preload_local_dotenv_policy() -> None:
    """Load only local resource keys before native runtime bootstrap.

    Hosted cells keep their inherited environment authoritative; local servers
    later load the full cwd ``.env`` through ``initialize_runtime`` as usual.
    """
    if os.environ.get("EXOMEM_HOSTED_CELL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    dotenv_path = Path.cwd() / ".env"
    if not dotenv_path.is_file():
        return
    from dotenv import dotenv_values

    values = dotenv_values(dotenv_path)
    for name in _RESOURCE_POLICY_ENV:
        value = values.get(name)
        if value is not None:
            os.environ[name] = value


def bootstrap() -> ComputePolicy:
    """Install the native-thread environment before a heavy runtime imports."""
    policy = resolve_policy()
    if policy.native_overrides_unsafe:
        return policy
    for name, fixed_value in _NATIVE_ENV.items():
        os.environ[name] = fixed_value or str(policy.cpu_threads)
    return policy


def configure_torch(torch: Any | None = None) -> None:
    """Apply explicit PyTorch limits when the optional runtime is installed."""
    if torch is None:
        try:
            import torch as torch_module
        except ModuleNotFoundError as exc:
            if exc.name != "torch":
                raise
            return

        torch = torch_module
    policy = resolve_policy()
    torch.set_num_threads(policy.cpu_threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # PyTorch rejects a later inter-op change. The entrypoint bootstrap and
        # direct callers keep this before parallel work in the ordinary path.
        pass


def configure_onnx_session_options(options: Any) -> None:
    """Make ONNX's otherwise independent pools obey the common budget."""
    policy = resolve_policy()
    options.intra_op_num_threads = policy.cpu_threads
    options.inter_op_num_threads = 1


class ModelBusyError(RuntimeError):
    """Retryable refusal when admitted model work would consume general capacity."""

    code = "MODEL_BUSY"

    def as_semantic_validation_error(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "remediation": "Retry shortly; model compute is at its admitted capacity.",
        }


class ModelAdmissionGate:
    """Bounded model admission with serialized, owner-thread-reentrant execution."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._admitted = threading.BoundedSemaphore(capacity)
        self._admission_lock = threading.Lock()
        self._admitted_count = 0
        self._execution = threading.RLock()
        self._local = threading.local()

    @contextlib.contextmanager
    def execution(self):
        depth = getattr(self._local, "depth", 0)
        admitted = depth == 0
        if admitted and not self._admitted.acquire(blocking=False):
            raise ModelBusyError("model compute is busy; retry shortly")
        if admitted:
            with self._admission_lock:
                self._admitted_count += 1
        try:
            with self._execution:
                self._local.depth = depth + 1
                try:
                    yield
                finally:
                    self._local.depth = depth
        finally:
            if admitted:
                with self._admission_lock:
                    self._admitted_count -= 1
                self._admitted.release()

    def admitted_count(self) -> int:
        with self._admission_lock:
            return self._admitted_count


_gate_lock = threading.Lock()
_gate: ModelAdmissionGate | None = None
_gate_capacity: int | None = None


def model_execution():
    """Return the process-wide model gate for an embedding, reranker, CLIP, or ASR call."""
    global _gate, _gate_capacity
    capacity = resolve_policy().model_admission
    with _gate_lock:
        if _gate is None or _gate_capacity != capacity:
            _gate = ModelAdmissionGate(capacity)
            _gate_capacity = capacity
        return _gate.execution()


def lifespan(inner=None):
    """Wrap local and hosted FastMCP lifespans with the shared sync-worker limit."""

    @asynccontextmanager
    async def _lifespan(server):
        import anyio

        anyio.to_thread.current_default_thread_limiter().total_tokens = resolve_policy().sync_workers
        if inner is None:
            yield {}
            return
        async with inner(server) as state:
            yield state

    return _lifespan


def lower_background_priority(*, platform: str | None = None) -> bool:
    """Best-effort lower priority for disposable media work, reporting success."""
    global _background_priority_applied
    chosen = platform or os.name
    try:
        if chosen == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            _background_priority_applied = bool(
                ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)
            )  # BELOW_NORMAL_PRIORITY_CLASS
        elif chosen == "posix":
            os.nice(10)
            _background_priority_applied = True
        else:
            _background_priority_applied = False
    except OSError:
        _background_priority_applied = False
    return _background_priority_applied


def effective_online_cpus() -> int:
    """Use the process-visible CPU set, with one as a safe fallback."""
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    return max(1, count)


def systemd_cpu_quota(online_cpus: int | None = None) -> str:
    """Reserve at least half the host and cap one cell at four cores."""
    online = max(1, online_cpus if online_cpus is not None else effective_online_cpus())
    return f"{min(400, 50 * online)}%"


def status() -> dict[str, object]:
    """Return allocation-free compute budgets and scheduling posture."""
    policy = resolve_policy()
    online_cpus = effective_online_cpus()
    return {
        **policy.__dict__,
        "background_priority": {
            "requested": "media-child-best-effort-lowered",
            "current_process": (
                "applied"
                if _background_priority_applied is True
                else "not_applied"
                if _background_priority_applied is False
                else "unverified"
            ),
        },
        "systemd": {
            "cpu_weight": SYSTEMD_CPU_WEIGHT,
            "cpu_quota": systemd_cpu_quota(online_cpus),
            "online_cpus": online_cpus,
        },
    }


def evaluate_active_envelope(
    *,
    cpu_samples: list[float] | None,
    duration_seconds: float,
    quota_percent: int,
    health_latencies: list[float] | None,
) -> dict[str, object]:
    """Evaluate deterministic process-tree CPU and probe observations for the release gate."""
    if not cpu_samples or len(cpu_samples) < 2:
        return {"ok": False, "failures": ["CPU metrics are unreadable"]}
    if duration_seconds <= 0:
        return {"ok": False, "failures": ["CPU sample duration is unreadable"]}
    failures: list[str] = []
    cpu_cores = (cpu_samples[-1] - cpu_samples[0]) / duration_seconds
    allowed_cores = quota_percent / 100 + 0.25
    if cpu_cores > allowed_cores:
        failures.append(f"cpu rate {cpu_cores:.2f} cores exceeds {allowed_cores:.2f}")
    if not health_latencies:
        failures.append("health/status probe metrics are unreadable")
    elif max(health_latencies) >= 1:
        failures.append("health/status probe latency exceeds 1 second")
    return {"ok": not failures, "failures": failures}
