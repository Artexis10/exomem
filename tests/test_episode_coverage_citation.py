"""Compile coverage of a recap is citation: a live page names a recap in `sources:`.

The real chain runs here: `record_episode` writes the recap Source, a compiled note
cites it through the governed writer, source closure writes the back-ref, and the
audit family reads that one signal. The family says nothing about whether a note
states a decided item.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_episode_memory_command import _audience, _episodes, _record, _withhold_notes_from

from exomem import audit as audit_module
from exomem import commands, episode_coverage, find
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture(autouse=True)
def _owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


def _cite(vault: Path, recap_path: str, title: str = "Lamp pick") -> None:
    """Write a live compiled note that cites the recap in its `sources:`."""
    arguments = {
        "content": "## Claim\n\nThe brass lamp was chosen.\n",
        "title": title,
        "note_type": "insight",
        "sources": [recap_path],
    }
    preview = commands.op_remember(vault, validate_only=True, **arguments)
    commands.op_remember(
        vault,
        **arguments,
        draft_id=preview["draft_id"],
        draft_hash=preview["draft_hash"],
        draft_token=preview["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=preview["draft_hash"],
        relation_review_reason="No relation exists in this fixture.",
    )


def _due(vault: Path) -> list:
    find.clear_cache()
    return audit_module.audit(vault, categories=["uncompiled_episode"]).findings


def test_a_cited_revision_keeps_its_lines_covered_when_a_later_revision_supersedes_it(vault: Path) -> None:
    """A supersession that orphaned the citation would re-open every line the page covers."""
    first = _record(vault, decided=["Buy the brass lamp", "Ship by rail"])["source"]["path"]
    assert [(f.meta["decided"], f.meta["open"]) for f in _due(vault)] == [(2, 2)]

    _cite(vault, first)
    assert _due(vault) == []

    _record(vault, decided=["Buy the brass lamp", "Ship by rail"], open=["Pick the paint"])
    assert len(_episodes(vault)) == 2
    assert _due(vault) == []

    _record(vault, decided=["Buy the brass lamp", "Ship by rail", "Pay by card"], open=["Pick the paint"])
    (finding,) = _due(vault)
    assert (finding.meta["decided"], finding.meta["open"]) == (3, 1)
    assert finding.path == _episodes(vault)[-1].relative_to(vault).as_posix()


def test_a_citing_page_the_caller_cannot_see_reads_as_no_citation(vault: Path) -> None:
    """A withheld page must not close an item: the caller's item would then differ from the
    one an uncited recap gives, and the difference would show that a withheld page exists."""
    path = _record(vault, decided=["Buy the brass lamp"])["source"]["path"]
    (uncited,) = _due(vault)
    _cite(vault, path, title="Withheld lamp pick")
    with request_scope(_audience("client-a")):
        assert _due(vault) == [], "the audience sees the citing page until it is withheld"
    _withhold_notes_from(vault, "client-a")

    with request_scope(_audience("client-a")):
        (seen,) = _due(vault)

    assert (seen.detail, seen.proposed_fix, seen.meta) == (uncited.detail, uncited.proposed_fix, uncited.meta)


def test_a_body_that_is_not_a_recap_as_recorded_holds_no_lines() -> None:
    """A hand edit must read as no lines, never as a partial reading that closes a line."""
    assert episode_coverage.decided_lines("# Title\n\n## Capture\n\nSome prose, not a recap.\n") == ()
