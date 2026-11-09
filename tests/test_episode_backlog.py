"""The `uncompiled_episode` family: a recap with a Decided line that no live page cites.

It ships dark. The tests read what an owner reads: the stored projection a write keeps
current, the served block and attention list, and the dispositions effect block.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_episode_coverage_citation import _cite, _due
from test_episode_memory_command import (
    _audience,
    _episodes,
    _frontmatter,
    _record,
    _withhold_notes_from,
)

from exomem import audit, commands, due_state, find, review_state
from exomem.governance.principal import owner_principal, request_scope

FAMILY = due_state.EPISODE_BACKLOG
FAMILY_REF = f"exomem://review/family/{FAMILY}"


@pytest.fixture(autouse=True)
def _owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


def _rows(vault: Path) -> dict:
    return due_state.load(vault)["categories"][FAMILY]


def _surfaced_row(vault: Path) -> dict:
    """The one stored row, stamped on the first-surfaced ledger as a served block would stamp it."""
    due_state.reconcile(vault)
    ((path, entries),) = _rows(vault).items()
    entry = entries["open"][0]
    review_state.record_surfaced(vault, [(entry["item_id"], entry["fingerprint"], FAMILY)], surface="review")
    return {"path": path, **entry}


def _effect(vault: Path) -> dict:
    return commands.op_review_memory(vault, mode="dispositions")["effect"]["families"][FAMILY]


def test_a_write_keeps_the_stored_rows_as_a_full_rebuild_would_leave_them(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that cites a recap, or revises one, must refresh the live page's row
    without a vault-wide rebuild, and agree with the rebuild: a stale row nags about a settled item."""
    due_state.reconcile(vault)
    monkeypatch.setattr(due_state, "reconcile", lambda *_a, **_k: pytest.fail("a write must not rebuild"))
    lines = ["Buy the brass lamp", "Ship by rail"]

    first = _record(vault, decided=lines)["source"]["path"]
    assert list(_rows(vault)) == [first]

    _cite(vault, first)
    assert _rows(vault) == {}

    _record(vault, decided=[*lines, "Pay by card"], open=["Pick the paint"])
    ((live, entries),) = _rows(vault).items()
    assert live == first == _episodes(vault)[-1].relative_to(vault).as_posix()
    assert entries["open"][0]["due_since"] == _frontmatter(_episodes(vault)[-1])["captured"].date().isoformat()
    assert _rows(vault) == due_state.recompute(vault)["categories"][FAMILY]


def test_the_family_ships_off_is_not_in_attention_and_lists_once_set_to_normal(vault: Path) -> None:
    """A shipped-dark family must stay silent on every carrier until its owner turns it on,
    and `normal` must be able to override the shipped default."""
    _record(vault, decided=["Buy the brass lamp"])
    due_state.reconcile(vault)

    recap = _episodes(vault)[0].relative_to(vault).as_posix()

    def listed(**kwargs) -> list[str]:
        find.clear_cache()
        return [item["path"] for item in commands.op_attention(vault, limit=0, **kwargs)["items"]]

    assert (due_state.served(vault) or {}).get("categories", {}).get(FAMILY) is None
    assert listed(categories=[FAMILY]) == []
    shipped = {row["family"]: row for row in commands.op_review_memory(vault, mode="dispositions")["dispositions"]}
    assert shipped[FAMILY]["disposition"] == "off" and shipped[FAMILY]["origin"] == "default"

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")

    assert listed(categories=[FAMILY]) == [recap]
    assert recap not in listed(), "the default attention union never carries the family"
    assert due_state.served(vault)["categories"][FAMILY] == 1
    assert FAMILY not in {row["family"] for row in commands.op_review_memory(vault, mode="dispositions")["dispositions"]}


def test_a_default_audit_leaves_recaps_out_and_an_explicit_request_lists_them(vault: Path) -> None:
    """After an upgrade every past recap with a Decided line is uncited. A default audit
    that listed each one would tell the agent to compile the whole conversation history."""
    _record(vault, decided=["Buy the brass lamp"])
    recap = _episodes(vault)[0].relative_to(vault).as_posix()
    find.clear_cache()

    assert FAMILY not in {finding.category for finding in audit.audit(vault).findings}
    assert [finding.path for finding in audit.audit(vault, categories=[FAMILY]).findings] == [recap]


@pytest.mark.usefixtures("earlier_layout")
def test_a_restricted_caller_is_not_served_the_stored_rows_and_its_audit_item_ignores_the_withheld_citer(
    vault: Path,
) -> None:
    """The stored row counts lines against the owner's view of the citing pages, so serving it to a
    restricted caller would show a count that moves with a page withheld from it. Its own audit
    already reads as if the page did not exist. Only an episode that an earlier release left as
    several files holds a partly cited recap, so the test lays one out."""
    lines = ["Buy the brass lamp", "Ship by rail"]
    first = _record(vault, decided=lines[:1])["source"]["path"]
    _record(vault, decided=lines)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")
    (twin,) = _due(vault)
    _cite(vault, first, title="Withheld lamp pick")
    _withhold_notes_from(vault, "client-a")
    due_state.reconcile(vault)

    assert due_state.served(vault)["categories"][FAMILY] == 1, "the owner is served the row"
    with request_scope(_audience("client-a")):
        restricted = due_state.served(vault)
        (seen,) = _due(vault)

    assert FAMILY not in ((restricted or {}).get("categories") or {})
    assert (seen.meta["open"], seen.meta["decided"]) == (twin.meta["open"], twin.meta["decided"]) == (2, 2)
    assert (seen.detail, seen.proposed_fix, seen.meta) == (twin.detail, twin.proposed_fix, twin.meta)


def test_a_citation_that_predates_surfacing_does_not_count_as_a_later_action(vault: Path) -> None:
    """A cached review can surface after citation; reconciliation must not invent a later action."""
    path = _record(vault, decided=["Buy the brass lamp"])["source"]["path"]
    due_state.reconcile(vault)
    ((_, entries),) = _rows(vault).items()
    entry = entries["open"][0]

    _cite(vault, path)
    review_state.record_surfaced(vault, [(entry["item_id"], entry["fingerprint"], FAMILY)], surface="review")
    due_state.reconcile(vault)

    assert _effect(vault) == {"surfaced": 1, "dismissed": 0, "snoozed": 0, "cleared": 1, "acted": "unknown", "open": 0}


def test_a_supersession_that_drops_the_open_line_counts_as_cleared(vault: Path) -> None:
    """Dropping a line closes the item without anyone compiling it, so it is not `acted`."""
    _record(vault, decided=["Buy the brass lamp"])
    _surfaced_row(vault)

    _record(vault, decided=[], summary="Dropped the lamp purchase.")
    due_state.reconcile(vault)

    assert _effect(vault) == {"surfaced": 1, "dismissed": 0, "snoozed": 0, "cleared": 1, "acted": "unknown", "open": 0}


def test_a_deleted_recap_counts_as_cleared(vault: Path) -> None:
    _record(vault, decided=["Buy the brass lamp"])
    _surfaced_row(vault)

    _episodes(vault)[0].unlink()
    find.clear_cache()
    due_state.reconcile(vault)

    assert _effect(vault) == {"surfaced": 1, "dismissed": 0, "snoozed": 0, "cleared": 1, "acted": "unknown", "open": 0}
