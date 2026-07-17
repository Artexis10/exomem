from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import vault as vault_module
from exomem import writer_lease as writer_lease_module
from exomem.cli_ops import OpError, error_dict, http_status_for
from exomem.lease_coordinator import SQLiteLeaseStore
from exomem.mutation_lock import VaultMutationCoordinator
from exomem.vault import PlannedWrite, batch_atomic_write
from exomem.writer_lease import (
    LeaseConfig,
    LeaseManager,
    LeaseRecord,
    invoke_command,
    reset_managers_for_tests,
)


def _committed_error(tmp_path: Path, *, targets: tuple[str, ...] = ("note.md",)):
    raw = PermissionError(
        f"{tmp_path}/.exomem-batch-{'a' * 32}/stage-0.tmp: raw storage detail"
    )
    error = vault_module.BatchWriteError(
        "BATCH_CLEANUP_INCOMPLETE",
        vault_module.BatchTargetSummary(len(targets), targets, 0),
        committed=True,
        diagnostics=(raw,),
    )
    try:
        raise error from raw
    except vault_module.BatchWriteError as raised:
        return raised


def _explicit_storage_key(manager: LeaseManager, public_key: str) -> str:
    assert manager.config.vault_id is not None
    return writer_lease_module._namespaced_idempotency_key(
        "explicit", f"cell:{manager.config.vault_id}", public_key
    )


def _row(manager: LeaseManager, public_key: str) -> tuple[str, str, bytes | None]:
    key = _explicit_storage_key(manager, public_key)
    with sqlite3.connect(manager.idempotency.path) as connection:
        digest, state, result = connection.execute(
            "SELECT digest, state, result FROM mutations WHERE key = ?", (key,)
        ).fetchone()
    return digest, state, result


def test_config_is_default_off_and_requires_identities() -> None:
    assert LeaseConfig.from_env({}).enabled is False
    with pytest.raises(ValueError, match="WRITER_LEASE_CONFIG"):
        LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_URL": "https://lease.example"})


def test_config_loads_without_exposing_token_in_status(tmp_path: Path) -> None:
    config = LeaseConfig.from_env(
        {
            "EXOMEM_WRITER_LEASE_URL": "https://lease.example/",
            "EXOMEM_WRITER_LEASE_VAULT_ID": "main",
            "EXOMEM_WRITER_LEASE_REPLICA_ID": "desktop",
            "EXOMEM_WRITER_LEASE_TOKEN": "secret",
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path),
        }
    )
    manager = LeaseManager(config, client=FakeClient(LeaseRecord("desktop", 99, 7)))
    status = manager.status()
    assert status["role"] == "writer"
    assert "secret" not in repr(status)
    assert "url" not in status


def test_coordinator_requests_use_cloudflare_compatible_user_agent(monkeypatch) -> None:
    from exomem.writer_lease import LeaseCoordinatorClient

    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"holder":null,"expires_at":null,"fencing_token":0}'

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        seen["user_agent"] = request.get_header("User-agent")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LeaseCoordinatorClient(
        LeaseConfig(url="https://lease.example", vault_id="main", replica_id="desktop")
    )
    client.status()
    assert seen["user_agent"].startswith("Mozilla/5.0")
    assert "Exomem-Coordinator" in seen["user_agent"]


class FakeClient:
    def __init__(self, record: LeaseRecord | Exception):
        self.record = record
        self.releases: list[int] = []
        self.acquisitions = 0

    def _get(self) -> LeaseRecord:
        if isinstance(self.record, Exception):
            raise self.record
        return self.record

    def acquire(self) -> LeaseRecord:
        self.acquisitions += 1
        record = self._get()
        return LeaseRecord(
            record.holder, record.expires_at, record.fencing_token, record.holder == "desktop"
        )

    def status(self) -> LeaseRecord:
        return self._get()

    def renew(self, fencing_token: int) -> LeaseRecord:
        return self.acquire()

    def release(self, fencing_token: int) -> LeaseRecord:
        self.releases.append(fencing_token)
        return LeaseRecord(None, None, fencing_token, True)


class StoreClient:
    def __init__(self, store: SQLiteLeaseStore, replica_id: str):
        self.store = store
        self.replica_id = replica_id

    def acquire(self) -> LeaseRecord:
        return LeaseRecord.from_json(self.store.acquire("main", self.replica_id, 10))

    def status(self) -> LeaseRecord:
        return LeaseRecord.from_json(self.store.status("main"))

    def renew(self, fencing_token: int) -> LeaseRecord:
        return LeaseRecord.from_json(
            self.store.renew("main", self.replica_id, fencing_token, 10)
        )

    def release(self, fencing_token: int) -> LeaseRecord:
        return LeaseRecord.from_json(self.store.release("main", self.replica_id, fencing_token))


class BlockingRejectedRenewalClient(FakeClient):
    def __init__(self):
        super().__init__(LeaseRecord("desktop", 200, 3))
        self.renew_started = threading.Event()
        self.resume_renewal = threading.Event()

    def renew(self, fencing_token: int) -> LeaseRecord:
        assert fencing_token == 1
        self.renew_started.set()
        assert self.resume_renewal.wait(timeout=5)
        return LeaseRecord("laptop", 200, 2, False)


class TwoStepStop:
    def __init__(self):
        self.calls = 0

    def wait(self, timeout: float) -> bool:  # noqa: ARG002
        self.calls += 1
        return self.calls > 1


def _command(*, writes: bool, leaf):  # noqa: ANN001
    return SimpleNamespace(name="mutate" if writes else "read", read_only=not writes, leaf=leaf)


def _manager(tmp_path: Path, record: LeaseRecord | Exception) -> LeaseManager:
    return LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=FakeClient(record),
    )


def test_reads_bypass_unavailable_coordinator(tmp_path: Path) -> None:
    manager = _manager(tmp_path, OpError("WRITER_COORDINATOR_UNAVAILABLE", "down"))
    assert (
        manager.invoke(_command(writes=False, leaf=lambda value: value + 1), (), {"value": 2}) == 3
    )


def test_hosted_reads_serialize_without_contacting_unavailable_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "true")
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "state"
    manager = _manager(state_root, OpError("WRITER_COORDINATOR_UNAVAILABLE", "down"))
    coordinator = VaultMutationCoordinator(state_root, vault)
    boundary_entered = threading.Event()
    release_boundary = threading.Event()
    read_finished = threading.Event()
    result: list[str] = []

    def hold_mutation() -> None:
        with coordinator.hold(timeout_seconds=2.0):
            boundary_entered.set()
            assert release_boundary.wait(2.0)

    def read() -> None:
        result.append(
            manager.invoke(
                _command(writes=False, leaf=lambda _vault: "read-ok"),
                (vault,),
                {},
            )
        )
        read_finished.set()

    writer = threading.Thread(target=hold_mutation)
    reader = threading.Thread(target=read)
    writer.start()
    assert boundary_entered.wait(1.0)
    reader.start()
    assert not read_finished.wait(0.1)
    release_boundary.set()
    assert read_finished.wait(1.0)
    writer.join(timeout=2.0)
    reader.join(timeout=2.0)

    assert result == ["read-ok"]


def test_read_only_invocation_bypasses_held_mutation_boundary(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(state_root, vault)
    entered = threading.Event()
    release = threading.Event()

    def hold_mutation() -> None:
        with coordinator.hold(timeout_seconds=2.0):
            entered.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=hold_mutation)
    thread.start()
    assert entered.wait(1.0)
    manager = LeaseManager(LeaseConfig(state_dir=state_root))
    try:
        assert manager.invoke(
            _command(writes=False, leaf=lambda _vault: "read"),
            (vault,),
            {},
        ) == "read"
    finally:
        release.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_hosted_read_waits_for_complete_multi_file_mutation_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "true")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(state_root, vault)
    entered = threading.Event()
    release = threading.Event()
    read_finished = threading.Event()
    result: list[str] = []

    def hold_mutation() -> None:
        with coordinator.hold(timeout_seconds=2.0):
            entered.set()
            assert release.wait(2.0)

    manager = LeaseManager(LeaseConfig(state_dir=state_root))

    def read() -> None:
        result.append(
            manager.invoke(
                _command(writes=False, leaf=lambda _vault: "consistent"),
                (vault,),
                {},
            )
        )
        read_finished.set()

    writer = threading.Thread(target=hold_mutation)
    reader = threading.Thread(target=read)
    writer.start()
    assert entered.wait(1.0)
    reader.start()
    assert not read_finished.wait(0.1)
    release.set()
    assert read_finished.wait(1.0)
    writer.join(timeout=2.0)
    reader.join(timeout=2.0)
    assert result == ["consistent"]


def test_write_leaf_is_serialized_for_entire_invocation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    first_manager = LeaseManager(LeaseConfig(state_dir=state_root))
    second_manager = LeaseManager(LeaseConfig(state_dir=state_root))
    first_entered = threading.Event()
    second_attempting = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def first_leaf(_vault: Path) -> str:
        first_entered.set()
        assert release_first.wait(2.0)
        return "first"

    def second_leaf(_vault: Path) -> str:
        second_entered.set()
        return "second"

    def run_first() -> None:
        first_manager.invoke(_command(writes=True, leaf=first_leaf), (vault,), {})

    def run_second() -> None:
        second_attempting.set()
        second_manager.invoke(_command(writes=True, leaf=second_leaf), (vault,), {})

    first_thread = threading.Thread(target=run_first)
    second_thread = threading.Thread(target=run_second)
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


def test_mutation_guard_is_reentrant_and_revalidates_writer_authority(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    client = FakeClient(LeaseRecord("desktop", 99, 4))
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path / "state",
        ),
        client=client,
    )

    with manager.mutation_guard(vault) as outer:
        with manager.mutation_guard(vault / ".") as inner:
            assert outer.lock_path == inner.lock_path
            assert outer.identity == inner.identity

    assert client.acquisitions == 2


def test_direct_mutation_guard_threads_fence_to_atomic_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "note.md"
    manager = _manager(tmp_path / "state", LeaseRecord("desktop", 99, 4))
    validated: list[int] = []
    monkeypatch.setattr(manager, "validate_fencing_token", validated.append)

    with manager.mutation_guard(vault):
        batch_atomic_write([PlannedWrite(target, "fenced bytes")])

    assert target.read_text(encoding="utf-8") == "fenced bytes"
    assert validated == [4]


def test_invoke_routes_writes_through_reusable_mutation_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    events: list[str] = []

    @contextmanager
    def guard(subject: Path):
        assert subject == vault
        events.append("guard-enter")
        yield SimpleNamespace(identity="vault:test")
        events.append("guard-exit")

    monkeypatch.setattr(manager, "mutation_guard", guard, raising=False)
    command = _command(writes=True, leaf=lambda _vault: events.append("leaf") or "ok")

    assert manager.invoke(command, (vault,), {}) == "ok"
    assert events == ["guard-enter", "leaf", "guard-exit"]


def _unreachable_coordinator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "main")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "desktop")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TIMEOUT", "0.05")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-state"))


def _recording_product_command(command, calls: list[dict], result: str):  # noqa: ANN001, ANN201
    selector = {
        "connect_memory": "operation",
        "observe_memory": "operation",
        "adopt_vault": "mode",
    }.get(command.name)
    if selector is None:
        return replace(
            command,
            leaf=lambda _vault_root, **leaf_kwargs: calls.append(leaf_kwargs) or result,
        )

    default = inspect.signature(command.leaf).parameters[selector].default
    if selector == "operation":

        def leaf(_vault_root, operation=default, **leaf_kwargs):  # noqa: ANN001, ANN202
            calls.append({"operation": operation, **leaf_kwargs})
            return result

    else:

        def leaf(_vault_root, mode=default, **leaf_kwargs):  # noqa: ANN001, ANN202
            calls.append({"mode": mode, **leaf_kwargs})
            return result

    return replace(command, leaf=leaf)


@pytest.mark.parametrize(
    ("command_name", "kwargs"),
    [
        pytest.param("connect_memory", {}, id="connect-default-suggest-links"),
        pytest.param(
            "connect_memory", {"operation": "suggest-links"}, id="connect-suggest-links"
        ),
        pytest.param(
            "connect_memory",
            {"operation": "suggest-relations"},
            id="connect-suggest-relations",
        ),
        pytest.param("connect_memory", {"operation": "context"}, id="connect-context"),
        pytest.param(
            "connect_memory", {"operation": "graph-context"}, id="connect-graph-context"
        ),
        pytest.param(
            "connect_memory", {"operation": "inbound-links"}, id="connect-inbound-links"
        ),
        pytest.param("adopt_vault", {}, id="adopt-default-scan-only"),
        pytest.param("adopt_vault", {"mode": "scan-only"}, id="adopt-scan-only"),
        pytest.param(
            "observe_memory", {"operation": "validate"}, id="observe-validate"
        ),
    ],
)
def test_read_only_product_operations_bypass_unreachable_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_name: str,
    kwargs: dict,
) -> None:
    from exomem.commands import product_commands_for

    _unreachable_coordinator(monkeypatch, tmp_path)
    calls: list[dict] = []
    command = next(c for c in product_commands_for("mcp") if c.name == command_name)
    command = _recording_product_command(command, calls, "read-ok")
    try:
        assert invoke_command(command, tmp_path, **kwargs) == "read-ok"
        assert len(calls) == 1
        selector = "mode" if command_name == "adopt_vault" else "operation"
        expected = dict(kwargs)
        expected.setdefault(selector, inspect.signature(command.leaf).parameters[selector].default)
        assert calls == [expected]
    finally:
        reset_managers_for_tests()


def test_process_media_status_bypasses_writer_but_mutations_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem.commands import product_commands_for

    _unreachable_coordinator(monkeypatch, tmp_path)
    command = next(
        command for command in product_commands_for("mcp") if command.name == "process_media"
    )
    calls: list[dict] = []
    command = replace(
        command,
        leaf=lambda _vault_root, **kwargs: calls.append(kwargs) or kwargs["operation"],
    )
    try:
        assert invoke_command(command, tmp_path, operation="status") == "status"
        for operation in ("process", "retry"):
            with pytest.raises(OpError, match="WRITER_COORDINATOR_UNAVAILABLE"):
                invoke_command(command, tmp_path, operation=operation)
        assert calls == [{"operation": "status"}]
    finally:
        reset_managers_for_tests()


def test_default_connect_and_adopt_calls_run_during_coordinator_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vault: Path
) -> None:
    from exomem.commands import product_commands_for

    _unreachable_coordinator(monkeypatch, tmp_path)
    commands = {c.name: c for c in product_commands_for("mcp")}
    try:
        suggestions = invoke_command(
            commands["connect_memory"],
            vault,
            draft_title="Lease-safe read",
            draft_body="A draft that must remain readable during coordinator downtime.",
        )
        report = invoke_command(commands["adopt_vault"], vault)
    finally:
        reset_managers_for_tests()

    assert isinstance(suggestions, list)
    assert report["mode"] == "scan-only"


@pytest.mark.parametrize(
    ("command_name", "selector_default"),
    [
        pytest.param("connect_memory", inspect.Parameter.empty, id="connect-default-absent"),
        pytest.param("connect_memory", "future-mode", id="connect-default-unknown"),
        pytest.param("connect_memory", "create-entity", id="connect-default-write"),
        pytest.param("adopt_vault", inspect.Parameter.empty, id="adopt-default-absent"),
        pytest.param("adopt_vault", "future-mode", id="adopt-default-unknown"),
        pytest.param("adopt_vault", "save-manifest", id="adopt-default-write"),
    ],
)
def test_omitted_selector_fails_closed_when_leaf_default_is_not_known_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_name: str,
    selector_default: object,
) -> None:
    from exomem.commands import product_commands_for

    _unreachable_coordinator(monkeypatch, tmp_path)
    calls: list[dict] = []
    command = next(c for c in product_commands_for("mcp") if c.name == command_name)
    if selector_default is inspect.Parameter.empty:

        def leaf(_vault_root, **leaf_kwargs):  # noqa: ANN001, ANN202
            calls.append(leaf_kwargs)
            return "write-ran"

    elif command_name == "connect_memory":

        def leaf(_vault_root, operation=selector_default, **leaf_kwargs):  # noqa: ANN001, ANN202
            calls.append({"operation": operation, **leaf_kwargs})
            return "write-ran"

    else:

        def leaf(_vault_root, mode=selector_default, **leaf_kwargs):  # noqa: ANN001, ANN202
            calls.append({"mode": mode, **leaf_kwargs})
            return "write-ran"

    command = replace(command, leaf=leaf)
    try:
        with pytest.raises(OpError, match="WRITER_COORDINATOR_UNAVAILABLE"):
            invoke_command(command, tmp_path)
        assert calls == []
    finally:
        reset_managers_for_tests()


@pytest.mark.parametrize(
    ("command_name", "kwargs"),
    [
        pytest.param(
            "connect_memory", {"operation": "create-entity"}, id="connect-create-entity"
        ),
        pytest.param(
            "connect_memory", {"operation": "accept-relation"}, id="connect-accept-relation"
        ),
        pytest.param("connect_memory", {"operation": ""}, id="connect-empty"),
        pytest.param("connect_memory", {"operation": None}, id="connect-explicit-none"),
        pytest.param("connect_memory", {"operation": "entity"}, id="connect-nonexistent-entity"),
        pytest.param(
            "connect_memory", {"operation": "future-read-mode"}, id="connect-future-mode"
        ),
        pytest.param("adopt_vault", {"mode": "save-manifest"}, id="adopt-save-manifest"),
        pytest.param(
            "adopt_vault", {"mode": "copy-as-sources"}, id="adopt-copy-as-sources"
        ),
        pytest.param(
            "adopt_vault", {"mode": "compile-selected"}, id="adopt-compile-selected"
        ),
        pytest.param("adopt_vault", {"mode": ""}, id="adopt-empty"),
        pytest.param("adopt_vault", {"mode": None}, id="adopt-explicit-none"),
        pytest.param("adopt_vault", {"mode": "future-mode"}, id="adopt-future-mode"),
        pytest.param("observe_memory", {}, id="observe-default-add"),
        pytest.param("observe_memory", {"operation": "add"}, id="observe-add"),
        pytest.param("observe_memory", {"operation": "update"}, id="observe-update"),
        pytest.param("observe_memory", {"operation": "remove"}, id="observe-remove"),
        pytest.param(
            "observe_memory", {"operation": "future-mode"}, id="observe-future-mode"
        ),
        pytest.param("remember", {}, id="generic-write-capable-command"),
    ],
)
def test_write_and_unknown_product_operations_fail_closed_without_calling_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_name: str,
    kwargs: dict,
) -> None:
    from exomem.commands import product_commands_for

    _unreachable_coordinator(monkeypatch, tmp_path)
    calls: list[dict] = []
    command = next(c for c in product_commands_for("mcp") if c.name == command_name)
    command = _recording_product_command(command, calls, "write-ran")
    try:
        with pytest.raises(OpError, match="WRITER_COORDINATOR_UNAVAILABLE"):
            invoke_command(command, tmp_path, **kwargs)
        assert calls == []
    finally:
        reset_managers_for_tests()


def test_writer_executes_but_follower_and_outage_fail_closed(tmp_path: Path) -> None:
    calls: list[str] = []
    command = _command(writes=True, leaf=lambda: calls.append("write") or "ok")
    assert _manager(tmp_path / "a", LeaseRecord("desktop", 99, 4)).invoke(command, (), {}) == "ok"
    with pytest.raises(OpError, match="WRITER_LEASE_REQUIRED"):
        _manager(tmp_path / "b", LeaseRecord("laptop", 99, 5)).invoke(command, (), {})
    with pytest.raises(OpError, match="WRITER_COORDINATOR_UNAVAILABLE"):
        _manager(tmp_path / "c", OpError("WRITER_COORDINATOR_UNAVAILABLE", "down")).invoke(
            command, (), {}
        )
    assert calls == ["write"]
    assert http_status_for("WRITER_LEASE_REQUIRED") == 409
    assert http_status_for("WRITER_COORDINATOR_UNAVAILABLE") == 503


def test_superseded_replica_cannot_land_staged_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    replica_a = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path / "desktop-state",
        ),
        client=StoreClient(store, "desktop"),
    )
    replica_b = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="laptop",
            state_dir=tmp_path / "laptop-state",
        ),
        client=StoreClient(store, "laptop"),
    )
    target = tmp_path / "vault" / "note.md"
    target.parent.mkdir()
    target.write_text("old bytes", encoding="utf-8")
    staged = threading.Event()
    resume = threading.Event()
    original_create_artifact = vault_module._BatchWorkspace.create_artifact

    def pause_after_staging(workspace, name: str, content: bytes):  # noqa: ANN001
        result = original_create_artifact(workspace, name, content)
        if name.startswith("stage-"):
            staged.set()
            assert resume.wait(timeout=5)
        return result

    monkeypatch.setattr(
        vault_module._BatchWorkspace,
        "create_artifact",
        pause_after_staging,
    )
    command = _command(
        writes=True,
        leaf=lambda: batch_atomic_write([PlannedWrite(target, "stale bytes")]),
    )
    outcome: list[BaseException | object] = []

    def run_replica_a() -> None:
        try:
            outcome.append(replica_a.invoke(command, (), {}))
        except BaseException as exc:  # noqa: BLE001 - assertion inspects worker failure
            outcome.append(exc)

    worker = threading.Thread(target=run_replica_a)
    worker.start()
    assert staged.wait(timeout=5)
    clock.value = 111
    assert replica_b.ensure_writer().fencing_token == 2
    resume.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], OpError)
    assert outcome[0].code == "WRITER_FENCED"
    assert target.read_text(encoding="utf-8") == "old bytes"
    assert list(target.parent.glob(".exomem-batch-*")) == []


def test_delayed_rejected_renewal_does_not_clear_newer_local_token(tmp_path: Path) -> None:
    client = BlockingRejectedRenewalClient()
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=client,
    )
    manager._fencing_token = 1
    manager._stop = TwoStepStop()
    renewer = threading.Thread(target=manager._renew_loop)
    renewer.start()
    assert client.renew_started.wait(timeout=5)

    assert manager.ensure_writer().fencing_token == 3
    client.resume_renewal.set()
    renewer.join(timeout=5)

    assert not renewer.is_alive()
    assert manager._fencing_token == 3


def test_idempotency_returns_saved_result_and_rejects_mismatch(tmp_path: Path) -> None:
    calls: list[int] = []
    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=lambda value: calls.append(value) or {"value": value})
    assert manager.invoke(command, (), {"value": 1}, idempotency_key="request-1") == {"value": 1}
    assert manager.invoke(command, (), {"value": 1}, idempotency_key="request-1") == {"value": 1}
    with pytest.raises(OpError, match="IDEMPOTENCY_KEY_REUSED"):
        manager.invoke(command, (), {"value": 2}, idempotency_key="request-1")
    assert calls == [1]


def test_identical_inflight_retry_waits_for_original_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf_started = threading.Event()
    release_leaf = threading.Event()
    pending_seen = threading.Event()
    calls: list[int] = []
    outcomes: dict[str, object] = {}
    manager = LeaseManager(
        LeaseConfig(state_dir=tmp_path),
        mutation_timeout_seconds=0,
        idempotency_wait_seconds=2,
    )

    def leaf(value: int) -> dict[str, object]:
        calls.append(value)
        leaf_started.set()
        assert release_leaf.wait(timeout=2)
        return {"committed": True, "value": value}

    command = _command(writes=True, leaf=leaf)
    original_wait = manager.idempotency._wait_for_terminal

    def observed_wait(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        pending_seen.set()
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(manager.idempotency, "_wait_for_terminal", observed_wait)

    def invoke(name: str) -> None:
        try:
            outcomes[name] = manager.invoke(
                command,
                (),
                {"value": 1},
                implicit_idempotency_scope="principal:alice",
            )
        except BaseException as error:  # noqa: BLE001 - assert thread outcome below
            outcomes[name] = error

    original = threading.Thread(target=invoke, args=("original",), daemon=True)
    retry = threading.Thread(target=invoke, args=("retry",), daemon=True)
    original.start()
    assert leaf_started.wait(timeout=2)
    retry.start()
    assert pending_seen.wait(timeout=2)
    release_leaf.set()
    original.join(timeout=2)
    retry.join(timeout=2)

    assert not original.is_alive()
    assert not retry.is_alive()
    expected = {"committed": True, "value": 1}
    assert outcomes == {"original": expected, "retry": expected}
    assert calls == [1]


def test_terminal_receipt_survives_acknowledgement_cancellation(tmp_path: Path) -> None:
    calls: list[int] = []
    interrupt = True

    def after_terminal_persisted() -> None:
        nonlocal interrupt
        if interrupt:
            interrupt = False
            raise asyncio.CancelledError

    manager = LeaseManager(
        LeaseConfig(state_dir=tmp_path),
        after_terminal_persisted=after_terminal_persisted,
    )
    command = _command(
        writes=True,
        leaf=lambda value: calls.append(value) or {"committed": True, "value": value},
    )

    with pytest.raises(asyncio.CancelledError):
        manager.invoke(
            command,
            (),
            {"value": 1},
            idempotency_key="ack-lost",
            idempotency_principal_scope="principal:alice",
        )

    assert manager.invoke(
        command,
        (),
        {"value": 1},
        idempotency_key="ack-lost",
        idempotency_principal_scope="principal:alice",
    ) == {"committed": True, "value": 1}
    assert calls == [1]


def test_identical_orphaned_pending_reports_acknowledgement_uncertain(
    tmp_path: Path,
) -> None:
    manager = LeaseManager(
        LeaseConfig(state_dir=tmp_path),
        idempotency_wait_seconds=0,
    )
    command = _command(writes=True, leaf=lambda: pytest.fail("pending retry ran leaf"))
    digest = writer_lease_module._command_digest(command, {})
    key = writer_lease_module._effective_idempotency_key(
        manager,
        command=command,
        mutation_subject="standalone",
        digest=digest,
        idempotency_key="pending",
        principal_scope="principal:alice",
    )[0]
    with sqlite3.connect(manager.idempotency.path) as connection:
        connection.execute(
            "INSERT INTO mutations(key, digest, state, updated_at) "
            "VALUES (?, ?, 'pending', ?)",
            (key, digest, 100.0),
        )

    with pytest.raises(OpError) as pending:
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="pending",
            idempotency_principal_scope="principal:alice",
        )
    assert pending.value.code == "MUTATION_ACKNOWLEDGEMENT_PENDING"
    pending_payload = error_dict(pending.value)
    assert pending_payload["status"] == "uncertain"
    assert pending_payload["committed"] is None
    assert pending_payload["request_id"]
    assert pending_payload["idempotency_key"] == "pending"
    assert pending_payload["receipt_id"]


def test_different_identity_busy_is_precommit(tmp_path: Path) -> None:
    leaf_started = threading.Event()
    release_leaf = threading.Event()
    first_calls: list[str] = []
    second_calls: list[str] = []
    outcome: list[object] = []
    manager = LeaseManager(
        LeaseConfig(state_dir=tmp_path),
        mutation_timeout_seconds=0,
    )

    def first_leaf() -> str:
        first_calls.append("first")
        leaf_started.set()
        assert release_leaf.wait(timeout=2)
        return "committed"

    first = _command(writes=True, leaf=first_leaf)
    second = SimpleNamespace(
        name="other-mutation",
        read_only=False,
        leaf=lambda: second_calls.append("second") or "unexpected",
    )

    def invoke_first() -> None:
        try:
            outcome.append(
                manager.invoke(
                    first,
                    (),
                    {},
                    idempotency_key="first",
                    idempotency_principal_scope="alice",
                )
            )
        except BaseException as error:  # noqa: BLE001
            outcome.append(error)

    worker = threading.Thread(target=invoke_first, daemon=True)
    worker.start()
    assert leaf_started.wait(timeout=2)
    with pytest.raises(OpError) as busy:
        manager.invoke(
            second,
            (),
            {},
            idempotency_key="second",
            idempotency_principal_scope="alice",
        )
    assert busy.value.code == "MUTATION_BUSY"
    busy_payload = error_dict(busy.value)
    assert busy_payload["status"] == "retryable"
    assert busy_payload["committed"] is False
    assert busy_payload["retry_after_ms"] == 750
    assert busy_payload["request_id"]
    assert busy_payload["idempotency_key"] == "second"
    assert busy_payload["receipt_id"]
    busy_wire = json.loads(str(busy.value))
    assert busy_wire["ok"] is False
    assert busy_wire["error_code"] == "MUTATION_BUSY"
    assert busy_wire["request_id"] == busy_payload["request_id"]
    assert second_calls == []
    release_leaf.set()
    worker.join(timeout=2)
    assert outcome == ["committed"]
    assert first_calls == ["first"]


def test_postcommit_error_cannot_escape_as_precommit_retryable(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    def commits_then_misreports(vault: Path) -> None:
        nonlocal calls
        calls += 1
        batch_atomic_write(
            [PlannedWrite(target, "committed\n")],
            vault_root=vault,
        )
        raise OpError("MUTATION_BUSY", "misleading post-commit error")

    command = _command(writes=True, leaf=commits_then_misreports)
    for _ in range(2):
        with pytest.raises(OpError) as uncertain:
            manager.invoke(
                command,
                (tmp_path,),
                {},
                idempotency_key="postcommit-error",
                idempotency_principal_scope="principal:alice",
            )
        assert uncertain.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
        payload = error_dict(uncertain.value)
        assert payload["status"] == "committed"
        assert payload["committed"] is True

    assert target.read_text(encoding="utf-8") == "committed\n"
    assert calls == 1


def test_empty_batch_does_not_mark_a_commit(tmp_path: Path) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    calls = 0

    def no_commit(vault: Path) -> None:
        nonlocal calls
        calls += 1
        batch_atomic_write([], vault_root=vault)
        raise OpError("MUTATION_BUSY", "pre-commit rejection")

    command = _command(writes=True, leaf=no_commit)
    for _ in range(2):
        with pytest.raises(OpError) as busy:
            manager.invoke(command, (tmp_path,), {}, idempotency_key="empty-batch")
        assert busy.value.code == "MUTATION_BUSY"
        assert error_dict(busy.value)["committed"] is False

    assert calls == 2


def test_completed_result_receipt_failure_is_committed_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "note.md"
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))

    def commit(vault: Path) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        batch_atomic_write([PlannedWrite(target, "committed\n")], vault_root=vault)
        return {"committed": True}

    def fail_terminal_receipt(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("deterministic receipt write failure")

    monkeypatch.setattr(manager.idempotency, "_persist_completed", fail_terminal_receipt)
    command = _command(writes=True, leaf=commit)

    for _ in range(2):
        with pytest.raises(OpError) as uncertain:
            manager.invoke(
                command,
                (tmp_path,),
                {},
                idempotency_key="receipt-failure",
                idempotency_principal_scope="principal:alice",
            )
        assert uncertain.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
        assert error_dict(uncertain.value)["committed"] is True

    assert target.read_text(encoding="utf-8") == "committed\n"
    assert calls == 1


def test_uncommitted_result_receipt_failure_does_not_claim_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    manager = LeaseManager(
        LeaseConfig(state_dir=tmp_path / "state"), idempotency_wait_seconds=0
    )

    def validate_only() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"validate_only": True, "committed": False}

    def fail_terminal_receipt(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("deterministic receipt write failure")

    monkeypatch.setattr(manager.idempotency, "_persist_completed", fail_terminal_receipt)
    command = _command(writes=True, leaf=validate_only)

    for _ in range(2):
        with pytest.raises(OpError) as pending:
            manager.invoke(
                command,
                (),
                {},
                idempotency_key="validate-receipt-failure",
            )
        assert pending.value.code == "MUTATION_ACKNOWLEDGEMENT_PENDING"
        assert error_dict(pending.value)["committed"] is None

    assert calls == 1


def test_precommit_failure_releases_pending_receipt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path))
    command = _command(writes=True, leaf=lambda: (_ for _ in ()).throw(ValueError("no commit")))
    deletes = 0
    original_delete = manager.idempotency._delete_pending

    def counted_delete(key: str, digest: str) -> None:
        nonlocal deletes
        deletes += 1
        original_delete(key, digest)

    monkeypatch.setattr(manager.idempotency, "_delete_pending", counted_delete)

    with pytest.raises(ValueError, match="no commit"):
        manager.invoke(command, (), {}, idempotency_key="precommit-failure")

    assert deletes == 1


def test_hosted_audit_does_not_hold_mutation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_started = threading.Event()
    release_audit = threading.Event()
    audit_outcome: list[object] = []
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"), mutation_timeout_seconds=0)

    def audit_leaf(_vault: Path, *, mode: str = "audit") -> str:  # noqa: ARG001
        audit_started.set()
        assert release_audit.wait(timeout=2)
        return "audited"

    audit = SimpleNamespace(name="maintain_memory", read_only=False, leaf=audit_leaf)
    mutation = SimpleNamespace(
        name="remember", read_only=False, leaf=lambda _vault: "committed"
    )
    monkeypatch.setattr(writer_lease_module, "content_private_logging_enabled", lambda: True)

    def run_audit() -> None:
        try:
            audit_outcome.append(
                manager.invoke(audit, (tmp_path,), {"mode": "audit"}, read_only=True)
            )
        except BaseException as error:  # noqa: BLE001
            audit_outcome.append(error)

    worker = threading.Thread(target=run_audit, daemon=True)
    worker.start()
    assert audit_started.wait(timeout=2)
    assert manager.invoke(mutation, (tmp_path,), {}) == "committed"
    release_audit.set()
    worker.join(timeout=2)

    assert audit_outcome == ["audited"]


def test_explicit_idempotency_receipts_have_bounded_retention(tmp_path: Path) -> None:
    clock = Clock()
    calls: list[int] = []
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path), clock=clock)
    command = _command(writes=True, leaf=lambda: calls.append(1) or len(calls))

    assert manager.invoke(command, (), {}, idempotency_key="bounded-explicit") == 1
    assert manager.invoke(command, (), {}, idempotency_key="bounded-explicit") == 1
    clock.value += writer_lease_module._EXPLICIT_RETRY_TTL_SECONDS + 1
    assert manager.invoke(command, (), {}, idempotency_key="bounded-explicit") == 2
    assert calls == [1, 1]


def test_explicit_idempotency_is_isolated_by_principal(tmp_path: Path) -> None:
    calls: list[str] = []
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path))
    command = _command(writes=True, leaf=lambda: calls.append("write") or len(calls))

    assert manager.invoke(
        command,
        (),
        {},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:alice",
    ) == 1
    assert manager.invoke(
        command,
        (),
        {},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:bob",
    ) == 2
    assert manager.invoke(
        command,
        (),
        {},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:alice",
    ) == 1
    assert calls == ["write", "write"]


@pytest.mark.parametrize("implicit", [False, True], ids=["explicit", "implicit"])
def test_committed_cleanup_failure_replays_exact_public_payload_without_reinvoking(
    tmp_path: Path,
    implicit: bool,
) -> None:
    calls = 0
    original = _committed_error(tmp_path)

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise original

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=committed_failure)
    marker = (
        {"implicit_idempotency_scope": "alice"}
        if implicit
        else {"idempotency_key": "request-committed"}
    )

    with pytest.raises(vault_module.BatchWriteError) as first:
        manager.invoke(command, (), {}, **marker)
    with pytest.raises(ValueError) as replay:
        manager.invoke(command, (), {}, **marker)

    assert first.value is original
    assert replay.value is not original
    assert replay.value.as_public_dict() == original.as_public_dict()
    assert str(replay.value) == str(original)
    assert calls == 1


def test_committed_failure_replays_without_reacquiring_writer_authority(
    tmp_path: Path,
) -> None:
    calls = 0
    original = _committed_error(tmp_path)

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise original

    client = FakeClient(LeaseRecord("desktop", 99, 4))
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=client,
    )
    command = _command(writes=True, leaf=committed_failure)
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {}, idempotency_key="authority-bound")

    client.record = LeaseRecord("laptop", 99, 5)
    with pytest.raises(ValueError) as replay:
        manager.invoke(command, (), {}, idempotency_key="authority-bound")
    assert replay.value.as_public_dict() == original.as_public_dict()
    assert calls == 1
    assert client.acquisitions == 1


def test_committed_failure_persists_only_sanitized_public_json(tmp_path: Path) -> None:
    calls = 0
    original = _committed_error(tmp_path)

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise original

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=committed_failure)
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {}, idempotency_key="sanitized")

    _digest, state, stored = _row(manager, "sanitized")
    assert state == "committed_failure"
    assert json.loads(stored.decode("utf-8")) == original.as_public_dict()
    for secret in (
        str(tmp_path).encode(),
        b".exomem-batch-",
        b"stage-0.tmp",
        b"raw storage detail",
    ):
        assert secret not in stored
    assert calls == 1


def test_committed_failure_digest_mismatch_does_not_reinvoke(tmp_path: Path) -> None:
    calls: list[int] = []
    original = _committed_error(tmp_path)

    def committed_failure(value: int) -> None:
        calls.append(value)
        raise original

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=committed_failure)
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {"value": 1}, idempotency_key="same-key")
    with pytest.raises(OpError, match="IDEMPOTENCY_KEY_REUSED"):
        manager.invoke(command, (), {"value": 2}, idempotency_key="same-key")
    assert calls == [1]


def test_implicit_committed_failure_expires_under_retry_ttl(tmp_path: Path) -> None:
    clock = Clock()
    calls = 0

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise _committed_error(tmp_path)

    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=FakeClient(LeaseRecord("desktop", 99, 4)),
        clock=clock,
    )
    command = _command(writes=True, leaf=committed_failure)
    for _ in range(2):
        with pytest.raises(ValueError) as failure:
            manager.invoke(command, (), {}, implicit_idempotency_scope="alice")
        assert failure.value.as_public_dict()["outcome"]["committed"] is True
    assert calls == 1

    clock.value += 61
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {}, implicit_idempotency_scope="alice")
    assert calls == 2


@pytest.mark.parametrize(
    ("code", "committed"),
    [
        ("BATCH_ROLLBACK_INCOMPLETE", False),
        ("BATCH_CLEANUP_INCOMPLETE", False),
    ],
)
def test_uncommitted_batch_failures_remain_retryable(
    tmp_path: Path,
    code: str,
    committed: bool,
) -> None:
    attempts = 0

    def retryable() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise vault_module.BatchWriteError(
                code,
                vault_module.BatchTargetSummary(1, ("note.md",), 0),
                committed=committed,
            )
        return "ok"

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=retryable)
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {}, idempotency_key="retryable")
    assert manager.invoke(command, (), {}, idempotency_key="retryable") == "ok"
    assert attempts == 2


def _invalid_committed_payloads(valid: dict) -> list[tuple[str, dict]]:
    cases: list[tuple[str, dict]] = []

    def add(name: str, mutate) -> None:  # noqa: ANN001
        payload = deepcopy(valid)
        mutate(payload)
        cases.append((name, payload))

    add("missing top-level", lambda value: value.pop("message"))
    add("extra top-level", lambda value: value.update(extra="raw"))
    add("wrong code", lambda value: value.update(code="BATCH_ROLLBACK_INCOMPLETE"))
    add("wrong message", lambda value: value.update(message="raw detail"))
    add("wrong remediation", lambda value: value.update(remediation="retry it"))
    add("missing outcome", lambda value: value.pop("outcome"))
    add("extra outcome", lambda value: value["outcome"].update(extra="raw"))
    add("wrong kind", lambda value: value["outcome"].update(kind="rollback_incomplete"))
    add("nonliteral committed", lambda value: value["outcome"].update(committed=1))
    add("nonliteral incomplete", lambda value: value["outcome"].update(incomplete=1))
    add("boolean affected", lambda value: value["outcome"].update(affected_count=True))
    add("negative affected", lambda value: value["outcome"].update(affected_count=-1))
    add("boolean omitted", lambda value: value["outcome"].update(omitted_target_count=False))
    add("mismatched omitted", lambda value: value["outcome"].update(omitted_target_count=1))
    add("targets not list", lambda value: value["outcome"].update(targets=("note.md",)))
    add("too many targets", lambda value: value["outcome"].update(
        affected_count=17,
        targets=[f"note-{index}.md" for index in range(17)],
        omitted_target_count=0,
    ))
    for name, target in (
        ("empty target", ""),
        ("absolute target", "/vault/note.md"),
        ("backslash target", "folder\\note.md"),
        ("nul target", "folder/\0note.md"),
        ("dot target", "folder/./note.md"),
        ("parent target", "folder/../note.md"),
        ("drive target", "C:/vault/note.md"),
        ("reserved workspace target", "folder/.exomem-batch-raw/stage-0.tmp"),
        ("overlong target", f"{'x' * 1025}.md"),
        ("unencodable target", "bad-\udcff.md"),
    ):
        add(name, lambda value, target=target: value["outcome"].update(targets=[target]))
    return cases


@pytest.mark.parametrize(
    ("name", "payload"),
    _invalid_committed_payloads(
        vault_module.BatchWriteError(
            "BATCH_CLEANUP_INCOMPLETE",
            vault_module.BatchTargetSummary(1, ("note.md",), 0),
            committed=True,
        ).as_public_dict()
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_committed_failure_payload_validation_rejects_every_noncanonical_form(
    name: str,
    payload: dict,
) -> None:
    with pytest.raises(ValueError, match="committed failure payload"):
        writer_lease_module._validate_committed_failure_payload(payload)


def test_invalid_exception_payload_is_not_persisted(tmp_path: Path) -> None:
    calls = 0
    payload = _committed_error(tmp_path).as_public_dict()
    payload["raw"] = f"{tmp_path}/.exomem-batch-private/stage-0.tmp"

    class SpoofedFailure(ValueError):
        committed = True

        def as_public_dict(self) -> dict:
            return payload

    def invalid_failure() -> None:
        nonlocal calls
        calls += 1
        raise SpoofedFailure("raw")

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=invalid_failure)
    for _ in range(2):
        with pytest.raises(SpoofedFailure):
            manager.invoke(command, (), {}, idempotency_key="invalid")
    assert calls == 2


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        b"not-json",
        json.dumps(
            {
                **vault_module.BatchWriteError(
                    "BATCH_CLEANUP_INCOMPLETE",
                    vault_module.BatchTargetSummary(1, ("note.md",), 0),
                    committed=True,
                ).as_public_dict(),
                "raw": "private",
            }
        ).encode(),
    ],
    ids=["invalid-json", "extra-field"],
)
def test_corrupt_committed_failure_row_fails_closed_without_reinvoking(
    tmp_path: Path,
    corrupt_payload: bytes,
) -> None:
    calls = 0
    original = _committed_error(tmp_path)

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise original

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=committed_failure)
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {}, idempotency_key="corrupt")
    with sqlite3.connect(manager.idempotency.path) as connection:
        connection.execute(
            "UPDATE mutations SET result = ? WHERE key = ?",
            (corrupt_payload, _explicit_storage_key(manager, "corrupt")),
        )

    with pytest.raises(OpError) as blocked:
        manager.invoke(command, (), {}, idempotency_key="corrupt")
    assert blocked.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert "not-json" not in str(blocked.value)
    assert "private" not in str(blocked.value)
    assert calls == 1


def test_corrupt_implicit_committed_failure_timestamp_fails_closed_without_reinvoking(
    tmp_path: Path,
) -> None:
    calls = 0
    original = _committed_error(tmp_path)

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise original

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=committed_failure)
    marker = {"implicit_idempotency_scope": "alice"}
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {}, **marker)
    with sqlite3.connect(manager.idempotency.path) as connection:
        connection.execute(
            "UPDATE mutations SET updated_at = 'corrupt' "
            "WHERE state = 'committed_failure'"
        )

    with pytest.raises(OpError) as blocked:
        manager.invoke(command, (), {}, **marker)
    assert blocked.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert "corrupt" not in str(blocked.value)
    assert calls == 1


def test_expired_corrupt_implicit_committed_failure_payload_fails_closed_without_reinvoking(
    tmp_path: Path,
) -> None:
    clock = Clock()
    calls = 0
    original = _committed_error(tmp_path)

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise original

    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=FakeClient(LeaseRecord("desktop", 99, 4)),
        clock=clock,
    )
    command = _command(writes=True, leaf=committed_failure)
    marker = {"implicit_idempotency_scope": "alice"}
    with pytest.raises(vault_module.BatchWriteError):
        manager.invoke(command, (), {}, **marker)
    with sqlite3.connect(manager.idempotency.path) as connection:
        connection.execute(
            "UPDATE mutations SET result = ? WHERE state = 'committed_failure'",
            (b"not-json",),
        )

    clock.value += 61
    with pytest.raises(OpError) as blocked:
        manager.invoke(command, (), {}, **marker)
    assert blocked.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert "not-json" not in str(blocked.value)
    assert calls == 1


@pytest.mark.parametrize("failure_point", ["serialize", "update"])
def test_committed_marker_storage_failure_keeps_pending_and_blocks_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    calls = 0
    original = _committed_error(tmp_path)

    def committed_failure() -> None:
        nonlocal calls
        calls += 1
        raise original

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=committed_failure)
    if failure_point == "serialize":
        monkeypatch.setattr(
            writer_lease_module,
            "_serialize_committed_failure_payload",
            lambda payload: (_ for _ in ()).throw(OSError("private serialization detail")),
        )
    else:
        with sqlite3.connect(manager.idempotency.path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_committed_update "
                "BEFORE UPDATE ON mutations WHEN NEW.state = 'committed_failure' "
                "BEGIN SELECT RAISE(FAIL, 'private sqlite detail'); END"
            )

    with pytest.raises(vault_module.BatchWriteError) as first:
        manager.invoke(command, (), {}, idempotency_key="storage-failure")
    assert first.value is original
    assert first.value.__cause__ is not None
    assert "private" not in str(first.value)
    _digest, state, stored = _row(manager, "storage-failure")
    assert state == "pending"
    assert stored is None

    with pytest.raises(OpError) as blocked:
        manager.invoke(command, (), {}, idempotency_key="storage-failure")
    assert blocked.value.code == "MUTATION_ACKNOWLEDGEMENT_PENDING"
    assert calls == 1


def test_explicit_idempotency_blocks_orphaned_pending_after_process_abort(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    calls: list[str] = []

    def aborted() -> str:
        calls.append("aborted")
        raise SystemExit(70)

    with pytest.raises(SystemExit):
        manager.invoke(
            _command(writes=True, leaf=aborted),
            (),
            {},
            idempotency_key="request-after-crash",
        )

    recovered = _command(
        writes=True,
        leaf=lambda: calls.append("recovered") or "ok",
    )
    with pytest.raises(OpError) as blocked:
        manager.invoke(recovered, (), {}, idempotency_key="request-after-crash")
    assert blocked.value.code == "MUTATION_ACKNOWLEDGEMENT_PENDING"
    assert calls == ["aborted"]


def test_implicit_idempotency_is_bounded_and_principal_scoped(tmp_path: Path) -> None:
    clock = Clock()
    calls: list[int] = []
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=FakeClient(LeaseRecord("desktop", 99, 4)),
        clock=clock,
    )
    command = _command(writes=True, leaf=lambda value: calls.append(value) or {"value": value})

    assert manager.invoke(command, (), {"value": 1}, implicit_idempotency_scope="alice") == {
        "value": 1
    }
    assert manager.invoke(command, (), {"value": 1}, implicit_idempotency_scope="alice") == {
        "value": 1
    }
    assert manager.invoke(command, (), {"value": 1}, implicit_idempotency_scope="bob") == {
        "value": 1
    }
    assert calls == [1, 1]

    clock.value += 61
    assert manager.invoke(command, (), {"value": 1}, implicit_idempotency_scope="alice") == {
        "value": 1
    }
    assert calls == [1, 1, 1]


def test_failed_implicit_mutation_remains_retryable(tmp_path: Path) -> None:
    attempts = 0

    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("transient")
        return "ok"

    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=flaky)
    with pytest.raises(ValueError, match="transient"):
        manager.invoke(command, (), {}, implicit_idempotency_scope="alice")
    assert manager.invoke(command, (), {}, implicit_idempotency_scope="alice") == "ok"
    assert attempts == 2


def test_explicit_idempotency_also_works_without_writer_lease(tmp_path: Path) -> None:
    calls: list[int] = []
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path))
    command = _command(writes=True, leaf=lambda value: calls.append(value) or value)
    assert manager.invoke(command, (), {"value": 1}, idempotency_key="standalone-1") == 1
    assert manager.invoke(command, (), {"value": 1}, idempotency_key="standalone-1") == 1
    with pytest.raises(OpError, match="IDEMPOTENCY_KEY_REUSED"):
        manager.invoke(command, (), {"value": 2}, idempotency_key="standalone-1")
    assert calls == [1]


def test_explicit_idempotency_key_is_independent_across_vaults(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    vault_a.mkdir()
    vault_b.mkdir()
    calls: list[str] = []
    manager = LeaseManager(LeaseConfig(state_dir=state_root))
    command = _command(
        writes=True,
        leaf=lambda vault: calls.append(vault.name) or vault.name,
    )

    assert manager.invoke(command, (vault_a,), {}, idempotency_key="request-1") == "vault-a"
    assert manager.invoke(command, (vault_b,), {}, idempotency_key="request-1") == "vault-b"
    assert calls == ["vault-a", "vault-b"]


@dataclass
class Clock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


def test_sqlite_coordinator_exclusivity_expiry_takeover_and_fencing(tmp_path: Path) -> None:
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    desktop = store.acquire("main", "desktop", 10)
    assert desktop["granted"] and desktop["fencing_token"] == 1
    laptop = store.acquire("main", "laptop", 10)
    assert not laptop["granted"] and laptop["holder"] == "desktop"

    clock.value = 111
    laptop = store.acquire("main", "laptop", 10)
    assert laptop["granted"] and laptop["fencing_token"] == 2
    stale = store.renew("main", "desktop", desktop["fencing_token"], 10)
    assert not stale["granted"] and stale["holder"] == "laptop"


def test_release_allows_immediate_takeover_and_vaults_are_independent(tmp_path: Path) -> None:
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite")
    first = store.acquire("main", "desktop", 30)
    assert store.acquire("other", "laptop", 30)["granted"]
    assert store.release("main", "desktop", first["fencing_token"])["granted"]
    assert store.acquire("main", "laptop", 30)["granted"]


def test_coordination_status_is_a_read_only_public_command() -> None:
    from exomem.commands import product_commands_for

    for surface in ("mcp", "rest", "cli"):
        command = next(c for c in product_commands_for(surface) if c.name == "coordination_status")
        assert command.read_only
