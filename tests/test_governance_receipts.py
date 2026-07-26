"""Tamper-evident governance receipt chain coverage."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import audit
from exomem import reconcile as reconcile_module
from exomem.governance import receipts, store

_PRIOR = "a" * 64
_TARGET = "b" * 64

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
        vault, event_type="disclosure", event_id="boundary-1", payload={"outcomes": []}
    )
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipts_head SET observed_seq = 0, observed_hash = ?", ("0" * 64,))
        conn.commit()
    finally:
        conn.close()

    retry = receipts.append_event(
        vault, event_type="disclosure", event_id="boundary-1", payload={"outcomes": []}
    )

    assert retry["hash"] == event["hash"]
    assert receipts.verify_chain(vault)["instances"][event["instance_id"]]["tail_seq"] == 1


def test_reconcile_repairs_ahead_critical_anchor_and_lost_buffered_suffix(vault: Path) -> None:
    critical = receipts.append_event(vault, event_type="deletion", payload={"manifest_digest": "a" * 64}, critical=True)
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
        subprocess.Popen([sys.executable, "-c", script, str(vault)], env={"PYTHONPATH": "src", "EXOMEM_DISABLE_EMBEDDINGS": "1"})
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
        subprocess.Popen([sys.executable, "-c", script, str(root)], env={"PYTHONPATH": "src"})
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
        vault, event_type="deletion", event_id="critical-retry", payload={"manifest_digest": "a" * 64}, critical=True
    )
    conn = store.open_connection(vault)
    try:
        conn.execute("UPDATE receipts_head SET durable_seq=0, durable_hash=?, observed_seq=0, observed_hash=?", ("0" * 64, "0" * 64))
        conn.commit()
    finally:
        conn.close()

    receipts.append_event(
        vault, event_type="deletion", event_id="critical-retry", payload={"manifest_digest": "a" * 64}, critical=True
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
            vault, event_type="deletion", event_id="crash-critical", payload={"manifest_digest": "a" * 64}, critical=True
        )
    monkeypatch.setattr(receipts, "_crash_point", lambda _point: None)

    receipts.append_event(
        vault, event_type="deletion", event_id="crash-critical", payload={"manifest_digest": "a" * 64}, critical=True
    )
    assert receipts.verify_chain(vault)["valid"] is True


@pytest.mark.parametrize("point", ["after_jsonl_write", "after_jsonl_flush", "after_jsonl_fsync", "after_terminal_append"])
def test_critical_write_order_crashes_recover_idempotently(
    vault: Path, monkeypatch, point: str
) -> None:
    event_id = f"crash-{point}"

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
            append(vault, event_type="deletion", event_id=event_id, payload={"manifest_digest": "a" * 64}, critical=True)

        def retry() -> None:
            append(
                vault,
                event_type="deletion",
                event_id=event_id,
                payload={"manifest_digest": "a" * 64},
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
        payload={"manifest_digest": "a" * 64},
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
    receipts.append_event(vault, event_type="deletion", payload={"manifest_digest": "a" * 64}, critical=True)
    tombstone = vault / "Knowledge Base" / "_Governance" / "deletion-tombstones" / "pending.json"
    tombstone.parent.mkdir(parents=True)
    tombstone.write_bytes(b'{"pending":true}\n')
    derivative = vault / "Knowledge Base" / "Notes" / "derived.bin"
    derivative.parent.mkdir(exist_ok=True)
    derivative.write_bytes(b"unchanged")
    files = [
        *sorted((vault / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl")),
        store.sidecar_path(vault),
        tombstone,
        derivative,
    ]
    before = {path: path.read_bytes() for path in files}

    receipts.reconcile(vault, dry_run=True)

    assert {path: path.read_bytes() for path in files} == before


def test_reconcile_repair_is_idempotent(vault: Path) -> None:
    event = receipts.append_event(vault, event_type="deletion", payload={"manifest_digest": "a" * 64}, critical=True)
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
