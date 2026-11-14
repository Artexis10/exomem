"""Review handle reads cannot block unrelated governed writes or weaken commit guards."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from exomem import (
    agent_refusal,
    commands,
    corpus_aware,
    epistemic_graph,
    find,
    mutation_journal,
    mutation_lock,
    review_ids,
    review_state,
    writer_lease,
)
from exomem.cli_ops import OpError
from exomem.command_surface import Command


@pytest.mark.parametrize("binding", ["queue", "write-advisory", "accept-relation"])
def test_governed_write_completes_while_review_handle_resolution_is_blocked(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding: str,
) -> None:
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer"), mutation_timeout_seconds=0,
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    reading, release, written = threading.Event(), threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def blocked_read(*args, **kwargs):
        reading.set()
        assert release.wait(30)
        return None if binding == "write-advisory" else {"items": []}

    monkeypatch.setattr(commands, "op_review_memory", blocked_read)
    monkeypatch.setattr(review_state, "last_surfaced_fingerprint", blocked_read)
    command = next(c for c in commands.PRODUCT_COMMANDS
                   if c.name == ("connect_memory" if binding == "accept-relation" else "triage_memory"))
    handle = corpus_aware.write_advisory_ref("a" * 24) if binding == "write-advisory" else "missing.version"
    arguments = {"operation": "accept-relation"} if binding == "accept-relation" else {"action": "dismiss"}

    def resolve() -> None:
        try:
            writer_lease.invoke_command(command, vault, item_id=handle, why="handled: resolved", **arguments)
        except OpError as error:
            if error.code != "ITEM_NOT_FOUND":
                failures.append(error)
        except BaseException as error:  # noqa: BLE001 - carry thread failures to the test
            failures.append(error)

    saved = vault / "Knowledge Base/Notes/Insights/concurrent.md"

    def save(root: Path) -> dict:
        saved.write_text("# Concurrent write\n", encoding="utf-8")
        return {"saved": True}

    write = Command("concurrent_write", save, (), frozenset({"cli"}), cli_writes=True)

    def mutate() -> None:
        try:
            writer_lease.invoke_command(write, vault)
            written.set()
        except BaseException as error:  # noqa: BLE001 - carry thread failures to the test
            failures.append(error)

    reader = threading.Thread(target=resolve)
    writer = threading.Thread(target=mutate)
    reader.start()
    try:
        assert reading.wait(10)
        writer.start()
        assert written.wait(10), failures
        assert saved.read_text(encoding="utf-8") == "# Concurrent write\n"
        assert not release.is_set()
    finally:
        release.set()
        reader.join(10)
        if writer.ident is not None:
            writer.join(10)
    assert not reader.is_alive() and not writer.is_alive()
    assert failures == []


def test_stale_review_handle_refuses_with_current_id_before_writer_admission(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"ref": "exomem://review/current", "fingerprint": "a" * 24}
    stale = review_ids.item_id({**row, "fingerprint": "b" * 24})
    journal = tmp_path / "mutations.jsonl"
    monkeypatch.setattr(mutation_journal, "journal_path", lambda: journal)

    def surfaced_queue(*args, **kwargs):
        review_state.record_surfaced(vault, [("current", row["fingerprint"])], surface="review")
        return {"items": [row]}

    monkeypatch.setattr(commands, "op_review_memory", surfaced_queue)

    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "writer"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)

    def no_writer(*args, **kwargs):
        pytest.fail("a stale handle must refuse without acquiring the writer")

    monkeypatch.setattr(manager, "ensure_writer", no_writer)
    monkeypatch.setattr(manager, "mutation_guard", no_writer)
    triage = next(c for c in commands.PRODUCT_COMMANDS if c.name == "triage_memory")
    with pytest.raises(OpError) as caught:
        writer_lease.invoke_command(triage, vault, item_id=stale, action="dismiss", why="handled: resolved")
    assert caught.value.code == "STALE_ITEM"
    assert caught.value.details["current_id"] == review_ids.item_id(row)
    recorded = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert recorded["error_code"] == "STALE_ITEM"
    assert recorded["boundary_wait_ms"] is None
    assert recorded["boundary_hold_ms"] is None


@pytest.mark.parametrize("decision", ["dismiss", "accept"])
def test_changed_relation_after_binding_refuses_before_saving_a_decision(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str,
) -> None:
    notes = vault / "Knowledge Base/Notes/Insights"
    source = notes / "acorn.md"
    source.write_text(
        "---\ntype: insight\nstatus: active\n---\n# Acorn\n\n"
        "Depends on [[Knowledge Base/Notes/Insights/birch]].\n", encoding="utf-8",
    )
    (notes / "birch.md").write_text(
        "---\ntype: insight\nstatus: active\n---\n# Birch\n\nA measured fact.\n", encoding="utf-8",
    )
    find.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    queue = commands.op_review_memory(vault, mode="relation-queue", limit=200, state="all")
    row = next(item for group in queue["groups"] for item in group["items"]
               if item["from"].endswith("acorn.md") and item["to"].endswith("birch.md"))
    monkeypatch.setattr(commands, "op_review_memory", lambda *args, **kwargs: queue)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "writer"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    guard = manager.mutation_guard

    def change_then_guard(*args, **kwargs):
        original = source.read_text(encoding="utf-8")
        # Equivalent YAML leaves the candidate signal unchanged; accept must still guard the page bytes.
        changed = (original.replace("type: insight", "type: 'insight'") if decision == "accept"
                   else original + "\nA changed measurement.\n")
        source.write_text(changed, encoding="utf-8")
        find.clear_cache()
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        return guard(*args, **kwargs)

    monkeypatch.setattr(manager, "mutation_guard", change_then_guard)
    command = next(c for c in commands.PRODUCT_COMMANDS
                   if c.name == ("connect_memory" if decision == "accept" else "triage_memory"))
    arguments = {"operation": "accept-relation"} if decision == "accept" else {"action": "dismiss"}
    before = review_state.ReviewStateStore(vault).load()
    with pytest.raises(ValueError, match="STALE_EDIT" if decision == "accept" else "REVIEW_ITEM_CHANGED"):
        writer_lease.invoke_command(command, vault, item_id=row["id"], why="handled: resolved", **arguments)
    assert review_state.ReviewStateStore(vault).load() == before
    assert "## Relations" not in source.read_text(encoding="utf-8")


def test_completed_triage_replays_original_receipt_after_advisory_fingerprint_changes(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "writer"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    review_id, original, changed = "a" * 24, "b" * 24, "c" * 24
    review_state.record_surfaced(vault, [(review_id, original)], surface="review")
    triage = next(c for c in commands.PRODUCT_COMMANDS if c.name == "triage_memory")
    arguments = {"item_id": corpus_aware.write_advisory_ref(review_id),
                 "action": "dismiss", "why": "handled: resolved"}
    first = writer_lease.invoke_command(triage, vault, idempotency_key="review-decision", **arguments)
    review_state.record_surfaced(vault, [(review_id, changed)], surface="review")

    def no_read(*args, **kwargs):
        pytest.fail("a completed call must replay without binding the handle again")

    monkeypatch.setattr(review_state, "last_surfaced_fingerprint", no_read)
    replay = writer_lease.invoke_command(triage, vault, idempotency_key="review-decision", **arguments)
    assert replay == first
    assert review_state.ReviewStateStore(vault).decision(review_id, changed) is None


@pytest.mark.parametrize("wait_field", ["waited_ms", "wait_ms"])
def test_busy_refusal_keeps_holder_age_and_caller_wait_in_diagnostics(wait_field: str) -> None:
    refusal = {
        "code": "MUTATION_BUSY", "status": "retryable", "committed": False,
        wait_field: 4267.891, "holder": {"operation": "triage_memory", "age_seconds": 120.125},
    }
    reply = agent_refusal.reply(
        refusal, None, command="triage_memory", called={}, parameters=(),
        write=True, full=True, envelope=refusal,
    )
    message = reply["error"]["message"]
    assert message == "Memory was busy with another write, so this was not saved. Retry the same call."
    assert reply["error"]["diagnostics"] == {
        "holder_operation": "triage_memory", "holder_age_seconds": 120.1, "caller_wait_seconds": 4.3,
    }
    assert reply["diagnostics"] == refusal


def test_busy_refusal_omits_unknown_holder_diagnostics() -> None:
    refusal = {"code": "MUTATION_BUSY", "status": "retryable", "wait_ms": 0}
    reply = agent_refusal.reply(
        refusal, None, command="triage_memory", called={}, parameters=(),
        write=True, full=False, envelope=refusal,
    )
    assert reply["error"]["message"] == (
        "Memory was busy with another write, so this was not saved. Retry the same call."
    )
    assert reply["error"]["diagnostics"] == {"caller_wait_seconds": 0.0}


def test_busy_refusal_omits_the_placeholder_for_another_process_holder() -> None:
    # Another process's hold that left no record reads as the lock's placeholder: no operation, no measured age.
    refusal = {
        "code": "MUTATION_BUSY", "status": "retryable", "wait_ms": 2500,
        "holder": {"operation": mutation_lock.UNKNOWN_LABEL, "age_seconds": 0.0, "verified": False},
    }
    reply = agent_refusal.reply(
        refusal, None, command="triage_memory", called={}, parameters=(),
        write=True, full=False, envelope=refusal,
    )
    assert reply["error"]["diagnostics"] == {"caller_wait_seconds": 2.5}
