from __future__ import annotations

from pathlib import Path

import pytest

from exomem.runtime_readiness import (
    HTTP_TRANSPORT,
    RUNTIME_CONTRACT,
    build_runtime_readiness,
)


def test_standalone_runtime_is_ready_without_multi_host_coordination() -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="a" * 64,
    )

    fingerprint = snapshot.pop("mcp_tool_surface_sha256", None)
    assert isinstance(fingerprint, str)
    assert fingerprint == "a" * 64

    assert snapshot == {
        "status": "ready",
        "service": "exomem",
        "release": "1.2.3",
        "runtime_contract": RUNTIME_CONTRACT,
        "transport": HTTP_TRANSPORT,
        "replica_id": None,
        "coordination": {
            "enabled": False,
            "role": "standalone",
            "coordinator_healthy": True,
            "mutation_boundary": {"state": "free"},
        },
        "session_store": {"state": "ok", "stale_served_count": 0},
        "takeover_eligible": True,
        "reasons": [],
    }


def test_healthy_coordinated_follower_is_takeover_eligible() -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": True,
            "role": "follower",
            "vault_id": "must-not-leak",
            "replica_id": "laptop",
            "holder": "desktop",
            "fencing_token": 9,
            "coordinator_healthy": True,
        },
        release="1.2.4",
        mcp_tool_surface_sha256="b" * 64,
    )

    assert snapshot["status"] == "ready"
    assert snapshot["takeover_eligible"] is True
    assert snapshot["replica_id"] == "laptop"
    assert snapshot["coordination"] == {
        "enabled": True,
        "role": "follower",
        "coordinator_healthy": True,
        "mutation_boundary": {"state": "free"},
    }
    rendered = repr(snapshot).lower()
    assert "must-not-leak" not in rendered
    assert "vault_id" not in rendered
    assert "fencing_token" not in rendered


def test_coordinator_outage_is_not_ready_and_uses_stable_reason() -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": True,
            "role": "unknown",
            "replica_id": "desktop",
            "coordinator_healthy": False,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="c" * 64,
    )

    assert snapshot["status"] == "not_ready"
    assert snapshot["takeover_eligible"] is False
    assert snapshot["reasons"] == ["coordinator_unavailable", "coordination_role_unknown"]


def test_missing_replica_identity_is_not_ready_when_coordination_enabled() -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": True,
            "role": "follower",
            "replica_id": None,
            "coordinator_healthy": True,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="d" * 64,
    )

    assert snapshot["status"] == "not_ready"
    assert snapshot["reasons"] == ["replica_identity_missing"]


def test_readiness_exposes_only_bounded_mutation_holder_metadata() -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
            "mutation_boundary": {
                "state": "held",
                "request_id": "req-ready",
                "operation": "edit_memory",
                "holder_kind": "command",
                "age_seconds": 31.2,
                "overdue": True,
                "verified": True,
                "vault_path": "must-not-leak",
                "credential": "must-not-leak-either",
                "tenant_id": "tenant-must-not-leak",
            },
        },
        release="1.2.3",
        mcp_tool_surface_sha256="e" * 64,
    )

    assert snapshot["coordination"]["mutation_boundary"] == {
        "state": "held",
        "request_id": "req-ready",
        "operation": "edit_memory",
        "holder_kind": "command",
        "age_seconds": 31.2,
        "overdue": True,
        "verified": True,
    }
    assert "must-not-leak" not in repr(snapshot)


def test_runtime_readiness_measures_the_configured_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_readiness as readiness_module
    from exomem import writer_lease

    vault = tmp_path / "configured-vault"
    observed: list[Path | None] = []

    def fake_coordination_status(vault_root=None):  # noqa: ANN001
        observed.append(Path(vault_root) if vault_root is not None else None)
        return {
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
            "mutation_boundary": {"state": "free"},
        }

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(writer_lease, "coordination_status", fake_coordination_status)
    readiness_module.runtime_readiness(mcp_tool_surface_sha256="f" * 64)

    assert observed == [vault]


def test_runtime_readiness_uses_vault_path_identity_for_a_real_held_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_readiness as readiness_module
    from exomem import writer_lease

    vault = tmp_path / "vault"
    vault.mkdir()
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)

    with manager.mutation_guard(
        vault,
        request_id="req-readiness-held",
        operation="remember",
        holder_kind="command",
    ):
        snapshot = readiness_module.runtime_readiness(
            mcp_tool_surface_sha256="a" * 64
        )

    boundary = snapshot["coordination"]["mutation_boundary"]
    assert boundary["state"] == "held"
    assert boundary["verified"] is True
    assert boundary["request_id"] == "req-readiness-held"
