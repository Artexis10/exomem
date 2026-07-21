"""Content-free runtime admission metadata for HA and hosted orchestration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

RUNTIME_CONTRACT = 1
HTTP_TRANSPORT = "streamable-http-stateless"


def _public_mutation_boundary(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("state") != "held":
        return {"state": "free"}
    return {
        "state": "held",
        "request_id": str(value.get("request_id") or "untracked"),
        "operation": str(value.get("operation") or "unknown"),
        "holder_kind": str(value.get("holder_kind") or "unknown"),
        "age_seconds": float(value.get("age_seconds") or 0.0),
        "overdue": bool(value.get("overdue")),
    }


def package_release() -> str:
    """Return the installed distribution release without making readiness fragile."""
    try:
        return version("exomem")
    except PackageNotFoundError:
        return "0+unknown"
    except Exception:  # noqa: BLE001 - metadata failure must become diagnostic state
        return "0+unknown"


def build_runtime_readiness(
    *,
    coordination: Mapping[str, Any],
    release: str,
    mcp_tool_surface_sha256: str | None,
    session_store: Mapping[str, Any] | None = None,
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
    return {
        "status": "ready" if takeover_eligible else "not_ready",
        "service": "exomem",
        "release": release,
        "mcp_tool_surface_sha256": mcp_tool_surface_sha256,
        "runtime_contract": RUNTIME_CONTRACT,
        "transport": HTTP_TRANSPORT,
        "replica_id": replica_id,
        "coordination": {
            "enabled": enabled,
            "role": role,
            "coordinator_healthy": healthy,
            "mutation_boundary": _public_mutation_boundary(
                coordination.get("mutation_boundary")
            ),
        },
        "session_store": {
            "state": session_store_state,
            "stale_served_count": stale_served_count,
        },
        "takeover_eligible": takeover_eligible,
        "reasons": reasons,
    }


def runtime_readiness(*, mcp_tool_surface_sha256: str | None) -> dict[str, Any]:
    """Measure this process's eligibility without exposing vault or credential state."""
    from .session_validation_cache import session_store_readiness
    from .writer_lease import coordination_status

    try:
        coordination = coordination_status()
    except Exception:  # noqa: BLE001 - readiness must return structured 503 state
        coordination = {
            "enabled": bool(os.environ.get("EXOMEM_WRITER_LEASE_URL", "").strip()),
            "role": "unknown",
            "replica_id": os.environ.get("EXOMEM_WRITER_LEASE_REPLICA_ID") or None,
            "coordinator_healthy": False,
        }
    return build_runtime_readiness(
        coordination=coordination,
        release=package_release(),
        mcp_tool_surface_sha256=mcp_tool_surface_sha256,
        session_store=session_store_readiness(),
    )
