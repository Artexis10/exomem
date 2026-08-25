"""Content-free runtime admission metadata for HA and hosted orchestration."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

RUNTIME_CONTRACT = 1
HTTP_TRANSPORT = "streamable-http-stateless"

log = logging.getLogger(__name__)

# The incident behind #581 lasted roughly three days, and its stale poller hit
# readiness every few seconds.  A full day avoids treating an ordinary quiet
# night on a personal KB as an outage; 288 probes prove a sustained average
# cadence of at least one probe per five minutes rather than one late burst.
SILENT_TRAFFIC_WINDOW_SECONDS = 24 * 60 * 60
SILENT_TRAFFIC_MINIMUM_HEALTH_PROBES = 288
# A 2.4k-note Windows vault needs about 0.27 seconds to read its mutation
# boundary plus graph epoch under memory pressure.  The former 0.25-second
# ceiling therefore rejected ordinary work and made successive readiness
# probes alternate 503/200 as the second probe reused the first one's late
# result.  Keep a hard sub-second bound, with enough headroom for the measured
# steady state; genuinely blocked graph publication still fails closed.
COORDINATION_STATUS_TIMEOUT_SECONDS = 0.75

_COORDINATION_PROBES_LOCK = threading.Lock()
_COORDINATION_PROBES: dict[str, tuple[threading.Event, dict[str, object]]] = {}


class SilentTrafficMonitor:
    """Process-local relationship between health probes and successful tools."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = SILENT_TRAFFIC_WINDOW_SECONDS,
        minimum_health_probes: int = SILENT_TRAFFIC_MINIMUM_HEALTH_PROBES,
    ) -> None:
        self.clock = clock
        self.window_seconds = float(window_seconds)
        self.minimum_health_probes = int(minimum_health_probes)
        self.process_started_at = float(clock())
        self.last_successful_tool_call_at: float | None = None
        self.last_health_probe_at: float | None = None
        self.successful_tool_call_count = 0
        self.health_probe_count = 0
        self._health_probe_count_at_last_tool_call = 0
        self._first_health_probe_after_tool_call_at: float | None = None
        self._suspected_silent_outage = False
        self._lock = threading.Lock()

    def record_health_probe(self) -> dict[str, Any]:
        with self._lock:
            now = float(self.clock())
            self.last_health_probe_at = now
            self.health_probe_count += 1
            if self._first_health_probe_after_tool_call_at is None:
                self._first_health_probe_after_tool_call_at = now
            self._enter_suspicious_state_if_due(now)
            return self._snapshot(now)

    def record_successful_tool_call(self) -> dict[str, Any]:
        with self._lock:
            now = float(self.clock())
            self.last_successful_tool_call_at = now
            self.successful_tool_call_count += 1
            self._health_probe_count_at_last_tool_call = self.health_probe_count
            self._first_health_probe_after_tool_call_at = None
            if self._suspected_silent_outage:
                self._suspected_silent_outage = False
                log.info(
                    "event=silent_traffic_outage_cleared successful MCP tool "
                    "traffic reached the origin; the edge, tunnel, and route are "
                    "delivering calls again"
                )
            return self._snapshot(now)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot(float(self.clock()))

    def _enter_suspicious_state_if_due(self, now: float) -> None:
        first_probe = self._first_health_probe_after_tool_call_at
        probes = self.health_probe_count - self._health_probe_count_at_last_tool_call
        if (
            self._suspected_silent_outage
            or first_probe is None
            or probes < self.minimum_health_probes
            or now - first_probe < self.window_seconds
        ):
            return
        self._suspected_silent_outage = True
        log.warning(
            "event=silent_traffic_outage_suspected health probes have continued "
            "for %.0fs (%d probes) without a successful MCP tool call; check the "
            "edge, tunnel, and route",
            max(0.0, now - first_probe),
            probes,
        )

    def _snapshot(self, now: float) -> dict[str, Any]:
        first_probe = self._first_health_probe_after_tool_call_at
        last_tool = self.last_successful_tool_call_at
        last_probe = self.last_health_probe_at
        return {
            "suspected_silent_outage": self._suspected_silent_outage,
            "successful_tool_call_count": self.successful_tool_call_count,
            "health_probe_count": self.health_probe_count,
            "health_probes_since_last_tool_call": (
                self.health_probe_count - self._health_probe_count_at_last_tool_call
            ),
            "seconds_without_successful_tool_call": round(
                max(
                    0.0,
                    now
                    - (
                        last_tool
                        if last_tool is not None
                        else self.process_started_at
                    ),
                ),
                3,
            ),
            "probe_window_seconds": round(
                max(0.0, now - first_probe) if first_probe is not None else 0.0,
                3,
            ),
            "last_successful_tool_call_age_seconds": (
                round(max(0.0, now - last_tool), 3) if last_tool is not None else None
            ),
            "last_health_probe_age_seconds": (
                round(max(0.0, now - last_probe), 3) if last_probe is not None else None
            ),
            "window_seconds": self.window_seconds,
            "minimum_health_probes": self.minimum_health_probes,
        }


_SILENT_TRAFFIC_MONITOR = SilentTrafficMonitor()


def get_silent_traffic_monitor() -> SilentTrafficMonitor:
    return _SILENT_TRAFFIC_MONITOR


def _instance_id() -> str | None:
    value = os.environ.get("EXOMEM_INSTANCE_ID", "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        return value
    return None


_SAFE_READINESS_LABEL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _safe_readiness_label(value: object, *, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_READINESS_LABEL.fullmatch(candidate) else fallback


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _public_last_holder(value: object) -> dict[str, Any] | None:
    """Project the last-known holder into content-free, allowlisted fields.

    `pid` is deliberately NOT projected here.  `/health/ready` is an
    unauthenticated surface documented as content-free and identity-free, and a
    process id is host process metadata.  It stays in the MUTATION_BUSY error
    payload, where it is attribution handed to an authenticated caller who just
    lost the boundary.
    """
    if not isinstance(value, Mapping):
        return None
    observed_at = value.get("observed_at")
    source = value.get("source")
    return {
        "request_id": _safe_readiness_label(
            value.get("request_id"), fallback="untracked"
        ),
        "operation": _safe_readiness_label(value.get("operation"), fallback="unknown"),
        "holder_kind": _safe_readiness_label(
            value.get("holder_kind"), fallback="unknown"
        ),
        "observed_at": float(observed_at)
        if isinstance(observed_at, (int, float)) and not isinstance(observed_at, bool)
        else 0.0,
        "source": source if source in {"refusal", "release"} else "unknown",
    }


def _public_contention(value: object) -> dict[str, Any] | None:
    """Project the boundary's contention counters, dropping anything unmodelled.

    The flag alone cannot show a bounded waiter starving against a stream of
    short holds — every one-shot probe lands in a gap.  These counters can, so
    they ride along with every boundary state.
    """
    if not isinstance(value, Mapping):
        return None
    window = value.get("recent_window_seconds")
    return {
        "acquire_attempts": _non_negative_int(value.get("acquire_attempts")),
        "busy_refusals": _non_negative_int(value.get("busy_refusals")),
        "busy_refusals_recent": _non_negative_int(value.get("busy_refusals_recent")),
        "recent_window_seconds": float(window)
        if isinstance(window, (int, float)) and not isinstance(window, bool)
        else 0.0,
        # Stated, not implied: the counters describe this process only, so a
        # zero refusal count is not evidence that the boundary is uncontended.
        "scope": "process_local",
        "last_holder": _public_last_holder(value.get("last_holder")),
    }


def _public_mutation_boundary(value: object) -> dict[str, Any]:
    """Project the coordination boundary into three honest public states.

    - `free`: a probe ran and verified the boundary is not held.
    - `held`: a probe found a holder; the bounded, content-free holder block
      rides along (`verified` distinguishes a confirmed holder from one read
      without the metadata mutex).
    - `unknown`: this process could not determine the boundary's state, with a
      `reason` naming why (`process_local_only` when no vault identity was
      configured so only this process's own holds were visible,
      `status_error` when the coordination probe itself failed,
      `status_timeout` when it did not complete inside the readiness budget,
      `unavailable` when no boundary block was reported at all).

    A missing or unrecognised block is `unknown`, never `free`.  "We did not
    measure" and "we measured free" are different claims, and publishing the
    first as the second is what let `/health/ready` report a free boundary
    while live writes were being refused MUTATION_BUSY by a real concurrent
    holder in another process.
    """
    if not isinstance(value, Mapping):
        return {"state": "unknown", "reason": "unavailable"}
    state = value.get("state")
    if state == "held":
        public: dict[str, Any] = {
            "state": "held",
            "request_id": str(value.get("request_id") or "untracked"),
            "operation": str(value.get("operation") or "unknown"),
            "holder_kind": str(value.get("holder_kind") or "unknown"),
            "age_seconds": float(value.get("age_seconds") or 0.0),
            "overdue": bool(value.get("overdue")),
            "verified": bool(value.get("verified")),
        }
    elif state == "free":
        public = {"state": "free"}
    else:
        public = {
            "state": "unknown",
            "reason": _safe_readiness_label(
                value.get("reason"), fallback="unspecified"
            ),
        }
    contention = _public_contention(value.get("contention"))
    if contention is not None:
        public["contention"] = contention
    return public


def _public_graph_sync(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    state = value.get("state")
    generation = value.get("generation")
    if state not in {"current", "recovery_required", "unavailable"}:
        return None
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return None
    return {"state": state, "generation": generation}


def package_release() -> str:
    """Return the installed distribution release without making readiness fragile."""
    try:
        return version("exomem")
    except PackageNotFoundError:
        return "0+unknown"
    except Exception:  # noqa: BLE001 - metadata failure must become diagnostic state
        return "0+unknown"


_DEFAULT_OBSERVABILITY: dict[str, Any] = {
    "log_dir_writable": None,
    "metrics_snapshot_age_seconds": None,
    "journal_ok": None,
}


def build_runtime_readiness(
    *,
    coordination: Mapping[str, Any],
    release: str,
    mcp_tool_surface_sha256: str | None,
    session_store: Mapping[str, Any] | None = None,
    observability: Mapping[str, Any] | None = None,
    traffic: Mapping[str, Any] | None = None,
    retrieval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the public readiness payload from already-measured coordination state."""
    enabled = bool(coordination.get("enabled"))
    healthy = bool(coordination.get("coordinator_healthy"))
    role = str(coordination.get("role") or "unknown")
    replica_raw = coordination.get("replica_id")
    replica_id = replica_raw if isinstance(replica_raw, str) and replica_raw else None

    reasons: list[str] = []
    if mcp_tool_surface_sha256 is None:
        reasons.append("mcp_tool_surface_unavailable")
    if coordination.get("status_timed_out") is True:
        reasons.append("coordination_status_timeout")
    if enabled:
        if not healthy:
            reasons.append("coordinator_unavailable")
        if role not in {"writer", "follower"}:
            reasons.append("coordination_role_unknown")
        if replica_id is None:
            reasons.append("replica_identity_missing")

    retrieval_payload: dict[str, object] | None = None
    retrieval_admitted = True
    if retrieval is not None:
        state = str(retrieval.get("state") or "unverified")
        if state not in {"ready", "warming", "unavailable", "unverified"}:
            state = "unverified"
        retrieval_admitted = bool(retrieval.get("admitted")) and state == "ready"
        retrieval_payload = {"state": state, "admitted": retrieval_admitted}
        if not retrieval_admitted:
            reasons.append(f"retrieval_{state}")

    takeover_eligible = not reasons
    session_store_state = (
        "degraded"
        if session_store and session_store.get("state") == "degraded"
        else "ok"
    )
    raw_stale_count = session_store.get("stale_served_count", 0) if session_store else 0
    stale_served_count = (
        raw_stale_count
        if isinstance(raw_stale_count, int) and raw_stale_count >= 0
        else 0
    )
    coordination_payload = {
        "enabled": enabled,
        "role": role,
        "coordinator_healthy": healthy,
        "mutation_boundary": _public_mutation_boundary(
            coordination.get("mutation_boundary")
        ),
    }
    graph_sync = _public_graph_sync(coordination.get("graph_sync"))
    if graph_sync is not None:
        coordination_payload["graph_sync"] = graph_sync
    payload = {
        "status": "ready" if takeover_eligible and retrieval_admitted else "not_ready",
        "service": "exomem",
        "release": release,
        "mcp_tool_surface_sha256": mcp_tool_surface_sha256,
        "runtime_contract": RUNTIME_CONTRACT,
        "transport": HTTP_TRANSPORT,
        "instance_id": _instance_id(),
        "replica_id": replica_id,
        "coordination": coordination_payload,
        "session_store": {
            "state": session_store_state,
            "stale_served_count": stale_served_count,
        },
        "observability": dict(observability) if observability is not None else dict(_DEFAULT_OBSERVABILITY),
        "takeover_eligible": takeover_eligible,
        "reasons": reasons,
    }
    if retrieval_payload is not None:
        payload["retrieval"] = retrieval_payload
    if traffic is not None:
        payload["traffic"] = dict(traffic)
    return payload


def _measure_observability() -> dict[str, Any]:
    """Measure log-dir writability, metrics-snapshot freshness, and journal
    health, without ever raising into readiness."""
    log_dir_writable: bool | None = None
    metrics_snapshot_age_seconds: float | None = None
    journal_ok: bool | None = None
    try:
        from .logging_config import resolve_log_dir

        log_dir = resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / f".exomem-writable-probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        log_dir_writable = True
    except Exception:  # noqa: BLE001 - readiness must stay structured
        log_dir_writable = False
    try:
        from . import metrics
        from .writer_lease import get_manager

        snapshot_path = metrics.snapshot_path(get_manager().config.state_dir)
        if snapshot_path.exists():
            metrics_snapshot_age_seconds = round(
                time.time() - snapshot_path.stat().st_mtime, 3
            )
    except Exception:  # noqa: BLE001 - readiness must stay structured
        metrics_snapshot_age_seconds = None
    try:
        from .mutation_journal import journal_path

        path = journal_path()
        if not path.exists():
            journal_ok = True
        else:
            last_line = path.read_text(encoding="utf-8").splitlines()[-1:] or [""]
            import json as _json

            _json.loads(last_line[0]) if last_line[0].strip() else None
            journal_ok = True
    except Exception:  # noqa: BLE001 - readiness must stay structured
        journal_ok = False
    return {
        "log_dir_writable": log_dir_writable,
        "metrics_snapshot_age_seconds": metrics_snapshot_age_seconds,
        "journal_ok": journal_ok,
    }


def _bounded_coordination_status(
    vault_root: Path | None,
    probe: Callable[[Path | None], Mapping[str, Any]],
    *,
    timeout_seconds: float = COORDINATION_STATUS_TIMEOUT_SECONDS,
) -> Mapping[str, Any] | None:
    """Run at most one status probe per vault and fail closed on its deadline.

    The underlying diagnostic traverses graph state and can encounter a live
    publication hold.  Readiness cannot inherit that unbounded wait: the public
    endpoint must answer within the edge budget, and repeated health polls must
    not create an unbounded thread pile while the first probe is still blocked.
    """
    key = (
        "<process-local>"
        if vault_root is None
        else os.path.normcase(str(vault_root.resolve(strict=False)))
    )
    with _COORDINATION_PROBES_LOCK:
        current = _COORDINATION_PROBES.get(key)
        if current is None:
            completed = threading.Event()
            result: dict[str, object] = {}
            current = (completed, result)
            _COORDINATION_PROBES[key] = current

            def run_probe() -> None:
                try:
                    result["value"] = probe(vault_root)
                except Exception as error:  # noqa: BLE001 - re-raised on request thread
                    result["error"] = error
                finally:
                    completed.set()

            threading.Thread(
                target=run_probe,
                name="exomem-readiness-coordination",
                daemon=True,
            ).start()

    completed, result = current
    if not completed.wait(timeout_seconds):
        return None
    with _COORDINATION_PROBES_LOCK:
        if _COORDINATION_PROBES.get(key) is current:
            _COORDINATION_PROBES.pop(key, None)
    error = result.get("error")
    if isinstance(error, Exception):
        raise error
    value = result.get("value")
    if not isinstance(value, Mapping):
        raise RuntimeError("coordination status returned an invalid payload")
    return value


def runtime_readiness(
    *,
    mcp_tool_surface_sha256: str | None,
    traffic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure this process's eligibility without exposing vault or credential state."""
    from . import readiness
    from .session_validation_cache import session_store_readiness
    from .writer_lease import coordination_status

    coordination: Mapping[str, Any]
    try:
        configured_raw = os.environ.get("EXOMEM_VAULT_PATH", "").strip()
        # Only an absolute path names a boundary this process can probe. A
        # relative one resolves against whatever the service's cwd happens to
        # be, so it would measure some other boundary and publish that as this
        # vault's state; that is a blind "free", which is the failure being
        # fixed here. Fall through to the process-local `unknown` instead.
        configured_vault = (
            Path(configured_raw)
            if configured_raw and Path(configured_raw).is_absolute()
            else None
        )
        measured = _bounded_coordination_status(
            configured_vault,
            coordination_status,
        )
        if measured is None:
            coordination = {
                "enabled": bool(os.environ.get("EXOMEM_WRITER_LEASE_URL", "").strip()),
                "role": "unknown",
                "replica_id": os.environ.get("EXOMEM_WRITER_LEASE_REPLICA_ID") or None,
                "coordinator_healthy": False,
                "status_timed_out": True,
                "mutation_boundary": {
                    "state": "unknown",
                    "reason": "status_timeout",
                },
            }
        else:
            coordination = measured
    except Exception:  # noqa: BLE001 - readiness must return structured 503 state
        coordination = {
            "enabled": bool(os.environ.get("EXOMEM_WRITER_LEASE_URL", "").strip()),
            "role": "unknown",
            "replica_id": os.environ.get("EXOMEM_WRITER_LEASE_REPLICA_ID") or None,
            "coordinator_healthy": False,
            # The probe failed; the boundary was not measured. Saying "free"
            # here is what made MUTATION_LOCK_UNAVAILABLE read as healthy.
            "mutation_boundary": {"state": "unknown", "reason": "status_error"},
        }
    return build_runtime_readiness(
        coordination=coordination,
        release=package_release(),
        mcp_tool_surface_sha256=mcp_tool_surface_sha256,
        session_store=session_store_readiness(),
        observability=_measure_observability(),
        traffic=(
            traffic
            if traffic is not None
            else get_silent_traffic_monitor().snapshot()
        ),
        retrieval=readiness.retrieval_admission(),
    )
