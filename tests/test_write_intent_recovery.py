"""Crash recovery, the writer lease and the vocabulary binding of the write-intent applier.

Each crash case kills a real applier process at one named point of one apply, then starts a fresh
applier. The exit is `os._exit`, so no `finally` runs and the kernel drops the dead process's locks as a
kill would. Each case names the failure only it catches.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_connector_boundary import PRIVATE, configured_boundary  # noqa: F401 - the fixture
from test_vocabulary_application import _proposed
from test_write_intent_accept import (
    _draft_kwargs,
    _journal,
    _manager,
    _remember,
    held_boundary_in_thread,
)
from test_write_intent_applier import _command, _no_applier, _wait_for
from test_writer_lease import StoreClient

from exomem import intent_applier, mutation_journal, writer_lease
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope
from exomem.lease_coordinator import SQLiteLeaseStore
from exomem.vocabulary_state import VocabularyState
from exomem.writer_lease import LeaseConfig, LeaseManager


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "always")
    yield
    writer_lease.reset_managers_for_tests()


# -- the crash table ---------------------------------------------------------------------------------


def _die() -> None:
    os._exit(9)  # noqa: SLF001 - a kill: no finally, no atexit, locks dropped by the kernel


def _crashing_process(vault_root: str, point: str, kwargs: dict) -> None:
    """A process that accepts or applies one write and dies at `point`. The parent reads what it left."""
    from exomem import intent_accept as accept_module
    from exomem import writer_lease as lease_module

    root = Path(vault_root)
    manager = lease_module.LeaseManager(lease_module.LeaseConfig.from_env())
    if point == "after_accept":
        accept_module.accepted_envelope = lambda *args, **kwargs: _die()
        intent_applier.ensure_running = lambda *args, **kwargs: None
        with request_scope(owner_principal(surface="mcp")):
            manager.invoke(_command("remember"), (root,), kwargs, idempotency_key="crash-row")
        return  # pragma: no cover - the accept never returns
    if point == "before_claim":
        lease_module.invoke_command = lambda *args, **kwargs: _die()
    elif point == "after_claim":
        claim = lease_module.IdempotencyStore._claim_or_inspect

        def after_claim(self, *args, **kwargs):
            result = claim(self, *args, **kwargs)
            if result[0] == "owner":
                _die()
            return result

        lease_module.IdempotencyStore._claim_or_inspect = after_claim
    elif point == "after_canonical_commit":
        lease_module.IdempotencyStore._persist_completed_from_canonical = (
            lambda self, *args, **kwargs: _die()
        )
    elif point == "after_terminal":
        lease_module.IdempotencyStore._after_terminal_persisted = lambda self, *args, **kwargs: (
            _die()
        )
    applier = intent_applier.ensure_running(manager, root, kind=intent_applier.KIND_SERVICE)
    applier._thread.join(120)


def _crash_at(
    vault: Path, monkeypatch: pytest.MonkeyPatch, point: str, number: int
) -> tuple[str, dict]:
    """Accept one write with no applier, let a service applier in another process die at `point`, return its intent and arguments."""
    manager = _manager()
    kwargs = _draft_kwargs(vault, number)
    with _no_applier(monkeypatch):
        accepted = _remember(manager, vault, kwargs, idempotency_key="crash-row")
    assert accepted["state"] == "accepted"
    _run_child(vault, point, kwargs)
    return accepted["intent_id"], kwargs


def _run_child(vault: Path, point: str, kwargs: dict) -> None:
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_crashing_process, args=(str(vault), point, kwargs))
    child.start()
    child.join(120)
    assert child.exitcode == 9, f"the process did not die at {point}: exit {child.exitcode}"


def _recover(vault: Path, intent_id: str):
    """A fresh service applier in this process, as after a restart, and the intent it ends."""
    manager = _manager()
    intent_applier.ensure_running(manager, vault, kind=intent_applier.KIND_SERVICE)
    return _wait_for(
        _journal(manager, vault), intent_id, ("committed", "conflicted", "rejected"), 60
    )


def _pages(vault: Path, number: int) -> list[Path]:
    return list(vault.rglob(f"accepted-save-{number}*.md"))


def test_a_caller_killed_after_its_row_committed_still_gets_one_commit_and_a_resend_finds_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 1. Without the row's durability the write is lost with the caller; without the identity a resend writes twice."""
    kwargs = _draft_kwargs(vault, 60)
    _run_child(vault, "after_accept", kwargs)
    manager = _manager()
    (intent,) = _journal(manager, vault).non_terminal()
    assert intent.state == "accepted" and not _pages(vault, 60)

    done = _recover(vault, intent.intent_id)
    assert done.state == "committed", done.conflict
    assert len(_pages(vault, 60)) == 1
    with _no_applier(monkeypatch):
        assert _remember(manager, vault, kwargs, idempotency_key="crash-row") == done.terminal
    assert len(_pages(vault, 60)) == 1


def test_an_applier_killed_before_it_claimed_the_key_ends_outcome_unknown_and_writes_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 5. The key has no receipt, and a lost receipt cannot prove the leaf never ran, so a re-run could write twice."""
    intent_id, _ = _crash_at(vault, monkeypatch, "before_claim", 61)
    assert _journal(_manager(), vault).get(intent_id).state == "applying"

    done = _recover(vault, intent_id)
    assert (done.state, done.conflict["cause"]) == ("conflicted", "outcome_unknown")
    assert not _pages(vault, 61)
    targets = done.conflict["evidence"]["targets"]
    assert (
        set(targets) == set(done.planned_paths or ())
        and "logged_outcomes" in done.conflict["evidence"]
    )


def test_an_applier_killed_after_the_claim_before_the_commit_ends_outcome_unknown_and_never_reruns_the_leaf(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 2. An abandoned claim carries no evidence; a re-run would duplicate a write that may have landed."""
    intent_id, _ = _crash_at(vault, monkeypatch, "after_claim", 62)

    done = _recover(vault, intent_id)
    assert (done.state, done.conflict["cause"]) == ("conflicted", "outcome_unknown")
    assert not _pages(vault, 62)
    assert done.arguments is not None, "the owner decides from the retained arguments"


def test_an_applier_killed_after_the_canonical_commit_resumes_the_terminal_from_the_receipt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 3. The page is written and the terminal is not; resuming from the receipt, not re-running, keeps one page."""
    intent_id, _ = _crash_at(vault, monkeypatch, "after_canonical_commit", 63)
    assert len(_pages(vault, 63)) == 1

    done = _recover(vault, intent_id)
    assert done.state == "committed", done.conflict
    assert done.terminal["state"] == "committed"
    assert len(_pages(vault, 63)) == 1
    assert _manager().idempotency.claim_state(done.retry_key) == "completed"


def test_an_applier_killed_after_the_terminal_persisted_replays_it_and_the_row_becomes_committed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 4. The receipt already holds the terminal; dispatching again must replay it, not write a second page."""
    intent_id, _ = _crash_at(vault, monkeypatch, "after_terminal", 64)
    assert len(_pages(vault, 64)) == 1
    assert _journal(_manager(), vault).get(intent_id).state == "applying"

    done = _recover(vault, intent_id)
    assert done.state == "committed", done.conflict
    assert len(_pages(vault, 64)) == 1
    assert mutation_journal.outcomes_for_request(done.request_id).count("committed") <= 1


def test_recovery_runs_before_the_queue_so_a_later_intent_never_overtakes_a_crashed_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order is global per vault; applying the next intent first would put a write behind one that may have landed."""
    intent_id, _ = _crash_at(vault, monkeypatch, "after_claim", 65)
    manager = _manager()
    with _no_applier(monkeypatch), request_scope(owner_principal(surface="mcp")):
        later = manager.invoke(
            _command("remember"), (vault,), _draft_kwargs(vault, 66), idempotency_key="later-row"
        )
    journal = _journal(manager, vault)
    intent_applier.ensure_running(manager, vault, kind=intent_applier.KIND_SERVICE)
    first = _wait_for(journal, intent_id, ("conflicted",), 60)
    second = _wait_for(journal, later["intent_id"], ("committed",), 60)
    assert first.updated_at <= second.terminal_at
    assert len(_pages(vault, 66)) == 1


# -- the writer lease --------------------------------------------------------------------------------


def _replica(
    tmp_path: Path, store: SQLiteLeaseStore, replica: str, *, preferred: bool
) -> LeaseManager:
    return LeaseManager(
        LeaseConfig(
            url="https://lease.example",
            vault_id="main",
            replica_id=replica,
            state_dir=tmp_path / replica,
            idle_release_seconds=10.0,
            ttl_seconds=10.0,
            preferred_writer=preferred,
        ),
        client=StoreClient(store, replica),
    )


def test_a_fenced_apply_leaves_the_page_and_commits_once_the_replica_holds_the_lease_again(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica that lost the lease mid-apply must roll back and wait; a commit under a stale token splits the brain.

    The other replica takes the lease at the commit boundary, the first replica's apply is fenced, and the
    intent waits with the cause `writer_lease`. While it waits, idle release keeps the lease.
    """
    clock = [1000.0]
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite", clock=lambda: clock[0])
    replica_a = _replica(tmp_path, store, "a", preferred=False)
    replica_b = _replica(tmp_path, store, "b", preferred=True)
    monkeypatch.setitem(
        writer_lease._MANAGERS, LeaseConfig.from_env(), replica_a
    )  # the applier dispatches through the process manager
    replica_a.ensure_writer()
    token_a = replica_a._fencing_token

    with _no_applier(monkeypatch):
        with request_scope(owner_principal(surface="mcp")):
            accepted = replica_a.invoke(
                _command("remember"),
                (vault,),
                _draft_kwargs(vault, 67),
                idempotency_key="fenced-row",
            )
    assert accepted["state"] == "accepted"

    real = writer_lease.validate_active_write_fence
    fenced = threading.Event()

    def lose_the_lease_first(*args, **kwargs):
        if not fenced.is_set():
            fenced.set()
            clock[0] += 60.0  # replica A's lease runs out and replica B takes it
            replica_b.ensure_writer()
            replica_a._granted_monotonic -= (
                3600.0  # a grant that old is checked with the coordinator, not trusted
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(writer_lease, "validate_active_write_fence", lose_the_lease_first)
    journal = _journal(replica_a, vault)
    intent_applier.ensure_running(replica_a, vault, kind=intent_applier.KIND_SERVICE)
    assert fenced.wait(60)
    deadline = time.monotonic() + 30
    while (
        time.monotonic() < deadline
        and journal.get(accepted["intent_id"]).wait_cause != "writer_lease"
    ):
        time.sleep(0.05)
    waiting = journal.get(accepted["intent_id"])
    assert (waiting.state in ("accepted", "applying"), waiting.wait_cause) == (
        True,
        "writer_lease",
    ), (waiting.state, waiting.conflict, waiting.terminal)
    assert not _pages(vault, 67), "a fenced apply must leave the page unchanged"

    # Replica B lets go and replica A, which holds the queued intent, takes the lease again.
    replica_b.client.release(replica_b._fencing_token)
    done = _wait_for(journal, accepted["intent_id"], ("committed", "conflicted"), 60)
    assert done.state == "committed", done.conflict
    assert len(_pages(vault, 67)) == 1
    assert replica_a._fencing_token != token_a


def test_idle_release_and_reclaim_leave_the_lease_where_it_is_while_intents_wait(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without counting intents an idle replica hands its lease away and its own applier must win it back, so the lease alternates."""
    store = SQLiteLeaseStore(tmp_path / "leases.sqlite")
    replica_a = _replica(tmp_path, store, "a", preferred=False)
    replica_b = _replica(tmp_path, store, "b", preferred=True)
    monkeypatch.setitem(writer_lease._MANAGERS, LeaseConfig.from_env(), replica_a)
    replica_a.ensure_writer()
    token = replica_a._fencing_token
    with _no_applier(monkeypatch):
        with request_scope(owner_principal(surface="mcp")):
            accepted = replica_a.invoke(
                _command("remember"), (vault,), _draft_kwargs(vault, 68), idempotency_key="idle-row"
            )
    replica_a._last_activity_monotonic = time.monotonic() - 3600.0

    for _ in range(3):
        assert replica_a._maybe_idle_release(token) is False
        replica_b._attempt_preferred_reclaim()
    assert replica_a._fencing_token == token
    assert store.status("main")["holder"] == "a"

    intent_applier.ensure_running(replica_a, vault, kind=intent_applier.KIND_SERVICE)
    _wait_for(_journal(replica_a, vault), accepted["intent_id"], ("committed",), 60)
    replica_a._last_activity_monotonic = time.monotonic() - 3600.0
    assert replica_a._maybe_idle_release(replica_a._fencing_token) is True, (
        "an empty queue must let the lease go"
    )


# -- the vocabulary binding --------------------------------------------------------------------------


def _vocabulary_command(calls: list[int]) -> SimpleNamespace:
    """A keyed vocabulary-applying command that the journal may accept; no registry command that takes `vocabulary_ref` is eligible yet."""

    def leaf(vault, **kwargs):
        calls.append(1)
        path = vault / "Knowledge Base/Entities/Organizations/acme-labs.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: entity\nentity_type: organization\ntitle: Acme Labs\n---\nA durable organization identity.\n"
        )
        writer_lease.mark_active_mutation_committed()
        return {"path": str(path.relative_to(vault))}

    return SimpleNamespace(
        name="connect_memory",
        leaf=leaf,
        read_only=False,
        needs_schema=False,
        intent_eligible=True,
        intent_modes=None,
        intent_staged_argument=None,
        path_roles=(),
        params=(),
    )


def _apply_vocabulary(manager, vault: Path, command, item, key: str):
    kwargs = {
        "operation": "create-entity",
        "entity_type": "organization",
        "name": "Acme Labs",
        "summary": "A durable organization identity.",
        "vocabulary_ref": item.ref,
        "vocabulary_fingerprint": item.fingerprint,
    }
    with request_scope(owner_principal(surface="mcp")):
        return manager.invoke(command, (vault,), kwargs, idempotency_key=key)


def test_a_keyed_vocabulary_application_accepted_under_a_held_boundary_commits_once_and_applies_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The applier's dispatch binds the same key a second time; a refusal there would strand the item `applying`."""
    item = _proposed(vault)
    calls: list[int] = []
    command = _vocabulary_command(calls)
    monkeypatch.setattr(intent_applier, "_command", lambda name, surface: command)
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        accepted = _apply_vocabulary(manager, vault, command, item, "vocab-row")
    assert accepted["state"] == "accepted"
    assert VocabularyState(vault).get(item.ref)["state"] == "applying"

    done = _wait_for(
        _journal(manager, vault), accepted["intent_id"], ("committed", "conflicted", "rejected"), 60
    )
    assert done.state == "committed", (done.conflict, done.terminal)
    assert len(calls) == 1
    applied = VocabularyState(vault).get(item.ref)
    assert applied["state"] == "applied" and len(applied["receipts"]) == 1


def test_a_discarded_write_frees_its_vocabulary_application_so_the_item_can_be_applied_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding left `applying` after a discard makes every later application of the item refuse."""
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")  # the second application runs inline
    item = _proposed(vault)
    calls: list[int] = []
    command = _vocabulary_command(calls)
    monkeypatch.setattr(intent_applier, "_command", lambda name, surface: command)
    manager = _manager()
    journal = _journal(manager, vault)
    with _no_applier(monkeypatch):
        with held_boundary_in_thread(vault, manager):
            accepted = _apply_vocabulary(manager, vault, command, item, "discarded-row")
        journal.finish(accepted["intent_id"], "conflicted", conflict={"cause": "basis_changed"})
    assert VocabularyState(vault).get(item.ref)["state"] == "applying"

    assert intent_applier.discard(journal, vault, accepted["intent_id"], "owner decision") is True
    assert journal.get(accepted["intent_id"]).state == "discarded"
    assert VocabularyState(vault).get(item.ref)["state"] == "proposed"

    again = _apply_vocabulary(manager, vault, command, item, "second-row")
    assert again["state"] == "committed" and len(calls) == 1
    assert VocabularyState(vault).get(item.ref)["state"] == "applied"


def test_a_write_rejected_at_apply_frees_its_vocabulary_application(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected write never commits; its binding must not outlive it, or the reviewed choice is stuck."""
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")  # the second application runs inline
    item = _proposed(vault)
    calls: list[int] = []
    command = _vocabulary_command(calls)
    refuse = [True]
    real_leaf = command.leaf

    def leaf(root, **kwargs):
        if refuse[0]:
            raise OpError("INVALID_ARGUMENT", "the command refuses this write")
        return real_leaf(root, **kwargs)

    command.leaf = leaf
    monkeypatch.setattr(intent_applier, "_command", lambda name, surface: command)
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        accepted = _apply_vocabulary(manager, vault, command, item, "rejected-row")
    assert accepted["validation"] == "deferred"
    done = _wait_for(
        _journal(manager, vault), accepted["intent_id"], ("committed", "conflicted", "rejected"), 60
    )
    assert done.state == "rejected", (done.conflict, done.terminal)
    assert VocabularyState(vault).get(item.ref)["state"] == "proposed"

    refuse[0] = False
    assert _apply_vocabulary(manager, vault, command, item, "retried-row")["state"] == "committed"
    assert VocabularyState(vault).get(item.ref)["state"] == "applied"
