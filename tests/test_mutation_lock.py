from __future__ import annotations

import json
import logging
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from exomem import mutation_lock as mutation_lock_module
from exomem.cli_ops import OpError
from exomem.mutation_lock import VaultMutationCoordinator, active_mutation_snapshot


def _process_hold(
    state_root: str,
    vault_root: str,
    attempting,
    entered,
    release,
    request_id: str | None = None,
    operation: str | None = None,
    holder_kind: str = "unknown",
) -> None:
    coordinator = VaultMutationCoordinator(Path(state_root), Path(vault_root))
    attempting.set()
    with coordinator.hold(
        timeout_seconds=3.0,
        request_id=request_id,
        operation=operation,
        holder_kind=holder_kind,
    ):
        entered.set()
        if not release.wait(5.0):
            raise RuntimeError("test release signal was not received")


def _process_hold_paused_before_publish(
    state_root: str,
    vault_root: str,
    acquired,
    publish,
    entered,
    release,
) -> None:
    original = VaultMutationCoordinator._publish_holder_metadata

    def paused_publish(self, holder):  # noqa: ANN001
        acquired.set()
        if not publish.wait(5.0):
            raise RuntimeError("test publish signal was not received")
        return original(self, holder)

    VaultMutationCoordinator._publish_holder_metadata = paused_publish
    coordinator = VaultMutationCoordinator(Path(state_root), Path(vault_root))
    with coordinator.hold(
        timeout_seconds=3.0,
        request_id="req-new-generation",
        operation="replace_memory",
        holder_kind="command",
    ):
        entered.set()
        if not release.wait(5.0):
            raise RuntimeError("test release signal was not received")


def _process_crash(state_root: str, vault_root: str, entered) -> None:
    coordinator = VaultMutationCoordinator(Path(state_root), Path(vault_root))
    guard = coordinator.hold(timeout_seconds=3.0)
    guard.__enter__()
    entered.set()
    time.sleep(0.05)
    os._exit(23)


def _join_or_terminate(processes: list[multiprocessing.Process]) -> None:
    for process in processes:
        process.join(timeout=5.0)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)


def test_same_canonical_vault_serializes_competing_threads(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    alias = vault / ".." / "vault"
    first = VaultMutationCoordinator(state_root, vault)
    second = VaultMutationCoordinator(state_root, alias)
    first_entered = threading.Event()
    second_attempting = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with first.hold(timeout_seconds=2.0):
            first_entered.set()
            assert release_first.wait(2.0)

    def enter_second() -> None:
        second_attempting.set()
        with second.hold(timeout_seconds=2.0):
            second_entered.set()

    first_thread = threading.Thread(target=hold_first)
    second_thread = threading.Thread(target=enter_second)
    first_thread.start()
    assert first_entered.wait(1.0)
    second_thread.start()
    assert second_attempting.wait(1.0)
    assert not second_entered.wait(0.1)
    release_first.set()
    assert second_entered.wait(1.0)
    first_thread.join(timeout=2.0)
    second_thread.join(timeout=2.0)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()


def test_same_canonical_vault_serializes_competing_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    first_attempting = context.Event()
    first_entered = context.Event()
    release_first = context.Event()
    second_attempting = context.Event()
    second_entered = context.Event()
    release_second = context.Event()
    first = context.Process(
        target=_process_hold,
        args=(
            str(state_root),
            str(vault),
            first_attempting,
            first_entered,
            release_first,
        ),
    )
    second = context.Process(
        target=_process_hold,
        args=(
            str(state_root),
            str(vault / ".." / "vault"),
            second_attempting,
            second_entered,
            release_second,
        ),
    )
    processes = [first, second]
    try:
        first.start()
        assert first_attempting.wait(2.0)
        assert first_entered.wait(2.0)
        second.start()
        assert second_attempting.wait(2.0)
        assert not second_entered.wait(0.2)
        release_first.set()
        assert second_entered.wait(2.0)
        release_second.set()
    finally:
        release_first.set()
        release_second.set()
        _join_or_terminate(processes)
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_independent_vaults_can_mutate_concurrently(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    vault_a.mkdir()
    vault_b.mkdir()
    first = VaultMutationCoordinator(state_root, vault_a)
    second = VaultMutationCoordinator(state_root, vault_b)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with first.hold(timeout_seconds=2.0):
            first_entered.set()
            assert release_first.wait(2.0)

    first_thread = threading.Thread(target=hold_first)
    first_thread.start()
    assert first_entered.wait(1.0)
    with second.hold(timeout_seconds=0.2):
        second_entered.set()
    assert second_entered.is_set()
    release_first.set()
    first_thread.join(timeout=2.0)
    assert not first_thread.is_alive()


def test_nested_acquisition_is_reentrant_across_coordinator_instances(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    first = VaultMutationCoordinator(tmp_path / "state", vault)
    second = VaultMutationCoordinator(tmp_path / "state", vault / ".")

    with first.hold(timeout_seconds=0.2):
        with second.hold(timeout_seconds=0.0):
            assert first.lock_path == second.lock_path


def test_bounded_timeout_raises_actionable_mutation_busy(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    holder = VaultMutationCoordinator(state_root, vault)
    contender = VaultMutationCoordinator(state_root, vault)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with holder.hold(timeout_seconds=2.0):
            entered.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(1.0)
    started = time.monotonic()
    try:
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a held mutation boundary")
        assert raised.value.code == "MUTATION_BUSY"
        assert raised.value.remediation
        assert "retry" in raised.value.remediation.lower()
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_process_contention_uses_same_bounded_timeout_contract(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    attempting = context.Event()
    entered = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_process_hold,
        args=(str(state_root), str(vault), attempting, entered, release),
    )
    holder.start()
    try:
        assert attempting.wait(2.0)
        assert entered.wait(2.0)
        contender = VaultMutationCoordinator(state_root, vault)
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a process-held mutation boundary")
        assert raised.value.code == "MUTATION_BUSY"
        assert raised.value.remediation
    finally:
        release.set()
        _join_or_terminate([holder])
    assert holder.exitcode == 0


def test_cross_process_status_and_busy_error_report_verified_current_holder(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    attempting = context.Event()
    entered = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_process_hold,
        args=(
            str(state_root),
            str(vault),
            attempting,
            entered,
            release,
            "req-external",
            "edit_memory",
            "command",
        ),
    )
    holder.start()
    try:
        assert attempting.wait(2.0)
        assert entered.wait(2.0)
        contender = VaultMutationCoordinator(state_root, vault)
        snapshot = contender.snapshot()
        assert snapshot | {"age_seconds": 0.0} == {
            "state": "held",
            "request_id": "req-external",
            "operation": "edit_memory",
            "holder_kind": "command",
            "age_seconds": 0.0,
            "overdue": False,
            "verified": True,
        }
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a process-held mutation boundary")
        busy_holder = raised.value.details["holder"]
        assert busy_holder["verified"] is True
        assert busy_holder["request_id"] == "req-external"
        assert busy_holder["operation"] == "edit_memory"
        assert busy_holder["holder_kind"] == "command"
    finally:
        release.set()
        _join_or_terminate([holder])
    assert holder.exitcode == 0


@pytest.mark.parametrize("payload", ["not json", '{"schema": 1}'])
def test_stale_or_malformed_metadata_cannot_report_a_verified_holder(
    tmp_path: Path, payload: str
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)
    coordinator.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    coordinator.metadata_path.write_text(payload, encoding="utf-8")

    assert coordinator.snapshot() == {"state": "free"}
    assert not coordinator.metadata_path.exists()


@pytest.mark.parametrize("payload", [None, "not json", '{"schema": 1}'])
def test_external_holder_without_valid_metadata_is_explicitly_unverified(
    tmp_path: Path, payload: str | None
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)
    handle = coordinator._open_lock_file(coordinator.lock_path)
    assert mutation_lock_module._try_os_lock(handle)
    try:
        if payload is not None:
            coordinator.metadata_path.write_text(payload, encoding="utf-8")
        snapshot = coordinator.snapshot()
        assert snapshot == {
            "state": "held",
            "request_id": "untracked",
            "operation": "unknown",
            "holder_kind": "external",
            "age_seconds": 0.0,
            "overdue": False,
            "verified": False,
        }
    finally:
        mutation_lock_module._release_os_lock(handle)
        handle.close()


def test_holder_sidecar_is_bounded_content_free_runtime_metadata(tmp_path: Path) -> None:
    vault = tmp_path / "private-vault-name"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with coordinator.hold(
        request_id="req-content-free",
        operation="remember",
        holder_kind="command",
    ):
        holder = json.loads(coordinator.metadata_path.read_text(encoding="utf-8"))
        assert set(holder) == {
            "schema",
            "generation",
            "request_id",
            "operation",
            "holder_kind",
            "acquired_at",
            "long_holder_seconds",
        }
        rendered = repr(holder).lower()
        assert "private-vault-name" not in rendered
        assert "credential" not in rendered
        assert "tenant" not in rendered


def test_status_waits_out_acquire_to_publish_generation_transition(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    acquired = context.Event()
    publish = context.Event()
    entered = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_process_hold_paused_before_publish,
        args=(
            str(state_root),
            str(vault),
            acquired,
            publish,
            entered,
            release,
        ),
    )
    holder.start()
    result: list[dict[str, object]] = []
    status_thread = threading.Thread(
        target=lambda: result.append(
            VaultMutationCoordinator(state_root, vault).snapshot()
        )
    )
    try:
        assert acquired.wait(2.0)
        status_thread.start()
        time.sleep(0.05)
        assert not result
        publish.set()
        assert entered.wait(2.0)
        status_thread.join(timeout=2.0)
        assert result
        assert result[0]["state"] == "held"
        assert result[0]["verified"] is True
        assert result[0]["request_id"] == "req-new-generation"
    finally:
        publish.set()
        release.set()
        status_thread.join(timeout=2.0)
        _join_or_terminate([holder])
    assert holder.exitcode == 0


def test_probe_cleanup_cannot_delete_a_new_holders_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    status_coordinator = VaultMutationCoordinator(state_root, vault)
    status_coordinator.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    status_coordinator.metadata_path.write_text("stale", encoding="utf-8")
    cleanup_started = threading.Event()
    continue_cleanup = threading.Event()
    original = mutation_lock_module._clear_holder_metadata
    paused = False

    def paused_clear(path: Path) -> None:
        nonlocal paused
        original(path)
        if not paused:
            paused = True
            cleanup_started.set()
            assert continue_cleanup.wait(2.0)

    monkeypatch.setattr(mutation_lock_module, "_clear_holder_metadata", paused_clear)
    status_result: list[dict[str, object]] = []
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def read_status() -> None:
        status_result.append(status_coordinator.snapshot())

    def acquire_after_probe() -> None:
        writer = VaultMutationCoordinator(state_root, vault)
        with writer.hold(
            timeout_seconds=2.0,
            request_id="req-after-cleanup",
            operation="remember",
            holder_kind="command",
        ):
            holder_entered.set()
            assert release_holder.wait(2.0)

    status_thread = threading.Thread(target=read_status)
    holder_thread = threading.Thread(target=acquire_after_probe)
    status_thread.start()
    assert cleanup_started.wait(1.0)
    holder_thread.start()
    assert not holder_entered.wait(0.05)
    continue_cleanup.set()
    status_thread.join(timeout=2.0)
    assert status_result == [{"state": "free"}]
    assert holder_entered.wait(1.0)
    snapshot = status_coordinator.snapshot()
    assert snapshot["verified"] is True
    assert snapshot["request_id"] == "req-after-cleanup"
    release_holder.set()
    holder_thread.join(timeout=2.0)
    assert not status_thread.is_alive()
    assert not holder_thread.is_alive()


def test_unusable_state_root_raises_actionable_lock_error(tmp_path: Path) -> None:
    state_root = tmp_path / "not-a-directory"
    state_root.write_text("occupied", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(state_root, vault)

    with pytest.raises(OpError) as raised:
        with coordinator.hold(timeout_seconds=0.05):
            pytest.fail("coordinator entered with an unusable state root")
    assert raised.value.code == "MUTATION_LOCK_UNAVAILABLE"
    assert raised.value.remediation


def test_exception_releases_mutation_authority(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with pytest.raises(RuntimeError, match="boom"):
        with coordinator.hold(timeout_seconds=0.2):
            raise RuntimeError("boom")

    with coordinator.hold(timeout_seconds=0.2):
        pass


def test_holder_snapshot_is_content_free_and_clears_after_release(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with coordinator.hold(
        timeout_seconds=0.2,
        request_id="req-123",
        operation="edit_memory",
        holder_kind="command",
    ):
        snapshot = coordinator.snapshot()
        assert snapshot["state"] == "held"
        assert snapshot["request_id"] == "req-123"
        assert snapshot["operation"] == "edit_memory"
        assert snapshot["holder_kind"] == "command"
        assert snapshot["age_seconds"] >= 0
        assert str(vault) not in str(snapshot)

    assert coordinator.snapshot() == {"state": "free"}


def test_active_mutation_snapshot_reports_oldest_process_holder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    assert active_mutation_snapshot() == {"state": "free"}
    with coordinator.hold(
        request_id="req-status",
        operation="edit_memory",
        holder_kind="command",
    ):
        snapshot = active_mutation_snapshot()
        assert snapshot["state"] == "held"
        assert snapshot["request_id"] == "req-status"
        assert snapshot["operation"] == "edit_memory"
        assert snapshot["holder_kind"] == "command"
        assert str(vault) not in str(snapshot)
    assert active_mutation_snapshot() == {"state": "free"}


def test_long_holder_warning_is_bounded_and_content_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = tmp_path / "private-vault-name"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(
        tmp_path / "state", vault, long_holder_seconds=0.01
    )

    with caplog.at_level(logging.WARNING, logger="exomem.mutation_lock"):
        with coordinator.hold(
            request_id="req-slow",
            operation="background_media_reconcile",
            holder_kind="background",
        ):
            time.sleep(0.02)
            assert coordinator.snapshot()["overdue"] is True
            assert coordinator.snapshot()["overdue"] is True

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "req-slow" in messages[0]
    assert "private-vault-name" not in messages[0]


def test_process_exit_releases_os_mutation_lock(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    entered = context.Event()
    crashing = context.Process(
        target=_process_crash,
        args=(str(state_root), str(vault), entered),
    )
    crashing.start()
    assert entered.wait(2.0)
    crashing.join(timeout=3.0)
    if crashing.is_alive():
        crashing.terminate()
        crashing.join(timeout=2.0)
    assert crashing.exitcode == 23

    recovered = VaultMutationCoordinator(state_root, vault)
    with recovered.hold(timeout_seconds=1.0):
        assert recovered.lock_path.exists()
