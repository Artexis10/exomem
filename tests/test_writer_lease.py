from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import readiness
from exomem import vault as vault_module
from exomem import writer_lease as writer_lease_module
from exomem.cli_ops import OpError, error_dict, http_status_for
from exomem.lease_coordinator import SQLiteLeaseStore
from exomem.mutation_lock import VaultMutationCoordinator, active_mutation_snapshot
from exomem.mutation_terminal import committed_terminal
from exomem.vault import PlannedWrite, batch_atomic_write
from exomem.writer_lease import (
    IdempotencyStore,
    LeaseConfig,
    LeaseManager,
    LeaseRecord,
    SchemaAdmission,
    invoke_command,
    reset_managers_for_tests,
)

#: The wall-clock shape of every contention test in this file.
#:
#: A HOLD parks a thread or process while the test observes an ordering; an
#: OBSERVATION is how long the test waits for that state to be reached. The gap
#: between them is the entire discriminating power of these tests -- a hold that
#: does not outlast its observation lets the ordering pass vacuously, and an
#: observation sized for an idle laptop fails on a loaded shard while the code
#: under test behaves correctly.
#:
#: A NEGATIVE wait (`assert not x.wait(0.1)`) proves something has NOT happened
#: yet and stays tight: widening one changes the scenario rather than merely
#: slowing it, because the product's own timeouts run in the same window.
#:
#: `join(timeout=N)` followed by `assert t.is_alive()` is the SAME negative
#: observation in join form, and it is the one shape that consumes its whole
#: window on every healthy run -- it exists to prove a competitor is still
#: parked. Widening one from 0.3s to 60s bought nothing and cost a minute a run.
#:
#: Both constants stay strictly under pytest's per-test `timeout` (pyproject
#: `[tool.pytest.ini_options]`). A valve at or above it never gets to fire: the
#: harness kills the test first and you get a thread dump where a named
#: assertion should have been. tests/test_timing_assertion_hygiene.py pins that.
#:
#: These are not latency claims. Nothing here asserts the product is fast.
_HOLD_SECONDS = 45.0
_OBSERVE_SECONDS = 15.0

#: How long to let a thread run before asserting it is STILL parked.
#: Negative, so it stays tight -- a loaded runner can only make it more
#: true, never less, and this is the one window a healthy run always
#: spends in full.
_STILL_BLOCKED_SECONDS = 0.3

def _boundary(snapshot: dict) -> dict:
    """Drop the additive contention block so the boundary shape stays exact.

    Contention attribution is covered by `tests/test_readiness_honesty.py`;
    stripping only that key keeps these assertions exact-shape, so a future key
    leaking into the free payload still fails here.
    """
    return {key: value for key, value in snapshot.items() if key != "contention"}


class _UnknownLengthMapping(Mapping[str, str]):
    def __init__(self, item_count: int) -> None:
        self._values = {f"key-{index}": "private result content" for index in range(item_count)}
        self.items_iterated = 0

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        for key in self._values:
            self.items_iterated += 1
            if self.items_iterated > writer_lease_module._RECEIPT_RESULT_SUMMARY_MAX_ITEMS + 1:
                raise AssertionError("receipt summary consumed too many mapping items")
            yield key

    def __len__(self) -> int:
        raise TypeError("mapping length is unavailable")


def test_receipt_summary_closes_large_dict_without_visiting_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = writer_lease_module._receipt_result_summary
    visited = 0

    def counted(value: object, *, depth: int = 0) -> dict[str, object]:
        nonlocal visited
        visited += 1
        return original(value, depth=depth)

    monkeypatch.setattr(writer_lease_module, "_receipt_result_summary", counted)
    summary = original({f"key-{index}": "private result content" for index in range(200_000)})

    assert summary == {"type": "mapping", "size": 200_000, "truncated": True}
    assert visited == 0
    assert "private result content" not in json.dumps(summary)


def test_receipt_summary_closes_unknown_length_mapping_after_bounded_probe() -> None:
    mapping = _UnknownLengthMapping(writer_lease_module._RECEIPT_RESULT_SUMMARY_MAX_ITEMS + 50)

    summary = writer_lease_module._receipt_result_summary(mapping)

    assert summary == {"type": "mapping", "truncated": True}
    assert mapping.items_iterated == writer_lease_module._RECEIPT_RESULT_SUMMARY_MAX_ITEMS + 1
    assert "private result content" not in json.dumps(summary)


def test_receipt_result_digest_remains_stable_and_sensitive_for_small_mappings() -> None:
    first = {"z": "private alpha", "a": {"hidden": "one"}}
    same_values_different_order = {"a": {"hidden": "one"}, "z": "private alpha"}
    changed = {"a": {"hidden": "two"}, "z": "private alpha"}

    assert writer_lease_module._receipt_result_sha256(first) == writer_lease_module._receipt_result_sha256(
        same_values_different_order
    )
    assert writer_lease_module._receipt_result_sha256(first) != writer_lease_module._receipt_result_sha256(
        changed
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_idempotency_secret_runtime_artifacts_are_owner_only(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    store.run("secret-modes", "digest", lambda: {"ok": True})

    for path in (
        store.state_dir,
        store.state_dir / "idempotency-owners",
        store.path,
        store.path.with_name(f"{store.path.name}-wal"),
        store.path.with_name(f"{store.path.name}-shm"),
    ):
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_insecure_idempotency_database_is_rejected_before_unpickle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    database = state / "idempotency.sqlite"
    database.write_bytes(b"not sqlite")
    database.chmod(0o666)
    monkeypatch.setattr(
        writer_lease_module.pickle,
        "loads",
        lambda _raw: pytest.fail("unsafe idempotency state reached pickle.loads"),
    )

    store = IdempotencyStore(database)
    with pytest.raises(RuntimeError, match="cannot be upgraded safely"):
        store.run("unsafe", "digest", lambda: pytest.fail("unsafe state ran a leaf"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_legacy_owner_owned_runtime_state_is_hardened_before_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "legacy-state"
    state.mkdir(mode=0o755)
    database = state / "idempotency.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE mutations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
            "state TEXT NOT NULL, result BLOB, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO mutations VALUES (?, ?, 'completed', ?, 1.0)",
            ("legacy", "digest", sqlite3.Binary(__import__("pickle").dumps({"kept": True}))),
        )
    state.chmod(0o755)
    database.chmod(0o644)
    real_connect = writer_lease_module.sqlite3.connect

    def secure_connect(path: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        if Path(path) == database:
            assert stat.S_IMODE(database.stat().st_mode) == 0o600
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(writer_lease_module.sqlite3, "connect", secure_connect)
    store = IdempotencyStore(database)

    assert store.run("legacy", "digest", lambda: pytest.fail("legacy replay lost")) == {
        "kept": True
    }
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_legacy_owner_lock_is_hardened_with_runtime_state(tmp_path: Path) -> None:
    state = tmp_path / "legacy-lock-state"
    owners = state / "idempotency-owners"
    owners.mkdir(parents=True, mode=0o755)
    lock = owners / "legacy.lock"
    lock.write_bytes(b"")
    state.chmod(0o755)
    owners.chmod(0o755)
    lock.chmod(0o644)

    IdempotencyStore(state / "idempotency.sqlite")

    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


class _EnvelopeProtector:
    """Portable stand-in for the Windows DPAPI boundary."""

    provider = 1

    def __init__(self) -> None:
        self.protect_calls: list[tuple[bytes, bytes]] = []
        self.unprotect_calls: list[tuple[bytes, bytes]] = []
        self._values: dict[bytes, tuple[bytes, bytes]] = {}

    def protect(self, secret: bytes, entropy: bytes) -> bytes:
        self.protect_calls.append((secret, entropy))
        ciphertext = b"protected:" + len(self._values).to_bytes(2, "big") + secret[::-1]
        self._values[ciphertext] = (secret, entropy)
        return ciphertext

    def unprotect(self, ciphertext: bytes, entropy: bytes) -> bytes:
        self.unprotect_calls.append((ciphertext, entropy))
        return self._values[ciphertext][0] if self._values[ciphertext][1] == entropy else b""


class _FixedCiphertextProtector:
    provider = 1

    def __init__(self, ciphertext: bytes) -> None:
        self.ciphertext = ciphertext

    def protect(self, _secret: bytes, _entropy: bytes) -> bytes:
        return self.ciphertext


def test_protected_attempt_secret_never_writes_plaintext_to_sqlite(tmp_path: Path) -> None:
    protector = _EnvelopeProtector()
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite", secret_protector=protector)
    digest = "a" * 64

    assert store._claim_or_inspect("protected", digest, None) == ("owner", None)
    attempt = store._attempts["protected"]
    with sqlite3.connect(store.path) as connection:
        stored = connection.execute(
            "SELECT commit_secret FROM mutations WHERE key = 'protected'"
        ).fetchone()[0]

    assert stored != attempt.commit_secret
    assert stored.startswith(writer_lease_module._WINDOWS_SECRET_ENVELOPE_MAGIC)
    assert store._read_exact_evidence(
        lambda _digest, _attempt_id, _token, secret: secret == attempt.commit_secret,
        digest,
        writer_lease_module._ExecutionAttempt(
            attempt.attempt_id, attempt.commit_token, stored, None
        ),
    ) is True
    assert len(protector.protect_calls) == len(protector.unprotect_calls) == 1
    assert protector.protect_calls[0][1] == (
        b"exomem-graph-commit-receipt-dpapi:v1\0"
        + attempt.attempt_id.encode("utf-8")
        + b"\0"
        + attempt.commit_token.encode("utf-8")
    )
    assert protector.unprotect_calls[0][1] == protector.protect_calls[0][1]


def test_protected_attempt_secret_envelope_caps_total_blob_at_4096_bytes(tmp_path: Path) -> None:
    attempt = writer_lease_module._ExecutionAttempt("a" * 24, "b" * 24, b"s" * 32, None)
    digest = "d" * 64
    accepted = IdempotencyStore(
        tmp_path / "accepted.sqlite", secret_protector=_FixedCiphertextProtector(b"x" * 4086)
    )

    envelope = accepted._stored_commit_secret(digest, attempt)

    assert len(envelope) == 4096
    assert envelope[:4] == writer_lease_module._WINDOWS_SECRET_ENVELOPE_MAGIC
    assert envelope[4:6] == b"\x01\x01"
    assert int.from_bytes(envelope[6:10], "big") == 4086
    assert envelope[10:] == b"x" * 4086

    rejected = IdempotencyStore(
        tmp_path / "rejected.sqlite", secret_protector=_FixedCiphertextProtector(b"x" * 4087)
    )
    with pytest.raises(RuntimeError, match="invalid ciphertext"):
        rejected._stored_commit_secret(digest, attempt)


@pytest.mark.parametrize(
    "stored",
    [
        b"EXID\x02\x01" + (1).to_bytes(4, "big") + b"x",
        b"EXID\x01\x02" + (1).to_bytes(4, "big") + b"x",
        b"EXID\x01\x01" + (2).to_bytes(4, "big") + b"x",
        b"EXID\x01\x01" + (1).to_bytes(4, "big") + b"xy",
        b"EXID\x01\x01" + (4087).to_bytes(4, "big") + b"x" * 4087,
    ],
)
def test_protected_attempt_secret_rejects_non_exact_envelopes(
    tmp_path: Path, stored: bytes
) -> None:
    protector = _EnvelopeProtector()
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite", secret_protector=protector)
    attempt = writer_lease_module._ExecutionAttempt("a" * 24, "b" * 24, stored, None)

    assert store._unprotected_commit_secret("e" * 64, attempt) is None
    assert protector.unprotect_calls == []


@pytest.mark.parametrize("replacement", [b"x" * 32, b"", b"EXID\x01\x01\x00\x00\x00\x20x"])
def test_malformed_or_legacy_protected_attempt_secret_fails_closed(
    tmp_path: Path, replacement: bytes
) -> None:
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite", secret_protector=_EnvelopeProtector())
    digest = "b" * 64
    assert store._claim_or_inspect("protected", digest, None) == ("owner", None)
    attempt = store._attempts["protected"]

    assert store._read_exact_evidence(
        lambda *_args: pytest.fail("invalid secret reached receipt verification"),
        digest,
        writer_lease_module._ExecutionAttempt(
            attempt.attempt_id, attempt.commit_token, replacement, None
        ),
    ) is None


def test_protected_attempt_secret_binds_the_attempt_identity(tmp_path: Path) -> None:
    protector = _EnvelopeProtector()
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite", secret_protector=protector)
    digest = "c" * 64
    assert store._claim_or_inspect("one", digest, None) == ("owner", None)
    assert store._claim_or_inspect("two", digest, None) == ("owner", None)
    one = store._attempts["one"]
    two = store._attempts["two"]
    with sqlite3.connect(store.path) as connection:
        stored_one = connection.execute("SELECT commit_secret FROM mutations WHERE key = 'one'").fetchone()[0]
        stored_two = connection.execute("SELECT commit_secret FROM mutations WHERE key = 'two'").fetchone()[0]

    assert protector.protect_calls[0][1] != protector.protect_calls[1][1]
    assert store._read_exact_evidence(
        lambda *_args: pytest.fail("swapped envelope reached receipt verification"),
        digest,
        writer_lease_module._ExecutionAttempt(two.attempt_id, two.commit_token, stored_one, None),
    ) is None
    assert store._read_exact_evidence(
        lambda _digest, _attempt_id, _token, secret: secret == one.commit_secret,
        digest,
        writer_lease_module._ExecutionAttempt(one.attempt_id, one.commit_token, stored_one, None),
    ) is True
    assert stored_one != stored_two


def test_protection_failure_refuses_before_creating_an_execution_row(tmp_path: Path) -> None:
    class BrokenProtector:
        provider = 1

        def protect(self, _secret: bytes, _entropy: bytes) -> bytes:
            raise OSError("DPAPI unavailable")

    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite", secret_protector=BrokenProtector())

    with pytest.raises(OpError) as error:
        store.run("unprotected", "d" * 64, lambda: pytest.fail("leaf ran"))
    assert error.value.code == "IDEMPOTENCY_SECRET_PROTECTION_UNAVAILABLE"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM mutations").fetchone() == (0,)


def test_legacy_raw_attempt_secret_never_heals_a_dead_execution(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite", secret_protector=_EnvelopeProtector())
    digest = "e" * 64
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO mutations(key, digest, state, updated_at, owner, attempt_id, commit_token, commit_secret) "
            "VALUES (?, ?, 'executing', ?, ?, ?, ?, ?)",
            (
                "legacy-raw", digest, 0.0, "dead-owner", "a" * 24, "b" * 24, b"x" * 32,
            ),
        )

    with pytest.raises(OpError) as error:
        store.run(
            "legacy-raw", digest, lambda: pytest.fail("legacy raw row replayed"),
            commit_evidence=lambda *_args: pytest.fail("legacy raw row verified"),
        )
    assert error.value.code == "MUTATION_OUTCOME_UNKNOWN"


def test_windows_runtime_dacl_parser_rejects_a_broadened_or_unprotected_namespace() -> None:
    from exomem.mutation_lock import _windows_private_dacl_is_valid

    sid = "S-1-5-21-1-2-3-4"
    protected = f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    inherited_file = f"D:(A;ID;FA;;;{sid})(A;ID;FA;;;SY)(A;ID;FA;;;BA)"

    assert _windows_private_dacl_is_valid(protected, sid, directory=True)
    assert _windows_private_dacl_is_valid(inherited_file, sid, directory=False)
    assert not _windows_private_dacl_is_valid(
        protected + "(A;OICI;FA;;;WD)", sid, directory=True
    )
    assert not _windows_private_dacl_is_valid(
        protected.removeprefix("D:P").join(("D:", "")), sid, directory=True
    )


def test_windows_runtime_validation_closes_every_non_reparse_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import mutation_lock

    database = tmp_path / "idempotency.sqlite"
    database.write_bytes(b"sqlite")
    closed: list[int] = []
    sid = "S-1-5-21-1-2-3-4"
    monkeypatch.setattr(mutation_lock, "_windows_open_path", lambda _path, *, directory: 10)
    monkeypatch.setattr(mutation_lock, "_windows_close_handle", closed.append)
    monkeypatch.setattr(
        mutation_lock,
        "_windows_dacl_sddl",
        lambda _path: f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
    )

    mutation_lock._validate_windows_runtime_entry(
        database, directory=False, sid=sid
    )

    assert closed == [10]


def test_windows_runtime_validation_walks_and_closes_every_ancestor_before_sqlite_leaf(
    tmp_path: Path,
) -> None:
    """The Windows path walk is injectable, so this guards it off Windows too."""
    from exomem import mutation_lock

    opened: list[Path] = []
    closed: list[int] = []
    next_handle = iter(range(100, 200))

    def open_path(path: Path, *, directory: bool) -> int:
        del directory
        opened.append(path)
        if path.name == "runtime":
            raise OSError("reparse points are not allowed")
        return next(next_handle)

    with pytest.raises(OSError, match="reparse"):
        mutation_lock._acquire_windows_secure_directory(
            tmp_path / "runtime" / "state",
            create=False,
            mode=0o700,
            open_path=open_path,
            close_handle=closed.append,
        )

    assert any(path.name == "runtime" for path in opened)
    assert closed == list(reversed(range(100, 100 + len(closed))))


def _committed_error(tmp_path: Path, *, targets: tuple[str, ...] = ("note.md",)):
    raw = PermissionError(f"{tmp_path}/.exomem-batch-{'a' * 32}/stage-0.tmp: raw storage detail")
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
    assert LeaseConfig.from_env({}).schema_version == 4
    with pytest.raises(ValueError, match="WRITER_LEASE_CONFIG"):
        LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_URL": "https://lease.example"})


def test_record_replay_receipt_has_a_noop_terminal(tmp_path: Path) -> None:
    from exomem.command_surface import Command

    receipt = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "append",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": "22222222-2222-4222-8222-222222222222",
        "before_item_hash": "a" * 64,
        "after_item_hash": "a" * 64,
        "before_container_hash": "b" * 64,
        "after_container_hash": "b" * 64,
        "affected_paths": ["Knowledge Base/Records/log.md"],
        "payload_hash": "c" * 64,
        "outcome": "replayed",
        "audit_correlation": "d" * 24,
    }
    command = Command(
        name="record_memory",
        leaf=lambda _root: receipt,
        params=(),
        surfaces=frozenset({"mcp"}),
        cli_writes=True,
    )

    result = LeaseManager(
        LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "state")})
    ).invoke(command, (tmp_path,), {})

    assert result["status"] == "replayed"
    assert result["mutated"] is False
    assert result["outcome"] == "replayed"


def test_lifecycle_idempotency_replay_keeps_committed_receipt_and_replays_terminal(tmp_path: Path) -> None:
    receipt = {
        "_record_receipt": "exomem.records-mutation", "receipt_version": 2,
        "operation": "revise", "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": None, "before_item_hash": None, "after_item_hash": None,
        "before_manifest_hash": "a" * 64, "after_manifest_hash": "b" * 64,
        "before_container_hash": "c" * 64, "after_container_hash": "d" * 64,
        "affected_paths": ["Knowledge Base/Records/Test/_collection.md"], "payload_hash": "e" * 64,
        "outcome": "committed", "audit_correlation": "f" * 24, "continuity": True,
        "acknowledged_gap_codes": [], "gap_fingerprint": None, "checkpoint_snapshot_hash": None,
        "minimum_reader_version": 2,
    }
    terminal = committed_terminal(receipt, request_id="first", receipt_id="r", idempotency_key="same")
    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    assert store.run("same", "digest", lambda: terminal) == terminal
    replay = store.run("same", "digest", lambda: pytest.fail("lifecycle leaf reran"))
    assert replay["status"] == "replayed" and replay["mutated"] is False
    assert replay["leaf_result"] == receipt


def test_mutation_timeout_stays_within_the_edge_budget_and_is_tunable() -> None:
    """The boundary wait is a share of the edge budget, not a free parameter.

    The HA edge worker abandons a mutation-capable request at
    MCP_TOOL_TIMEOUT_MS (default 60s) and never replays it, because the origin
    may commit after the edge stops waiting. Time spent queueing here is time
    unavailable to the write. A default at or near the edge timeout would make
    a 504-with-a-committed-write the *expected* outcome under contention
    rather than the incident it is.
    """
    # The edge budget this must stay under: MCP_TOOL_TIMEOUT_MS default, in
    # seconds. Formerly mirrored from the HA edge worker's MCP_TOOL_TIMEOUT_MS;
    # that worker is retired, so this value now stands on its own here.
    edge_tool_budget_seconds = 60.0
    default = LeaseConfig.from_env({}).mutation_timeout_seconds
    assert default == 5.0
    # The invariant that matters: waiting must leave the majority of the edge
    # budget for the write itself.
    assert default < edge_tool_budget_seconds / 2
    tuned = LeaseConfig.from_env({"EXOMEM_MUTATION_TIMEOUT": "8"})
    assert tuned.mutation_timeout_seconds == 8.0
    # Misconfiguration fails loudly rather than silently reverting to a default,
    # matching every other lease timeout. A silently-ignored value here would
    # leave an operator believing they had widened the boundary when they had not.
    with pytest.raises(ValueError, match="EXOMEM_MUTATION_TIMEOUT"):
        LeaseConfig.from_env({"EXOMEM_MUTATION_TIMEOUT": "not-a-number"})
    with pytest.raises(ValueError, match="EXOMEM_MUTATION_TIMEOUT"):
        LeaseConfig.from_env({"EXOMEM_MUTATION_TIMEOUT": "0"})


def test_default_manager_uses_configured_mutation_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "8")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path))
    try:
        manager = writer_lease_module.get_manager()
        assert manager.config.mutation_timeout_seconds == 8.0
        assert manager._mutation_timeout_seconds == 8.0
    finally:
        reset_managers_for_tests()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_default_manager_hardens_legacy_configured_state_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Released default-manager state upgrades before its idempotency DB is opened."""
    state = tmp_path / "legacy-default-state"
    state.mkdir(mode=0o755)
    safe_name = __import__("hashlib").sha256(b"standalone\0standalone").hexdigest()[:20]
    database = state / f"idempotency-{safe_name}.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE mutations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
            "state TEXT NOT NULL, result BLOB, updated_at REAL NOT NULL)"
        )
    state.chmod(0o755)
    database.chmod(0o644)
    reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state))
    try:
        manager = writer_lease_module.get_manager()
        assert manager.idempotency.path == database
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
    finally:
        reset_managers_for_tests()


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
    assert "vault_id" not in status


def test_status_reports_ttl_remaining_and_renewer_liveness(tmp_path: Path) -> None:
    expires_at = time.time() + 42.0
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=FakeClient(LeaseRecord("desktop", expires_at, 7)),
    )
    status = manager.status()
    assert status["ttl_remaining_seconds"] == pytest.approx(42.0, abs=1.0)
    assert status["renewer_alive"] is False
    assert status["last_renew_age_seconds"] is None
    assert status["last_coordinator_error"] is None


def test_status_records_coordinator_error_instead_of_silently_returning_unhealthy(
    tmp_path: Path,
) -> None:
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=FakeClient(OpError("WRITER_COORDINATOR_UNAVAILABLE", "down")),
    )
    status = manager.status()
    assert status["coordinator_healthy"] is False
    assert status["last_coordinator_error"]["code"] == "WRITER_COORDINATOR_UNAVAILABLE"
    assert status["last_coordinator_error"]["age_seconds"] >= 0


def test_status_last_coordinator_error_persists_across_a_later_healthy_status(
    tmp_path: Path,
) -> None:
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
        ),
        client=FakeClient(OpError("WRITER_COORDINATOR_UNAVAILABLE", "down")),
    )
    manager.status()
    manager.client.record = LeaseRecord("desktop", time.time() + 10, 7)
    status = manager.status()
    assert status["coordinator_healthy"] is True
    assert status["last_coordinator_error"]["code"] == "WRITER_COORDINATOR_UNAVAILABLE"


def test_renewer_alive_reflects_a_running_renew_thread(tmp_path: Path) -> None:
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
            ttl_seconds=300.0,
        ),
        client=FakeClient(LeaseRecord("desktop", time.time() + 300, 1)),
    )
    manager.start_renewer()
    try:
        assert manager.status()["renewer_alive"] is True
    finally:
        manager.close()


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


def test_coordinator_acquire_and_renew_attest_the_current_schema(monkeypatch) -> None:
    from exomem.writer_lease import LeaseCoordinatorClient

    bodies: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"holder":"desktop","expires_at":99,"fencing_token":7,"granted":true}'

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        bodies.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LeaseCoordinatorClient(
        LeaseConfig(url="https://lease.example", vault_id="main", replica_id="desktop")
    )

    client.acquire()
    client.renew(7)

    assert bodies == [
        {"replica_id": "desktop", "ttl_seconds": 30.0, "schema_version": 4},
        {
            "replica_id": "desktop",
            "fencing_token": 7,
            "ttl_seconds": 30.0,
            "schema_version": 4,
        },
    ]


def test_coordinator_schema_admission_uses_the_external_gate(monkeypatch) -> None:
    from exomem.writer_lease import LeaseCoordinatorClient

    seen: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return (
                b'{"admitted":false,"governance_enrolled":true,'
                b'"required_schema_version":4,"schema_fence_generation":8}'
            )

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LeaseCoordinatorClient(
        LeaseConfig(url="https://lease.example", vault_id="main", replica_id="old-v3")
    )

    result = client.schema_admission(3)

    assert seen == {
        "url": "https://lease.example/v1/vaults/main/schema-fence/admit",
        "body": {"replica_id": "old-v3", "schema_version": 3},
    }
    assert result.admitted is False
    assert result.required_schema_version == 4
    assert result.schema_fence_generation == 8


def test_coordinator_schema_fence_operator_reads_and_advances_exact_generation(
    monkeypatch,
) -> None:
    from exomem.writer_lease import LeaseCoordinatorClient

    seen: list[tuple[str, str, object]] = []
    responses = iter(
        (
            b'{"governance_enrolled":true,"schema_version":3,"generation":8}',
            b'{"governance_enrolled":true,"schema_version":4,"generation":9}',
        )
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return next(responses)

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        seen.append(
            (
                request.method,
                request.full_url,
                None if request.data is None else json.loads(request.data),
            )
        )
        assert request.headers["Authorization"] == "Bearer operator-secret"
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LeaseCoordinatorClient(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="offline-coordinator",
            token="operator-secret",
        )
    )

    current = client.schema_fence()
    advanced = client.transition_schema_fence(
        expected_generation=current.generation,
        schema_version=4,
    )

    assert (current.schema_version, current.generation) == (3, 8)
    assert (advanced.schema_version, advanced.generation) == (4, 9)
    assert seen == [
        (
            "GET",
            "https://lease.example/v1/vaults/main/schema-fence",
            None,
        ),
        (
            "PUT",
            "https://lease.example/v1/vaults/main/schema-fence",
            {"expected_generation": 8, "schema_version": 4},
        ),
    ]


def test_configured_schema_fence_operator_requires_a_distinct_protected_token() -> None:
    from exomem.writer_lease import configured_schema_fence_operator_client

    base = {
        "EXOMEM_WRITER_LEASE_URL": "https://lease.example",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "main",
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "offline-coordinator",
        "EXOMEM_WRITER_LEASE_TOKEN": "ordinary-secret",
    }

    with pytest.raises(OpError, match="operator credential"):
        configured_schema_fence_operator_client(base)
    with pytest.raises(OpError, match="distinct"):
        configured_schema_fence_operator_client(
            {**base, "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN": "ordinary-secret"}
        )

    client = configured_schema_fence_operator_client(
        {**base, "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN": "operator-secret"}
    )
    assert client is not None
    assert client.config.token == "operator-secret"


@pytest.mark.parametrize(
    ("response", "operation"),
    [
        (
            b'{"holder":"desktop","expires_at":99,"fencing_token":7,'
            b'"granted":"true"}',
            "acquire",
        ),
        (
            b'{"admitted":true,"governance_enrolled":true,'
            b'"required_schema_version":4,"schema_fence_generation":8}',
            "schema_admission",
        ),
    ],
)
def test_coordinator_schema_responses_fail_closed_as_unavailable(
    monkeypatch,
    response: bytes,
    operation: str,
) -> None:
    from exomem.writer_lease import LeaseCoordinatorClient

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return response

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    client = LeaseCoordinatorClient(
        LeaseConfig(url="https://lease.example", vault_id="main", replica_id="desktop")
    )

    with pytest.raises(OpError) as raised:
        if operation == "acquire":
            client.acquire()
        else:
            client.schema_admission(3)

    assert raised.value.code == "WRITER_COORDINATOR_UNAVAILABLE"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "admitted": False,
            "governance_enrolled": True,
            "required_schema_version": None,
            "schema_fence_generation": None,
        },
        {
            "admitted": False,
            "governance_enrolled": False,
            "required_schema_version": 4,
            "schema_fence_generation": 8,
        },
        {
            "admitted": True,
            "governance_enrolled": True,
            "required_schema_version": 4,
            "schema_fence_generation": 8,
            "unexpected": "field",
        },
    ],
)
def test_schema_admission_rejects_inconsistent_or_open_responses(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="schema admission"):
        SchemaAdmission.from_json(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "holder": None,
            "expires_at": None,
            "fencing_token": 9,
            "granted": False,
            "governance_enrolled": True,
        },
        {
            "holder": None,
            "expires_at": None,
            "fencing_token": 9,
            "granted": False,
            "governance_enrolled": False,
            "required_schema_version": 4,
            "schema_fence_generation": 8,
        },
    ],
)
def test_lease_record_rejects_inconsistent_schema_fence_metadata(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="schema fence metadata"):
        LeaseRecord.from_json(payload)


def _coordinator_answering(monkeypatch, error: Exception):
    """Point the lease client at a URL that answers with `error`."""

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    from exomem.writer_lease import LeaseCoordinatorClient

    return LeaseCoordinatorClient(
        LeaseConfig(url="https://lease.example", vault_id="main", replica_id="desktop")
    )


def test_a_url_that_does_not_serve_the_lease_contract_is_not_an_outage(monkeypatch) -> None:
    """404 is a configuration answer, and it will not change on the next try.

    Collapsing it into WRITER_COORDINATOR_UNAVAILABLE made a misconfigured lease
    URL indistinguishable from a coordinator that was merely down, so the
    availability probe kept asking at the follower cadence -- 1,200 requests in
    25 minutes across seven server processes, every one a 404, none of them ever
    saying why.
    """
    import urllib.error

    client = _coordinator_answering(
        monkeypatch,
        urllib.error.HTTPError("https://lease.example", 404, "Not Found", {}, None),
    )

    with pytest.raises(OpError) as raised:
        client.acquire()

    error = raised.value
    assert error.code == "WRITER_COORDINATOR_CONTRACT_ABSENT"
    assert error.details["status"] == 404
    # The route it asked for, so the operator can see it is the lease contract
    # that is missing rather than the whole host.
    assert "/v1/vaults/<id>/lease/acquire" in error.message
    assert "Retrying cannot" in (error.remediation or "")


@pytest.mark.parametrize("status", [404, 405, 501])
def test_every_contract_absent_status_is_classified_alike(monkeypatch, status: int) -> None:
    """405 and 501 say the same thing 404 does: not served here.

    A coordinator that is present answers the lease route; one that is present
    and refuses the METHOD, or declares the operation unimplemented, is the
    wrong service or the wrong version. Neither is fixed by waiting.
    """
    import urllib.error

    client = _coordinator_answering(
        monkeypatch,
        urllib.error.HTTPError("https://lease.example", status, "nope", {}, None),
    )

    with pytest.raises(OpError) as raised:
        client.acquire()

    assert raised.value.code == "WRITER_COORDINATOR_CONTRACT_ABSENT"


@pytest.mark.parametrize("status", [500, 502, 503, 401, 403])
def test_a_coordinator_that_is_present_stays_transiently_unavailable(
    monkeypatch, status: int
) -> None:
    """Everything else keeps the old code, and the old fail-closed handling.

    A 5xx is a coordinator having a bad moment and a 401/403 is a credential
    the operator can fix in place; both are worth asking again, and neither
    should inherit the slow cadence meant for a URL that serves no contract.
    """
    import urllib.error

    client = _coordinator_answering(
        monkeypatch,
        urllib.error.HTTPError("https://lease.example", status, "nope", {}, None),
    )

    with pytest.raises(OpError) as raised:
        client.acquire()

    assert raised.value.code == "WRITER_COORDINATOR_UNAVAILABLE"


def test_an_unreachable_coordinator_stays_transiently_unavailable(monkeypatch) -> None:
    """A transport failure never reached a service, so it classifies nothing."""
    import urllib.error

    client = _coordinator_answering(monkeypatch, urllib.error.URLError("connection refused"))

    with pytest.raises(OpError) as raised:
        client.acquire()

    assert raised.value.code == "WRITER_COORDINATOR_UNAVAILABLE"


def test_a_credential_in_the_coordinator_url_is_not_echoed_back(monkeypatch) -> None:
    """`https://user:secret@host/` is a legal coordinator URL.

    The refusal reaches the server log and, through the error payload, MCP
    clients. Naming the host is the point of the message; naming the password
    is a leak.
    """
    import urllib.error

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        raise urllib.error.HTTPError("https://lease.example", 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    from exomem.writer_lease import LeaseCoordinatorClient

    client = LeaseCoordinatorClient(
        LeaseConfig(
            url="https://operator:hunter2@lease.example:8443",
            vault_id="main",
            replica_id="desktop",
        )
    )

    with pytest.raises(OpError) as raised:
        client.acquire()

    rendered = f"{raised.value.message} {raised.value.details}"
    assert "hunter2" not in rendered
    assert "operator" not in rendered
    assert raised.value.details["coordinator_url"] == "https://lease.example:8443"


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
        return LeaseRecord.from_json(self.store.renew("main", self.replica_id, fencing_token, 10))

    def release(self, fencing_token: int) -> LeaseRecord:
        return LeaseRecord.from_json(self.store.release("main", self.replica_id, fencing_token))


def test_manager_names_schema_fence_refusal_without_acquiring_writer(
    tmp_path: Path,
) -> None:
    class RefusingClient:
        def acquire(self) -> LeaseRecord:
            return LeaseRecord(
                None,
                None,
                9,
                False,
                required_schema_version=3,
                schema_fence_generation=7,
                governance_enrolled=True,
            )

    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="current-v4",
            state_dir=tmp_path,
        ),
        client=RefusingClient(),  # type: ignore[arg-type]
    )

    with pytest.raises(OpError) as raised:
        manager.ensure_writer()

    assert raised.value.code == "WRITER_SCHEMA_FENCE_MISMATCH"
    assert raised.value.details == {
        "required_schema_version": 3,
        "schema_fence_generation": 7,
    }

class BlockingRejectedRenewalClient(FakeClient):
    def __init__(self):
        super().__init__(LeaseRecord("desktop", 200, 3))
        self.renew_started = threading.Event()
        self.resume_renewal = threading.Event()

    def renew(self, fencing_token: int) -> LeaseRecord:
        assert fencing_token == 1
        self.renew_started.set()
        assert self.resume_renewal.wait(_HOLD_SECONDS)
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


def test_hosted_reads_never_contact_the_coordinator_or_wait_for_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Superseded contract (R3): hosted reads no longer take the mutation
    boundary at all — they behave exactly like local reads, because atomic
    staging already makes a concurrent whole-file read torn-free. This
    supersedes the archived decision at
    openspec/changes/archive/2026-07-20-make-mcp-acknowledgement-replay-safe/design.md:64.
    """
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "true")
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "state"
    manager = _manager(state_root, OpError("WRITER_COORDINATOR_UNAVAILABLE", "down"))
    coordinator = VaultMutationCoordinator(state_root, vault)
    boundary_entered = threading.Event()
    release_boundary = threading.Event()

    def hold_mutation() -> None:
        with coordinator.hold(timeout_seconds=2.0):
            boundary_entered.set()
            assert release_boundary.wait(_HOLD_SECONDS)

    writer = threading.Thread(target=hold_mutation)
    writer.start()
    try:
        assert boundary_entered.wait(_OBSERVE_SECONDS)
        # The read returns immediately without waiting for release — it
        # never even attempts to acquire the boundary a concurrent writer
        # is holding.
        assert (
            manager.invoke(
                _command(writes=False, leaf=lambda _vault: "read-ok"),
                (vault,),
                {},
            )
            == "read-ok"
        )
    finally:
        release_boundary.set()
        writer.join(timeout=_HOLD_SECONDS)
    assert not writer.is_alive()


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
            assert release.wait(_HOLD_SECONDS)

    thread = threading.Thread(target=hold_mutation)
    thread.start()
    assert entered.wait(_OBSERVE_SECONDS)
    manager = LeaseManager(LeaseConfig(state_dir=state_root))
    try:
        assert (
            manager.invoke(
                _command(writes=False, leaf=lambda _vault: "read"),
                (vault,),
                {},
            )
            == "read"
        )
    finally:
        release.set()
        thread.join(timeout=_HOLD_SECONDS)
    assert not thread.is_alive()


def test_hosted_plain_read_bypasses_boundary_held_by_other_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `test_hosted_public_audit_routes_bypass_boundary_held_by_other_manager`,
    but for an ORDINARY (non-audit) hosted read — proving R3's universal
    bypass rather than the superseded audit-only allowlist."""
    state_dir = tmp_path / "shared-state"
    vault = tmp_path / "vault"
    vault.mkdir()
    holder = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.0)
    reader = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.0)
    boundary_held = threading.Event()
    release_boundary = threading.Event()

    def hold_boundary() -> None:
        with holder.mutation_guard(vault):
            boundary_held.set()
            assert release_boundary.wait(_HOLD_SECONDS)

    worker = threading.Thread(target=hold_boundary, daemon=True)
    worker.start()
    assert boundary_held.wait(_OBSERVE_SECONDS)
    monkeypatch.setattr(writer_lease_module, "content_private_logging_enabled", lambda: True)

    try:
        assert (
            reader.invoke(
                _command(writes=False, leaf=lambda _vault: "consistent"),
                (vault,),
                {},
            )
            == "consistent"
        )
    finally:
        release_boundary.set()
        worker.join(timeout=_HOLD_SECONDS)
    assert not worker.is_alive()


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
        assert release_first.wait(_HOLD_SECONDS)
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
    assert first_entered.wait(_OBSERVE_SECONDS)
    second_thread.start()
    assert second_attempting.wait(_OBSERVE_SECONDS)
    assert not second_entered.wait(0.1)
    release_first.set()
    assert second_entered.wait(_OBSERVE_SECONDS)
    first_thread.join(timeout=_HOLD_SECONDS)
    second_thread.join(timeout=_HOLD_SECONDS)
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


def test_reserved_identity_guard_does_not_fsync_diagnostic_holder_metadata(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    state_dir = tmp_path / "state"
    manager = LeaseManager(LeaseConfig(state_dir=state_dir))

    with manager.reserved_identity_guard(
        vault,
        domains={"graph-store"},
        exclusive=False,
    ) as first_generation:
        assert list((state_dir / "mutation-locks").glob("*.holder.json")) == []

    with manager.reserved_identity_guard(
        vault,
        domains={"graph-store"},
        exclusive=False,
    ) as second_generation:
        pass

    with manager.reserved_identity_guard(
        vault,
        domains={"graph-store"},
        exclusive=False,
        advance_generation=False,
    ) as read_generation:
        pass

    with manager.reserved_identity_guard(
        vault,
        domains={"graph-store"},
        exclusive=True,
    ) as observed_generation:
        pass

    assert first_generation != second_generation
    assert read_generation == second_generation
    assert observed_generation == second_generation


def test_reserved_identity_owner_acquires_gate_before_its_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    entered: list[str] = []

    class RecordingCoordinator:
        def __init__(self, identity: str) -> None:
            self.identity = identity

        @contextmanager
        def hold(self, **_kwargs: object):
            entered.append(self.identity)
            yield self

    monkeypatch.setattr(
        manager,
        "_mutation_coordinator_for",
        lambda identity: RecordingCoordinator(str(identity)),
    )

    with manager.reserved_identity_guard(
        vault,
        domains={"graph-store"},
        exclusive=False,
    ):
        pass

    assert len(entered) == 2
    assert ":gate:" in entered[0]
    assert ":graph-store:" in entered[1]


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
    def guard(subject: Path, **_metadata):
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
        pytest.param("connect_memory", {"operation": "suggest-links"}, id="connect-suggest-links"),
        pytest.param(
            "connect_memory",
            {"operation": "suggest-relations"},
            id="connect-suggest-relations",
        ),
        pytest.param("connect_memory", {"operation": "context"}, id="connect-context"),
        pytest.param("connect_memory", {"operation": "graph-context"}, id="connect-graph-context"),
        pytest.param("connect_memory", {"operation": "inbound-links"}, id="connect-inbound-links"),
        pytest.param("adopt_vault", {}, id="adopt-default-scan-only"),
        pytest.param("adopt_vault", {"mode": "scan-only"}, id="adopt-scan-only"),
        pytest.param("observe_memory", {"operation": "validate"}, id="observe-validate"),
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


def test_explicit_process_media_hash_yields_global_guard_and_replays_idempotently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import media_processing
    from exomem.commands import product_commands_for

    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "large.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"large-enough-for-a-blocked-explicit-provenance-read")
    state_dir = tmp_path / "state"
    manager = LeaseManager(
        LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=1.0,
    )
    contender = LeaseManager(
        LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=0.05,
    )
    command = next(
        command for command in product_commands_for("mcp") if command.name == "process_media"
    )
    monkeypatch.setattr(writer_lease_module, "get_manager", lambda: manager)
    hash_started = threading.Event()
    continue_hash = threading.Event()
    commit_seen = threading.Event()
    errors: list[BaseException] = []
    results: list[object] = []
    provenance_calls = 0
    original_read = media_processing._read_provenance
    original_batch = media_processing.batch_atomic_write

    def blocked_read(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal provenance_calls
        provenance_calls += 1
        hash_started.set()
        assert continue_hash.wait(_HOLD_SECONDS)
        return original_read(*args, **kwargs)

    def guarded_batch(*args, **kwargs):  # noqa: ANN002, ANN003
        boundary = manager.status(vault)["mutation_boundary"]
        assert boundary["state"] == "held"
        assert boundary["operation"] == "process_media_process_commit"
        commit_seen.set()
        return original_batch(*args, **kwargs)

    monkeypatch.setattr(media_processing, "_read_provenance", blocked_read)
    monkeypatch.setattr(media_processing, "batch_atomic_write", guarded_batch)
    kwargs = {
        "operation": "process",
        "path": binary.relative_to(vault).as_posix(),
    }

    def process() -> None:
        try:
            results.append(
                manager.invoke(
                    command,
                    (vault,),
                    kwargs,
                    implicit_idempotency_scope="principal:test",
                )
            )
        except BaseException as error:  # noqa: BLE001 - inspect thread outcome
            errors.append(error)

    worker = threading.Thread(target=process)
    worker.start()
    assert hash_started.wait(_OBSERVE_SECONDS)
    try:
        with contender.mutation_guard(
            vault,
            request_id="foreground-during-hash",
            operation="remember",
        ):
            assert contender.status(vault)["mutation_boundary"]["request_id"] == (
                "foreground-during-hash"
            )
    finally:
        continue_hash.set()
        worker.join(timeout=_HOLD_SECONDS)

    assert not worker.is_alive()
    assert errors == []
    assert commit_seen.is_set()
    assert provenance_calls == 1
    replay = manager.invoke(
        command,
        (vault,),
        kwargs,
        implicit_idempotency_scope="principal:test",
    )
    assert replay == results[0]
    assert provenance_calls == 1


@pytest.mark.parametrize("operation", ["process", "retry"])
def test_pathless_process_media_propagates_per_artifact_mutation_busy(
    operation: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import media_jobs, media_processing
    from exomem.commands import product_commands_for

    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / f"{operation}.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"pathless media")
    if operation == "retry":
        reconciled = media_processing.reconcile_media(vault, binary)
        store = media_jobs.MediaJobStore(vault)
        claimed = store.claim_next()
        assert claimed is not None and claimed.id == reconciled.job_id
        store.mark(claimed.id, media_jobs.FAILED, "InvalidDataError: retry me")

    state_dir = tmp_path / "state"
    manager = LeaseManager(
        LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=0.05,
    )
    holder = LeaseManager(
        LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=1.0,
    )
    monkeypatch.setattr(writer_lease_module, "get_manager", lambda: manager)
    command = next(
        command for command in product_commands_for("mcp") if command.name == "process_media"
    )
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with holder.mutation_guard(vault, operation="foreground-holder"):
            held.set()
            assert release.wait(_HOLD_SECONDS)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(_OBSERVE_SECONDS)
    try:
        with pytest.raises(OpError) as raised:
            manager.invoke(
                command,
                (vault,),
                {"operation": operation},
                implicit_idempotency_scope="principal:pathless",
            )
    finally:
        release.set()
        thread.join(timeout=_HOLD_SECONDS)

    assert raised.value.code == "MUTATION_BUSY"
    assert raised.value.details["status"] == "retryable"
    assert raised.value.details["receipt_id"] is not None
    assert raised.value.details["request_id"]


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


@pytest.mark.parametrize("selector_source", ["explicit", "leaf-default"])
def test_unknown_selector_with_local_writer_never_executes_the_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selector_source: str,
) -> None:
    from exomem.commands import product_commands_for

    calls: list[dict] = []
    command = next(c for c in product_commands_for("mcp") if c.name == "connect_memory")
    if selector_source == "explicit":
        command = _recording_product_command(command, calls, "unreceipted-payload")
        kwargs = {"operation": "future-read-mode"}
    else:

        def leaf(_vault_root, operation="future-read-mode", **leaf_kwargs):  # noqa: ANN001, ANN202
            calls.append({"operation": operation, **leaf_kwargs})
            return "unreceipted-payload"

        command = replace(command, leaf=leaf)
        kwargs = {}

    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    monkeypatch.setattr(writer_lease_module, "get_manager", lambda: manager)
    try:
        with pytest.raises(OpError) as raised:
            invoke_command(command, tmp_path / "vault", **kwargs)
    finally:
        manager.close()

    assert raised.value.code == "RECEIPT_OUTCOME_MISSING"
    assert raised.value.message == "command selector is not release-covered"
    assert calls == []


@pytest.mark.parametrize(
    ("command_name", "kwargs"),
    [
        pytest.param("connect_memory", {"operation": "create-entity"}, id="connect-create-entity"),
        pytest.param(
            "connect_memory", {"operation": "accept-relation"}, id="connect-accept-relation"
        ),
        pytest.param("connect_memory", {"operation": ""}, id="connect-empty"),
        pytest.param("connect_memory", {"operation": None}, id="connect-explicit-none"),
        pytest.param("connect_memory", {"operation": "entity"}, id="connect-nonexistent-entity"),
        pytest.param("connect_memory", {"operation": "future-read-mode"}, id="connect-future-mode"),
        pytest.param("adopt_vault", {"mode": "save-manifest"}, id="adopt-save-manifest"),
        pytest.param("adopt_vault", {"mode": "copy-as-sources"}, id="adopt-copy-as-sources"),
        pytest.param("adopt_vault", {"mode": "compile-selected"}, id="adopt-compile-selected"),
        pytest.param("adopt_vault", {"mode": ""}, id="adopt-empty"),
        pytest.param("adopt_vault", {"mode": None}, id="adopt-explicit-none"),
        pytest.param("adopt_vault", {"mode": "future-mode"}, id="adopt-future-mode"),
        pytest.param("observe_memory", {}, id="observe-default-add"),
        pytest.param("observe_memory", {"operation": "add"}, id="observe-add"),
        pytest.param("observe_memory", {"operation": "update"}, id="observe-update"),
        pytest.param("observe_memory", {"operation": "remove"}, id="observe-remove"),
        pytest.param("observe_memory", {"operation": "future-mode"}, id="observe-future-mode"),
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
            assert resume.wait(_HOLD_SECONDS)
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
    assert staged.wait(_OBSERVE_SECONDS)
    clock.value = 111
    assert replica_b.ensure_writer().fencing_token == 2
    resume.set()
    worker.join(timeout=_HOLD_SECONDS)

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
    assert client.renew_started.wait(_OBSERVE_SECONDS)

    assert manager.ensure_writer().fencing_token == 3
    client.resume_renewal.set()
    renewer.join(timeout=_HOLD_SECONDS)

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


def test_idempotency_replays_reviewed_none_alias_as_the_same_mutation(tmp_path: Path) -> None:
    calls: list[str] = []
    manager = _manager(tmp_path, LeaseRecord("desktop", 99, 4))
    command = _command(
        writes=True,
        leaf=lambda relation_disposition: calls.append(relation_disposition)
        or {"relation_disposition": relation_disposition},
    )

    first = manager.invoke(
        command,
        (),
        {"relation_disposition": "reviewed-none"},
        idempotency_key="reviewed-none-retry",
    )
    replay = manager.invoke(
        command,
        (),
        {"relation_disposition": "reviewed_none"},
        idempotency_key="reviewed-none-retry",
    )

    assert first == replay == {"relation_disposition": "reviewed_none"}
    assert calls == ["reviewed_none"]


def test_identical_inflight_retry_waits_for_original_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf_started = threading.Event()
    release_leaf = threading.Event()
    pending_seen = threading.Event()
    calls: list[int] = []
    outcomes: dict[str, object] = {}
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    manager = LeaseManager(
        LeaseConfig(state_dir=tmp_path),
        mutation_timeout_seconds=0,
        idempotency_wait_seconds=2,
    )

    def leaf(_vault: Path, value: int) -> dict[str, object]:
        calls.append(value)
        leaf_started.set()
        assert release_leaf.wait(_HOLD_SECONDS)
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
                (vault,),
                {"value": 1},
                implicit_idempotency_scope="principal:alice",
            )
        except BaseException as error:  # noqa: BLE001 - assert thread outcome below
            outcomes[name] = error

    original = threading.Thread(target=invoke, args=("original",), daemon=True)
    retry = threading.Thread(target=invoke, args=("retry",), daemon=True)
    original.start()
    assert leaf_started.wait(_OBSERVE_SECONDS)
    retry.start()
    assert pending_seen.wait(_OBSERVE_SECONDS)
    release_leaf.set()
    original.join(timeout=_HOLD_SECONDS)
    retry.join(timeout=_HOLD_SECONDS)

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


def test_identical_orphaned_legacy_pending_becomes_abandoned_after_grace_period(
    tmp_path: Path,
) -> None:
    """Superseded contract (R4/GAP B): a `pending` row from before the owner
    column existed (NULL owner) — the exact "orphaned pending never resolves"
    bug this feature fixes — transitions to `abandoned` once the legacy grace
    period has passed, instead of blocking every retry forever."""
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
            "INSERT INTO mutations(key, digest, state, updated_at, owner) "
            "VALUES (?, ?, 'pending', ?, NULL)",
            (key, digest, 100.0),
        )

    with pytest.raises(OpError) as outcome_unknown:
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="pending",
            idempotency_principal_scope="principal:alice",
        )
    assert outcome_unknown.value.code == "MUTATION_OUTCOME_UNKNOWN"
    payload = error_dict(outcome_unknown.value)
    assert payload["status"] == "uncertain"
    assert payload["committed"] is None
    assert payload["abandoned"] is True
    assert payload["request_id"]
    assert payload["idempotency_key"] == "pending"
    assert payload["receipt_id"]


def test_identical_recently_orphaned_legacy_pending_still_reports_acknowledgement_pending(
    tmp_path: Path,
) -> None:
    """A legacy NULL-owner row younger than the grace period is honored under
    the pre-existing any-pending-blocks rule — it has not yet earned
    abandonment."""
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
            "INSERT INTO mutations(key, digest, state, updated_at, owner) "
            "VALUES (?, ?, 'pending', ?, NULL)",
            (key, digest, time.time()),
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


def test_pending_row_with_dead_owner_becomes_abandoned(tmp_path: Path) -> None:
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
    dead_owner = "999999:deadowner00000000"
    with sqlite3.connect(manager.idempotency.path) as connection:
        connection.execute(
            "INSERT INTO mutations(key, digest, state, updated_at, owner) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (key, digest, time.time(), dead_owner),
        )

    with pytest.raises(OpError) as outcome_unknown:
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="pending",
            idempotency_principal_scope="principal:alice",
        )
    assert outcome_unknown.value.code == "MUTATION_OUTCOME_UNKNOWN"


def test_identical_retry_never_replays_an_outcome_unknown_execution(
    tmp_path: Path,
) -> None:
    """An unprovable execution stays fail-closed after its owner dies."""
    clock = Clock()
    manager = LeaseManager(
        LeaseConfig(state_dir=tmp_path),
        idempotency_wait_seconds=0,
        clock=clock,
    )
    calls: list[str] = []
    command = _command(writes=True, leaf=lambda: calls.append("ran") or "fresh-result")
    digest = writer_lease_module._command_digest(command, {})
    key = writer_lease_module._effective_idempotency_key(
        manager,
        command=command,
        mutation_subject="standalone",
        digest=digest,
        idempotency_key="pending",
        principal_scope="principal:alice",
    )[0]
    dead_owner = "999999:deadowner00000001"
    with sqlite3.connect(manager.idempotency.path) as connection:
        connection.execute(
            "INSERT INTO mutations(key, digest, state, updated_at, owner) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (key, digest, clock.value, dead_owner),
        )

    with pytest.raises(OpError) as outcome_unknown:
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="pending",
            idempotency_principal_scope="principal:alice",
        )
    assert outcome_unknown.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert calls == []

    clock.value += writer_lease_module._IDEMPOTENCY_ABANDONED_RETRY_AFTER_SECONDS + 1
    with pytest.raises(OpError) as repeated:
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="pending",
            idempotency_principal_scope="principal:alice",
        )
    assert repeated.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert calls == []


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
        assert release_leaf.wait(_HOLD_SECONDS)
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
    assert leaf_started.wait(_OBSERVE_SECONDS)
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
    worker.join(timeout=_HOLD_SECONDS)
    assert outcome == ["committed"]
    assert first_calls == ["first"]


def test_mutation_during_semantic_warm_returns_without_holding_boundary(
    tmp_path: Path,
) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path))
    calls: list[str] = []
    command = _command(writes=True, leaf=lambda: calls.append("called"))
    readiness.begin_warm()
    readiness.mark_ready("lexical")
    try:
        with pytest.raises(OpError) as warming:
            manager.invoke(command, (), {}, mutation_request_id="warm-request")
    finally:
        readiness.reset()

    assert warming.value.code == "MUTATION_WARMING"
    payload = error_dict(warming.value)
    assert payload["status"] == "retryable"
    assert payload["committed"] is False
    assert payload["retry_after_ms"] == 750
    assert payload["request_id"] == "warm-request"
    assert calls == []
    assert active_mutation_snapshot()["state"] == "free"


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
    for expected_code in (
        "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN",
        "MUTATION_OUTCOME_UNKNOWN",
    ):
        with pytest.raises(OpError) as uncertain:
            manager.invoke(
                command,
                (tmp_path,),
                {},
                idempotency_key="postcommit-error",
                idempotency_principal_scope="principal:alice",
            )
        assert uncertain.value.code == expected_code
        payload = error_dict(uncertain.value)
        assert payload["status"] == ("committed" if expected_code.startswith("MUTATION_COMMITTED") else "uncertain")
        assert payload["committed"] is (True if expected_code.startswith("MUTATION_COMMITTED") else None)

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

    original_persist = manager.idempotency._persist_completed_from_canonical
    failed = False

    def fail_terminal_receipt(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("deterministic receipt write failure")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        manager.idempotency, "_persist_completed_from_canonical", fail_terminal_receipt
    )
    command = _command(writes=True, leaf=commit)

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
    replay = manager.invoke(
        command,
        (tmp_path,),
        {},
        idempotency_key="receipt-failure",
        idempotency_principal_scope="principal:alice",
    )
    assert replay["status"] == "committed"

    assert target.read_text(encoding="utf-8") == "committed\n"
    assert calls == 1


def test_uncommitted_result_is_completed_without_a_graph_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"), idempotency_wait_seconds=0)

    def validate_only() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"validate_only": True, "committed": False}

    command = _command(writes=True, leaf=validate_only)

    assert manager.invoke(
        command, (), {}, idempotency_key="validate-receipt-failure"
    ) == {"validate_only": True, "committed": False}
    assert manager.invoke(
        command, (), {}, idempotency_key="validate-receipt-failure"
    ) == {"validate_only": True, "committed": False}

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
        assert release_audit.wait(_HOLD_SECONDS)
        return "audited"

    audit = SimpleNamespace(name="maintain_memory", read_only=False, leaf=audit_leaf)
    mutation = SimpleNamespace(name="remember", read_only=False, leaf=lambda _vault: "committed")
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
    assert audit_started.wait(_OBSERVE_SECONDS)
    assert manager.invoke(mutation, (tmp_path,), {}) == "committed"
    release_audit.set()
    worker.join(timeout=_HOLD_SECONDS)

    assert audit_outcome == ["audited"]


@pytest.mark.parametrize(
    ("command_name", "kwargs"),
    [
        ("audit", {"detail": "full"}),
        ("review_memory", {"mode": "audit", "detail": "full"}),
        ("maintain_memory", {"mode": "audit", "detail": "full"}),
    ],
)
def test_hosted_public_audit_routes_bypass_boundary_held_by_other_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    kwargs: dict[str, str],
) -> None:
    state_dir = tmp_path / "shared-state"
    vault = tmp_path / "vault"
    vault.mkdir()
    holder = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.0)
    auditor = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.0)
    boundary_held = threading.Event()
    release_boundary = threading.Event()

    def hold_boundary() -> None:
        with holder.mutation_guard(vault):
            boundary_held.set()
            assert release_boundary.wait(_HOLD_SECONDS)

    worker = threading.Thread(target=hold_boundary, daemon=True)
    worker.start()
    assert boundary_held.wait(_OBSERVE_SECONDS)
    monkeypatch.setattr(writer_lease_module, "content_private_logging_enabled", lambda: True)
    command = SimpleNamespace(
        name=command_name,
        read_only=command_name != "maintain_memory",
        leaf=lambda _vault, **_kwargs: "audited",
    )

    try:
        assert auditor.invoke(command, (vault,), kwargs, read_only=True) == "audited"
    finally:
        release_boundary.set()
        worker.join(timeout=_HOLD_SECONDS)

    assert not worker.is_alive()


@pytest.mark.parametrize(
    ("command_name", "kwargs"),
    [
        ("remember", {"validate_only": True}),
        (
            "edit_memory",
            {
                "operation": {
                    "kind": "replace_string",
                    "old_string": "Before",
                    "new_string": "After",
                    "validate_only": True,
                }
            },
        ),
    ],
)
def test_hosted_mutation_previews_bypass_boundary_held_by_other_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    kwargs: dict[str, object],
) -> None:
    state_dir = tmp_path / "shared-state"
    vault = tmp_path / "vault"
    vault.mkdir()
    holder = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.0)
    previewer = LeaseManager(LeaseConfig(state_dir=state_dir), mutation_timeout_seconds=0.0)
    boundary_held = threading.Event()
    release_boundary = threading.Event()

    def hold_boundary() -> None:
        with holder.mutation_guard(vault):
            boundary_held.set()
            assert release_boundary.wait(_HOLD_SECONDS)

    worker = threading.Thread(target=hold_boundary, daemon=True)
    worker.start()
    assert boundary_held.wait(_OBSERVE_SECONDS)
    monkeypatch.setattr(writer_lease_module, "content_private_logging_enabled", lambda: True)
    command = SimpleNamespace(
        name=command_name,
        read_only=False,
        leaf=lambda _vault, **_kwargs: "previewed",
    )

    try:
        assert previewer.invoke(command, (vault,), kwargs, read_only=True) == "previewed"
    finally:
        release_boundary.set()
        worker.join(timeout=_HOLD_SECONDS)

    assert not worker.is_alive()


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

    assert (
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="same-public-key",
            idempotency_principal_scope="principal:alice",
        )
        == 1
    )
    assert (
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="same-public-key",
            idempotency_principal_scope="principal:bob",
        )
        == 2
    )
    assert (
        manager.invoke(
            command,
            (),
            {},
            idempotency_key="same-public-key",
            idempotency_principal_scope="principal:alice",
        )
        == 1
    )
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
    assert state == "completed"
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

    clock.value += writer_lease_module._IMPLICIT_RETRY_TTL_SECONDS + 1
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
    add(
        "too many targets",
        lambda value: value["outcome"].update(
            affected_count=17,
            targets=[f"note-{index}.md" for index in range(17)],
            omitted_target_count=0,
        ),
    )
    for name, target in (
        ("empty target", ""),
        ("absolute target", "/vault/note.md"),
        ("backslash target", "folder\\note.md"),
        ("nul target", "folder/\0note.md"),
        ("dot target", "folder/./note.md"),
        ("parent target", "folder/../note.md"),
        ("drive target", "C:" + "/vault/note.md"),
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
            "UPDATE mutations SET updated_at = 'corrupt' WHERE state = 'completed'"
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
            "UPDATE mutations SET result = ? WHERE state = 'completed'",
            (b"not-json",),
        )

    clock.value += writer_lease_module._IMPLICIT_RETRY_TTL_SECONDS + 1
    with pytest.raises(OpError) as blocked:
        manager.invoke(command, (), {}, **marker)
    assert blocked.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert "not-json" not in str(blocked.value)
    assert calls == 1


@pytest.mark.parametrize("failure_point", ["serialize", "update"])
def test_committed_marker_storage_failure_keeps_execution_fail_closed(
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
                "BEFORE UPDATE ON mutations WHEN NEW.state = 'completed' "
                "BEGIN SELECT RAISE(FAIL, 'private sqlite detail'); END"
            )

    with pytest.raises(vault_module.BatchWriteError) as first:
        manager.invoke(command, (), {}, idempotency_key="storage-failure")
    assert first.value is original
    assert first.value.__cause__ is not None
    assert "private" not in str(first.value)
    _digest, state, stored = _row(manager, "storage-failure")
    assert state == "executing"
    assert stored is None

    with pytest.raises(OpError) as blocked:
        manager.invoke(command, (), {}, idempotency_key="storage-failure")
    assert blocked.value.code == "MUTATION_OUTCOME_UNKNOWN"
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
    assert blocked.value.code == "MUTATION_OUTCOME_UNKNOWN"
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

    clock.value += writer_lease_module._IMPLICIT_RETRY_TTL_SECONDS + 1
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


def _reclaim_replica(
    store: SQLiteLeaseStore,
    tmp_path: Path,
    replica_id: str,
    *,
    preferred: bool,
    ttl_seconds: float = 30.0,
) -> LeaseManager:
    return LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id=replica_id,
            state_dir=tmp_path / f"{replica_id}-state",
            preferred_writer=preferred,
            ttl_seconds=ttl_seconds,
        ),
        client=StoreClient(store, replica_id),
    )


def test_preferred_follower_reclaims_once_the_previous_lease_expires(tmp_path: Path) -> None:
    """A preferred replica must keep trying, not give up after one startup race.

    Regression: `start_server_lifecycle()` attempted acquisition once and
    swallowed the failure, reasoning that mutations would retry. The HA edge
    routes mutations to the lease holder, so a follower never receives one. The
    preferred replica lost a startup race on 2026-07-19 and stayed a follower
    for 15 hours while reporting healthy.
    """
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    laptop = _reclaim_replica(store, tmp_path, "laptop", preferred=False)
    desktop = _reclaim_replica(store, tmp_path, "desktop", preferred=True)

    assert laptop.ensure_writer().holder == "laptop"
    # Desktop starts while the laptop holds a live lease: startup acquisition fails.
    with pytest.raises(OpError, match="WRITER_LEASE_REQUIRED"):
        desktop.ensure_writer()
    assert desktop.status()["role"] == "follower"

    # Retrying changes nothing while the holder is live.
    desktop._attempt_preferred_reclaim()
    assert desktop.status()["role"] == "follower"

    # The laptop stops renewing and its lease lapses.
    clock.value += 60
    desktop._attempt_preferred_reclaim()
    assert desktop.status()["role"] == "writer"


def test_reclaim_never_preempts_a_live_holder(tmp_path: Path) -> None:
    """Repeated reclaim must not displace a running writer or disturb its fencing token."""
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    laptop = _reclaim_replica(store, tmp_path, "laptop", preferred=False)
    desktop = _reclaim_replica(store, tmp_path, "desktop", preferred=True)

    granted = laptop.ensure_writer()
    for _ in range(5):
        desktop._attempt_preferred_reclaim()

    held = store.status("main")
    assert held["holder"] == "laptop"
    # The holder's fencing token is untouched: no takeover was even half-applied.
    assert held["fencing_token"] == granted.fencing_token
    assert desktop.status()["role"] == "follower"


def test_renew_loop_actually_drives_reclaim(tmp_path: Path) -> None:
    """The renewer must invoke reclaim, not merely have a reclaim method available.

    The defect was in the loop itself: it read `_fencing_token`, found None, and
    `continue`d — renewing an existing lease but never acquiring one. Tests that
    call the reclaim helper directly would all still pass with that `continue`
    restored, so this asserts the wiring end to end.
    """
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    laptop = _reclaim_replica(store, tmp_path, "laptop", preferred=False)
    # ttl/3 floors at a 1s renew interval, keeping the test bounded.
    desktop = _reclaim_replica(store, tmp_path, "desktop", preferred=True, ttl_seconds=1.0)

    assert laptop.ensure_writer().holder == "laptop"
    with pytest.raises(OpError, match="WRITER_LEASE_REQUIRED"):
        desktop.ensure_writer()

    clock.value += 60  # the laptop's lease lapses
    desktop.start_renewer()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if store.status("main")["holder"] == "desktop":
                break
            time.sleep(0.05)
        assert store.status("main")["holder"] == "desktop", (
            "renew loop never attempted reclaim while preferred and unleased"
        )
    finally:
        desktop.close()


def test_non_preferred_follower_does_not_self_promote(tmp_path: Path) -> None:
    """Only a preferred replica reclaims in the background; others wait for a mutation."""
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    follower = _reclaim_replica(store, tmp_path, "laptop", preferred=False)

    # Lease is entirely free — the only thing stopping acquisition is the policy.
    assert store.status("main")["holder"] is None
    follower._attempt_preferred_reclaim()

    assert store.status("main")["holder"] is None
    assert follower.status()["role"] == "follower"


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


def test_coordination_status_includes_content_free_mutation_boundary(tmp_path: Path) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    vault = tmp_path / "private-vault"
    vault.mkdir()

    assert _boundary(manager.status(vault)["mutation_boundary"]) == {"state": "free"}
    with manager.mutation_guard(
        vault,
        request_id="req-health",
        operation="edit_memory",
        holder_kind="command",
    ):
        boundary = manager.status(vault)["mutation_boundary"]
        assert boundary["state"] == "held"
        assert boundary["verified"] is True
        assert boundary["request_id"] == "req-health"
        assert boundary["operation"] == "edit_memory"
        assert str(vault) not in str(boundary)


def test_coordination_status_measures_only_the_requested_vault(tmp_path: Path) -> None:
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "state"))
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    vault_a.mkdir()
    vault_b.mkdir()

    with manager.mutation_guard(
        vault_a,
        request_id="req-vault-a",
        operation="remember",
        holder_kind="command",
    ):
        assert _boundary(manager.status(vault_b)["mutation_boundary"]) == {"state": "free"}
        boundary = manager.status(vault_a)["mutation_boundary"]
        assert boundary["state"] == "held"
        assert boundary["request_id"] == "req-vault-a"


def test_public_coordination_command_forwards_its_injected_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, writer_lease

    vault = tmp_path / "vault"
    observed: list[Path] = []

    def fake_status(vault_root=None):  # noqa: ANN001
        observed.append(Path(vault_root))
        return {"mutation_boundary": {"state": "free"}}

    monkeypatch.setattr(writer_lease, "coordination_status", fake_status)
    assert commands.op_coordination_status(vault) == {"mutation_boundary": {"state": "free"}}
    assert observed == [vault]


# --------------------------------------------------------------------------- #
# R5 — writer-lease idle release
# --------------------------------------------------------------------------- #


def _idle_manager(
    tmp_path: Path,
    record: LeaseRecord,
    *,
    idle_release_seconds: float = 60.0,
    preferred_writer: bool = False,
    ttl_seconds: float = 30.0,
) -> LeaseManager:
    return LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id=record.holder or "desktop",
            state_dir=tmp_path,
            idle_release_seconds=idle_release_seconds,
            preferred_writer=preferred_writer,
            ttl_seconds=ttl_seconds,
        ),
        client=FakeClient(record),
    )


def test_idle_release_config_defaults_to_60_seconds() -> None:
    assert LeaseConfig().idle_release_seconds == 60.0
    config = LeaseConfig.from_env(
        {
            "EXOMEM_WRITER_LEASE_URL": "https://lease.example",
            "EXOMEM_WRITER_LEASE_VAULT_ID": "main",
            "EXOMEM_WRITER_LEASE_REPLICA_ID": "desktop",
        }
    )
    assert config.idle_release_seconds == 60.0


def test_idle_release_config_zero_disables_and_is_valid() -> None:
    config = LeaseConfig.from_env(
        {
            "EXOMEM_WRITER_LEASE_URL": "https://lease.example",
            "EXOMEM_WRITER_LEASE_VAULT_ID": "main",
            "EXOMEM_WRITER_LEASE_REPLICA_ID": "desktop",
            "EXOMEM_WRITER_LEASE_IDLE_SECONDS": "0",
        }
    )
    assert config.idle_release_seconds == 0.0


def test_idle_release_config_rejects_a_value_between_zero_and_ttl() -> None:
    with pytest.raises(ValueError, match="EXOMEM_WRITER_LEASE_IDLE_SECONDS"):
        LeaseConfig.from_env(
            {
                "EXOMEM_WRITER_LEASE_URL": "https://lease.example",
                "EXOMEM_WRITER_LEASE_VAULT_ID": "main",
                "EXOMEM_WRITER_LEASE_REPLICA_ID": "desktop",
                "EXOMEM_WRITER_LEASE_TTL": "30",
                "EXOMEM_WRITER_LEASE_IDLE_SECONDS": "10",
            }
        )


def test_idle_release_config_accepts_a_value_at_or_above_ttl() -> None:
    config = LeaseConfig.from_env(
        {
            "EXOMEM_WRITER_LEASE_URL": "https://lease.example",
            "EXOMEM_WRITER_LEASE_VAULT_ID": "main",
            "EXOMEM_WRITER_LEASE_REPLICA_ID": "desktop",
            "EXOMEM_WRITER_LEASE_TTL": "30",
            "EXOMEM_WRITER_LEASE_IDLE_SECONDS": "30",
        }
    )
    assert config.idle_release_seconds == 30.0


def test_idle_release_fires_at_exactly_idle_release_seconds(tmp_path: Path) -> None:
    manager = _idle_manager(tmp_path, LeaseRecord("desktop", 99, 1), idle_release_seconds=60.0)
    manager.ensure_writer()
    manager._last_activity_monotonic = time.monotonic() - 60.0

    assert manager._maybe_idle_release(1) is True
    assert manager._fencing_token is None
    assert manager._expires_at is None
    assert manager.client.releases == [1]


def test_idle_release_does_not_fire_before_the_threshold(tmp_path: Path) -> None:
    manager = _idle_manager(tmp_path, LeaseRecord("desktop", 99, 1), idle_release_seconds=60.0)
    manager.ensure_writer()
    manager._last_activity_monotonic = time.monotonic() - 59.9

    assert manager._maybe_idle_release(1) is False
    assert manager._fencing_token == 1
    assert manager.client.releases == []


def test_activity_at_t59_defers_release_to_t119(tmp_path: Path) -> None:
    """Activity at T+59 resets the idle clock, so idle release does not fire
    at T+60 (only 1s since the reset) but does fire once T+119 arrives (60s
    after the T+59 reset)."""
    manager = _idle_manager(tmp_path, LeaseRecord("desktop", 99, 1), idle_release_seconds=60.0)
    manager.ensure_writer()
    start = time.monotonic()
    manager._last_activity_monotonic = start - 59.0  # T+59 activity happened

    # T+60 overall (only 1s since the T+59 reset): must not release.
    assert manager._maybe_idle_release(1) is False
    assert manager._fencing_token == 1

    # Advance to T+119 relative to the reset (60s since T+59): must release.
    manager._last_activity_monotonic = start - 119.0
    assert manager._maybe_idle_release(1) is True


def test_in_flight_mutation_blocks_release_until_it_completes(tmp_path: Path) -> None:
    manager = _idle_manager(tmp_path, LeaseRecord("desktop", 99, 1), idle_release_seconds=60.0)
    manager.ensure_writer()
    manager._last_activity_monotonic = time.monotonic() - 60.0

    entered = threading.Event()
    release_guard = threading.Event()

    def hold_guard() -> None:
        with manager.writer_authority_guard():
            entered.set()
            assert release_guard.wait(_HOLD_SECONDS)

    worker = threading.Thread(target=hold_guard)
    worker.start()
    try:
        assert entered.wait(_OBSERVE_SECONDS)
        # In flight: idle release must not fire even though activity is stale
        # (writer_authority_guard refreshed it, but force it stale again to
        # prove the mutation-count gate — not just the timestamp — blocks it).
        manager._last_activity_monotonic = time.monotonic() - 60.0
        assert manager._maybe_idle_release(1) is False
    finally:
        release_guard.set()
        worker.join(timeout=_HOLD_SECONDS)
    assert not worker.is_alive()

    # First tick after completion: activity was refreshed on guard exit, so
    # backdate it once more to simulate the idle window having elapsed since.
    manager._last_activity_monotonic = time.monotonic() - 60.0
    assert manager._maybe_idle_release(1) is True


def test_preferred_replica_never_idle_releases(tmp_path: Path) -> None:
    manager = _idle_manager(
        tmp_path, LeaseRecord("desktop", 99, 1), idle_release_seconds=60.0, preferred_writer=True
    )
    manager.ensure_writer()
    manager._last_activity_monotonic = time.monotonic() - 3600.0

    assert manager._maybe_idle_release(1) is False
    assert manager._fencing_token == 1
    assert manager.client.releases == []


def test_idle_release_disabled_when_idle_seconds_is_zero(tmp_path: Path) -> None:
    manager = _idle_manager(tmp_path, LeaseRecord("desktop", 99, 1), idle_release_seconds=0.0)
    manager.ensure_writer()
    manager._last_activity_monotonic = time.monotonic() - 3600.0

    assert manager._maybe_idle_release(1) is False
    assert manager.client.releases == []


def test_idle_release_swallows_coordinator_release_rpc_failure(tmp_path: Path) -> None:
    class FailingReleaseClient(FakeClient):
        def release(self, fencing_token: int) -> LeaseRecord:
            raise OpError("WRITER_COORDINATOR_UNAVAILABLE", "down")

    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path,
            idle_release_seconds=60.0,
        ),
        client=FailingReleaseClient(LeaseRecord("desktop", 99, 1)),
    )
    manager.ensure_writer()
    manager._last_activity_monotonic = time.monotonic() - 60.0

    # The token is cleared LOCALLY even though the release RPC failed — a
    # degraded handover (the coordinator's own TTL closes the gap), never a
    # crash and never split-brain.
    assert manager._maybe_idle_release(1) is True
    assert manager._fencing_token is None


def test_mid_release_race_gets_a_fresh_bumped_token_no_writer_fenced(tmp_path: Path) -> None:
    """After idle release, an `ensure_writer` racing in (from this or another
    replica) acquires a strictly newer fencing token — no confusion, no
    WRITER_FENCED for that fresh acquisition."""
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    laptop = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="laptop",
            state_dir=tmp_path / "laptop-state",
            idle_release_seconds=60.0,
        ),
        client=StoreClient(store, "laptop"),
    )
    token = laptop.ensure_writer().fencing_token
    assert token == 1
    laptop._last_activity_monotonic = time.monotonic() - 60.0

    assert laptop._maybe_idle_release(token) is True

    desktop = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path / "desktop-state",
        ),
        client=StoreClient(store, "desktop"),
    )
    fresh = desktop.ensure_writer()
    assert fresh.fencing_token > token
    # The fresh holder is fully authorized: validating its own token succeeds.
    desktop.validate_fencing_token(fresh.fencing_token)


def test_straggler_write_is_fenced_after_idle_release(tmp_path: Path) -> None:
    """A write authorized before idle release cleared the local token is
    rejected at the fence-validation boundary once another replica has
    acquired a newer token — `writer_authority_guard()`'s single choke point
    keeps `_active_mutations` accurate for real invocations, so this proves
    the independent defense-in-depth layer (`validate_active_write_fence`)
    still catches a stale token that reaches a commit boundary."""
    clock = Clock()
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=clock)
    laptop = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="laptop",
            state_dir=tmp_path / "laptop-state",
            idle_release_seconds=60.0,
        ),
        client=StoreClient(store, "laptop"),
    )
    stale_token = laptop.ensure_writer().fencing_token
    fence_context = writer_lease_module._ACTIVE_WRITE_FENCE.set((laptop, stale_token))
    try:
        laptop._last_activity_monotonic = time.monotonic() - 60.0
        assert laptop._maybe_idle_release(stale_token) is True

        desktop = LeaseManager(
            LeaseConfig(
                url="https://lease.example",
                vault_id="main",
                replica_id="desktop",
                state_dir=tmp_path / "desktop-state",
            ),
            client=StoreClient(store, "desktop"),
        )
        fresh = desktop.ensure_writer()
        assert fresh.fencing_token > stale_token

        with pytest.raises(OpError) as fenced:
            writer_lease_module.validate_active_write_fence()
        assert fenced.value.code == "WRITER_FENCED"
    finally:
        writer_lease_module._ACTIVE_WRITE_FENCE.reset(fence_context)


def test_two_manager_handover_completes_within_idle_plus_ttl_third(
    tmp_path: Path,
) -> None:
    """A non-preferred holder (laptop) idles out; the preferred replica
    (desktop) reclaims within roughly one renew tick after that — bounded by
    `idle_release_seconds + ttl_seconds/3` (desktop's own reclaim cadence)."""
    ttl = 3.0
    idle = 1.0
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite")
    laptop = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="laptop",
            state_dir=tmp_path / "laptop-state",
            ttl_seconds=ttl,
            idle_release_seconds=idle,
        ),
        client=StoreClient(store, "laptop"),
    )
    desktop = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="desktop",
            state_dir=tmp_path / "desktop-state",
            ttl_seconds=ttl,
            idle_release_seconds=idle,
            preferred_writer=True,
        ),
        client=StoreClient(store, "desktop"),
    )
    laptop.ensure_writer()
    started = time.monotonic()
    laptop.start_renewer()
    desktop.start_renewer()
    try:
        deadline = started + idle + ttl / 3 + 3.0  # generous scheduling slack
        reclaimed = False
        while time.monotonic() < deadline:
            if desktop.status()["role"] == "writer":
                reclaimed = True
                break
            time.sleep(0.05)
        assert reclaimed, "preferred replica did not reclaim after the idle holder released"
        elapsed = time.monotonic() - started
        assert elapsed <= idle + ttl / 3 + 3.0
    finally:
        laptop.close()
        desktop.close()

def test_owner_lock_registration_failure_falls_back_to_ownerless_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a store whose own owner-lock file cannot be held must NOT
    stamp its owner id onto pending rows — a peer would probe that owner as
    dead and abandon a LIVE mutation. NULL ownership rides the fail-closed
    legacy grace period instead."""
    with monkeypatch.context() as patched:
        patched.setattr(
            writer_lease_module, "_acquire_own_owner_lock", lambda *_args: None
        )
        manager = LeaseManager(
            LeaseConfig(state_dir=tmp_path), idempotency_wait_seconds=0
        )
    assert manager.idempotency.owner_id is None

    command = _command(writes=True, leaf=lambda: pytest.fail("pending retry ran leaf"))
    digest = writer_lease_module._command_digest(command, {})
    key = writer_lease_module._effective_idempotency_key(
        manager,
        command=command,
        mutation_subject="standalone",
        digest=digest,
        idempotency_key="live",
        principal_scope="principal:alice",
    )[0]
    with sqlite3.connect(manager.idempotency.path) as connection:
        connection.execute(
            "INSERT INTO mutations(key, digest, state, updated_at, owner) "
            "VALUES (?, ?, 'pending', ?, NULL)",
            (key, digest, time.time()),
        )

    peer = LeaseManager(LeaseConfig(state_dir=tmp_path), idempotency_wait_seconds=0)
    with pytest.raises(OpError) as pending:
        peer.invoke(
            command,
            (),
            {},
            idempotency_key="live",
            idempotency_principal_scope="principal:alice",
        )
    assert pending.value.code == "MUTATION_ACKNOWLEDGEMENT_PENDING"

def test_idle_release_default_tracks_a_raised_ttl() -> None:
    """Raising EXOMEM_WRITER_LEASE_TTL alone must never brick startup on the
    idle>=ttl validation: the unset-idle default tracks the TTL."""
    config = LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_TTL": "90"})
    assert config.idle_release_seconds == 90.0
    assert LeaseConfig.from_env({}).idle_release_seconds == 60.0


def test_probe_acquire_does_not_refresh_idle_activity(tmp_path: Path) -> None:
    """An availability probe (media worker polls every 5s) must not count as
    write activity, or a stuck queue would suppress idle release forever."""
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite")
    manager = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="laptop",
            state_dir=tmp_path / "laptop-state",
            idle_release_seconds=60.0,
        ),
        client=StoreClient(store, "laptop"),
    )
    manager.ensure_writer()
    backdated = time.monotonic() - 120.0
    manager._last_activity_monotonic = backdated
    manager.ensure_writer(cause="probe")
    assert manager._last_activity_monotonic == backdated


def test_ensure_writer_racing_a_live_in_flight_release_gets_a_fresh_token(
    tmp_path: Path,
) -> None:
    """A genuinely concurrent race: `client.release()` is blocked mid-flight
    while the idle-release path holds `self._lock`; an `ensure_writer`
    arriving during that window must wait out the locked release and then
    acquire a strictly newer token — never a grant the release then revokes."""
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite")
    release_entered = threading.Event()
    release_unblocked = threading.Event()

    class BlockingReleaseClient(StoreClient):
        def release(self, fencing_token):  # noqa: ANN001, ANN201
            release_entered.set()
            assert release_unblocked.wait(_HOLD_SECONDS)
            return super().release(fencing_token)

    laptop = LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id="laptop",
            state_dir=tmp_path / "laptop-state",
            idle_release_seconds=60.0,
        ),
        client=BlockingReleaseClient(store, "laptop"),
    )
    first = laptop.ensure_writer().fencing_token
    laptop._last_activity_monotonic = time.monotonic() - 60.0

    release_result: dict[str, bool] = {}

    def run_release() -> None:
        release_result["released"] = laptop._maybe_idle_release(first)

    releaser = threading.Thread(target=run_release)
    releaser.start()
    assert release_entered.wait(_OBSERVE_SECONDS)

    acquired: dict[str, object] = {}

    def run_acquire() -> None:
        acquired["record"] = laptop.ensure_writer()

    acquirer = threading.Thread(target=run_acquire)
    acquirer.start()
    acquirer.join(_STILL_BLOCKED_SECONDS)
    assert acquirer.is_alive(), "ensure_writer did not wait for the locked release"
    assert "record" not in acquired

    release_unblocked.set()
    releaser.join(timeout=_HOLD_SECONDS)
    acquirer.join(timeout=_HOLD_SECONDS)
    assert not acquirer.is_alive()
    assert release_result["released"] is True
    record = acquired["record"]
    assert record.fencing_token > first
    laptop.validate_fencing_token(record.fencing_token)
