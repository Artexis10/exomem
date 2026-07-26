"""Tamper-evident governance receipt chain coverage."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import audit
from exomem import reconcile as reconcile_module
from exomem.governance import receipts, store

_PRIOR = "a" * 64
_TARGET = "b" * 64
_MEMORY_REF = "exomem://memory/12345678-1234-1234-1234-123456789abc"
_OPERATION_ID = "d" * 64
_BOUNDARY_ID = "e" * 32
_RETRY_ID = "f" * 32
_CRASH_ID = "1" * 32


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
    fsynced: list[Path] = []
    original_fsync = receipts.os.fsync

    def record_fsync(fd: int) -> None:
        fsynced.append(Path(f"/proc/self/fd/{fd}").resolve())
        original_fsync(fd)

    monkeypatch.setattr(receipts.os, "fsync", record_fsync)
    receipts.append_event(
        vault,
        event_type="deletion",
        payload=_lifecycle_payload(),
        critical=True,
        timestamp="2026-07-01T12:00:00Z",
    )

    assert {path.name for path in fsynced if path.suffix == ".jsonl"} == {"2026-06.jsonl", "2026-07.jsonl"}


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
    original_fsync = receipts.os.fsync
    original_update = receipts._update_durable_head

    def track_fsync(fd: int) -> None:
        path = Path(f"/proc/self/fd/{fd}").resolve()
        if path.suffix == ".jsonl":
            order.append("file")
        original_fsync(fd)

    def track_directories(*args, **kwargs) -> None:
        order.append("directory")

    def track_sidecar(*args, **kwargs) -> None:
        order.append("sidecar")
        original_update(*args, **kwargs)

    monkeypatch.setattr(receipts.os, "fsync", track_fsync)
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
        assert conn.execute("SELECT durable_seq FROM receipts_head").fetchone()[0] == 0
    finally:
        conn.close()
    assert event["seq"] == 1


def test_final_month_open_permission_error_is_normalized(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event = receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / event["instance_id"]
    name = next(instance_dir.glob("*.jsonl")).name
    original_open = receipts.os.open

    def deny_final_entry(path, *args, **kwargs):
        if Path(path).name == name:
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", deny_final_entry)
    with pytest.raises(receipts.ReceiptError, match="evidence path"):
        with receipts._open_month_fd(instance_dir, name):
            pass


def test_exclusive_month_create_permission_error_is_normalized(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipts.append_event(
        vault, event_type="disclosure", payload={"outcomes": []}, timestamp="2026-06-30T12:00:00Z"
    )
    original_open = receipts.os.open

    def deny_create(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise PermissionError("denied")
        return original_open(path, flags, *args, **kwargs)

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
