"""Tamper-evident governance receipt chain coverage."""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from exomem import audit, delete_directory, delete_file, embeddings, index_sync, recover_from_trash
from exomem import reconcile as reconcile_module
from exomem.governance import egress, receipts, store

_PRIOR = "a" * 64
_TARGET = "b" * 64
_MEMORY_REF = "exomem://memory/12345678-1234-1234-1234-123456789abc"
_OPERATION_ID = "d" * 64
_BOUNDARY_ID = "e" * 32
_RETRY_ID = "f" * 32
_CRASH_ID = "1" * 32

_LIFECYCLE_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_LIFECYCLE_RULE = "01ARZ3NDEKTSV4RRFFQ69G5FB0"


@pytest.fixture(autouse=True)
def receipt_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "machine-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(root))
    return root


def _lifecycle_payload() -> dict[str, object]:
    return {
        "manifest_digest": "a" * 64,
        "affected_refs": [_MEMORY_REF],
        "content_hashes": ["b" * 64],
        "exact_state_digest": "c" * 64,
        "causation_id": _OPERATION_ID,
    }


def _write_restricting_policy(vault: Path, pattern: str) -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    scope = governance / "scopes" / "lifecycle.yaml"
    rule = governance / "rules" / "lifecycle.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    rule.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f'governance_version: 1\nid: {_LIFECYCLE_SCOPE}\npaths: ["{pattern}"]\n',
        encoding="utf-8",
    )
    rule.write_text(
        f"governance_version: 1\nid: {_LIFECYCLE_RULE}\n"
        f"scope_ids: [\"{_LIFECYCLE_SCOPE}\"]\naudience: external\nceiling: 4\n",
        encoding="utf-8",
    )
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _receipt_records(vault: Path) -> list[dict[str, object]]:
    root = vault / "Knowledge Base" / "_Governance" / "events"
    return [
        json.loads(line)
        for path in sorted(root.rglob("*.jsonl")) if path.is_file()
        for line in path.read_text(encoding="utf-8").splitlines()
    ] if root.exists() else []


def _recursive_snapshot(*roots: Path) -> dict[tuple[int, str], tuple[str, bytes | None]]:
    snapshot: dict[tuple[int, str], tuple[str, bytes | None]] = {}
    for index, root in enumerate(roots):
        if not root.exists():
            snapshot[(index, ".")] = ("missing", None)
            continue
        snapshot[(index, ".")] = ("directory", None)
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            snapshot[(index, relative)] = (
                ("directory", None) if path.is_dir() else ("file", path.read_bytes())
            )
    return snapshot


def _semantic_snapshot(*roots: Path) -> dict[tuple[int, str], tuple[str, bytes | None]]:
    """Snapshot evidence/data, excluding SQLite's empty lock coordination files."""
    return {
        key: value
        for key, value in _recursive_snapshot(*roots).items()
        if not key[1].endswith(".sqlite-shm")
        and not (key[1].endswith(".sqlite-wal") and value == ("file", b""))
    }


def test_append_canonical_record_and_verify_chain(vault: Path) -> None:
    record = receipts.append_event(
        vault,
        event_type="disclosure",
        payload={"outcomes": [{"ref": "Notes/one", "size": 1}]},
        timestamp="2026-07-26T12:00:00Z",
    )

    assert record["seq"] == 1
    assert record["prev"] == "0" * 64
    assert receipts.verify_chain(vault)["valid"] is True


def test_events_follow_the_configured_knowledge_base_directory(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Vault")
    (vault / "Vault").mkdir()

    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert list((vault / "Vault" / "_Governance" / "events").rglob("*.jsonl"))


def test_audit_reports_tamper_without_repairing_the_chain(vault: Path) -> None:
    record = receipts.append_event(
        vault,
        event_type="disclosure",
        payload={"outcomes": []},
        timestamp="2026-07-26T12:00:00Z",
    )
    event_file = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
    original = event_file.read_text(encoding="utf-8")
    event_file.write_text(original.replace(record["hash"], "f" * 64), encoding="utf-8")

    report = audit.audit(vault, categories=["governance_receipts"])

    assert any(f.category == "governance_receipts" for f in report.findings)
    assert event_file.read_text(encoding="utf-8") != original


def test_verify_reports_edited_truncated_and_broken_month_chains(vault: Path) -> None:
    first = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-06-30T12:00:00Z"
    )
    receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-07-01T12:00:00Z"
    )
    july = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("2026-07.jsonl"))
    july_record = json.loads(july.read_text(encoding="utf-8"))
    july_record["prev"] = "0" * 64
    july.write_text(json.dumps(july_record) + "\n", encoding="utf-8")

    codes = {issue["code"] for issue in receipts.verify_chain(vault)["issues"]}

    assert {"broken_month_link", "broken_link", "hash_mismatch"} <= codes
    july.write_text("", encoding="utf-8")
    assert any(issue["code"] == "truncated_tail" for issue in receipts.verify_chain(vault)["issues"])
    assert first["seq"] == 1


def test_rejects_nonfinite_payload_and_stale_anchor(vault: Path) -> None:
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": [], "size": float("nan")})
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipts_head SET observed_seq = 0, observed_hash = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(receipts.ReceiptError, match="stale"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


def test_internal_retry_adopts_its_own_verified_tail_without_duplication(vault: Path) -> None:
    event = receipts.append_event(
        vault, event_type="disclosure", event_id=_BOUNDARY_ID, payload={"outcomes": []}
    )
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipts_head SET observed_seq = 0, observed_hash = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()

    retry = receipts.append_event(
        vault, event_type="disclosure", event_id=_BOUNDARY_ID, payload={"outcomes": []}
    )

    assert retry["hash"] == event["hash"]
    assert receipts.verify_chain(vault)["instances"][event["instance_id"]]["tail_seq"] == 1


def test_reconcile_repairs_ahead_critical_anchor_and_lost_buffered_suffix(vault: Path) -> None:
    critical = receipts.append_event(
        vault, event_type="deletion", payload=_lifecycle_payload(), critical=True
    )
    conn = store.open_connection(vault)
    try:
        conn.execute(
            "UPDATE receipts_head SET durable_seq=0, durable_hash=?, observed_seq=0, observed_hash=?",
            ("0" * 64, "0" * 64),
        )
        conn.commit()
    finally:
        conn.close()
    sidecar = store.sidecar_path(vault)
    before_preview = sidecar.read_bytes()
    preview = receipts.reconcile(vault, dry_run=True)
    assert preview["repairs"] == [{"kind": "anchor", "instance_id": critical["instance_id"], "durable_seq": 1, "observed_seq": 1}]
    assert sidecar.read_bytes() == before_preview
    receipts.reconcile(vault)
    assert receipts.verify_chain(vault)["valid"] is True

    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    event_file = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
    event_file.write_bytes(event_file.read_bytes().splitlines()[0] + b"\n")
    receipts.reconcile(vault)
    assert receipts.verify_chain(vault)["valid"] is True


def test_critical_intents_are_receipt_first_exactly_once_and_reconcilable(vault: Path) -> None:
    intent = receipts.begin_event(vault, operation="delete", prior=_PRIOR, target=_TARGET)
    assert receipts.commit_event(vault, intent["event_id"])["phase"] == "committed"
    assert receipts.commit_event(vault, intent["event_id"])["phase"] == "committed"
    with pytest.raises(receipts.ReceiptError):
        receipts.abort_event(vault, intent["event_id"])
    # An unresolved, ambiguous intent remains unresolved without a resolver.
    unresolved = receipts.begin_event(vault, operation="recover", prior=_PRIOR, target=_TARGET)
    assert receipts.reconcile(vault, dry_run=True)["unresolved"]
    receipts.reconcile(vault, state_resolver=lambda _: _PRIOR)
    assert receipts.abort_event(vault, unresolved["event_id"])["phase"] == "aborted"


def test_same_instance_process_appenders_serialize_sequences(vault: Path) -> None:
    script = (
        "from pathlib import Path; from exomem.governance import receipts; "
        "import sys; receipts.append_event(Path(sys.argv[1]), event_type='disclosure', payload={'outcomes': []})"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(vault)],
            env={**os.environ, "PYTHONPATH": "src", "EXOMEM_DISABLE_EMBEDDINGS": "1"},
        )
        for _ in range(4)
    ]
    assert [process.wait(timeout=20) for process in processes] == [0, 0, 0, 0]
    instance = next(iter(receipts.verify_chain(vault)["instances"].values()))
    assert instance["tail_seq"] == 4


def test_maintain_reconcile_calls_receipt_reconcile_only_in_write_route(vault: Path, monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(receipts, "reconcile", lambda _vault, *, dry_run: calls.append(dry_run) or {"repairs": []})

    report = reconcile_module.reconcile(vault, dry_run=True)

    assert calls == [True]
    assert report.receipt_reconcile == {"repairs": []}


def test_fresh_sidecar_appenders_bootstrap_once_under_a_shared_lock(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    (root / "Knowledge Base").mkdir(parents=True)
    script = (
        "from pathlib import Path; from exomem.governance import receipts; import sys,time; "
        "time.sleep(.1); "
        "receipts.append_event(Path(sys.argv[1]), event_type='disclosure', payload={'outcomes': []})"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(root)], env={**os.environ, "PYTHONPATH": "src"})
        for _ in range(12)
    ]
    assert [process.wait(timeout=20) for process in processes] == [0] * 12
    assert next(iter(receipts.verify_chain(root)["instances"].values()))["tail_seq"] == 12


def test_quiesced_receipt_connections_allow_an_atomic_sidecar_replacement(
    vault: Path, tmp_path: Path
) -> None:
    first = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    sidecar = store.sidecar_path(vault).resolve()
    old_entry = receipts._RECEIPT_CONNECTIONS[sidecar]
    receipts._close_receipt_connections()
    with pytest.raises(sqlite3.ProgrammingError):
        old_entry.connection.execute("SELECT 1")

    replacement_vault = tmp_path / "replacement-vault"
    (replacement_vault / "Knowledge Base").mkdir(parents=True)
    replacement = store.open_connection(replacement_vault)
    replacement_instance = "9" * 32
    try:
        replacement.execute(
            "INSERT INTO receipt_instance(singleton, instance_id) VALUES (1, ?)",
            (replacement_instance,),
        )
        replacement.commit()
    finally:
        replacement.close()
    os.replace(store.sidecar_path(replacement_vault), sidecar)

    second = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )

    assert first["instance_id"] != replacement_instance
    assert second["instance_id"] == replacement_instance
    assert second["seq"] == 1


def test_receipt_connection_uses_normal_for_buffered_append_and_full_for_critical(
    vault: Path,
) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    connection = receipts._RECEIPT_CONNECTIONS[store.sidecar_path(vault).resolve()].connection

    assert connection.execute("PRAGMA synchronous").fetchone() == (1,)

    receipts.append_event(
        vault,
        event_type="deletion",
        payload=_lifecycle_payload(),
        critical=True,
    )

    assert connection.execute("PRAGMA synchronous").fetchone() == (2,)


def test_warmed_receipt_connection_preserves_an_in_place_future_schema(
    vault: Path,
) -> None:
    first = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    path = store.sidecar_path(vault).resolve()
    prior = receipts._RECEIPT_CONNECTIONS[path]
    external = store.open_connection(vault)
    try:
        external.execute("CREATE TABLE future_receipt_marker(value TEXT)")
        external.execute("PRAGMA user_version = 4")
        external.commit()
    finally:
        external.close()

    second = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    current = store.open_connection(vault)
    try:
        version = current.execute("PRAGMA user_version").fetchone()[0]
        marker = current.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='future_receipt_marker'"
        ).fetchone()
    finally:
        current.close()

    assert second["instance_id"] == first["instance_id"]
    assert second["seq"] == 2
    assert receipts._RECEIPT_CONNECTIONS[path] is not prior
    with pytest.raises(sqlite3.ProgrammingError):
        prior.connection.execute("SELECT 1")
    assert version == 4
    assert marker == (1,)


def test_receipt_connection_exception_is_evicted_and_reopened(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    original_read_tail = receipts._read_tail_record
    path = store.sidecar_path(vault).resolve()
    failed_entry = receipts._RECEIPT_CONNECTIONS[path]

    def fail_tail(_instance_dir: Path) -> None:
        raise RuntimeError("injected tail failure")

    monkeypatch.setattr(receipts, "_read_tail_record", fail_tail)
    with pytest.raises(RuntimeError, match="injected tail failure"):
        receipts.append_event(
            vault, event_type="disclosure", payload={"outcomes": []}
        )
    assert path not in receipts._RECEIPT_CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError):
        failed_entry.connection.execute("SELECT 1")

    monkeypatch.setattr(receipts, "_read_tail_record", original_read_tail)
    second = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    assert second["instance_id"] == first["instance_id"]
    assert second["seq"] == 2


def test_receipt_connection_quiesce_closes_descriptors_and_reopens(
    vault: Path,
) -> None:
    first = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    path = store.sidecar_path(vault).resolve()
    closed_entry = receipts._RECEIPT_CONNECTIONS[path]

    receipts._close_receipt_connections()

    assert receipts._RECEIPT_ACTIVE_CONNECTIONS == 0
    assert not receipts._RECEIPT_CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError):
        closed_entry.connection.execute("SELECT 1")
    second = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    assert second["instance_id"] == first["instance_id"]
    assert second["seq"] == 2


def test_receipt_connection_lru_evicts_and_closes_idle_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts._close_receipt_connections()
    monkeypatch.setattr(receipts, "_RECEIPT_CONNECTIONS_MAX", 2)
    roots = [tmp_path / f"lru-{index}" for index in range(3)]
    for root in roots:
        (root / "Knowledge Base").mkdir(parents=True)
    ready = threading.Barrier(4)
    release = threading.Event()
    opened: list[sqlite3.Connection] = []

    def hold(root: Path) -> None:
        with receipts._receipt_connection(root) as connection:
            opened.append(connection)
            ready.wait(timeout=5)
            assert release.wait(timeout=5)

    threads = [threading.Thread(target=hold, args=(root,)) for root in roots]
    for thread in threads:
        thread.start()
    ready.wait(timeout=5)
    assert len(receipts._RECEIPT_CONNECTIONS) == 3
    release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert len(receipts._RECEIPT_CONNECTIONS) == 2
    closed = 0
    for connection in opened:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            closed += 1
    assert closed == 1


#: A child that deadlocks must fail this test, not the session. Forking with
#: another thread inside a receipt connection is the whole point here, and a
#: fork handler that ever fails to quiesce leaves the child holding a mutex
#: no thread will release. An unbounded `os.read`/`os.waitpid` then blocks the
#: parent forever: pytest's session timeout eventually kills the shard, no
#: junit is written, and CI reports a whole shard missing rather than one
#: named test. Ten seconds is ~200x the timer this test arms.
_FORK_PROBE_TIMEOUT_SECONDS = 10.0


def _kill_child(pid: int) -> None:
    """Stop a wedged child so the reap below cannot block in turn."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)


def _read_from_child(read_fd: int, pid: int) -> bytes:
    """Read the child's one report, or say plainly that it never sent one."""
    ready, _, _ = select.select([read_fd], [], [], _FORK_PROBE_TIMEOUT_SECONDS)
    if not ready:
        _kill_child(pid)
        raise AssertionError(
            f"forked child sent nothing within {_FORK_PROBE_TIMEOUT_SECONDS:.0f}s; "
            "the fork handlers left it holding a receipt lock no thread can release"
        )
    return os.read(read_fd, 4096)


def _reap_child(pid: int) -> tuple[int, int]:
    """Wait for the child, bounded, so a late hang is named rather than hung on."""
    deadline = time.monotonic() + _FORK_PROBE_TIMEOUT_SECONDS
    while True:
        reaped, status = os.waitpid(pid, os.WNOHANG)
        if reaped:
            return reaped, status
        if time.monotonic() >= deadline:
            _kill_child(pid)
            raise AssertionError(
                f"forked child wrote its report but did not exit within "
                f"{_FORK_PROBE_TIMEOUT_SECONDS:.0f}s"
            )
        time.sleep(0.01)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_quiesces_active_receipt_connection_and_reopens_per_pid(
    vault: Path,
) -> None:
    first = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    inherited_entry = receipts._RECEIPT_CONNECTIONS[
        store.sidecar_path(vault).resolve()
    ]
    active = threading.Event()
    release = threading.Event()

    def hold_connection() -> None:
        with receipts._receipt_connection(vault):
            active.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_connection)
    holder.start()
    assert active.wait(timeout=5)
    with pytest.raises(receipts.ReceiptError, match="still in use"):
        receipts._close_receipt_connections()
    timer = threading.Timer(0.05, release.set)
    timer.start()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions are reported through the pipe
        try:
            os.close(read_fd)
            assert receipts._RECEIPT_ACTIVE_CONNECTIONS == 0
            assert not receipts._RECEIPT_CONNECTIONS
            child = receipts.append_event(
                vault, event_type="disclosure", payload={"outcomes": []}
            )
            result = {"instance_id": child["instance_id"], "seq": child["seq"]}
        except Exception as exc:  # noqa: BLE001 - child reports failures through pipe
            result = {"error": repr(exc)}
        os.write(write_fd, json.dumps(result).encode("utf-8"))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    child_payload = _read_from_child(read_fd, pid)
    os.close(read_fd)
    _, status = _reap_child(pid)
    timer.join(timeout=5)
    holder.join(timeout=5)
    assert not holder.is_alive()
    assert os.waitstatus_to_exitcode(status) == 0
    result = json.loads(child_payload)
    assert "error" not in result, result
    assert result == {"instance_id": first["instance_id"], "seq": 2}
    assert receipts._RECEIPT_ACTIVE_CONNECTIONS == 0
    assert not receipts._RECEIPT_CONNECTIONS
    with pytest.raises(sqlite3.ProgrammingError):
        inherited_entry.connection.execute("SELECT 1")

    parent = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}
    )
    assert parent["instance_id"] == first["instance_id"]
    assert parent["seq"] == 3


def test_reconcile_does_not_promote_buffered_records_to_durable(vault: Path) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipts_head SET observed_seq=0, observed_hash=?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()

    receipts.reconcile(vault)

    conn = store.open_connection(vault)
    try:
        durable = conn.execute("SELECT durable_seq, observed_seq FROM receipts_head").fetchone()
    finally:
        conn.close()
    assert durable == (0, event["seq"])


def test_critical_retry_promotes_the_durable_anchor(vault: Path) -> None:
    event = receipts.append_event(
        vault,
        event_type="deletion",
        event_id=_RETRY_ID,
        payload=_lifecycle_payload(),
        critical=True,
    )
    conn = store.open_connection(vault)
    try:
        conn.execute(
            "UPDATE receipts_head SET durable_seq=0, durable_hash=?, observed_seq=0, "
            "observed_hash=?, path='', byte_offset=0",
            ("0" * 64, "0" * 64),
        )
        conn.commit()
    finally:
        conn.close()

    receipts.append_event(
        vault,
        event_type="deletion",
        event_id=_RETRY_ID,
        payload=_lifecycle_payload(),
        critical=True,
    )

    conn = store.open_connection(vault)
    try:
        assert conn.execute("SELECT durable_seq, observed_seq FROM receipts_head").fetchone() == (event["seq"], event["seq"])
    finally:
        conn.close()


def test_competing_terminals_serialize_to_exactly_one(vault: Path) -> None:
    intent = receipts.begin_event(vault, operation="delete", prior=_PRIOR, target=_TARGET)
    outcomes: list[str] = []

    def terminal(phase: str) -> None:
        try:
            getattr(receipts, f"{phase}_event")(vault, intent["event_id"])
            outcomes.append(phase)
        except receipts.ReceiptError:
            outcomes.append("refused")

    threads = [threading.Thread(target=terminal, args=(phase,)) for phase in ("commit", "abort")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    records, _ = receipts._chain_state(next((vault / "Knowledge Base" / "_Governance" / "events").iterdir()))
    terminals = [record for record in records if record.get("causation_id") == intent["event_id"]]
    assert len(terminals) == 1
    assert outcomes.count("refused") == 1


@pytest.mark.parametrize("payload", [
    {"outcomes": [], "released_text": "secret"},
    {"outcomes": [], "scope_label": "Human label"},
    {"outcomes": [], "token_bytes": "token"},
    {"outcomes": [], "unregistered": "field"},
])
def test_receipt_schemas_reject_plaintext_and_unknown_fields(vault: Path, payload: dict) -> None:
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type="disclosure", payload=payload)


@pytest.mark.parametrize("timestamp", ["../../outside", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z"])
def test_receipt_timestamp_cannot_escape_its_month_directory(vault: Path, timestamp: str) -> None:
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []}, timestamp=timestamp)


def test_audit_reports_malformed_json_records_without_crashing(vault: Path) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    path = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["seq"] = "not-an-int"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    report = receipts.verify_chain(vault)

    assert any(item["code"] == "invalid_envelope" for item in report["issues"])
    assert event["seq"] == 1


def test_ordinary_append_uses_only_a_bounded_tail_read(vault: Path, monkeypatch) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    monkeypatch.setattr(receipts, "_chain_state", lambda _path: pytest.fail("append scanned history"))

    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


def test_receipt_conflicts_are_reported_by_verification(vault: Path) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    root = vault / "Knowledge Base" / "_Governance" / "events"
    (root / "conflicted copy.jsonl").write_text("{}\n", encoding="utf-8")

    assert any(item["code"] == "evidence_conflict" for item in receipts.verify_chain(vault)["issues"])


def test_registered_state_resolver_reconciles_matching_intent(vault: Path) -> None:
    intent = receipts.begin_event(vault, operation="test-operation", prior=_PRIOR, target=_TARGET)
    receipts.register_state_resolver("test-operation", lambda _intent: _TARGET)
    try:
        report = receipts.reconcile(vault)
    finally:
        receipts.unregister_state_resolver("test-operation")

    assert {"kind": "terminal", "event_id": intent["event_id"], "phase": "committed"} in report["repairs"]


def test_crash_after_critical_fsync_leaves_sidecar_unadvanced_until_retry(vault: Path, monkeypatch) -> None:
    def crash(point: str) -> None:
        if point == "before_sidecar_commit":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(receipts, "_crash_point", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        receipts.append_event(
            vault,
            event_type="deletion",
            event_id=_CRASH_ID,
            payload=_lifecycle_payload(),
            critical=True,
        )
    monkeypatch.setattr(receipts, "_crash_point", lambda _point: None)

    receipts.append_event(
        vault,
        event_type="deletion",
        event_id=_CRASH_ID,
        payload=_lifecycle_payload(),
        critical=True,
    )
    assert receipts.verify_chain(vault)["valid"] is True


@pytest.mark.parametrize("point", ["after_jsonl_write", "after_jsonl_flush", "after_jsonl_fsync", "after_terminal_append"])
def test_critical_write_order_crashes_recover_idempotently(
    vault: Path, monkeypatch, point: str
) -> None:
    event_id = _CRASH_ID

    def crash(candidate: str) -> None:
        if candidate == point:
            raise RuntimeError(point)

    monkeypatch.setattr(receipts, "_crash_point", crash)
    append = receipts.append_event
    if point == "after_terminal_append":
        intent = receipts.begin_event(vault, operation="delete", prior=_PRIOR, target=_TARGET)
        event_id = intent["event_id"]
        with pytest.raises(RuntimeError, match=point):
            receipts.commit_event(vault, event_id)

        def retry() -> None:
            receipts.commit_event(vault, event_id)
    else:
        with pytest.raises(RuntimeError, match=point):
            append(
                vault,
                event_type="deletion",
                event_id=event_id,
                payload=_lifecycle_payload(),
                critical=True,
            )

        def retry() -> None:
            append(
                vault,
                event_type="deletion",
                event_id=event_id,
                payload=_lifecycle_payload(),
                critical=True,
            )
    monkeypatch.setattr(receipts, "_crash_point", lambda _point: None)

    retry()

    assert receipts.verify_chain(vault)["valid"] is True


def test_write_reconcile_holds_the_receipt_lock(vault: Path, monkeypatch) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    entered: list[bool] = []

    @contextmanager
    def lock(_vault: Path):
        entered.append(True)
        yield

    monkeypatch.setattr(receipts, "_receipt_lock", lock)
    receipts.reconcile(vault)

    assert entered == [True]


def test_critical_append_fsyncs_the_entire_new_durable_prefix(vault: Path, monkeypatch) -> None:
    receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-06-30T12:00:00Z"
    )
    fsynced: list[str] = []
    original_fsync_path = receipts._fsync_path

    def record_fsync(path: Path) -> None:
        fsynced.append(path.name)
        original_fsync_path(path)

    monkeypatch.setattr(receipts, "_fsync_path", record_fsync)
    receipts.append_event(
        vault,
        event_type="deletion",
        payload=_lifecycle_payload(),
        critical=True,
        timestamp="2026-07-01T12:00:00Z",
    )

    assert set(fsynced) == {"2026-06.jsonl", "2026-07.jsonl"}


def test_successful_deterministic_intent_retry_does_not_append_twice(vault: Path) -> None:
    first = receipts.begin_event(vault, operation="delete", prior=_PRIOR, target=_TARGET)
    second = receipts.begin_event(vault, operation="delete", prior=_PRIOR, target=_TARGET)

    assert second["hash"] == first["hash"]
    assert receipts.verify_chain(vault)["instances"][first["instance_id"]]["tail_seq"] == 1


@pytest.mark.parametrize("payload", [
    {"outcomes": [{"ref": "human label"}]},
    {"outcomes": [{"content_hash": "not-a-digest"}]},
    {"outcomes": [{"size": True}]},
    {"outcomes": [{"level": 7}]},
])
def test_disclosure_outcomes_require_content_free_typed_values(vault: Path, payload: dict) -> None:
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type="disclosure", payload=payload)


@pytest.mark.parametrize("event_type,payload", [
    ("token_mint", {"token_id_digest": "a" * 64}),
    ("deletion", {"manifest_digest": "not-a-digest"}),
    ("credential_block", {"count": "one"}),
])
def test_event_schemas_require_their_declared_fields_and_types(
    vault: Path, event_type: str, payload: dict
) -> None:
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type=event_type, payload=payload)


def test_append_refuses_a_divergent_durable_anchor_behind_observed(vault: Path) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipts_head SET durable_seq=1, durable_hash=?", ("f" * 64,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(receipts.ReceiptError, match="durable"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


def test_append_rejects_backdated_month_rotation(vault: Path) -> None:
    receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-07-01T00:00:00Z"
    )

    with pytest.raises(receipts.ReceiptError, match="backdated"):
        receipts.append_event(
            vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-06-30T00:00:00Z"
        )


def test_append_rejects_a_partial_final_jsonl_line(vault: Path) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    path = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    with pytest.raises(receipts.ReceiptError, match="newline"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


def test_outcome_count_is_bounded_below_the_tail_window(vault: Path) -> None:
    maximum = [{"ref": f"Notes/{index}"} for index in range(receipts.MAX_OUTCOMES)]
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": maximum})

    too_many = [{"ref": f"Notes/{index}"} for index in range(receipts.MAX_OUTCOMES + 1)]
    with pytest.raises(receipts.ReceiptError, match="outcomes"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": too_many})


def test_reconcile_dry_run_keeps_evidence_and_operational_files_byte_identical(vault: Path) -> None:
    receipts.append_event(vault, event_type="deletion", payload=_lifecycle_payload(), critical=True)
    tombstone = vault / "Knowledge Base" / "_Governance" / "deletion-tombstones" / "pending.json"
    tombstone.parent.mkdir(parents=True)
    tombstone.write_bytes(b'{"pending":true}\n')
    derivative = vault / "Knowledge Base" / "Notes" / "derived.bin"
    derivative.parent.mkdir(exist_ok=True)
    derivative.write_bytes(b"unchanged")
    state_root = Path(os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"])
    before = _semantic_snapshot(vault, state_root)

    receipts.reconcile(vault, dry_run=True)

    assert _semantic_snapshot(vault, state_root) == before


def test_reconcile_repair_is_idempotent(vault: Path) -> None:
    event = receipts.append_event(vault, event_type="deletion", payload=_lifecycle_payload(), critical=True)
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipts_head SET durable_seq=0, durable_hash=?, observed_seq=0, observed_hash=?", ("0" * 64, "0" * 64))
        conn.commit()
    finally:
        conn.close()

    first = receipts.reconcile(vault)
    second = receipts.reconcile(vault)

    assert first["repairs"] == [{"kind": "anchor", "instance_id": event["instance_id"], "durable_seq": 1, "observed_seq": 1}]
    assert second["repairs"] == []


def test_after_sidecar_commit_intent_retry_is_idempotent(vault: Path, monkeypatch) -> None:
    def crash(point: str) -> None:
        if point == "after_sidecar_commit":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(receipts, "_crash_point", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        receipts.begin_event(vault, operation="delete", prior=_PRIOR, target=_TARGET)
    monkeypatch.setattr(receipts, "_crash_point", lambda _point: None)

    retry = receipts.begin_event(vault, operation="delete", prior=_PRIOR, target=_TARGET)

    assert retry["seq"] == 1


@pytest.mark.parametrize("corruption", ["path", "byte_offset"])
def test_corrupt_head_locator_is_reported_refused_and_reconciled(
    vault: Path, corruption: str
) -> None:
    event = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, critical=True
    )
    sidecar = store.sidecar_path(vault)
    conn = store.open_connection(vault)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(receipts_head)")}
        assert {"path", "byte_offset"} <= columns
        locator = conn.execute(
            "SELECT path FROM receipts_head WHERE instance_id=?", (event["instance_id"],)
        ).fetchone()[0]
        assert not Path(locator).is_absolute()
        assert (vault / locator).resolve() == next(
            (vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl")
        ).resolve()
        if corruption == "path":
            conn.execute(
                "UPDATE receipts_head SET path=? WHERE instance_id=?",
                ("../../outside.jsonl", event["instance_id"]),
            )
        else:
            conn.execute(
                "UPDATE receipts_head SET byte_offset=byte_offset+1 WHERE instance_id=?",
                (event["instance_id"],),
            )
        conn.commit()
    finally:
        conn.close()
    verification = receipts.verify_chain(vault)
    assert any(issue["code"].startswith("locator_") for issue in verification["issues"])
    assert audit.audit(vault, categories=["governance_receipts"]).findings
    with pytest.raises(receipts.ReceiptError, match="locator"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    before_dry_run = sidecar.read_bytes()
    preview = receipts.reconcile(vault, dry_run=True)
    assert any(repair["kind"] == "locator" for repair in preview["repairs"])
    assert sidecar.read_bytes() == before_dry_run
    receipts.reconcile(vault)

    assert receipts.verify_chain(vault)["valid"] is True
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


def test_verify_reports_a_non_newline_terminated_final_record_as_truncated(vault: Path) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    path = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
    path.write_bytes(path.read_bytes().removesuffix(b"\n"))

    report = receipts.verify_chain(vault)

    assert any(issue["code"] == "truncated_evidence" for issue in report["issues"])
    assert audit.audit(vault, categories=["governance_receipts"]).findings


def test_empty_vault_dry_run_creates_no_sidecar_events_or_lock_state(
    tmp_path: Path, receipt_state_root: Path
) -> None:
    empty = tmp_path / "empty-vault"
    (empty / "Knowledge Base").mkdir(parents=True)
    before = _recursive_snapshot(empty, receipt_state_root)

    report = receipts.reconcile(empty, dry_run=True)

    assert report["repairs"] == []
    assert _recursive_snapshot(empty, receipt_state_root) == before


def test_receipt_lock_uses_only_the_machine_writer_lease_state_root(
    vault: Path, receipt_state_root: Path
) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert list((receipt_state_root / "mutation-locks").glob("*.lock"))
    assert not list(vault.rglob("mutation-locks"))


def test_barrier_synchronized_first_label_digests_are_process_safe(
    vault: Path, receipt_state_root: Path, tmp_path: Path
) -> None:
    conn = store.open_connection(vault)
    conn.close()
    ready = tmp_path / "ready"
    ready.mkdir()
    go = tmp_path / "go"
    script = (
        "import os,sys,time; from pathlib import Path; "
        "from exomem.governance import receipts; "
        "vault,ready,go=map(Path,sys.argv[1:4]); "
        "original=receipts.os.urandom; "
        "receipts.os.urandom=lambda n:(ready.joinpath('secret-'+str(os.getpid())).touch(),time.sleep(.25),original(n))[2]; "
        "ready.joinpath('call-'+str(os.getpid())).touch(); "
        "exec(\"while not go.exists(): time.sleep(.005)\"); "
        "print(receipts.label_digest(vault, 'Sensitive project'))"
    )
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "EXOMEM_DISABLE_EMBEDDINGS": "1",
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(receipt_state_root),
    }
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(vault), str(ready), str(go)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    deadline = time.monotonic() + 10
    while len(list(ready.glob("call-*"))) < len(processes) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(list(ready.glob("call-*"))) == len(processes)
    go.touch()
    results = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0] * len(processes), results
    assert len({stdout.strip() for stdout, _stderr in results}) == 1


@pytest.mark.parametrize("event_type", ["deletion", "recovery"])
def test_lifecycle_schemas_require_the_complete_content_free_state(
    vault: Path, event_type: str
) -> None:
    record = receipts.append_event(vault, event_type=event_type, payload=_lifecycle_payload())

    assert record["affected_refs"] == [_MEMORY_REF]
    assert record["content_hashes"] == ["b" * 64]


@pytest.mark.parametrize(
    "payload",
    [
        {"manifest_digest": "a" * 64},
        {**_lifecycle_payload(), "affected_refs": ["human label"]},
        {**_lifecycle_payload(), "content_hashes": ["not-a-hash"]},
        {**_lifecycle_payload(), "exact_state_digest": True},
        {**_lifecycle_payload(), "causation_id": "human label"},
    ],
)
@pytest.mark.parametrize("event_type", ["deletion", "recovery"])
def test_lifecycle_schemas_reject_incomplete_or_mistyped_state(
    vault: Path, event_type: str, payload: dict[str, object]
) -> None:
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type=event_type, payload=payload)


def test_verify_reads_a_current_wal_anchor_and_detects_jsonl_rollback(vault: Path) -> None:
    receipts.append_event(vault, event_type="deletion", payload=_lifecycle_payload(), critical=True)
    reader = sqlite3.connect(store.sidecar_path(vault))
    reader.execute("BEGIN")
    reader.execute("SELECT durable_seq FROM receipts_head").fetchone()
    try:
        receipts.append_event(vault, event_type="deletion", payload=_lifecycle_payload(), critical=True)
        path = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
        path.write_bytes(path.read_bytes().splitlines()[0] + b"\n")
        before = _semantic_snapshot(vault)

        report = receipts.verify_chain(vault)

        assert any(issue["code"] == "durable_anchor_divergence" for issue in report["issues"])
        assert _semantic_snapshot(vault) == before
    finally:
        reader.close()


def test_verify_reads_a_locator_corruption_committed_to_wal(vault: Path) -> None:
    event = receipts.append_event(vault, event_type="deletion", payload=_lifecycle_payload(), critical=True)
    reader = sqlite3.connect(store.sidecar_path(vault))
    reader.execute("BEGIN")
    reader.execute("SELECT path FROM receipts_head").fetchone()
    try:
        conn = store.open_connection(vault)
        try:
            conn.execute(
                "UPDATE receipts_head SET path=? WHERE instance_id=?",
                ("../../outside.jsonl", event["instance_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        before = _semantic_snapshot(vault)

        report = receipts.verify_chain(vault)

        assert any(issue["code"] == "locator_divergence" for issue in report["issues"])
        assert _semantic_snapshot(vault) == before
    finally:
        reader.close()


def test_append_and_verify_reject_an_instance_symlink_escaping_events_root(
    vault: Path, tmp_path: Path
) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    outside = tmp_path / "outside-instance"
    instance_dir.rename(outside)
    instance_dir.symlink_to(outside, target_is_directory=True)
    before = _recursive_snapshot(outside)

    with pytest.raises(receipts.ReceiptError, match="instance"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    report = receipts.verify_chain(vault)
    assert any(issue["code"] == "instance_path_escape" for issue in report["issues"])
    assert _recursive_snapshot(outside) == before


def test_append_and_verify_reject_a_corrupt_sidecar_instance_id(vault: Path, tmp_path: Path) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    outside = tmp_path / "outside"
    outside.mkdir()
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipt_instance SET instance_id=?", ("../../outside",))
        conn.commit()
    finally:
        conn.close()
    before = _recursive_snapshot(outside)

    with pytest.raises(receipts.ReceiptError, match="instance"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    report = receipts.verify_chain(vault)
    assert any(issue["code"] == "invalid_instance_id" for issue in report["issues"])
    assert _recursive_snapshot(outside) == before


@pytest.mark.parametrize(
    "event_type,phase,event_id,payload",
    [
        ("disclosure", "recorded", "Patient Alice diagnosis", {"outcomes": []}),
        (
            "critical",
            "intent",
            "Patient Alice diagnosis",
            {"operation": "delete", "prior": _PRIOR, "target": _TARGET, "affected_ids": []},
        ),
    ],
)
def test_receipt_event_ids_must_be_opaque(
    vault: Path, event_type: str, phase: str, event_id: str, payload: dict[str, object]
) -> None:
    with pytest.raises(receipts.ReceiptError, match="event id"):
        receipts.append_event(
            vault, event_type=event_type, phase=phase, event_id=event_id, payload=payload, critical=True
        )


@pytest.mark.parametrize(
    "payload",
    [
        {**_lifecycle_payload(), "affected_refs": ["ConfidentialProject"]},
        {**_lifecycle_payload(), "affected_refs": ["Notes/one"]},
        {**_lifecycle_payload(), "causation_id": "Alice"},
    ],
)
def test_lifecycle_refs_and_causation_ids_must_be_opaque(vault: Path, payload: dict[str, object]) -> None:
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type="deletion", payload=payload)


def test_missing_events_tree_diverges_from_a_non_genesis_sidecar(vault: Path) -> None:
    receipts.append_event(vault, event_type="deletion", payload=_lifecycle_payload(), critical=True)
    events_root = vault / "Knowledge Base" / "_Governance" / "events"
    shutil.rmtree(events_root)

    report = receipts.verify_chain(vault)

    assert any(issue["code"] == "durable_anchor_divergence" for issue in report["issues"])
    assert audit.audit(vault, categories=["governance_receipts"]).findings
    with pytest.raises(receipts.ReceiptError, match="durable"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


def test_genesis_sidecar_without_events_is_valid(vault: Path) -> None:
    conn = store.open_connection(vault)
    try:
        receipts._instance_id(conn)
    finally:
        conn.close()

    assert receipts.verify_chain(vault)["valid"] is True


def test_invalid_active_instance_id_without_events_is_reported(vault: Path) -> None:
    conn = store.open_connection(vault)
    try:
        receipts._instance_id(conn)
        conn.execute("UPDATE receipt_instance SET instance_id='invalid'")
        conn.commit()
    finally:
        conn.close()

    report = receipts.verify_chain(vault)

    assert any(issue["code"] == "invalid_instance_id" for issue in report["issues"])


def test_unreadable_sidecar_without_events_is_reported(vault: Path) -> None:
    conn = store.open_connection(vault)
    try:
        conn.execute("DROP TABLE receipt_instance")
        conn.commit()
    finally:
        conn.close()

    report = receipts.verify_chain(vault)

    assert any(issue["code"] == "sidecar_read_error" for issue in report["issues"])


def test_verify_never_follows_a_historical_month_symlink(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_id = "2" * 32
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / instance_id
    instance_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"x" * (receipts.MAX_RECORD_BYTES + 1))
    (instance_dir / "2026-01.jsonl").symlink_to(outside)
    before = outside.read_bytes()
    original_open = Path.open

    def forbid_outside_open(path: Path, *args, **kwargs):
        if path.resolve() == outside:
            pytest.fail("verification followed outside monthly evidence")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_outside_open)
    report = receipts.verify_chain(vault)

    assert any(issue["code"] == "evidence_path_escape" for issue in report["issues"])
    assert original_open(outside, "rb").read() == before


def test_append_never_follows_a_current_month_symlink(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    path = next(
        (vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]).glob("*.jsonl")
    )
    outside = tmp_path / "outside.jsonl"
    path.replace(outside)
    path.symlink_to(outside)
    before = outside.read_bytes()
    original_open = Path.open

    def forbid_outside_open(candidate: Path, *args, **kwargs):
        if candidate.resolve() == outside:
            pytest.fail("append followed outside monthly evidence")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_outside_open)
    with pytest.raises(receipts.ReceiptError, match="evidence path"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert original_open(outside, "rb").read() == before


def test_lifecycle_schema_accepts_opaque_artifact_refs(vault: Path) -> None:
    artifact = "e" * 64
    second = "f" * 64
    payload = {
        **_lifecycle_payload(),
        "affected_refs": [f"sha256:{artifact}", _MEMORY_REF, f"sha256:{second}"],
        "content_hashes": [artifact, "b" * 64, second],
    }

    record = receipts.append_event(vault, event_type="deletion", payload=payload)

    assert record["affected_refs"] == payload["affected_refs"]


@pytest.mark.parametrize(
    "affected_ref,content_hash",
    [
        (f"sha256:{'e' * 64}", "f" * 64),
        (f"sha256:{'E' * 64}", "e" * 64),
        ("sha256:not-a-digest", "e" * 64),
    ],
)
def test_lifecycle_artifact_refs_require_the_parallel_digest(
    vault: Path, affected_ref: str, content_hash: str
) -> None:
    payload = {**_lifecycle_payload(), "affected_refs": [affected_ref], "content_hashes": [content_hash]}

    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type="deletion", payload=payload)


@pytest.mark.parametrize("entry_kind", ["regular", "same_root_symlink"])
def test_active_instance_entry_must_be_a_real_directory(vault: Path, entry_kind: str) -> None:
    conn = store.open_connection(vault)
    try:
        instance_id = receipts._instance_id(conn)
    finally:
        conn.close()
    events_root = vault / "Knowledge Base" / "_Governance" / "events"
    events_root.mkdir(parents=True)
    entry = events_root / instance_id
    if entry_kind == "regular":
        entry.write_text("not a directory", encoding="utf-8")
    else:
        alias = events_root / "alias"
        alias.mkdir()
        entry.symlink_to(alias, target_is_directory=True)

    report = receipts.verify_chain(vault)

    assert any(issue["code"] == "instance_path_escape" for issue in report["issues"])
    with pytest.raises(receipts.ReceiptError, match="instance"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


def test_month_swap_after_enumeration_never_opens_outside_evidence(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = receipts.append_event(vault, event_type="deletion", payload=_lifecycle_payload(), critical=True)
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    month = next(instance_dir.glob("*.jsonl"))
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"outside evidence")
    before = outside.read_bytes()
    swapped = False

    def swap_after_enumeration(_instance_dir: Path, name: str) -> None:
        nonlocal swapped
        if not swapped and name == month.name:
            month.unlink()
            month.symlink_to(outside)
            swapped = True

    monkeypatch.setattr(receipts, "_after_month_enumeration", swap_after_enumeration)
    report = receipts.verify_chain(vault)

    assert swapped is True
    assert any(issue["code"] == "evidence_path_escape" for issue in report["issues"])
    assert receipts._durable_locator_matches(
        vault,
        event["instance_id"],
        event["seq"],
        event["hash"],
        f"Knowledge Base/_Governance/events/{event['instance_id']}/{month.name}",
        0,
    ) is False
    with pytest.raises(receipts.ReceiptError, match="evidence path"):
        receipts._fsync_durable_prefix(instance_dir, month)
    with pytest.raises(receipts.ReceiptError, match="evidence path"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    assert outside.read_bytes() == before


def test_invalid_month_name_and_timestamp_month_mismatch_are_reported(vault: Path) -> None:
    event = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-07-01T00:00:00Z"
    )
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    month = next(instance_dir.glob("*.jsonl"))
    invalid = instance_dir / "2026-99.jsonl"
    month.rename(invalid)

    assert any(issue["code"] == "invalid_month_filename" for issue in receipts.verify_chain(vault)["issues"])

    invalid.rename(instance_dir / "2026-08.jsonl")
    assert any(issue["code"] == "timestamp_month_mismatch" for issue in receipts.verify_chain(vault)["issues"])


def test_oversized_line_is_drained_and_preserves_the_next_record_offset(vault: Path) -> None:
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    path = next((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
    original = path.read_bytes()
    oversized = b"{" + b"x" * receipts.MAX_RECORD_BYTES + b"\n"
    path.write_bytes(oversized + original)

    records, issues = receipts._read_records(path.parent)

    assert any(issue["code"] == "record_too_large" for issue in issues)
    assert records[0]["_offset"] == len(oversized)


def test_append_rejects_a_tail_whose_timestamp_does_not_match_its_month(vault: Path) -> None:
    event = receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-07-01T00:00:00Z"
    )
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    july = next(instance_dir.glob("*.jsonl"))
    june = instance_dir / "2026-06.jsonl"
    july.rename(june)

    with pytest.raises(receipts.ReceiptError, match="month"):
        receipts.append_event(
            vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-07-02T00:00:00Z"
        )

    assert not (instance_dir / "2026-07.jsonl").exists()


def test_critical_directory_fsync_precedes_the_sidecar_commit(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    original_fsync_path = receipts._fsync_path
    original_update = receipts._update_durable_head

    def track_fsync(path: Path) -> None:
        order.append("file")
        original_fsync_path(path)

    def track_directories(*args, **kwargs) -> None:
        order.append("directory")

    def track_sidecar(*args, **kwargs) -> None:
        order.append("sidecar")
        original_update(*args, **kwargs)

    monkeypatch.setattr(receipts, "_fsync_path", track_fsync)
    monkeypatch.setattr(receipts, "_fsync_durable_directories", track_directories)
    monkeypatch.setattr(receipts, "_update_durable_head", track_sidecar)
    receipts.append_event(
        vault, event_type="deletion", payload=_lifecycle_payload(), critical=True, timestamp="2026-06-30T12:00:00Z"
    )
    assert order.index("file") < order.index("directory") < order.index("sidecar")

    order.clear()
    receipts.append_event(
        vault, event_type="deletion", payload=_lifecycle_payload(), critical=True, timestamp="2026-07-01T12:00:00Z"
    )
    assert order.index("file") < order.index("directory") < order.index("sidecar")


def test_year_zero_month_is_invalid_without_a_sidecar(vault: Path) -> None:
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("3" * 32)
    instance_dir.mkdir(parents=True)
    (instance_dir / "0000-01.jsonl").write_bytes(b"")

    assert any(issue["code"] == "invalid_month_filename" for issue in receipts.verify_chain(vault)["issues"])


def test_instance_disappearance_at_the_month_open_seam_is_a_receipt_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    removed = False

    def remove_instance(_instance_dir: Path, _name: str) -> None:
        nonlocal removed
        if not removed:
            shutil.rmtree(instance_dir)
            removed = True

    monkeypatch.setattr(receipts, "_after_month_enumeration", remove_instance)
    with pytest.raises(receipts.ReceiptError):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})


@pytest.mark.parametrize("promotion", ["retry", "reconcile"])
def test_file_ahead_critical_promotion_requires_directory_durability(
    vault: Path, monkeypatch: pytest.MonkeyPatch, promotion: str
) -> None:
    event = receipts.append_event(
        vault, event_type="deletion", event_id=_RETRY_ID, payload=_lifecycle_payload(), critical=True
    )
    conn = store.open_connection(vault)
    try:
        conn.execute(
            "UPDATE receipts_head SET durable_seq=0, durable_hash=?, observed_seq=0, "
            "observed_hash=?, path='', byte_offset=0",
            ("0" * 64, "0" * 64),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        receipts,
        "_fsync_durable_directories",
        lambda *_args: (_ for _ in ()).throw(receipts.ReceiptError("directory fsync failed")),
    )
    if promotion == "retry":
        with pytest.raises(receipts.ReceiptError, match="directory"):
            receipts.append_event(
                vault, event_type="deletion", event_id=_RETRY_ID, payload=_lifecycle_payload(), critical=True
            )
    else:
        with pytest.raises(receipts.ReceiptError, match="directory"):
            receipts.reconcile(vault)

    conn = store.open_connection(vault)
    try:
        assert conn.execute("SELECT durable_seq, observed_seq FROM receipts_head").fetchone() == (0, 0)
    finally:
        conn.close()
    assert event["seq"] == 1


def test_final_month_open_permission_error_is_normalized(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    name = next(instance_dir.glob("*.jsonl")).name
    if receipts._is_windows():
        original_secure_open = receipts._open_secure_file_at

        def deny_final_entry(directory, child_name, flags, mode=0o600):
            if child_name == name:
                raise PermissionError("denied")
            return original_secure_open(directory, child_name, flags, mode)

        monkeypatch.setattr(receipts, "_open_secure_file_at", deny_final_entry)
    else:
        original_os_open = receipts.os.open

        def deny_final_entry(path, *args, **kwargs):
            if Path(path).name == name:
                raise PermissionError("denied")
            return original_os_open(path, *args, **kwargs)

        monkeypatch.setattr(receipts.os, "open", deny_final_entry)
    with pytest.raises(receipts.ReceiptError, match="evidence path"):
        with receipts._open_month_fd(instance_dir, name):
            pass


def test_exclusive_month_create_permission_error_is_normalized(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-06-30T12:00:00Z"
    )
    if receipts._is_windows():
        original_secure_open = receipts._open_secure_file_at

        def deny_create(directory, _name, flags, mode=0o600):
            if flags & os.O_CREAT:
                raise PermissionError("denied")
            return original_secure_open(directory, _name, flags, mode)

        monkeypatch.setattr(receipts, "_open_secure_file_at", deny_create)
    else:
        original_os_open = receipts.os.open

        def deny_create(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                raise PermissionError("denied")
            return original_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(receipts.os, "open", deny_create)
    with pytest.raises(receipts.ReceiptError, match="evidence path"):
        receipts.append_event(
            vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-07-01T00:00:00Z"
        )


def test_yielded_month_io_error_is_normalized_and_receipt_errors_are_preserved(vault: Path) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    name = next(instance_dir.glob("*.jsonl")).name

    with pytest.raises(receipts.ReceiptError, match="evidence path"):
        with receipts._open_month_fd(instance_dir, name):
            raise OSError("injected read failure")
    with pytest.raises(receipts.ReceiptError, match="already content-free"):
        with receipts._open_month_fd(instance_dir, name):
            raise receipts.ReceiptError("already content-free")


# ---------------------------------------------------------------------------
# Governed deletion/recovery lifecycle (OpenSpec tasks 3.1-3.2)
# ---------------------------------------------------------------------------


def test_governed_file_delete_records_lifecycle_before_terminal_and_keeps_lineage(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/governed-delete.md"
    target = vault / rel
    plaintext = "---\ntags: [secret]\n---\n# Never in evidence\nprivate body\n"
    target.write_text(plaintext, encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/governed-delete.md")

    result = delete_file.delete_file(vault, path=rel, confirm=True)

    records = _receipt_records(vault)
    phases = [(item["event_type"], item["phase"]) for item in records]
    intent_index = phases.index(("critical", "intent"))
    deletion_index = phases.index(("deletion", "recorded"))
    terminal_index = phases.index(("critical", "committed"))
    assert intent_index < deletion_index < terminal_index
    assert plaintext not in json.dumps(records)
    assert (vault / result.trash_path).read_text(encoding="utf-8") == plaintext
    tombstones = list(
        (vault / "Knowledge Base" / "_Governance" / "deletion-tombstones").glob("*.json")
    )
    assert len(tombstones) == 1
    assert json.loads(tombstones[0].read_text(encoding="utf-8"))["state"] == "committed"


def _write_restriction_shape(vault: Path, pattern: str, *, shape: str) -> None:
    """Write the SAME restriction two ways, so lifecycle can be diffed on them.

    `ceiling_zero` authors a rule that closes the scope to the one audience it
    names. `default_deny` declares the scope closed to every audience no rule
    names — and names no rule at all. A declaration restricts at least as much
    as the rule does, so every subsystem that asks "is this item restricted?"
    owes the same answer to both.
    """
    governance = vault / "Knowledge Base" / "_Governance"
    scope = governance / "scopes" / "lifecycle.yaml"
    rule = governance / "rules" / "lifecycle.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    rule.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f'governance_version: 1\nid: {_LIFECYCLE_SCOPE}\npaths: ["{pattern}"]\n'
        + ("default_deny: true\n" if shape == "default_deny" else ""),
        encoding="utf-8",
    )
    if shape == "ceiling_zero":
        rule.write_text(
            f"governance_version: 1\nid: {_LIFECYCLE_RULE}\n"
            f'scope_ids: ["{_LIFECYCLE_SCOPE}"]\naudience: external\nceiling: 0\n',
            encoding="utf-8",
        )
    elif rule.exists():
        rule.unlink()
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


_RESTRICTION_SHAPES = pytest.mark.parametrize(
    "shape", ["ceiling_zero", "default_deny"]
)


@_RESTRICTION_SHAPES
def test_deleting_a_restricted_item_tombstones_it_however_the_restriction_is_written(
    vault: Path, shape: str
) -> None:
    """A governed delete leaves a tombstone; an ungoverned one does not.

    Classify a `default_deny`-only item as ungoverned and `begin_deletion`
    takes the ungoverned branch: no tombstone, so `is_tombstoned` is False
    forever and the reference gate (`egress._ArtifactReferenceGate._permits`)
    can no longer withhold a path that no longer exists — a permitted note's
    wikilink to it renders in the clear. It also removes governed material with
    no `governed_delete` audit record.
    """
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/restricted-delete.md"
    (vault / rel).write_text(
        "---\ntype: insight\n---\n# Restricted\nprivate body\n", encoding="utf-8"
    )
    _write_restriction_shape(vault, "Notes/Insights/restricted-delete.md", shape=shape)

    delete_file.delete_file(vault, path=rel, confirm=True)

    tombstones = list(
        (vault / "Knowledge Base" / "_Governance" / "deletion-tombstones").glob("*.json")
    )
    assert len(tombstones) == 1
    assert lifecycle.is_tombstoned(vault, rel) is True
    assert any(item["event_type"] == "deletion" for item in _receipt_records(vault))


@_RESTRICTION_SHAPES
def test_a_tombstoned_path_stays_withheld_after_the_policy_is_removed(
    vault: Path, shape: str
) -> None:
    """The tombstone is what survives the item. Without it the path is
    published the moment the governance tree stops matching it."""
    rel = "Knowledge Base/Notes/Insights/restricted-stale.md"
    body = "---\ntype: insight\n---\n# Restricted stale\n"
    (vault / rel).write_text(body, encoding="utf-8")
    _write_restriction_shape(vault, "Notes/Insights/restricted-stale.md", shape=shape)
    delete_file.delete_file(vault, path=rel, confirm=True)
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    if (governance / "rules").exists():
        shutil.rmtree(governance / "rules")
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()

    assert egress.annotate_page(vault, {"path": rel, "body": body}) is None
    assert egress.release_level_for(vault, rel) is None


@_RESTRICTION_SHAPES
def test_restoring_a_restricted_item_is_governed_however_the_restriction_is_written(
    vault: Path, shape: str
) -> None:
    """`_is_governed_for_restore` decides on its own whenever the deletion left
    no committed lineage — here the item was deleted before the policy existed,
    so the restore is the first governed step and must be receipted."""
    rel = "Knowledge Base/Notes/Insights/restricted-restore.md"
    (vault / rel).write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Restore me\n", encoding="utf-8"
    )
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)
    assert not any(item["event_type"] == "deletion" for item in _receipt_records(vault))
    _write_restriction_shape(vault, "Notes/Insights/restricted-restore.md", shape=shape)

    recover_from_trash.recover_from_trash(vault, trash_path=deleted.trash_path)

    assert any(item["event_type"] == "recovery" for item in _receipt_records(vault))


def test_tombstone_suppresses_stale_page_before_empty_policy_and_not_same_hash_sibling(
    vault: Path,
) -> None:
    doomed_rel = "Knowledge Base/Notes/Insights/doomed.md"
    sibling_rel = "Knowledge Base/Notes/Insights/sibling.md"
    body = "---\ntype: insight\n---\n# Identical\n"
    (vault / doomed_rel).write_text(body, encoding="utf-8")
    (vault / sibling_rel).write_text(body, encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/doomed.md")
    delete_file.delete_file(vault, path=doomed_rel, confirm=True)
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    shutil.rmtree(governance / "rules")
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()

    assert egress.annotate_page(vault, {"path": doomed_rel, "body": body}) is None
    assert egress.annotate_page(vault, {"path": sibling_rel, "body": body}) == {
        "path": sibling_rel,
        "body": body,
    }
    assert egress.release_level_for(vault, doomed_rel) is None


def test_recovery_uses_committed_deletion_lineage_after_policy_disappears(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/recover-lineage.md"
    target = vault / rel
    target.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Restore me\n",
        encoding="utf-8",
    )
    _write_restricting_policy(vault, "Notes/Insights/recover-lineage.md")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    shutil.rmtree(governance / "rules")
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()

    recovered = recover_from_trash.recover_from_trash(
        vault, trash_path=deleted.trash_path
    )

    assert recovered.restored_path == rel
    assert target.exists()
    phases = [(item["event_type"], item["phase"]) for item in _receipt_records(vault)]
    recovery_index = phases.index(("recovery", "recorded"))
    recovery_terminal = max(
        index for index, item in enumerate(phases) if item == ("critical", "committed")
    )
    assert recovery_index < recovery_terminal
    assert not list(
        (governance / "deletion-tombstones").glob("*.json")
    )


def test_governed_directory_over_receipt_bound_refuses_before_any_evidence_or_move(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Scratch/too-many"
    root = vault / rel
    root.mkdir(parents=True)
    for index in range(receipts.MAX_OUTCOMES + 1):
        (root / f"{index:03}.md").write_text(f"# {index}\n", encoding="utf-8")
    _write_restricting_policy(vault, "Scratch/too-many/**")

    with pytest.raises(delete_directory.DeleteDirectoryError) as error:
        delete_directory.delete_directory(
            vault, path=rel, confirm=True, recursive=True, force_orphan=True
        )

    assert error.value.code == "GOVERNED_BATCH_LIMIT"
    assert root.exists()
    assert _receipt_records(vault) == []
    assert not (
        vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    ).exists()


@pytest.mark.parametrize(
    "rel,kind",
    [
        ("Knowledge Base", "directory"),
        ("Knowledge Base/_Governance", "directory"),
        ("Knowledge Base/_Governance/events", "directory"),
        ("Knowledge Base/_Governance/deletion-tombstones/pending.json", "file"),
    ],
)
def test_lifecycle_refuses_operational_state_and_every_ancestor(
    vault: Path, rel: str, kind: str
) -> None:
    target = vault / rel
    if kind == "file":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")

        def invoke() -> object:
            return delete_file.delete_file(vault, path=rel, confirm=True)

        error_type = delete_file.DeleteFileError
    else:
        target.mkdir(parents=True, exist_ok=True)

        def invoke() -> object:
            return delete_directory.delete_directory(
                vault,
                path=rel,
                confirm=True,
                recursive=True,
                force_orphan=True,
                allow_curated=True,
            )

        error_type = delete_directory.DeleteDirectoryError

    with pytest.raises(error_type) as error:
        invoke()

    assert error.value.code == "GOVERNANCE_STATE_PROTECTED"
    assert target.exists()


def test_recovery_terminal_crash_is_finalized_from_marker_without_replaying_move(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/recovery-crash.md"
    target = vault / rel
    target.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Restore once\n",
        encoding="utf-8",
    )
    _write_restricting_policy(vault, "Notes/Insights/recovery-crash.md")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)

    def crash(point: str) -> None:
        if point == "recovery_terminal":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(lifecycle, "_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        recover_from_trash.recover_from_trash(vault, trash_path=deleted.trash_path)
    assert target.exists()
    before = _semantic_snapshot(vault)

    preview = lifecycle.reconcile(vault, dry_run=True)

    assert _semantic_snapshot(vault) == before
    assert preview["repairs"] == [{"kind": "recovery_finalize", "event_id": preview["repairs"][0]["event_id"]}]
    monkeypatch.setattr(lifecycle, "_checkpoint", lambda _point: None)
    applied = lifecycle.reconcile(vault, dry_run=False)
    assert applied["repairs"] == preview["repairs"]
    assert target.exists()
    assert not list(
        (vault / "Knowledge Base" / "_Governance" / "deletion-tombstones").glob("*.json")
    )


def test_direct_residue_probe_blocks_terminal_even_when_fanout_claims_success(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import index_sync
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/stale-row.md"
    target = vault / rel
    target.write_text("# Indexed secret\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/stale-row.md")
    lexical = vault / "Knowledge Base" / ".lexical.sqlite"
    conn = sqlite3.connect(lexical)
    conn.execute("CREATE TABLE pages(path TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE semantic_units(parent_path TEXT)")
    conn.execute("INSERT INTO pages(path) VALUES (?)", (rel,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        index_sync,
        "delete_after_remove",
        lambda _vault, paths: index_sync.observed_delete_report(paths, degraded=False),
    )

    result = delete_file.delete_file(vault, path=rel, confirm=True)

    assert any("remains tombstoned" in warning for warning in result.warnings)
    records = _receipt_records(vault)
    assert not any(item["event_type"] == "deletion" for item in records)
    assert not any(item["phase"] == "committed" for item in records)
    assert egress.annotate_page(vault, {"path": rel, "body": "stale"}) is None
    conn = sqlite3.connect(lexical)
    conn.execute("DELETE FROM pages WHERE path = ?", (rel,))
    conn.commit()
    conn.close()

    preview = lifecycle.reconcile(vault, dry_run=True)
    assert preview["repairs"][0]["kind"] == "deletion_commit"
    lifecycle.reconcile(vault, dry_run=False)
    phases = [(item["event_type"], item["phase"]) for item in _receipt_records(vault)]
    assert phases.index(("deletion", "recorded")) < phases.index(("critical", "committed"))


@pytest.mark.parametrize(
    ("component", "sidecar", "schema", "insert"),
    [
        (
            "lexical",
            ".lexical.sqlite",
            "CREATE TABLE pages(path TEXT); CREATE TABLE semantic_units(parent_path TEXT)",
            "INSERT INTO pages(path) VALUES (?)",
        ),
        (
            "semantic_units",
            ".lexical.sqlite",
            "CREATE TABLE pages(path TEXT); CREATE TABLE semantic_units(parent_path TEXT)",
            "INSERT INTO semantic_units(parent_path) VALUES (?)",
        ),
        (
            "refs",
            ".refs.sqlite",
            "CREATE TABLE identities(path TEXT)",
            "INSERT INTO identities(path) VALUES (?)",
        ),
        (
            "graph",
            ".graph.sqlite",
            "CREATE TABLE graph_nodes(path TEXT); CREATE TABLE graph_parent_refs(path TEXT); "
            "CREATE TABLE graph_edges(source_path TEXT)",
            "INSERT INTO graph_nodes(path) VALUES (?)",
        ),
        (
            "embeddings",
            ".embeddings.sqlite",
            "CREATE TABLE chunks(file_path TEXT); CREATE TABLE semantic_unit_vectors(parent_path TEXT)",
            "INSERT INTO chunks(file_path) VALUES (?)",
        ),
        (
            "clip",
            ".clip.sqlite",
            "CREATE TABLE images(file_path TEXT)",
            "INSERT INTO images(file_path) VALUES (?)",
        ),
    ],
)
def test_direct_residue_probes_each_low_level_sidecar(
    vault: Path,
    component: str,
    sidecar: str,
    schema: str,
    insert: str,
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/probe.md"
    item = lifecycle.ManifestItem(rel, "Knowledge Base/_trash/probe.md", "a" * 64, 1, "file", f"sha256:{'a' * 64}")
    conn = sqlite3.connect(vault / "Knowledge Base" / sidecar)
    conn.executescript(schema)
    conn.execute(insert, (rel,))
    conn.commit()
    conn.close()

    assert lifecycle.direct_residue(vault, (item,))[component] is True


def test_direct_residue_probes_scene_frame_files(vault: Path) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Evidence/Video/probe.mp4"
    frame_dir = vault / f"{rel}.frames"
    frame_dir.mkdir(parents=True)
    (frame_dir / "scene-001-t0ms.jpg").write_bytes(b"frame")
    item = lifecycle.ManifestItem(rel, "Knowledge Base/_trash/probe.mp4", "a" * 64, 1, "file", f"sha256:{'a' * 64}")

    assert lifecycle.direct_residue(vault, (item,))["scene"] is True


def test_atomic_delete_refuses_cross_device_before_moving_content(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/cross-device.md"
    target = vault / rel
    target.write_text("# Same device only\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/cross-device.md")
    devices = iter((10, 11))
    monkeypatch.setattr(lifecycle, "_device", lambda _path: next(devices))

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(vault, path=rel, confirm=True)

    assert error.value.code == "CROSS_DEVICE_MOVE"
    assert target.exists()
    assert not list(
        (vault / "Knowledge Base" / "_Governance" / "deletion-tombstones").glob("*.json")
    )
    assert [(item["event_type"], item["phase"]) for item in _receipt_records(vault)] == [
        ("critical", "intent"),
        ("critical", "aborted"),
    ]


def test_directory_census_drift_after_tombstone_refuses_atomic_move(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Scratch/census-drift"
    root = vault / rel
    root.mkdir(parents=True)
    (root / "captured.md").write_text("# captured\n", encoding="utf-8")
    _write_restricting_policy(vault, "Scratch/census-drift/**")

    def drift(point: str) -> None:
        if point == "deletion_tombstone":
            (root / "late.md").write_text("# late\n", encoding="utf-8")

    monkeypatch.setattr(lifecycle, "_checkpoint", drift)
    with pytest.raises(delete_directory.DeleteDirectoryError) as error:
        delete_directory.delete_directory(
            vault,
            path=rel,
            confirm=True,
            recursive=True,
            force_orphan=True,
        )

    assert error.value.code == "LIFECYCLE_CENSUS_DRIFT"
    assert (root / "captured.md").exists()
    assert (root / "late.md").exists()


def test_tombstone_gates_registered_and_explicit_routes_without_policy(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/all-routes.md"
    target = vault / rel
    target.write_text("# stale derivative\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/all-routes.md")
    delete_file.delete_file(vault, path=rel, confirm=True)
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    shutil.rmtree(governance / "rules")
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()

    hit = SimpleNamespace(path=rel, parent_path="", graph_provenance=None)
    assert egress.annotate_hits(vault, [hit]).hits == []
    graph = {"seeds": [{"path": rel}], "nodes": [{"path": rel}], "edges": []}
    assert egress.guard_graph_context(vault, graph)["nodes"] == []
    assert egress.annotate_dataset(vault, {"path": rel}, representation="rows") is None
    assert egress.filter_withheld_entries(vault, [{"path": rel}]) == []
    assert egress.release_allows_download(vault, rel) is False
    assert egress.release_allows_frames(vault, rel) is False
    assert egress.redact_withheld_references(vault, f"read {rel}") == "read [withheld]"


def test_selector_tombstone_coverage_mutation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ("maintain_memory", "mode")
    monkeypatch.delitem(egress._SELECTOR_TOMBSTONE_ADAPTERS[key], "audit")

    with pytest.raises(RuntimeError, match="TOMBSTONE_GATE_MISSING"):
        egress.assert_tombstone_coverage()


def test_repeat_delete_recover_delete_cycles_have_distinct_persisted_identity(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/repeat-cycle.md"
    target = vault / rel
    target.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Repeat cycle\n",
        encoding="utf-8",
    )
    _write_restricting_policy(vault, "Notes/Insights/repeat-cycle.md")

    first = delete_file.delete_file(vault, path=rel, confirm=True)
    recover_from_trash.recover_from_trash(vault, trash_path=first.trash_path)
    delete_file.delete_file(vault, path=rel, confirm=True)

    deletion_records = [
        item for item in _receipt_records(vault) if item["event_type"] == "deletion"
    ]
    assert len(deletion_records) == 2
    assert deletion_records[0]["event_id"] != deletion_records[1]["event_id"]
    assert deletion_records[0]["causation_id"] != deletion_records[1]["causation_id"]


def test_governed_delete_retains_tombstone_when_graph_failure_handle_is_returned(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/failed-graph-delete.md"
    target = vault / rel
    target.write_text("---\ntype: insight\nstatus: draft\n---\n# Hold evidence\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/failed-graph-delete.md")
    failed = index_sync.IndexSyncReport(
        "delete",
        (rel,),
        (rel,),
        (index_sync.IndexComponentOutcome("epistemic_graph", "failed", "graph_handle_failed"),),
    )
    monkeypatch.setattr(index_sync, "delete_after_remove", lambda *_args, **_kwargs: failed)

    result = delete_file.delete_file(vault, path=rel, confirm=True)

    assert any("remains tombstoned" in warning for warning in result.warnings)
    markers = list((vault / "Knowledge Base/_Governance/deletion-tombstones").glob("*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text(encoding="utf-8"))["state"] == "pending"
    assert not any(item["event_type"] == "deletion" for item in _receipt_records(vault))
    assert lifecycle.is_tombstoned(vault, rel)


def test_governed_recovery_retains_markers_when_graph_failure_handle_is_returned(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/Insights/failed-graph-recovery.md"
    target = vault / rel
    target.write_text("---\ntype: insight\nstatus: draft\n---\n# Hold recovery evidence\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/failed-graph-recovery.md")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)
    failed = index_sync.IndexSyncReport(
        "upsert",
        (rel,),
        (rel,),
        (index_sync.IndexComponentOutcome("epistemic_graph", "failed", "graph_handle_failed"),),
    )
    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_args, **_kwargs: failed)

    result = recover_from_trash.recover_from_trash(vault, trash_path=deleted.trash_path)

    assert target.exists()
    assert any("remains tombstoned" in warning for warning in result.warnings)
    tombstones = list((vault / "Knowledge Base/_Governance/deletion-tombstones").glob("*.json"))
    recovery = list((vault / "Knowledge Base/_Governance/deletion-tombstones/recovery").glob("*.json"))
    assert len(tombstones) == 1
    assert len(recovery) == 1
    assert not any(item["event_type"] == "recovery" for item in _receipt_records(vault))


def _coerce_legacy_void_index_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the production fan-out's legacy void-callback adaptation."""
    from exomem import claims, deferred_index, epistemic_graph, graph_sync, lexstore, memory_refs

    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="a" * 24,
        paths=(),
        created_paths=(),
        scope="full",
    )
    monkeypatch.setattr(index_sync, "publish_corpus_delta", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lexstore, "delete_after_remove", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lexstore, "upsert_after_write", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(memory_refs, "delete_after_remove", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(memory_refs, "upsert_after_write", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claims, "delete_after_remove", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        deferred_index, "clear_semantic_receipts", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda *_args, **_kwargs: epistemic_graph.GraphDispatchResult(
            "registered", "graph_rebuild_registered", checkpoint
        ),
    )


def test_governed_delete_keeps_tombstone_for_legacy_void_index_callbacks(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/unverified-legacy-delete.md"
    target = vault / rel
    target.write_text("---\ntype: insight\nstatus: draft\n---\n# Hold evidence\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/unverified-legacy-delete.md")
    _coerce_legacy_void_index_callbacks(monkeypatch)

    result = delete_file.delete_file(vault, path=rel, confirm=True)

    assert any("remains tombstoned" in warning for warning in result.warnings)
    assert result.index is not None
    assert any(item["code"] == "accepted_unverified" for item in result.index["components"])
    assert {
        (item["outcome"], item["code"])
        for item in result.index["components"]
    } >= {("registered", "graph_rebuild_registered")}
    assert not any(item["event_type"] == "deletion" for item in _receipt_records(vault))
    assert lifecycle.is_tombstoned(vault, rel)


def test_governed_recovery_keeps_markers_for_legacy_void_index_callbacks(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/Insights/unverified-legacy-recovery.md"
    target = vault / rel
    target.write_text("---\ntype: insight\nstatus: draft\n---\n# Hold recovery\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/unverified-legacy-recovery.md")
    monkeypatch.setattr(
        index_sync,
        "delete_after_remove",
        lambda *_args, **_kwargs: index_sync.IndexSyncReport(
            "delete",
            (rel,),
            (rel,),
            (index_sync.IndexComponentOutcome("epistemic_graph", "registered", "graph_rebuild_registered"),),
        ),
    )
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)
    assert any(item["event_type"] == "deletion" for item in _receipt_records(vault))
    _coerce_legacy_void_index_callbacks(monkeypatch)

    result = recover_from_trash.recover_from_trash(vault, trash_path=deleted.trash_path)

    assert target.exists()
    assert any("remains tombstoned" in warning for warning in result.warnings)
    assert result.index is not None
    assert any(item["code"] == "accepted_unverified" for item in result.index["components"])
    assert {
        (item["outcome"], item["code"])
        for item in result.index["components"]
    } >= {("registered", "graph_rebuild_registered")}
    tombstones = list((vault / "Knowledge Base/_Governance/deletion-tombstones").glob("*.json"))
    recovery = list((vault / "Knowledge Base/_Governance/deletion-tombstones/recovery").glob("*.json"))
    assert len(tombstones) == 1
    assert len(recovery) == 1
    assert not any(item["event_type"] == "recovery" for item in _receipt_records(vault))


def test_index_report_exactness_requires_closed_accepted_codes() -> None:
    from exomem.governance import lifecycle

    assert lifecycle._index_report_is_exact(
        {
            "components": [
                {"outcome": "registered", "code": "graph_rebuild_registered"},
                {"outcome": "accepted", "code": "no_eligible_paths"},
            ],
            "paths_truncated": False,
            "reconcile_required": False,
        }
    )
    assert lifecycle._index_report_is_exact(
        {
            "components": [],
            "derived_work": "not_required",
            "paths_truncated": False,
            "reconcile_required": False,
        }
    )
    assert not lifecycle._index_report_is_exact(
        {
            "components": [{"outcome": "accepted", "code": "accepted_unverified"}],
            "paths_truncated": False,
            "reconcile_required": False,
        }
    )
    assert not lifecycle._index_report_is_exact(
        {
            "components": [{"outcome": "accepted", "code": "opaque_acceptance"}],
            "paths_truncated": False,
            "reconcile_required": False,
        }
    )


def test_same_cycle_retry_reuses_persisted_operation_identity(vault: Path) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/retry-cycle.md"
    (vault / rel).write_text("# Retry same intent\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/retry-cycle.md")
    trash_rel = "Knowledge Base/_trash/2026-07-27/retry-cycle.md"

    first = lifecycle.begin_deletion(vault, source_rel=rel, trash_rel=trash_rel)
    second = lifecycle.begin_deletion(vault, source_rel=rel, trash_rel=trash_rel)

    assert first.event_id == second.event_id
    assert first.operation_nonce == second.operation_nonce
    intents = [item for item in _receipt_records(vault) if item["phase"] == "intent"]
    assert len(intents) == 1


@pytest.mark.parametrize(
    ("crash_point", "expected_terminal"),
    [
        ("deletion_intent", "aborted"),
        ("deletion_tombstone", "aborted"),
        ("deletion_moved", "committed"),
        ("deletion_record", "committed"),
        ("deletion_terminal", "committed"),
    ],
)
def test_deletion_crash_windows_reconcile_once_in_receipt_order(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    expected_terminal: str,
) -> None:
    from exomem.governance import lifecycle

    rel = f"Knowledge Base/Notes/Insights/crash-{crash_point}.md"
    (vault / rel).write_text("# Crash-safe\n", encoding="utf-8")
    _write_restricting_policy(vault, f"Notes/Insights/crash-{crash_point}.md")

    def crash(point: str) -> None:
        if point == crash_point:
            raise RuntimeError("simulated lifecycle crash")

    monkeypatch.setattr(lifecycle, "_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated lifecycle crash"):
        delete_file.delete_file(vault, path=rel, confirm=True)
    monkeypatch.setattr(lifecycle, "_checkpoint", lambda _point: None)

    first = receipts.reconcile(vault, dry_run=False)
    second = receipts.reconcile(vault, dry_run=False)

    records = _receipt_records(vault)
    terminals = [
        item for item in records if item["phase"] in {"committed", "aborted"}
    ]
    assert [item["phase"] for item in terminals] == [expected_terminal]
    lifecycle_records = [item for item in records if item["event_type"] == "deletion"]
    if expected_terminal == "committed":
        assert len(lifecycle_records) == 1
        assert records.index(lifecycle_records[0]) < records.index(terminals[0])
    else:
        assert lifecycle_records == []
    assert second["repairs"] == []
    assert first["repairs"]


@pytest.mark.parametrize(
    ("crash_point", "expected_terminal"),
    [
        ("recovery_intent", "aborted"),
        ("recovery_marker", "aborted"),
        ("recovery_moved", "committed"),
        ("recovery_record", "committed"),
        ("recovery_terminal", "committed"),
    ],
)
def test_recovery_crash_windows_reconcile_without_replaying_restore(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    expected_terminal: str,
) -> None:
    from exomem.governance import lifecycle

    rel = f"Knowledge Base/Notes/Insights/crash-{crash_point}.md"
    target = vault / rel
    target.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Recover crash-safe\n",
        encoding="utf-8",
    )
    _write_restricting_policy(vault, f"Notes/Insights/crash-{crash_point}.md")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)

    def crash(point: str) -> None:
        if point == crash_point:
            raise RuntimeError("simulated recovery crash")

    monkeypatch.setattr(lifecycle, "_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated recovery crash"):
        recover_from_trash.recover_from_trash(vault, trash_path=deleted.trash_path)
    monkeypatch.setattr(lifecycle, "_checkpoint", lambda _point: None)

    if crash_point == "recovery_moved":
        # The semantic move happened but derived rebuild did not. The product
        # reconcile route heals ordinary indexes before receipt classification.
        first = reconcile_module.reconcile(vault, dry_run=False).receipt_reconcile
    else:
        first = receipts.reconcile(vault, dry_run=False)
    placement_after_first = (target.exists(), (vault / deleted.trash_path).exists())
    second = receipts.reconcile(vault, dry_run=False)

    records = _receipt_records(vault)
    recovery_intents = [
        item
        for item in records
        if item.get("operation") == "governed_recovery" and item["phase"] == "intent"
    ]
    assert len(recovery_intents) == 1
    recovery_terminals = [
        item
        for item in records
        if item.get("causation_id") == recovery_intents[0]["event_id"]
        and item["phase"] in {"committed", "aborted"}
    ]
    assert [item["phase"] for item in recovery_terminals] == [expected_terminal]
    recovery_records = [item for item in records if item["event_type"] == "recovery"]
    if expected_terminal == "committed":
        assert placement_after_first == (True, False)
        assert len(recovery_records) == 1
        assert records.index(recovery_records[0]) < records.index(recovery_terminals[0])
    else:
        assert placement_after_first == (False, True)
        assert recovery_records == []
    assert (target.exists(), (vault / deleted.trash_path).exists()) == placement_after_first
    assert second["repairs"] == []
    assert first["repairs"]


def test_governed_multi_item_directory_delete_and_inverse_recovery(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/governed-tree"
    root = vault / rel
    root.mkdir(parents=True)
    for name in ("one.md", "two.md"):
        (root / name).write_text(
            f"---\ntype: insight\nstatus: draft\n---\n# {name}\n",
            encoding="utf-8",
        )
    _write_restricting_policy(vault, "Notes/Insights/governed-tree/**")

    deleted = delete_directory.delete_directory(
        vault, path=rel, confirm=True, recursive=True, force_orphan=True
    )
    deletion = next(
        item for item in _receipt_records(vault) if item["event_type"] == "deletion"
    )
    assert len(deletion["affected_refs"]) == 2
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    shutil.rmtree(governance / "rules")
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()

    recovered = recover_from_trash.recover_from_trash(
        vault, trash_path=deleted.trash_path
    )

    assert recovered.kind == "directory"
    assert (root / "one.md").exists()
    assert (root / "two.md").exists()
    recovery = next(
        item for item in _receipt_records(vault) if item["event_type"] == "recovery"
    )
    assert len(recovery["affected_refs"]) == 2


def test_hypothetical_restore_frontmatter_can_govern_without_deletion_lineage(
    vault: Path,
) -> None:
    rel = "Knowledge Base/Notes/Insights/newly-governed.md"
    target = vault / rel
    target.write_text(
        "---\ntype: insight\nstatus: draft\ntags: [secret]\n---\n# Later governed\n",
        encoding="utf-8",
    )
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)
    governance = vault / "Knowledge Base" / "_Governance"
    scope = governance / "scopes" / "tagged.yaml"
    rule = governance / "rules" / "tagged.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    rule.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f"governance_version: 1\nid: {_LIFECYCLE_SCOPE}\ntags: [secret]\n",
        encoding="utf-8",
    )
    rule.write_text(
        f"governance_version: 1\nid: {_LIFECYCLE_RULE}\n"
        f"scope_ids: [\"{_LIFECYCLE_SCOPE}\"]\naudience: external\nceiling: 4\n",
        encoding="utf-8",
    )
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()

    recover_from_trash.recover_from_trash(vault, trash_path=deleted.trash_path)

    assert any(item["event_type"] == "recovery" for item in _receipt_records(vault))


def test_explicit_l6_only_material_is_an_ungoverned_lifecycle_noop(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/explicit-l6.md"
    target = vault / rel
    target.write_text("# Full disclosure\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/explicit-l6.md")
    rule = vault / "Knowledge Base" / "_Governance" / "rules" / "lifecycle.yaml"
    rule.write_text(rule.read_text(encoding="utf-8").replace("ceiling: 4", "ceiling: 6"), encoding="utf-8")
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()

    delete_file.delete_file(vault, path=rel, confirm=True)

    assert _receipt_records(vault) == []
    assert not (
        vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    ).exists()


def test_blocked_policy_treats_deleted_material_as_governed(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/blocked-policy.md"
    (vault / rel).write_text("# Fail closed\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/blocked-policy.md")
    rule = vault / "Knowledge Base" / "_Governance" / "rules" / "lifecycle.yaml"
    rule.write_text(rule.read_text(encoding="utf-8").replace("ceiling: 4", "ceiling: 9"), encoding="utf-8")
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()

    delete_file.delete_file(vault, path=rel, confirm=True)

    assert any(item["event_type"] == "deletion" for item in _receipt_records(vault))


def test_stable_memory_ref_remains_tombstoned_after_source_disappears(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/stable-ref.md"
    stable_ref = "exomem://memory/12345678-1234-1234-1234-123456789abc"
    (vault / rel).write_text(
        "---\nexomem_id: 12345678-1234-1234-1234-123456789abc\n---\n# Stable\n",
        encoding="utf-8",
    )
    _write_restricting_policy(vault, "Notes/Insights/stable-ref.md")
    delete_file.delete_file(vault, path=rel, confirm=True)
    deletion = next(
        item for item in _receipt_records(vault) if item["event_type"] == "deletion"
    )
    assert deletion["affected_refs"] == [stable_ref]
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    shutil.rmtree(governance / "rules")
    from exomem.governance import policy

    policy._CACHE.clear()

    assert egress.redact_withheld_references(vault, stable_ref) == "[withheld]"


def test_media_recovery_stays_hidden_until_clip_is_rebuilt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    rel = "Knowledge Base/Notes/Insights/recover-image.png"
    target = vault / rel
    target.write_bytes(b"not-a-real-image-but-an-exact-artifact")
    _write_restricting_policy(vault, "Notes/Insights/recover-image.png")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)

    recovered = recover_from_trash.recover_from_trash(
        vault, trash_path=deleted.trash_path
    )

    assert any("remains tombstoned" in warning for warning in recovered.warnings)
    assert not any(item["event_type"] == "recovery" for item in _receipt_records(vault))
    assert egress.release_allows_download(vault, rel) is False


def test_media_recovery_can_activate_when_clip_is_explicitly_disabled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    rel = "Knowledge Base/Notes/Insights/recover-disabled-image.png"
    target = vault / rel
    target.write_bytes(b"exact-disabled-artifact")
    _write_restricting_policy(vault, "Notes/Insights/recover-disabled-image.png")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)

    recovered = recover_from_trash.recover_from_trash(
        vault, trash_path=deleted.trash_path
    )

    assert not any("remains tombstoned" in warning for warning in recovered.warnings)
    assert recovered.index == {
        "components": [],
        "derived_work": "not_required",
        "paths_truncated": False,
        "reconcile_required": False,
    }
    assert any(item["event_type"] == "recovery" for item in _receipt_records(vault))


def test_staged_recovery_tombstones_a_custom_restore_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    original = "Knowledge Base/Notes/Insights/custom-origin.md"
    custom = "Knowledge Base/Notes/Insights/custom-target.md"
    (vault / original).write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Custom restore\n",
        encoding="utf-8",
    )
    _write_restricting_policy(vault, "Notes/Insights/custom-origin.md")
    deleted = delete_file.delete_file(vault, path=original, confirm=True)
    monkeypatch.setattr(lifecycle, "_restored_derivatives_exact", lambda _operation: False)

    recovered = recover_from_trash.recover_from_trash(
        vault,
        trash_path=deleted.trash_path,
        restore_path=custom,
    )

    assert any("remains tombstoned" in warning for warning in recovered.warnings)
    assert lifecycle.is_tombstoned(vault, custom) is True
    assert egress.annotate_page(vault, {"path": custom, "body": "stale"}) is None


def test_directory_target_with_uncaptured_extra_file_is_not_exact(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Scratch/target-census"
    root = vault / rel
    root.mkdir(parents=True)
    (root / "captured.md").write_text("# captured\n", encoding="utf-8")
    _write_restricting_policy(vault, "Scratch/target-census/**")

    def add_uncaptured_target(point: str) -> None:
        if point != "deletion_moved":
            return
        trash_root = vault / "Knowledge Base" / "_trash"
        moved = next(
            path
            for path in trash_root.rglob("*")
            if path.is_dir() and path.name.endswith("target-census")
        )
        (moved / "uncaptured.md").write_text("# uncaptured\n", encoding="utf-8")

    monkeypatch.setattr(lifecycle, "_checkpoint", add_uncaptured_target)
    deleted = delete_directory.delete_directory(
        vault,
        path=rel,
        confirm=True,
        recursive=True,
        force_orphan=True,
    )

    assert any("remains tombstoned" in warning for warning in deleted.warnings)
    assert not any(item["event_type"] == "deletion" for item in _receipt_records(vault))
    assert not any(item["phase"] == "committed" for item in _receipt_records(vault))


def test_scene_enabled_video_recovery_finalizes_after_exact_scene_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    rel = "Knowledge Base/Notes/Insights/recover-video.mp4"
    (vault / rel).write_bytes(b"video-recovery-artifact")
    _write_restricting_policy(vault, "Notes/Insights/recover-video.mp4")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)
    recovered = recover_from_trash.recover_from_trash(
        vault, trash_path=deleted.trash_path
    )
    assert any("remains tombstoned" in warning for warning in recovered.warnings)

    embeddings.ClipIndex(vault).upsert_frames(
        rel,
        [(5.0, np.zeros(embeddings.CLIP_DIM, dtype=np.float32))],
        0.0,
    )
    frame_dir = vault / f"{rel}.frames"
    frame_dir.mkdir(parents=True)
    frame = frame_dir / "scene-000-t5000ms.jpg"
    frame.write_bytes(b"frame")
    frame.with_name(frame.name + ".md").write_text(
        f"---\nparent_media: {rel}\nframe_ts: 5.0\n---\n",
        encoding="utf-8",
    )

    report = lifecycle.reconcile(vault)

    assert report["repairs"][0]["kind"] == "recovery_finalize"
    assert any(item["event_type"] == "recovery" for item in _receipt_records(vault))
    assert lifecycle.is_tombstoned(vault, rel) is False


def test_corrupt_lifecycle_marker_fails_closed_for_stale_egress(vault: Path) -> None:
    from exomem.governance import lifecycle, membership, policy

    rel = "Knowledge Base/Notes/Insights/corrupt-marker.md"
    (vault / rel).write_text("# Corrupt marker secret\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/corrupt-marker.md")
    delete_file.delete_file(vault, path=rel, confirm=True)
    tombstone = next(
        (
            vault
            / "Knowledge Base"
            / "_Governance"
            / "deletion-tombstones"
        ).glob("*.json")
    )
    tombstone.write_text("{not-json\n", encoding="utf-8")
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    shutil.rmtree(governance / "rules")
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    stale = SimpleNamespace(path=rel, parent_path="", graph_provenance=None)

    annotated = egress.annotate_hits(vault, [stale])

    assert annotated.hits == []
    assert annotated.active is True
    assert lifecycle.is_tombstoned(vault, rel) is True


def test_redirected_lifecycle_marker_manifest_fails_closed(vault: Path) -> None:
    from exomem.governance import lifecycle, membership, policy

    rel = "Knowledge Base/Notes/Insights/redirected-marker.md"
    (vault / rel).write_text("# Redirected marker secret\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/redirected-marker.md")
    delete_file.delete_file(vault, path=rel, confirm=True)
    tombstone = next(
        (
            vault
            / "Knowledge Base"
            / "_Governance"
            / "deletion-tombstones"
        ).glob("*.json")
    )
    marker = json.loads(tombstone.read_text(encoding="utf-8"))
    marker["manifest"][0]["source_path"] = (
        "Knowledge Base/Notes/Insights/redirect-target.md"
    )
    tombstone.write_text(json.dumps(marker), encoding="utf-8")
    governance = vault / "Knowledge Base" / "_Governance"
    shutil.rmtree(governance / "scopes")
    shutil.rmtree(governance / "rules")
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    stale = SimpleNamespace(path=rel, parent_path="", graph_provenance=None)

    annotated = egress.annotate_hits(vault, [stale])

    assert annotated.hits == []
    assert annotated.active is True
    assert lifecycle.is_tombstoned(vault, rel) is True


def test_lifecycle_tombstone_root_symlink_is_refused_before_intent_or_move(
    vault: Path, tmp_path: Path
) -> None:
    rel = "Knowledge Base/Notes/Insights/symlink-root.md"
    target = vault / rel
    target.write_text("# Symlink root secret\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/symlink-root.md")
    outside = tmp_path / "outside-tombstones"
    outside.mkdir()
    tombstone_root = (
        vault / "Knowledge Base" / "_Governance" / "deletion-tombstones"
    )
    tombstone_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(delete_file.DeleteFileError) as error:
        delete_file.delete_file(vault, path=rel, confirm=True)

    assert error.value.code == "LIFECYCLE_PATH_UNSAFE"
    assert target.exists()
    assert list(outside.iterdir()) == []
    assert _receipt_records(vault) == []


def test_tombstone_projection_validates_receipt_binding_once_per_snapshot(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/snapshot-cache.md"
    (vault / rel).write_text("# Snapshot cache\n", encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/snapshot-cache.md")
    delete_file.delete_file(vault, path=rel, confirm=True)
    original = receipts.event_records
    calls = 0

    def counted(root: Path) -> list[dict]:
        nonlocal calls
        calls += 1
        return original(root)

    monkeypatch.setattr(receipts, "event_records", counted)
    hits = [
        SimpleNamespace(path=rel, parent_path="", graph_provenance=None)
        for _index in range(10)
    ]

    annotated = egress.annotate_hits(vault, hits)

    assert annotated.hits == []
    assert calls == 1
    assert lifecycle.is_tombstoned(vault, rel) is True
    assert calls == 1


def test_reconcile_refuses_marker_redirected_away_from_original_source(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/reconcile-original.md"
    original = vault / rel
    content = "# Original must remain\n"
    original.write_text(content, encoding="utf-8")
    _write_restricting_policy(vault, "Notes/Insights/reconcile-original.md")

    def crash(point: str) -> None:
        if point == "deletion_tombstone":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(lifecycle, "_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        delete_file.delete_file(vault, path=rel, confirm=True)
    tombstone = next(
        (
            vault
            / "Knowledge Base"
            / "_Governance"
            / "deletion-tombstones"
        ).glob("*.json")
    )
    marker = json.loads(tombstone.read_text(encoding="utf-8"))
    decoy_source = "Knowledge Base/Notes/Insights/reconcile-decoy.md"
    decoy_trash = "Knowledge Base/_trash/reconcile-decoy.md"
    decoy_target = vault / decoy_trash
    decoy_target.parent.mkdir(parents=True, exist_ok=True)
    decoy_target.write_text(content, encoding="utf-8")
    marker["source_root"] = decoy_source
    marker["trash_root"] = decoy_trash
    marker["manifest"][0]["source_path"] = decoy_source
    marker["manifest"][0]["trash_path"] = decoy_trash
    tombstone.write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_checkpoint", lambda _point: None)

    report = lifecycle.reconcile(vault)

    assert report["repairs"] == []
    assert original.exists()
    assert json.loads(tombstone.read_text(encoding="utf-8"))["state"] == "pending"
    records = _receipt_records(vault)
    assert [(item["event_type"], item["phase"]) for item in records] == [
        ("critical", "intent")
    ]


def test_video_deletion_proves_scene_child_sidecar_residue_absent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import scene_frames
    from exomem.governance import lifecycle

    rel = "Knowledge Base/Notes/Insights/scene-residue.mp4"
    target = vault / rel
    target.write_bytes(b"scene-residue-video")
    _write_restricting_policy(vault, "Notes/Insights/scene-residue.mp4")
    frame_dir = scene_frames.frames_dir_for(target)
    frame_dir.mkdir(parents=True)
    frame = frame_dir / "scene-000-t0ms.jpg"
    frame.write_bytes(b"frame")
    sidecar = frame.with_name(frame.name + ".md")
    sidecar.write_text(
        f"---\nparent_media: {rel}\nframe_ts: 0.0\n---\n",
        encoding="utf-8",
    )
    sidecar_rel = sidecar.relative_to(vault).as_posix()
    lexical = sqlite3.connect(vault / "Knowledge Base" / ".lexical.sqlite")
    lexical.executescript(
        "CREATE TABLE pages(path TEXT PRIMARY KEY);"
        "CREATE TABLE semantic_units(parent_path TEXT)"
    )
    lexical.execute("INSERT INTO pages(path) VALUES (?)", (sidecar_rel,))
    lexical.commit()
    lexical.close()

    def clear_without_index_proof(_vault: Path, _video: Path) -> int:
        frame.unlink()
        sidecar.unlink()
        return 2

    monkeypatch.setattr(scene_frames, "clear_scene_frames", clear_without_index_proof)
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)

    assert any("remains tombstoned" in warning for warning in deleted.warnings)
    assert not any(item["event_type"] == "deletion" for item in _receipt_records(vault))
    assert lifecycle.is_tombstoned(vault, sidecar_rel) is True
    assert egress.annotate_page(vault, {"path": sidecar_rel, "body": "stale"}) is None


def test_scene_recovery_is_explicitly_disabled_with_clip(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import lifecycle

    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_VIDEO_SCENE_FRAMES", "1")
    rel = "Knowledge Base/Notes/Insights/recover-video-disabled.mp4"
    (vault / rel).write_bytes(b"disabled-video-recovery")
    _write_restricting_policy(vault, "Notes/Insights/recover-video-disabled.mp4")
    deleted = delete_file.delete_file(vault, path=rel, confirm=True)

    recovered = recover_from_trash.recover_from_trash(
        vault, trash_path=deleted.trash_path
    )

    assert not any("remains tombstoned" in warning for warning in recovered.warnings)
    assert any(item["event_type"] == "recovery" for item in _receipt_records(vault))
    assert lifecycle.is_tombstoned(vault, rel) is False


def test_sync_conflict_evidence_fails_the_append_closed(vault: Path) -> None:
    """Task 2.3 — a Syncthing conflict copy must not fork the append-only chain.

    The policy walk prunes operational state, so a conflict copy inside the
    receipt tree is invisible to the compile-time conflict refusal. The append
    path is the only thing standing between a forked hash chain and the next
    record, so it has to recognise the same filenames.
    """
    first = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    instance_dir = receipts._instance_dir(vault, first["instance_id"])
    conflict = instance_dir / "2026-07.sync-conflict-20260731-120000-ABCDEFG.jsonl"
    conflict.write_text(json.dumps(first) + "\n", encoding="utf-8")

    with pytest.raises(receipts.ReceiptError, match="conflicted receipt evidence"):
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert [record["seq"] for record in receipts.event_records(vault)] == [1]
    assert any(
        item["code"] == "evidence_conflict"
        for item in receipts.verify_chain(vault)["issues"]
    )
