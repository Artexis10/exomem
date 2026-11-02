"""How a caller observes an accepted write (design D6, D7): the poll, the list, and what reads say about it.

Real journal, real applier threads and a real vault copy. Each case names the failure only it catches.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from test_write_intent_accept import (
    _draft_kwargs,
    _journal,
    _manager,
    _remember,
    held_boundary_in_thread,
)
from test_write_intent_applier import _no_applier
from test_write_intent_decisions import _accept_edit, _edit_kwargs, _new_page

from exomem import commands, intent_disclosure, writer_lease
from exomem.governance import principal as principal_module
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    yield
    writer_lease.reset_managers_for_tests()


def _stranger() -> principal_module.RequestPrincipal:
    return principal_module.RequestPrincipal("principal:other", surface="mcp", resolved=True)


def _ref(intent) -> str:
    return f"exomem://write/{intent.intent_id}"


def test_the_owner_lists_every_open_write_and_another_audience_only_its_own(
    vault: Path, monkeypatch
) -> None:
    """A list that leaks across audiences tells one agent what another is writing."""
    manager = _manager()
    path = _new_page(manager, vault, 80)
    with _no_applier(monkeypatch):
        mine = _accept_edit(manager, vault, _edit_kwargs(path, "owner reword"), "list-owner")
        theirs = _accept_edit(
            manager, vault, _edit_kwargs(path, "other reword"), "list-other", who=_stranger()
        )
    with request_scope(owner_principal(surface="mcp")):
        owner_view = commands.op_review_memory(vault, mode="write-intent")
    with request_scope(_stranger()):
        other_view = commands.op_review_memory(vault, mode="write-intent")
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND") as foreign:
            commands.op_review_memory(vault, mode="write-intent", ref=_ref(mine))
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND") as unknown:
            commands.op_review_memory(vault, mode="write-intent", ref="exomem://write/" + "0" * 32)
    assert [row["ref"] for row in owner_view["writes"]] == [_ref(theirs), _ref(mine)]
    assert [row["ref"] for row in other_view["writes"]] == [_ref(theirs)]
    assert str(foreign.value) == str(unknown.value).replace("0" * 32, mine.intent_id)


def test_a_poll_with_a_wait_returns_the_committed_terminal_when_the_boundary_frees(
    vault: Path,
) -> None:
    """Without the wait an agent polls blind; without the terminal it cannot tell which page was written."""
    manager = _manager()
    with held_boundary_in_thread(vault, manager) as held:
        accepted = _remember(manager, vault, _draft_kwargs(vault, 81), idempotency_key="poll-wait")
        assert accepted["state"] == "accepted"
        threading.Timer(0.5, held.release.set).start()
        with request_scope(owner_principal(surface="mcp")):
            polled = commands.op_review_memory(
                vault, mode="write-intent", ref=accepted["intent_ref"], wait_seconds=30
            )
    assert polled["state"] == "committed"
    assert polled["terminal"]["state"] == "committed"
    assert polled["waited_seconds"] < 25


def test_a_poll_with_no_free_waiter_slot_returns_the_current_state_at_once(
    vault: Path, monkeypatch
) -> None:
    """A poll that parks a worker past the cap would starve the reads the cap protects."""
    manager = _manager()
    path = _new_page(manager, vault, 82)
    with _no_applier(monkeypatch):
        intent = _accept_edit(manager, vault, _edit_kwargs(path, "waiting"), "no-slot")
    monkeypatch.setattr(writer_lease, "_take_capture_waiter_slot", lambda: False)
    started = time.monotonic()
    with request_scope(owner_principal(surface="mcp")):
        polled = commands.op_review_memory(
            vault, mode="write-intent", ref=_ref(intent), wait_seconds=30
        )
    assert time.monotonic() - started < 2
    assert polled["state"] == "accepted" and polled["wait_refused"] == "no_waiter_slot"


def test_a_read_after_accept_returns_the_canonical_bytes_and_names_the_pending_intent(
    vault: Path, monkeypatch
) -> None:
    """An agent that re-reads its page must see the old bytes with a pending marker, never a guess at the new ones."""
    manager = _manager()
    path = _new_page(manager, vault, 83)
    with _no_applier(monkeypatch):
        intent = _accept_edit(manager, vault, _edit_kwargs(path, "NOT YET WRITTEN"), "read-pending")
    with request_scope(owner_principal(surface="mcp")):
        read = commands.op_read_memory(vault, path)
    assert "Keep capture off the client" in read["body"] and "NOT YET WRITTEN" not in read["body"]
    assert read["pending_writes"] == [
        {"ref": _ref(intent), "command": "edit_memory", "state": "accepted"}
    ]
    with request_scope(_stranger()):
        assert intent_disclosure.for_path(vault, path) == []


def test_a_read_still_succeeds_when_the_journal_cannot_be_read(vault: Path, monkeypatch) -> None:
    """A damaged journal must not take reads down with it; the read only loses its pending marker."""
    manager = _manager()
    path = _new_page(manager, vault, 84)
    with _no_applier(monkeypatch):
        _accept_edit(manager, vault, _edit_kwargs(path, "unseen"), "read-damaged")
    journal_file = _journal(manager, vault).path
    for sidecar in journal_file.parent.glob(journal_file.name + "-*"):
        sidecar.unlink()
    journal_file.write_bytes(b"this is not a database" * 64)
    with request_scope(owner_principal(surface="mcp")):
        read = commands.op_read_memory(vault, path)
        browse = commands.op_browse_memory(vault)
    assert read["path"] == path and "pending_writes" not in read
    assert "pending_writes" not in browse


def test_browse_and_bootstrap_count_the_readers_accepted_writes(vault: Path, monkeypatch) -> None:
    """The next agent turn learns of its pending writes only if the status it already reads says so."""
    manager = _manager()
    path = _new_page(manager, vault, 85)
    with _no_applier(monkeypatch):
        intent = _accept_edit(manager, vault, _edit_kwargs(path, "counted"), "status-count")
    with request_scope(owner_principal(surface="mcp")):
        browse = commands.op_browse_memory(vault)
        bootstrap = commands.op_bootstrap(vault)
    with request_scope(_stranger()):
        other = commands.op_browse_memory(vault)
    expected = {"accepted": 1, "conflicted": 0, "refs": [_ref(intent)]}
    assert browse["pending_writes"] == expected
    assert bootstrap["pending_writes"] == expected
    assert "pending_writes" not in other


def test_a_search_after_the_commit_returns_the_new_terms(vault: Path) -> None:
    """Read-your-write after accept rests on the pending overlay; a commit by the applier must reach keyword recall."""
    manager = _manager()
    kwargs = _draft_kwargs(vault, 86)
    with held_boundary_in_thread(vault, manager) as held:
        accepted = _remember(manager, vault, kwargs, idempotency_key="search-after")
        assert accepted["state"] == "accepted"
        held.release.set()
        with request_scope(owner_principal(surface="mcp")):
            polled = commands.op_review_memory(
                vault, mode="write-intent", ref=accepted["intent_ref"], wait_seconds=30
            )
    assert polled["state"] == "committed"
    with request_scope(owner_principal(surface="mcp")):
        found = commands.op_ask_memory(vault, query="Accepted save 86")
    assert "accepted-save-86" in str(found)
