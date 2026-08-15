"""Readiness must never claim a mutation boundary is free without measuring it.

Incident: three `edit_memory` calls were refused `409 MUTATION_BUSY` while
`/health/ready`, polled between them, reported
`mutation_boundary: {"state": "free"}`.  Two structural mechanisms made the two
surfaces disagree, and both are covered here:

1. the process-local blind spot (no configured vault -> the status only walks
   this process's own holds), and
2. the swallowed coordination-status exception (any failure was replaced by a
   dict with no boundary block, which projected as "free").

A third mechanism -- an instantaneous one-shot probe against a bounded,
queue-free acquire -- cannot be expressed by a flag at all, so the contention
counters and last-known-holder attribution asserted below are the surface that
makes a starving waiter visible.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from exomem import runtime_readiness as readiness_module
from exomem import writer_lease
from exomem.cli_ops import OpError
from exomem.mutation_lock import VaultMutationCoordinator
from exomem.writer_lease import LeaseConfig, LeaseManager

_SHA = "a" * 64


def _standalone(**overrides: object) -> dict[str, object]:
    coordination: dict[str, object] = {
        "enabled": False,
        "role": "standalone",
        "replica_id": None,
        "coordinator_healthy": True,
    }
    coordination.update(overrides)
    return coordination


# --- mechanism 1: the process-local blind spot ------------------------------


def test_process_local_status_reports_unknown_rather_than_free(tmp_path: Path) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    boundary = manager.status()["mutation_boundary"]

    assert boundary["state"] == "unknown"
    assert boundary["reason"] == "process_local_only"


def test_process_local_status_still_reports_a_real_local_holder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    with manager.mutation_guard(
        vault,
        request_id="req-local",
        operation="edit_memory",
        holder_kind="command",
    ):
        boundary = manager.status()["mutation_boundary"]

    assert boundary["state"] == "held"
    assert boundary["request_id"] == "req-local"
    assert str(vault) not in str(boundary)


@pytest.mark.parametrize("configured", [None, "", "   ", "relative/vault"])
def test_readiness_without_an_absolute_vault_is_unknown_not_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    if configured is None:
        monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_VAULT_PATH", configured)

    snapshot = readiness_module.runtime_readiness(mcp_tool_surface_sha256=_SHA)

    boundary = snapshot["coordination"]["mutation_boundary"]
    assert boundary["state"] == "unknown"
    assert boundary["reason"] == "process_local_only"


# --- mechanism 2: the swallowed coordination-status exception ---------------


def test_status_failure_is_unknown_and_keeps_readiness_non_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(vault_or_cell: object = None) -> dict[str, object]:
        raise OpError(
            "MUTATION_LOCK_UNAVAILABLE",
            "vault mutation authority could not be established",
            "Check runtime state storage.",
        )

    monkeypatch.delenv("EXOMEM_WRITER_LEASE_URL", raising=False)
    monkeypatch.setattr(writer_lease, "coordination_status", explode)

    snapshot = readiness_module.runtime_readiness(mcp_tool_surface_sha256=_SHA)

    boundary = snapshot["coordination"]["mutation_boundary"]
    assert boundary["state"] == "unknown"
    assert boundary["reason"] == "status_error"
    # The endpoint stays a 200: a boundary we could not measure is a reporting
    # gap, not a takeover-eligibility failure.
    assert snapshot["status"] == "ready"
    assert snapshot["takeover_eligible"] is True


def test_missing_boundary_block_projects_as_unknown() -> None:
    snapshot = readiness_module.build_runtime_readiness(
        coordination=_standalone(),
        release="1.2.3",
        mcp_tool_surface_sha256=_SHA,
    )

    assert snapshot["coordination"]["mutation_boundary"] == {
        "state": "unknown",
        "reason": "unavailable",
    }


def test_verified_free_boundary_is_still_reported_as_free() -> None:
    snapshot = readiness_module.build_runtime_readiness(
        coordination=_standalone(mutation_boundary={"state": "free"}),
        release="1.2.3",
        mcp_tool_surface_sha256=_SHA,
    )

    assert snapshot["coordination"]["mutation_boundary"] == {"state": "free"}


def test_unknown_reason_is_bounded_and_content_free() -> None:
    snapshot = readiness_module.build_runtime_readiness(
        coordination=_standalone(
            mutation_boundary={
                "state": "unknown",
                "reason": "/secret/vault/path.md",
                "vault_path": "must-not-leak",
            }
        ),
        release="1.2.3",
        mcp_tool_surface_sha256=_SHA,
    )

    assert snapshot["coordination"]["mutation_boundary"] == {
        "state": "unknown",
        "reason": "unspecified",
    }
    assert "must-not-leak" not in repr(snapshot)


# --- held boundaries keep their existing holder block -----------------------


def test_held_boundary_still_reports_the_bounded_holder_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)

    with manager.mutation_guard(
        vault,
        request_id="req-readiness-held",
        operation="remember",
        holder_kind="command",
    ):
        snapshot = readiness_module.runtime_readiness(mcp_tool_surface_sha256=_SHA)

    boundary = snapshot["coordination"]["mutation_boundary"]
    assert boundary["state"] == "held"
    assert boundary["verified"] is True
    assert boundary["request_id"] == "req-readiness-held"
    assert boundary["operation"] == "remember"
    assert boundary["holder_kind"] == "command"
    assert str(vault) not in repr(snapshot)


# --- contention attributability ---------------------------------------------


def test_snapshot_carries_process_local_contention_stats(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    idle = coordinator.snapshot()
    assert idle["state"] == "free"
    assert idle["contention"] == {
        "acquire_attempts": 0,
        "busy_refusals": 0,
        "busy_refusals_recent": 0,
        "recent_window_seconds": 60.0,
        "scope": "process_local",
        "last_holder": None,
    }

    with coordinator.hold(
        request_id="req-stats",
        operation="edit_memory",
        holder_kind="command",
    ):
        held = coordinator.snapshot()

    # The pre-existing holder keys are unchanged; the stats are additive.
    assert {
        key: held[key]
        for key in ("state", "request_id", "operation", "holder_kind", "verified")
    } == {
        "state": "held",
        "request_id": "req-stats",
        "operation": "edit_memory",
        "holder_kind": "command",
        "verified": True,
    }
    assert isinstance(held["age_seconds"], float)
    assert held["overdue"] is False
    assert held["contention"]["acquire_attempts"] == 1

    released = coordinator.snapshot()
    assert released["state"] == "free"
    assert released["contention"]["last_holder"] == {
        "pid": os.getpid(),
        "request_id": "req-stats",
        "operation": "edit_memory",
        "holder_kind": "command",
        "observed_at": released["contention"]["last_holder"]["observed_at"],
        "source": "release",
    }
    assert isinstance(released["contention"]["last_holder"]["observed_at"], float)
    assert str(vault) not in str(released)


def test_refused_acquire_is_counted_and_attributed_in_the_busy_payload(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    holder = VaultMutationCoordinator(state_root, vault)
    contender = VaultMutationCoordinator(state_root, vault)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with holder.hold(
            timeout_seconds=2.0,
            request_id="req-holder",
            operation="edit_memory",
            holder_kind="command",
        ):
            entered.set()
            assert release.wait(5.0)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        assert entered.wait(2.0)
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a held mutation boundary")
    finally:
        release.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive()

    error = raised.value
    assert error.code == "MUTATION_BUSY"
    details = error.details
    # Existing payload keys are untouched.
    assert details["status"] == "retryable"
    assert details["committed"] is False
    assert isinstance(details["retry_after_ms"], int)
    assert details["holder"]["request_id"] == "req-holder"
    assert "contention" not in details["holder"]
    # Additive attribution.
    assert details["busy_refusals"] >= 1
    assert details["busy_refusals_recent"] >= 1
    assert details["acquire_attempts"] >= 2
    assert details["contention_scope"] == "process_local"
    assert details["last_holder"]["request_id"] == "req-holder"
    assert details["last_holder"]["operation"] == "edit_memory"
    assert details["last_holder"]["holder_kind"] == "command"
    assert details["last_holder"]["source"] in {"refusal", "release"}
    assert str(vault) not in str(details)

    stats = contender.snapshot()["contention"]
    assert stats["busy_refusals"] >= 1
    assert stats["busy_refusals_recent"] >= 1


def test_readiness_publishes_only_bounded_contention_fields() -> None:
    snapshot = readiness_module.build_runtime_readiness(
        coordination=_standalone(
            mutation_boundary={
                "state": "free",
                "contention": {
                    "acquire_attempts": 12,
                    "busy_refusals": 3,
                    "busy_refusals_recent": 2,
                    "recent_window_seconds": 60.0,
                    "scope": "process_local",
                    "vault_path": "must-not-leak",
                    "last_holder": {
                        "pid": 4242,
                        "request_id": "req-busy",
                        "operation": "edit_memory",
                        "holder_kind": "command",
                        "observed_at": 1234.5,
                        "source": "refusal",
                        "vault_path": "must-not-leak-either",
                    },
                },
            }
        ),
        release="1.2.3",
        mcp_tool_surface_sha256=_SHA,
    )

    assert snapshot["coordination"]["mutation_boundary"] == {
        "state": "free",
        "contention": {
            "acquire_attempts": 12,
            "busy_refusals": 3,
            "busy_refusals_recent": 2,
            "recent_window_seconds": 60.0,
            "scope": "process_local",
            "last_holder": {
                "pid": 4242,
                "request_id": "req-busy",
                "operation": "edit_memory",
                "holder_kind": "command",
                "observed_at": 1234.5,
                "source": "refusal",
            },
        },
    }
    assert "must-not-leak" not in repr(snapshot)
