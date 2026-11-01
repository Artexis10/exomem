"""Reconciliation and decisions on accepted writes (design D5): the outcome table, the per-intent bound,
the stable-reference rewrite, and the write-conflict review item an owner decides.

Real applier threads over a real vault copy. Each case names the failure only it catches.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
import time
from pathlib import Path

import pytest
from test_write_intent_accept import _draft_kwargs, _journal, _manager, _remember
from test_write_intent_applier import _command, _no_applier, _seed, _wait_for

from exomem import (
    commands,
    intent_accept,
    intent_applier,
    intent_outcomes,
    writer_lease,
)
from exomem.cli_ops import OpError
from exomem.governance import principal as principal_module
from exomem.governance.principal import owner_principal, request_scope
from exomem.intent_outcomes import COMMITTED, CONFLICTED, REJECTED, WAIT


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    yield
    writer_lease.reset_managers_for_tests()


class _Committed(OpError):
    """An error that proves its write committed, as a recorded committed failure does."""

    committed = True


def _busy(committed: bool | None, cause: str | None = None) -> OpError:
    details = {"status": "retryable", "committed": committed, **({"cause": cause} if cause else {})}
    return OpError("MUTATION_BUSY", "held", details=details)


# One row each: the table maps a raised error to what the applier does. A wrong row loses a write, runs it twice,
# or parks it forever, and each of those looks like a working applier until the case that needs it.
_ROWS = [
    (
        "busy before commit waits, uncounted",
        _busy(False, "boundary_held"),
        "complete",
        (WAIT, "boundary_held", False),
    ),
    (
        "fenced waits for the lease",
        OpError("WRITER_FENCED", "x"),
        "complete",
        (WAIT, "writer_lease", False),
    ),
    (
        "coordinator unavailable waits for the lease",
        OpError("WRITER_COORDINATOR_UNAVAILABLE", "x"),
        "complete",
        (WAIT, "writer_lease", False),
    ),
    (
        "warming counts toward the bound",
        OpError("MUTATION_WARMING", "x"),
        "complete",
        (WAIT, "MUTATION_WARMING", True),
    ),
    (
        "unprepared reference counts toward the bound",
        OpError("REFERENCE_PREPARATION_UNAVAILABLE", "x"),
        "complete",
        (WAIT, "REFERENCE_PREPARATION_UNAVAILABLE", True),
    ),
    (
        "read-only cell counts toward the bound",
        OpError("CLOUD_CELL_READ_ONLY", "x"),
        "complete",
        (WAIT, "CLOUD_CELL_READ_ONLY", True),
    ),
    (
        "acknowledgement pending waits and re-dispatches",
        OpError("MUTATION_ACKNOWLEDGEMENT_PENDING", "x"),
        "complete",
        (WAIT, "acknowledgement_pending", True),
    ),
    (
        "committed uncertain is committed",
        writer_lease._PostCommitOutcomeUncertain(),
        "complete",
        (COMMITTED, None, False),
    ),
    (
        "committed failure is committed",
        _Committed("BATCH_CLEANUP_INCOMPLETE", "x"),
        "complete",
        (COMMITTED, None, False),
    ),
    (
        "stale edit is a changed basis",
        ValueError("STALE_EDIT: changed"),
        "complete",
        (CONFLICTED, "basis_changed", False),
    ),
    (
        "stale vocabulary binding",
        ValueError("STALE_VOCABULARY_BINDING: changed"),
        "complete",
        (CONFLICTED, "vocabulary_changed", False),
    ),
    (
        "reused identity",
        OpError("IDEMPOTENCY_KEY_REUSED", "x"),
        "complete",
        (CONFLICTED, "identity_conflict", False),
    ),
    (
        "unknown outcome",
        OpError("MUTATION_OUTCOME_UNKNOWN", "x"),
        "complete",
        (CONFLICTED, "outcome_unknown", False),
    ),
    (
        "a contract that now blocks, after complete validation",
        ValueError("RESERVED_PATH: x"),
        "complete",
        (CONFLICTED, "contract_changed", False),
    ),
    (
        "the same refusal after deferred validation",
        ValueError("RESERVED_PATH: x"),
        "deferred",
        (REJECTED, None, False),
    ),
    (
        "an error with no code",
        RuntimeError("boom"),
        "complete",
        (CONFLICTED, "apply_failed", False),
    ),
]


@pytest.mark.parametrize(
    ("name", "error", "validation", "expected"), _ROWS, ids=[row[0] for row in _ROWS]
)
def test_each_row_of_the_outcome_table_ends_the_dispatch_as_designed(
    name, error, validation, expected
) -> None:
    row = intent_outcomes.classify_error(error, validation=validation)
    assert (row.kind, row.cause, row.counts) == expected, name
    if name == "committed uncertain is committed":
        assert row.uncertain
    if name == "committed failure is committed":
        assert not row.uncertain


def test_a_committed_dispatch_error_ends_the_intent_committed_and_is_never_dispatched_again(
    vault: Path, monkeypatch
) -> None:
    """A retry of a write that committed duplicates it; the intent must end `committed` with its recorded failure."""
    manager = _manager()
    calls = []

    def leaf(*args, **kwargs):
        calls.append(1)
        raise _Committed("BATCH_CLEANUP_INCOMPLETE", "cleanup incomplete")

    real = intent_applier._command
    monkeypatch.setattr(
        intent_applier, "_command", lambda n, s: dataclasses.replace(real(n, s), leaf=leaf)
    )
    intent = _seed(
        manager,
        vault,
        "remember",
        _draft_kwargs(vault, 60),
        owner_principal(surface="mcp"),
        key="committed-failure",
    )
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(
        _journal(manager, vault), intent.intent_id, ("committed", "conflicted", "rejected")
    )
    assert done.state == "committed"
    assert (
        done.terminal["committed"] is True
        and done.terminal["error"]["code"] == "BATCH_CLEANUP_INCOMPLETE"
    )
    assert calls == [1]


def test_an_intent_whose_reference_never_prepares_is_stalled_after_the_bound_and_the_next_intent_commits(
    vault: Path, monkeypatch
) -> None:
    """Without the bound the first intent holds the line forever; with it, only that intent is parked."""
    monkeypatch.setattr(intent_accept, "PER_INTENT_BOUND_SECONDS", 0.6)
    manager = _manager()
    who = owner_principal(surface="mcp")
    stuck_kwargs = _draft_kwargs(vault, 61)
    real = intent_applier._command

    def command(name, surface):
        found = real(name, surface)

        def leaf(*args, **kwargs):
            if kwargs.get("slug") == "accepted-save-61":
                raise OpError("REFERENCE_PREPARATION_UNAVAILABLE", "the reference never prepares")
            return found.leaf(*args, **kwargs)

        return dataclasses.replace(found, leaf=leaf)

    monkeypatch.setattr(intent_applier, "_command", command)
    stuck = _seed(manager, vault, "remember", stuck_kwargs, who, key="stuck")
    later = _seed(manager, vault, "remember", _draft_kwargs(vault, 62), who, key="later")
    intent_applier.ensure_running(manager, vault)
    journal = _journal(manager, vault)
    parked = _wait_for(journal, stuck.intent_id, ("conflicted",))
    done = _wait_for(journal, later.intent_id, ("committed", "conflicted"))
    assert (
        parked.conflict["cause"] == "stalled"
        and parked.conflict["stalled_on"] == "REFERENCE_PREPARATION_UNAVAILABLE"
    )
    assert done.state == "committed"
    assert len(list(vault.rglob("accepted-save-62*.md"))) == 1
    assert not list(vault.rglob("accepted-save-61*.md"))


def test_a_lost_writer_lease_wait_never_stalls_an_intent(vault: Path, monkeypatch) -> None:
    """A lost lease blocks every intent alike; counting it would conflict the whole line for one replica's outage."""
    monkeypatch.setattr(intent_accept, "PER_INTENT_BOUND_SECONDS", 0.3)
    monkeypatch.setattr(intent_applier, "LEASE_WAIT_SECONDS", 0.05)
    manager = _manager()
    lost = {"on": True}
    real = intent_applier._command

    def command(name, surface):
        found = real(name, surface)

        def leaf(*args, **kwargs):
            if lost["on"]:
                raise OpError("WRITER_COORDINATOR_UNAVAILABLE", "no coordinator")
            return found.leaf(*args, **kwargs)

        return dataclasses.replace(found, leaf=leaf)

    monkeypatch.setattr(intent_applier, "_command", command)
    intent = _seed(
        manager,
        vault,
        "remember",
        _draft_kwargs(vault, 63),
        owner_principal(surface="mcp"),
        key="lease-wait",
    )
    intent_applier.ensure_running(manager, vault)
    journal = _journal(manager, vault)
    deadline = time.monotonic() + 30
    while (
        journal.get(intent.intent_id).wait_cause != "writer_lease" and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    time.sleep(1.0)  # three times the bound: a counted wait would have stalled the intent by now
    waiting = journal.get(intent.intent_id)
    assert waiting.state in ("accepted", "applying") and waiting.wait_cause == "writer_lease"
    lost["on"] = False
    assert _wait_for(journal, intent.intent_id, ("committed", "conflicted")).state == "committed"


# -- a moved or deleted target -----------------------------------------------------------------------


def _new_page(manager, vault: Path, number: int) -> str:
    """A committed page that already holds a typed relation, so a later edit of it needs no relation review."""
    kwargs = {
        "content": (
            f"# Accepted save {number}\n\n## Observations\n\n"
            "- [operating constraint] Keep capture off the client #reliability\n\n"
            "## Relations\n\n"
            "- mitigates [[Knowledge Base/Notes/Failures/cache-stampede-on-cold-start]]\n"
        ),
        "title": f"Accepted save {number}",
        "slug": f"accepted-save-{number}",
        "suggestions": False,
    }
    validation = commands.op_remember(vault, validate_only=True, **kwargs)
    kwargs.update(
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
    )
    done = _remember(manager, vault, kwargs, idempotency_key=f"page-{number}")
    return done["path"]


def _edit_kwargs(path: str, text: str, *, expected_hash: str | None = None) -> dict:
    operation = {
        "kind": "replace_string",
        "old_string": "Keep capture off the client",
        "new_string": text,
    }
    if expected_hash is not None:
        operation["expected_hash"] = expected_hash
    return {"path": path, "why": "reword", "operation": operation}


def _accept_edit(manager, vault: Path, kwargs: dict, key: str, who=None):
    """Record an edit as accept would, with the target's stable reference."""
    who = who or owner_principal(surface="mcp")
    command = _command("edit_memory")
    canonical = writer_lease._canonicalize_command_kwargs(kwargs)
    pages = intent_accept.target_pages(command, canonical)
    intent, created = _journal(manager, vault).accept(
        audience=who.audience_id,
        command="edit_memory",
        arguments=canonical,
        surface="mcp",
        principal=intent_accept.principal_snapshot(who),
        digest=writer_lease._command_digest(command, canonical),
        retry_key=key,
        window_seconds=600.0,
        validation="complete",
        planned_paths=sorted(pages),
        target_hashes=intent_accept.planned_hashes(vault, pages),
        base_guards={
            **(
                {"expected_hash": canonical["operation"]["expected_hash"]}
                if "expected_hash" in canonical["operation"]
                else {}
            ),
            intent_accept.TARGET_REFERENCES: intent_accept.planned_references(vault, pages),
        },
    )
    assert created
    return intent


def test_an_edit_follows_its_page_to_the_new_path_through_the_stable_reference(
    vault: Path, monkeypatch
) -> None:
    """The caller named a path that has since moved; dropping the edit loses a write the reference can still place."""
    manager = _manager()
    path = _new_page(manager, vault, 64)
    moved = path.replace("accepted-save-64", "accepted-save-64-renamed")
    with _no_applier(monkeypatch):
        intent = _accept_edit(
            manager, vault, _edit_kwargs(path, "Keep capture off the request"), "moved-target"
        )
        assert intent.base_guards[intent_accept.TARGET_REFERENCES][path].startswith(
            "exomem://memory/"
        )
        shutil.move(vault / path, vault / moved)
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(
        _journal(manager, vault), intent.intent_id, ("committed", "conflicted", "rejected")
    )
    assert done.state == "committed", (done.conflict, done.terminal)
    assert "Keep capture off the request" in (vault / moved).read_text()
    assert not (vault / path).exists()


def test_an_edit_whose_page_was_deleted_is_a_target_missing_conflict_with_its_arguments_kept(
    vault: Path, monkeypatch
) -> None:
    """A deleted page cannot be edited; the intent must stop for a decision and keep what the caller sent."""
    manager = _manager()
    path = _new_page(manager, vault, 65)
    with _no_applier(monkeypatch):
        intent = _accept_edit(manager, vault, _edit_kwargs(path, "gone"), "deleted-target")
        (vault / path).unlink()
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(
        _journal(manager, vault), intent.intent_id, ("committed", "conflicted", "rejected")
    )
    assert done.state == "conflicted" and done.conflict["cause"] == "target_missing"
    assert done.arguments["path"] == path


# -- the write-conflict review item and its decisions --------------------------------------------------


def _hand_edited_conflict(manager, vault: Path, number: int, monkeypatch, *, key: str):
    """An accepted edit under `expected_hash` whose page a person then edits by hand: the applier must conflict it."""
    path = _new_page(manager, vault, number)
    original = (vault / path).read_bytes()
    with _no_applier(monkeypatch):
        intent = _accept_edit(
            manager,
            vault,
            _edit_kwargs(
                path,
                "Keep capture off the request",
                expected_hash=hashlib.sha256(original).hexdigest(),
            ),
            key,
        )
        (vault / path).write_bytes(original + b"\nA hand edit.\n")
    intent_applier.ensure_running(manager, vault)
    conflicted = _wait_for(
        _journal(manager, vault), intent.intent_id, ("committed", "conflicted", "rejected")
    )
    assert conflicted.state == "conflicted", (conflicted.state, conflicted.terminal)
    return path, conflicted


def test_a_hand_edit_under_an_expected_hash_produces_a_review_item_with_the_decision_evidence(
    vault: Path, monkeypatch
) -> None:
    manager = _manager()
    path, conflicted = _hand_edited_conflict(manager, vault, 66, monkeypatch, key="hand-edit")
    assert conflicted.conflict["cause"] == "basis_changed"
    with request_scope(owner_principal(surface="mcp")):
        shown = commands.op_review_memory(
            vault, mode="item", ref=f"exomem://write/{conflicted.intent_id}"
        )
        attention = commands.op_review_memory(vault, mode="attention")
    assert shown["cause"] == "basis_changed" and shown["state"] == "conflicted"
    target = shown["targets"][path]
    assert target["at_accept"] != target["now"] and target["reference"].startswith(
        "exomem://memory/"
    )
    assert shown["arguments"]["path"] == path
    assert shown["decisions"]["retry-write"] == {
        "allowed": True,
        "needs_rebase": True,
    }
    assert attention["write_conflicts"] == {"count": 1}


def test_another_audience_gets_the_unknown_answer_for_a_conflict_it_does_not_own(
    vault: Path, monkeypatch
) -> None:
    """A different audience must learn nothing: not the cause, the arguments, nor that the reference exists."""
    manager = _manager()
    _, conflicted = _hand_edited_conflict(manager, vault, 67, monkeypatch, key="foreign")
    stranger = principal_module.RequestPrincipal("principal:other", surface="mcp", resolved=True)
    with request_scope(stranger):
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND") as known:
            commands.op_review_memory(
                vault, mode="item", ref=f"exomem://write/{conflicted.intent_id}"
            )
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND") as unknown:
            commands.op_review_memory(vault, mode="item", ref="exomem://write/" + "0" * 32)
        assert commands.op_review_memory(vault, mode="attention")["write_conflicts"] == {"count": 0}
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
            commands.op_triage_memory(
                vault, f"exomem://write/{conflicted.intent_id}", "discard-write"
            )
    assert str(known.value).split(":")[0] == str(unknown.value).split(":")[0]


def test_discard_write_ends_the_intent_discarded_and_deletes_its_arguments(
    vault: Path, monkeypatch
) -> None:
    manager = _manager()
    _, conflicted = _hand_edited_conflict(manager, vault, 68, monkeypatch, key="discarded")
    with request_scope(owner_principal(surface="mcp")):
        answer = commands.op_triage_memory(
            vault,
            f"exomem://write/{conflicted.intent_id}",
            "discard-write",
            why="the hand edit is the truth",
        )
    assert answer["state"] == "discarded"
    ended = _journal(manager, vault).get(conflicted.intent_id)
    assert ended.state == "discarded" and ended.arguments is None
    assert ended.terminal["reason"] == "the hand edit is the truth"
    with request_scope(owner_principal(surface="mcp")):
        assert commands.op_review_memory(vault, mode="attention")["write_conflicts"] == {"count": 0}


def test_retry_write_without_rebase_is_refused_for_a_changed_basis(
    vault: Path, monkeypatch
) -> None:
    """A retry that keeps the stale hash conflicts again; refusing it names the one flag that fixes it."""
    manager = _manager()
    _, conflicted = _hand_edited_conflict(manager, vault, 69, monkeypatch, key="no-rebase")
    with (
        request_scope(owner_principal(surface="mcp")),
        pytest.raises(ValueError, match="rebase=true"),
    ):
        commands.op_triage_memory(vault, f"exomem://write/{conflicted.intent_id}", "retry-write")
    assert _journal(manager, vault).get(conflicted.intent_id).state == "conflicted"


def test_retry_write_with_rebase_commits_on_current_content_under_the_retry_key(
    vault: Path, monkeypatch
) -> None:
    manager = _manager()
    path, conflicted = _hand_edited_conflict(manager, vault, 70, monkeypatch, key="rebased")
    with request_scope(owner_principal(surface="mcp")):
        answer = commands.op_triage_memory(
            vault, f"exomem://write/{conflicted.intent_id}", "retry-write", rebase=True
        )
    retried = answer["retried_as"]
    assert retried["retry_key"] == f"intent:{conflicted.intent_id}:retry:1"
    done = _wait_for(
        _journal(manager, vault), retried["intent_id"], ("committed", "conflicted", "rejected")
    )
    assert done.state == "committed", (done.conflict, done.terminal)
    text = (vault / path).read_text()
    assert "Keep capture off the request" in text and "A hand edit." in text
    old = _journal(manager, vault).get(conflicted.intent_id)
    assert old.state == "discarded" and old.arguments is None


def test_retry_write_is_refused_for_a_revoked_login_and_for_an_unknown_outcome_unless_owner(
    vault: Path, monkeypatch
) -> None:
    """The applier must never run a write with authority its caller no longer holds, nor let a non-owner risk a duplicate."""
    manager = _manager()
    caller = principal_module.RequestPrincipal("principal:caller", surface="mcp", resolved=True)
    journal = _journal(manager, vault)
    cases = {}
    for key, cause in (("revoked", "authorization_revoked"), ("unknown", "outcome_unknown")):
        intent = _seed(
            manager,
            vault,
            "remember",
            _draft_kwargs(vault, 71 if key == "revoked" else 72),
            caller,
            key=key,
        )
        assert journal.begin_apply(intent.intent_id)
        journal.finish(intent.intent_id, "conflicted", conflict={"cause": cause})
        cases[key] = intent.intent_id
    monkeypatch.setattr(intent_applier, "ensure_running", lambda *a, **k: None)
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(ValueError, match="authorization is gone"):
            commands.op_triage_memory(vault, f"exomem://write/{cases['revoked']}", "retry-write")
    with request_scope(caller):
        with pytest.raises(ValueError, match="only the vault owner"):
            commands.op_triage_memory(vault, f"exomem://write/{cases['unknown']}", "retry-write")
    with request_scope(owner_principal(surface="mcp")):
        answer = commands.op_triage_memory(
            vault, f"exomem://write/{cases['unknown']}", "retry-write"
        )
    assert answer["retried_as"]["state"] == "accepted"
    assert journal.get(cases["unknown"]).state == "discarded"
