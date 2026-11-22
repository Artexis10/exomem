"""Lifecycle namespace proofs for resumable Source receipts."""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import pytest
from test_governance_receipts import _write_restricting_policy

from exomem import request_budget
from exomem.governance import lifecycle, receipts

_NOTE = "Knowledge Base/Notes/Insights/withdrawn.md"


def _checkpoint(seconds: float = 0.25) -> request_budget.ReadCheckpoint:
    return request_budget.ReadCheckpoint(request_budget.RequestBudget(seconds=seconds))


@pytest.fixture
def marker(vault: Path) -> Path:
    (vault / _NOTE).write_text("# Withdrawn note\n", encoding="utf-8")
    _write_restricting_policy(vault, _NOTE.removeprefix("Knowledge Base/"))
    operation = lifecycle.begin_deletion(vault, source_rel=_NOTE, trash_rel="Knowledge Base/_trash/withdrawn.md")
    assert operation.tombstone_path is not None
    return operation.tombstone_path


def _settled(view: lifecycle.ReceiptLifecycle) -> lifecycle.ReceiptLifecycleProof:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        proof = view.snapshot(_checkpoint())
        if proof is not None:
            return proof
        time.sleep(0.01)
    pytest.fail("the marker namespace never finished validation")


def _append_fixtures(path: Path, tail: dict, count: int) -> dict:
    with path.open("ab") as log:
        for _ in range(count):
            tail = {**tail, "event_id": uuid.uuid4().hex, "seq": tail["seq"] + 1, "prev": tail["hash"]}
            tail["hash"] = receipts._record_hash(tail)
            log.write(receipts._canonical_json(tail) + b"\n")
    return tail


def test_receipt_namespace_validates_off_request_then_reuses_a_complete_deny_set(vault, marker, monkeypatch):
    tail = receipts.append_event(vault, event_type="credential_block", payload={"count": 1})
    log = receipts._month_path(vault, tail["instance_id"], tail["timestamp"])
    tail = _append_fixtures(log, tail, 4096)
    incoming = threading.current_thread()
    entered, release = threading.Event(), threading.Event()
    calls = []
    read_events = receipts.event_records

    def held_history(root, *, checkpoint=None):
        calls.append(threading.current_thread())
        assert calls[-1] is not incoming
        entered.set()
        if not release.wait(5):
            raise receipts.ReceiptError("history did not finish")
        rows = read_events(root, checkpoint=checkpoint)
        assert len(rows) >= 4096
        return rows

    monkeypatch.setattr(receipts, "event_records", held_history)
    view = lifecycle.ReceiptLifecycle(vault)
    try:
        assert view.snapshot(_checkpoint()) is None
        assert entered.wait(5)
        for _ in range(4):
            assert view.snapshot(_checkpoint()) is None
        assert len(calls) == 1
    finally:
        release.set()
    proof = _settled(view)
    assert _NOTE in proof.tombstones

    _append_fixtures(log, tail, 1)
    assert view.snapshot(_checkpoint()) is proof
    assert view.current(proof, _checkpoint())
    assert len(calls) == 1
    assert view.snapshot(_checkpoint(0)) is None
    assert not view.current(proof, _checkpoint(0))


def test_receipt_namespace_detects_an_in_place_edit_and_a_replaced_marker(vault, marker):
    view = lifecycle.ReceiptLifecycle(vault)
    proof = _settled(view)
    original = marker.read_bytes()
    initial = marker.stat()
    tampered = original.replace(b'"operation":"deletion"', b'"operation":"withdraw"', 1)
    assert tampered != original and len(tampered) == len(original)
    marker.write_bytes(tampered)
    os.utime(marker, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    assert not view.current(proof, _checkpoint())
    assert view.snapshot(_checkpoint()) is None

    replacement = marker.with_suffix(".replacement")
    replacement.write_bytes(original)
    os.utime(replacement, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    os.replace(replacement, marker)
    assert not view.current(proof, _checkpoint())
    assert _settled(view).tombstones == proof.tombstones


def test_receipt_namespace_retries_unavailable_event_evidence(vault, marker, monkeypatch):
    real_events = receipts.event_records
    unavailable = threading.Event()
    available = threading.Event()

    def interrupted(root, *, checkpoint=None):
        if not available.is_set():
            unavailable.set()
            raise receipts.ReceiptError("evidence cannot be read")
        return real_events(root, checkpoint=checkpoint)

    monkeypatch.setattr(receipts, "event_records", interrupted)
    view = lifecycle.ReceiptLifecycle(vault)
    assert view.snapshot(_checkpoint()) is None
    assert unavailable.wait(5)
    assert view.snapshot(_checkpoint()) is None
    available.set()
    assert _NOTE in _settled(view).tombstones


def test_empty_receipt_namespace_still_requires_current_valid_ancestry(vault):
    governance = vault / "Knowledge Base/_Governance"
    governance.mkdir(exist_ok=True)
    view = lifecycle.ReceiptLifecycle(vault)
    proof = view.snapshot(_checkpoint())
    assert proof is not None and proof.tombstones == frozenset()
    assert view.current(proof, _checkpoint())

    moved = governance.with_name("_Governance.old")
    governance.rename(moved)
    try:
        governance.symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    assert not view.current(proof, _checkpoint())
    assert view.snapshot(_checkpoint()) is None
