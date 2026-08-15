"""Content-free runtime admission metadata for HA and hosted orchestration."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

RUNTIME_CONTRACT = 1
HTTP_TRANSPORT = "streamable-http-stateless"


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
    """Project the last-known holder into content-free, allowlisted fields."""
    if not isinstance(value, Mapping):
        return None
    pid = value.get("pid")
    observed_at = value.get("observed_at")
    source = value.get("source")
    return {
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
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
    if enabled:
        if not healthy:
            reasons.append("coordinator_unavailable")
        if role not in {"writer", "follower"}:
            reasons.append("coordination_role_unknown")
        if replica_id is None:
            reasons.append("replica_identity_missing")

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
    return {
        "status": "ready" if takeover_eligible else "not_ready",
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


def runtime_readiness(*, mcp_tool_surface_sha256: str | None) -> dict[str, Any]:
    """Measure this process's eligibility without exposing vault or credential state."""
    from .session_validation_cache import session_store_readiness
    from .writer_lease import coordination_status

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
        coordination = coordination_status(configured_vault)
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
    )
