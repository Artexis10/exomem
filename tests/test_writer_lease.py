from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import vault as vault_module
from exomem import writer_lease as writer_lease_module
from exomem.cli_ops import OpError, http_status_for
from exomem.lease_coordinator import SQLiteLeaseStore
from exomem.writer_lease import LeaseConfig, LeaseManager, LeaseRecord


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


def _row(manager: LeaseManager, key: str) -> tuple[str, str, bytes | None]:
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
            (corrupt_payload, "corrupt"),
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
    assert blocked.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert calls == 1


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
