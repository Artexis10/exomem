"""Tamper-evident governance receipt chain coverage."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from exomem import audit
from exomem import reconcile as reconcile_module
from exomem.governance import receipts, store


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
    intent = receipts.begin_event(vault, operation="delete", prior="present", target="trashed")
    assert receipts.commit_event(vault, intent["event_id"])["phase"] == "committed"
    assert receipts.commit_event(vault, intent["event_id"])["phase"] == "committed"
    with pytest.raises(receipts.ReceiptError):
        receipts.abort_event(vault, intent["event_id"])
    # An unresolved, ambiguous intent remains unresolved without a resolver.
    unresolved = receipts.begin_event(vault, operation="recover", prior="trashed", target="present")
    assert receipts.reconcile(vault, dry_run=True)["unresolved"]
    receipts.reconcile(vault, state_resolver=lambda _: "trashed")
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
