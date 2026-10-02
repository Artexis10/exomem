"""Portable limits for active native model compute.

This module stays dependency-light so process entrypoints can establish the
native environment before optional model runtimes import.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CPU_THREADS_ENV = "EXOMEM_CPU_THREADS"
SYNC_WORKERS_ENV = "EXOMEM_SYNC_WORKERS"
ALLOW_NATIVE_OVERRIDES_ENV = "EXOMEM_ALLOW_NATIVE_THREAD_OVERRIDES"
ONNX_SHARE_WEIGHTS_ENV = "EXOMEM_ONNX_SHARE_WEIGHTS"
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
_MODEL_WORK: ContextVar[tuple[str, threading.Event | None]] = ContextVar(
    "exomem_model_work", default=("foreground", None)
)
SEMANTIC_PREPARATION_BYTES = 64 * 1024 * 1024
SEMANTIC_SMALL_RESERVE_BYTES = 4 * 1024 * 1024
SEMANTIC_SOURCE_MAX_BYTES = 16 * 1024 * 1024
_preparation_lock = threading.Lock()
_preparation_bytes = 0
_preparation_peak = 0
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
    from . import cloud_cell

    cloud_cell.resource_policy()
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

    Hosted and cloud cells keep their inherited environment authoritative;
    local servers later load the full cwd ``.env`` through
    ``initialize_runtime`` as usual. Cloud mode's "no `.env` file is loaded"
    (design D1.6) covers every loader, not only the server's, so this early,
    pre-bootstrap read is gated the same way `initialize_runtime` gates its
    own later one. Never from inside a vault, for the same reason: a `.env`
    a remote writer planted there must not configure even resource policy.
    """
    if os.environ.get("EXOMEM_HOSTED_CELL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    from . import cloud_cell

    if cloud_cell.cloud_mode_enabled():
        return
    from .dotenv_guard import working_directory_dotenv

    dotenv_path = working_directory_dotenv()
    if dotenv_path is None or not dotenv_path.is_file():
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


def configure_onnx_session_options(options: Any, *, default_threads: int | None = None) -> None:
    """Make ONNX's otherwise independent pools obey the common budget.

    `default_threads` is a session's own default while `EXOMEM_CPU_THREADS` is
    unset, never more than the CPUs this process may use; an explicit budget
    always wins.
    """
    policy = resolve_policy()
    threads = policy.cpu_threads
    from . import cloud_cell

    if default_threads is not None and policy.cpu_source == "default" and cloud_cell.resource_policy() != "service-v1":
        threads = max(1, min(default_threads, effective_online_cpus()))
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1


def onnx_share_weights_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether served ONNX sessions should retain file-backed shared weights.

    Cloud cells opt in by default because several isolated processes share one
    node. A personal or hosted server keeps ONNX Runtime's prepacked fast path.
    The explicit binary override wins when valid. Empty values are unset;
    malformed values warn and fall back to the deployment-mode default.
    """
    values = os.environ if env is None else env
    from . import cloud_cell

    hosted = str(values.get("EXOMEM_HOSTED_CELL", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    default = cloud_cell.cloud_mode_enabled(values) and not hosted
    raw = values.get(ONNX_SHARE_WEIGHTS_ENV)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip()
    if value not in {"0", "1"}:
        log.warning(
            "invalid %s=%r; using deployment default %s",
            ONNX_SHARE_WEIGHTS_ENV,
            raw,
            default,
        )
        return default
    return value == "1"


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
    """Bounded model admission with serialized, owner-thread-reentrant execution.

    Admission and execution are separate on purpose. A bulk encode is one unit of
    admitted work, so it holds one admission for its whole length, but it takes
    the execution slot one batch at a time (`admission` around several
    `execution` turns): between two batches, another caller runs.
    """

    def __init__(self, capacity: int) -> None:
        from . import cloud_cell

        self._fair = cloud_cell.resource_policy() == "service-v1"
        self._capacity = capacity
        self._admitted = threading.BoundedSemaphore(capacity)
        self._admission_lock = threading.Lock()
        self._admitted_count = 0
        self._execution = threading.RLock()
        self._local = threading.local()
        self._turns = threading.Condition()
        self._turn_owner: int | None = None
        self._turn_depth = 0
        self._waiters: list[tuple[object, str]] = []
        self._foreground_turns = 0

    @contextlib.contextmanager
    def admission(self):
        """Hold one admission, refusing rather than waiting when none is free.

        Reentrant for its owner thread: an execution turn inside a held
        admission is not admitted twice.
        """
        held = getattr(self._local, "admitted", 0)
        if held == 0:
            if not self._admitted.acquire(blocking=False):
                raise ModelBusyError("model compute is busy; retry shortly")
            with self._admission_lock:
                self._admitted_count += 1
        self._local.admitted = held + 1
        try:
            yield
        finally:
            self._local.admitted = held
            if held == 0:
                with self._admission_lock:
                    self._admitted_count -= 1
                self._admitted.release()

    @contextlib.contextmanager
    def execution(
        self, *, wait: bool = True, work_class: str | None = None,
        cancel_event: threading.Event | None = None,
    ):
        with self.admission():
            if self._fair:
                selected, cancellation = _MODEL_WORK.get()
                with self._fair_execution(
                    wait=wait, work_class=work_class or selected,
                    cancel_event=cancel_event if cancel_event is not None else cancellation,
                ):
                    yield
                return
            if not self._execution.acquire(blocking=wait):
                raise ModelBusyError("model compute is busy; retry shortly")
            try:
                yield
            finally:
                self._execution.release()

    def _next_waiter(self) -> object | None:
        foreground = next((token for token, kind in self._waiters if kind == "foreground"), None)
        bulk = next((token for token, kind in self._waiters if kind == "bulk"), None)
        if bulk is not None and (foreground is None or self._foreground_turns >= 8):
            return bulk
        return foreground

    @contextlib.contextmanager
    def _fair_execution(self, *, wait: bool, work_class: str, cancel_event: threading.Event | None):
        if work_class not in {"foreground", "bulk"}:
            raise ValueError("unknown model work class")
        owner = threading.get_ident()
        token = object()
        with self._turns:
            if self._turn_owner == owner:
                self._turn_depth += 1
            else:
                self._waiters.append((token, work_class))
                try:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise ModelBusyError("model compute is stopping")
                        if self._turn_owner is None and self._next_waiter() is token:
                            self._waiters.remove((token, work_class))
                            self._turn_owner, self._turn_depth = owner, 1
                            self._foreground_turns = (
                                self._foreground_turns + 1
                                if work_class == "foreground" and any(kind == "bulk" for _, kind in self._waiters)
                                else 0
                            )
                            break
                        if not wait:
                            raise ModelBusyError("model compute is busy; retry shortly")
                        self._turns.wait(timeout=0.1 if cancel_event is not None else None)
                finally:
                    if (token, work_class) in self._waiters:
                        self._waiters.remove((token, work_class))
                        self._turns.notify_all()
        try:
            yield
        finally:
            with self._turns:
                self._turn_depth -= 1
                if self._turn_depth == 0:
                    self._turn_owner = None
                    self._turns.notify_all()

    def waiting_counts(self) -> dict[str, int]:
        """Content-free fair waiters; each already occupies model admission."""
        with self._turns:
            return {kind: sum(item == kind for _, item in self._waiters) for kind in ("foreground", "bulk")}

    def admitted_count(self) -> int:
        with self._admission_lock:
            return self._admitted_count


_gate_lock = threading.Lock()
_gate: ModelAdmissionGate | None = None
_gate_capacity: int | None = None
_gate_profile: str | None = None


def _process_gate() -> ModelAdmissionGate:
    global _gate, _gate_capacity, _gate_profile
    from . import cloud_cell

    capacity = resolve_policy().model_admission
    profile = cloud_cell.resource_policy()
    with _gate_lock:
        if _gate is None or _gate_capacity != capacity or _gate_profile != profile:
            _gate = ModelAdmissionGate(capacity)
            _gate_capacity = capacity
            _gate_profile = profile
        return _gate


def model_execution(*, wait: bool = True):
    """Return the process-wide model gate for an embedding, reranker, CLIP, or ASR call."""
    return _process_gate().execution(wait=wait)


def model_admission():
    """One process-wide admission held across a bulk encode's execution turns."""
    return _process_gate().admission()


@contextlib.contextmanager
def model_work(work_class: str, *, cancel_event: threading.Event | None = None):
    """Classify one existing caller, without widening its admission budget."""
    if work_class not in {"foreground", "bulk"}:
        raise ValueError("unknown model work class")
    token = _MODEL_WORK.set((work_class, cancel_event))
    try:
        yield
    finally:
        _MODEL_WORK.reset(token)


class PreparationBudgetExceeded(RuntimeError):
    """This parent cannot be prepared inside the deployment envelope."""


class PreparationCapacityBusy(RuntimeError):
    """Another admitted parent temporarily occupies preparation capacity."""


def _source_line_bound(source: str) -> int:
    # Match str.splitlines' separators without allocating its line list before
    # the parser reservation. CRLF has already been normalized on input.
    return 1 + sum(source.count(marker) for marker in (
        "\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"
    ))


def _yaml_expanded_cost(text: str, *, maximum: int) -> int:
    """Bound alias/merge expansion from parser events before construction.

    libyaml constructors flatten repeated merges recursively. Byte size alone
    cannot bound that work. Count expanded node/scalar weight, allowing ordinary
    aliases while refusing cyclic or over-budget graphs without constructing
    their objects. This is a budget check, not another YAML parser.
    """
    import yaml

    from . import vault

    anchors: dict[str, int] = {}
    frames: list[tuple[str | None, int]] = []
    total = 0
    try:
        for event in yaml.parse(text, Loader=vault._YAML_SAFE_LOADER):
            anchor = getattr(event, "anchor", None)
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                if anchor:
                    anchors.pop(anchor, None)
                frames.append((anchor, 512))
                continue
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                anchor, cost = frames.pop()
            elif isinstance(event, yaml.ScalarEvent):
                cost = 512 + 4 * len(event.value.encode("utf-8"))
            elif isinstance(event, yaml.AliasEvent):
                if anchor not in anchors:
                    raise PreparationBudgetExceeded("semantic YAML expansion is unbounded")
                cost = anchors[anchor]
                anchor = None
            else:
                continue
            if anchor:
                anchors[anchor] = cost
            if frames:
                parent, weight = frames[-1]
                frames[-1] = parent, weight + cost
                used = weight + cost
            else:
                total += cost
                used = total
            if used > maximum:
                raise PreparationBudgetExceeded("semantic YAML expansion budget exceeded")
    except yaml.YAMLError:
        # The existing safe parser owns malformed-YAML handling. Nothing was
        # constructed here; its byte/line allowance remains reserved.
        return 0
    return total


class SemanticPreparation:
    """One checked parent reservation, including optional old-vector reuse.

    This bounds transient preparation, not native inference or corpus caches;
    those retain their existing controls and measured cgroup acceptance.
    """

    def __init__(self, source_bytes: int, *, bulk: bool) -> None:
        self.limit = (
            SEMANTIC_PREPARATION_BYTES - SEMANTIC_SMALL_RESERVE_BYTES
            if bulk else SEMANTIC_SMALL_RESERVE_BYTES
        )
        self.reuse_allowance = (4 if bulk else 1) * 1024 * 1024
        self.reserved = 0
        self.source = ""
        self.signature: Any = None
        self.guard: Any = None
        self.source_bytes = source_bytes
        self.parser_bytes = 0

    def reserve(self, amount: int) -> None:
        global _preparation_bytes, _preparation_peak
        with _preparation_lock:
            extra = max(0, amount - self.reserved)
            if amount > self.limit:
                raise PreparationBudgetExceeded("semantic preparation budget exceeded")
            if _preparation_bytes + extra > SEMANTIC_PREPARATION_BYTES:
                raise PreparationCapacityBusy("semantic preparation capacity busy")
            self.reserved += extra
            _preparation_bytes += extra
            _preparation_peak = max(_preparation_peak, _preparation_bytes)

    def check_page(self, page: Any, *, vector_dim: int) -> None:
        # Before the existing chunker/parser allocates: count an upper bound
        # of paragraphs, hard-split pieces and one possible unit per line.
        # Title duplication is charged at UTF-8 width; strings can use four
        # bytes per codepoint. Eight vector copies cover retained encode parts,
        # concatenation/reuse assembly, both projections and publication blobs.
        lines = _source_line_bound(self.source)
        body = page.body or ""
        chunks = body.count("\n") + (len(body) + 249) // 250 + 1
        text_bytes = len(body.encode("utf-8")) + chunks * len((page.title or "").encode("utf-8"))
        self.reserve(
            self.parser_bytes + 4 * text_bytes
            + (chunks + lines) * (4096 + 8 * max(1, vector_dim) * 4)
            + self.reuse_allowance
        )

    def release(self) -> None:
        global _preparation_bytes
        with _preparation_lock:
            _preparation_bytes -= self.reserved
            self.reserved = 0


@contextlib.contextmanager
def semantic_preparation(vault_root: Path, path: Path):
    """Reserve before source/parse allocations; never wait for memory capacity."""
    from . import find_corpus, freshness, vault

    signature = freshness.stat_signature(path)
    source_bytes = path.stat().st_size
    bulk = source_bytes > 1024 or _MODEL_WORK.get()[0] == "bulk"
    preparation = SemanticPreparation(source_bytes, bulk=bulk)
    try:
        if source_bytes > SEMANTIC_SOURCE_MAX_BYTES:
            raise PreparationBudgetExceeded("semantic source budget exceeded")
        preparation.reserve(8 * source_bytes + 65536)
        source, preparation.guard = vault.read_bounded_guarded_bytes(
            vault_root, path.relative_to(vault_root).as_posix(), limit=source_bytes
        )
        if len(source) != source_bytes or freshness.stat_signature(path) != signature:
            raise OSError("semantic input changed during preparation")
        preparation.source = source.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        preparation.signature = signature
        # Dense inline YAML has many nodes even on one line. Charge before
        # either existing page/unit parser constructs them. The conservative
        # 512-byte-per-input-byte charge includes simultaneous parser copies;
        # measured dense valid frontmatter needed over 120 bytes per byte for
        # just one parse. Body parsing remains separately line-bounded.
        frontmatter = find_corpus.FRONTMATTER_PATTERN.match(preparation.source)
        yaml_bytes = len(frontmatter.group(1).encode("utf-8")) if frontmatter else 0
        preparation.parser_bytes = (
            8 * source_bytes + 4096 * _source_line_bound(preparation.source) + 512 * yaml_bytes
        )
        preparation.reserve(preparation.parser_bytes)
        if frontmatter:
            expanded = _yaml_expanded_cost(
                frontmatter.group(1), maximum=(preparation.limit - preparation.parser_bytes) // 2
            )
            preparation.parser_bytes += 2 * expanded
            preparation.reserve(preparation.parser_bytes)
        yield preparation
    finally:
        preparation.release()


def semantic_preparation_status() -> dict[str, int]:
    with _preparation_lock:
        return {
            "budget_bytes": SEMANTIC_PREPARATION_BYTES,
            "small_reserve_bytes": SEMANTIC_SMALL_RESERVE_BYTES,
            "source_max_bytes": SEMANTIC_SOURCE_MAX_BYTES,
            "reserved_bytes": _preparation_bytes,
            "peak_reserved_bytes": _preparation_peak,
        }


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
        "semantic_preparation": semantic_preparation_status(),
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
