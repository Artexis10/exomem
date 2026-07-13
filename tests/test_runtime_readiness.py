from __future__ import annotations

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
    )

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
        },
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
    )

    assert snapshot["status"] == "ready"
    assert snapshot["takeover_eligible"] is True
    assert snapshot["replica_id"] == "laptop"
    assert snapshot["coordination"] == {
        "enabled": True,
        "role": "follower",
        "coordinator_healthy": True,
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
    )

    assert snapshot["status"] == "not_ready"
    assert snapshot["reasons"] == ["replica_identity_missing"]
