from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.cli_ops import OpError, http_status_for
from exomem.lease_coordinator import SQLiteLeaseStore
from exomem.writer_lease import LeaseConfig, LeaseManager, LeaseRecord


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

    def _get(self) -> LeaseRecord:
        if isinstance(self.record, Exception):
            raise self.record
        return self.record

    def acquire(self) -> LeaseRecord:
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


def test_idempotency_returns_saved_result_and_rejects_mismatch(tmp_path: Path) -> None:
    calls: list[int] = []
    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(writes=True, leaf=lambda value: calls.append(value) or {"value": value})
    assert manager.invoke(command, (), {"value": 1}, idempotency_key="request-1") == {"value": 1}
    assert manager.invoke(command, (), {"value": 1}, idempotency_key="request-1") == {"value": 1}
    with pytest.raises(OpError, match="IDEMPOTENCY_KEY_REUSED"):
        manager.invoke(command, (), {"value": 2}, idempotency_key="request-1")
    assert calls == [1]


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
