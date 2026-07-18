"""Content-free runtime admission metadata for HA and hosted orchestration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

RUNTIME_CONTRACT = 1
HTTP_TRANSPORT = "streamable-http-stateless"


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
        },
        "takeover_eligible": takeover_eligible,
        "reasons": reasons,
    }


def runtime_readiness(*, mcp_tool_surface_sha256: str | None) -> dict[str, Any]:
    """Measure this process's eligibility without exposing vault or credential state."""
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
    )
