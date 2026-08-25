from __future__ import annotations

import threading
import time
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
        "instance_id": None,
        "replica_id": None,
        "coordination": {
            "enabled": False,
            "role": "standalone",
            "coordinator_healthy": True,
            # No boundary was measured, so readiness must not claim one is
            # free — see tests/test_readiness_honesty.py.
            "mutation_boundary": {"state": "unknown", "reason": "unavailable"},
        },
        "session_store": {"state": "ok", "stale_served_count": 0},
        "observability": {
            "log_dir_writable": None,
            "metrics_snapshot_age_seconds": None,
            "journal_ok": None,
        },
        "takeover_eligible": True,
        "reasons": [],
    }


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (" laptop-01 ", "laptop-01"),
        ("", None),
        ("not public safe!", None),
        ("x" * 65, None),
    ],
)
def test_readiness_exposes_only_a_valid_trimmed_instance_id(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: str | None
) -> None:
    from exomem import runtime_readiness as readiness_module

    monkeypatch.setenv("EXOMEM_INSTANCE_ID", configured)
    snapshot = readiness_module.build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="a" * 64,
    )

    assert snapshot["instance_id"] == expected


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
        "mutation_boundary": {"state": "unknown", "reason": "unavailable"},
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


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("warming", "retrieval_warming"),
        ("unavailable", "retrieval_unavailable"),
        ("unverified", "retrieval_unverified"),
    ],
)
def test_retrieval_admission_withholds_runtime_readiness(
    state: str, reason: str
) -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="d" * 64,
        retrieval={
            "state": state,
            "admitted": False,
            "vault_path": "must-not-leak",
            "query": "must-not-leak-either",
        },
    )

    assert snapshot["status"] == "not_ready"
    assert snapshot["takeover_eligible"] is False
    assert snapshot["retrieval"] == {"state": state, "admitted": False}
    assert snapshot["reasons"] == [reason]
    assert "must-not-leak" not in repr(snapshot)


def test_ready_retrieval_catalog_admits_runtime_readiness() -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="d" * 64,
        retrieval={"state": "ready", "admitted": True},
    )

    assert snapshot["status"] == "ready"
    assert snapshot["retrieval"] == {"state": "ready", "admitted": True}
    assert snapshot["reasons"] == []


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


def test_runtime_readiness_fails_closed_within_a_tight_bound_when_status_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import readiness, session_validation_cache, writer_lease
    from exomem import runtime_readiness as readiness_module

    vault = tmp_path / "blocked-vault"
    entered = threading.Event()
    release = threading.Event()

    def blocked_coordination_status(_vault_root=None):  # noqa: ANN001
        entered.set()
        assert release.wait(5)
        return {
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
            "mutation_boundary": {"state": "free"},
        }

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(writer_lease, "coordination_status", blocked_coordination_status)
    monkeypatch.setattr(session_validation_cache, "session_store_readiness", lambda: {})
    monkeypatch.setattr(readiness_module, "_measure_observability", lambda: {})
    readiness.mark_ready("retrieval_catalog")

    assert readiness_module.COORDINATION_STATUS_TIMEOUT_SECONDS < 1.0
    started = time.monotonic()
    try:
        snapshot = readiness_module.runtime_readiness(
            mcp_tool_surface_sha256="a" * 64,
            traffic={},
        )
    finally:
        release.set()

    assert entered.is_set()
    assert time.monotonic() - started < 1.25
    assert snapshot["status"] == "not_ready"
    assert snapshot["takeover_eligible"] is False
    assert snapshot["reasons"] == ["coordination_status_timeout"]
    assert snapshot["coordination"]["mutation_boundary"] == {
        "state": "unknown",
        "reason": "status_timeout",
    }


def test_runtime_readiness_admits_a_slow_but_bounded_real_vault_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal large-vault snapshot must not trip the fail-fast ceiling.

    The 2.4k-note Windows service takes about 0.27 seconds to read the mutation
    boundary and graph epoch under memory pressure.  The former 0.25-second
    ceiling therefore alternated 503 and 200 as each late result was reused by
    the following probe.  Preserve sub-second failure for a genuinely blocked
    snapshot while leaving measured steady-state work enough headroom.
    """
    from exomem import readiness, session_validation_cache, writer_lease
    from exomem import runtime_readiness as readiness_module

    vault = tmp_path / "large-vault"

    def slow_coordination_status(_vault_root=None):  # noqa: ANN001
        time.sleep(0.35)
        return {
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
            "mutation_boundary": {"state": "free"},
        }

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(writer_lease, "coordination_status", slow_coordination_status)
    monkeypatch.setattr(session_validation_cache, "session_store_readiness", lambda: {})
    monkeypatch.setattr(readiness_module, "_measure_observability", lambda: {})
    readiness.mark_ready("retrieval_catalog")

    started = time.monotonic()
    snapshot = readiness_module.runtime_readiness(
        mcp_tool_surface_sha256="a" * 64,
        traffic={},
    )

    assert time.monotonic() - started < 1.25
    assert snapshot["status"] == "ready"
    assert snapshot["takeover_eligible"] is True
    assert snapshot["reasons"] == []
    assert snapshot["coordination"]["mutation_boundary"] == {"state": "free"}


def test_bounded_coordination_status_is_single_flight_and_reuses_late_result(
    tmp_path: Path,
) -> None:
    from exomem import runtime_readiness as readiness_module

    vault = tmp_path / "single-flight-vault"
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0
    expected = {"enabled": False, "coordinator_healthy": True}

    def blocked_probe(_vault_root: Path | None):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        finished.set()
        return expected

    results: list[object] = []
    workers = [
        threading.Thread(
            target=lambda: results.append(
                readiness_module._bounded_coordination_status(
                    vault,
                    blocked_probe,
                    timeout_seconds=0.05,
                )
            )
        )
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    assert entered.wait(2)
    for worker in workers:
        worker.join(2)
    assert all(not worker.is_alive() for worker in workers)
    assert results == [None] * 4
    assert calls == 1

    release.set()
    assert finished.wait(2)

    assert readiness_module._bounded_coordination_status(
        vault,
        blocked_probe,
        timeout_seconds=0.5,
    ) == expected
    assert calls == 1
    assert readiness_module._bounded_coordination_status(
        vault,
        blocked_probe,
        timeout_seconds=0.5,
    ) == expected
    assert calls == 2


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


def test_coordination_graph_health_requires_a_readable_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Epoch state alone must not make readiness claim the graph is current."""
    from exomem import epistemic_graph, writer_lease
    from exomem import vault as vault_module

    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert manager.status(empty)["graph_sync"] == {"state": "unavailable", "generation": 0}
    from exomem import runtime_readiness as readiness_module

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(empty))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    readiness = readiness_module.runtime_readiness(mcp_tool_surface_sha256="a" * 64)
    assert readiness["coordination"]["graph_sync"] == {
        "state": "unavailable",
        "generation": 0,
    }
    assert str(empty) not in repr(readiness)

    missing = tmp_path / "missing"
    note = missing / "Knowledge Base/Notes/health.md"
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# Health\n")],
        vault_root=missing,
        post_commit_fanout=False,
    )
    assert manager.status(missing)["graph_sync"] == {"state": "recovery_required", "generation": 1}

    index = epistemic_graph.EpistemicGraphIndex(missing)
    index.rebuild_all()
    assert manager.status(missing)["graph_sync"] == {"state": "current", "generation": 1}

    epistemic_graph.sidecar_path(missing).write_bytes(b"not sqlite")
    snapshot = manager.status(missing)
    assert snapshot["graph_sync"] == {"state": "unavailable", "generation": 1}
    assert str(missing) not in repr(snapshot)
