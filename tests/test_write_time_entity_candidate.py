"""The `entity_candidate` block wired through the real write path.

Nothing here stubs `semantic_writes`, the graph or the mutation terminal:
two real `remember` writes on a real vault, through `writer_lease` exactly as
a client reaches it, are what the `write-time-identity-candidates` capability
promises. `test_capture_sweep_entity_candidate.py` covers the producer's
exclusions and edge cases at the unit level; this file covers the wiring --
that the block actually rides the committed response, with MCP/CLI/REST
parity through `mutation_terminal.project_terminal`, and that `legacy`
detail drops it while `compact` keeps it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import capture_sweep, commands, graph_sync, mutation_terminal, writer_lease

NAME = "Harbour Studio"


@pytest.fixture(autouse=True)
def _relation_anchor(tmp_path: Path) -> None:
    """A pre-existing Entity every note below relates_to, so the semantic
    contract's qualifying-relation requirement is satisfied without the
    relation touching the unresolved identity `entity_candidate` is about."""
    anchor = tmp_path / "Knowledge Base" / "Entities" / "Organizations" / "General Context.md"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(
        "---\ntype: entity\ntitle: General Context\nentity_type: organization\n"
        "status: active\n---\n# General Context\n",
        encoding="utf-8",
    )


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _content(note: str, *, name: str = NAME) -> str:
    return (
        f"{note}\n\n"
        "## Observations\n\n"
        f"- [scheduling] Coordinated with [[{name}]] on the schedule.\n"
        "\n"
        "## Relations\n\n"
        "- relates_to [[General Context]]\n"
    )


def _remember(vault: Path, *, content: str, title: str, slug: str, **kwargs) -> dict:
    result = writer_lease.invoke_command(
        _command("remember"),
        vault,
        content=content,
        title=title,
        slug=slug,
        **kwargs,
    )
    # The graph converges on its own (design D5); a test asserting on the
    # NEXT write's dependency lookup joins the flight this one may have
    # started, exactly the way `graph_sync.await_active_rebuild`'s own
    # docstring names as its purpose ("a test asserting the graph's own
    # outcome"). Never reachable from the write path itself.
    graph_sync.await_active_rebuild(vault, timeout=10)
    return result


def test_the_first_page_carries_no_block(tmp_path: Path) -> None:
    result = _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )

    assert result["status"] == "committed"
    assert "entity_candidate" not in result


def test_the_second_page_carries_the_block(tmp_path: Path) -> None:
    first = _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )
    second = _remember(
        tmp_path,
        content=_content("Second meeting."),
        title="Second meeting",
        slug="second-meeting",
    )

    assert second["status"] == "committed"
    candidate = second["entity_candidate"]
    assert len(candidate["identities"]) == 1
    identity = candidate["identities"][0]
    assert identity["name"] == NAME
    assert identity["routes"] == ["resolve-entity", "create-entity"]
    assert set(identity["pages"]) == {
        first["path"],
        second["path"],
    }
    assert candidate["guidance"] == capture_sweep.ENTITY_CANDIDATE_GUIDANCE


def test_a_third_page_carries_no_block(tmp_path: Path) -> None:
    _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )
    _remember(
        tmp_path,
        content=_content("Second meeting."),
        title="Second meeting",
        slug="second-meeting",
    )
    third = _remember(
        tmp_path, content=_content("Third meeting."), title="Third meeting", slug="third-meeting"
    )

    assert third["status"] == "committed"
    assert "entity_candidate" not in third


def test_legacy_detail_drops_it_and_compact_keeps_it(tmp_path: Path) -> None:
    _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )
    compact = _remember(
        tmp_path,
        content=_content("Second meeting."),
        title="Second meeting",
        slug="second-meeting",
        response_detail="compact",
    )
    assert "entity_candidate" in compact

    legacy = _remember(
        tmp_path,
        content=_content("Third meeting."),
        title="Third meeting",
        slug="third-meeting",
        response_detail="legacy",
    )
    assert "entity_candidate" not in legacy


def test_a_link_to_an_existing_entity_yields_an_edge_not_a_block(tmp_path: Path) -> None:
    entity_path = tmp_path / "Knowledge Base" / "Entities" / "Organizations" / f"{NAME}.md"
    entity_path.parent.mkdir(parents=True, exist_ok=True)
    entity_path.write_text(
        "---\ntype: entity\ntitle: "
        f"{NAME}\nentity_type: organization\nstatus: active\n---\n# {NAME}\n",
        encoding="utf-8",
    )

    first = _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )
    second = _remember(
        tmp_path,
        content=_content("Second meeting."),
        title="Second meeting",
        slug="second-meeting",
    )

    assert first["status"] == "committed"
    assert "entity_candidate" not in first
    assert second["status"] == "committed"
    assert "entity_candidate" not in second


# --------------------------------------------------------------------- task 4.1/4.2


def test_create_entity_closes_the_candidate_and_both_notes_hold_their_edge(
    tmp_path: Path,
) -> None:
    """The proof sequence task 4.1 asks for, end to end on one seeded vault.

    A note links a page-less name (no block); a second note links it (the
    block fires); `create-entity` closes it; both notes' links resolve to
    the new Entity through the graph's OWN incremental maintenance -- this
    test never calls a rebuild, so whatever resolved it is the ordinary
    write path, not a forced reindex. Task 4.2 (context compiler) rides the
    same fixture: the created Entity is a new anchor `activate_context`
    resolves for a turn naming it, and no compiler code changed to make
    that true.
    """
    first = _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )
    second = _remember(
        tmp_path,
        content=_content("Second meeting."),
        title="Second meeting",
        slug="second-meeting",
    )
    candidate = second["entity_candidate"]["identities"][0]
    assert candidate["name"] == NAME
    assert candidate["routes"] == ["resolve-entity", "create-entity"]

    created = _remember_command(
        tmp_path,
        "connect_memory",
        operation="create-entity",
        entity_type="organization",
        name=NAME,
        summary=f"{NAME} is the venue the meetings above were coordinating with.",
    )
    assert created["mutated"] is True
    entity_path = created["path"]

    # No block for a THIRD note linking the same name: the identity resolves
    # now, so it is an edge, never a candidate again.
    third = _remember(
        tmp_path, content=_content("Third meeting."), title="Third meeting", slug="third-meeting"
    )
    assert "entity_candidate" not in third

    # Both original notes' links now resolve to the created Entity page --
    # read fresh from the vault, through the product's own inbound-links
    # surface, with no rebuild call anywhere in this test.
    inbound = commands.op_list_inbound_links(tmp_path, target=entity_path)
    inbound_paths = {row["path"] for row in inbound["inbound"]}
    assert first["path"] in inbound_paths
    assert second["path"] in inbound_paths

    # 4.2: the created Entity is a new anchor, resolved without any compiler
    # code change -- `activate_context` is the existing context-compiler
    # entry point, unmodified by this change.
    packet = commands.op_activate_context(tmp_path, turn=f"Tell me about {NAME}.")
    anchor_paths = {anchor.get("path") for anchor in packet.get("anchors", ())}
    assert entity_path in anchor_paths, packet


def _remember_command(vault: Path, command_name: str, **kwargs) -> dict:
    result = writer_lease.invoke_command(_command(command_name), vault, **kwargs)
    graph_sync.await_active_rebuild(vault, timeout=10)
    return result


# ------------------------------------------------------------------------- 4.3


def test_ask_memory_and_find_are_unaffected_by_a_pending_candidate(
    tmp_path: Path,
) -> None:
    """`ask_memory`/`find` never carry, branch on, or read `entity_candidate`.

    The block is a write-response advisory, produced inside `semantic_writes`
    and projected only by `mutation_terminal`; neither read command imports
    either module's new code (confirmed by grep: `entity_recurrence`,
    `capture_sweep` and the new graph method appear in neither), so a query
    that finds the two notes returns the same hits whether or not the OTHER
    command's response happened to carry a candidate.
    """
    from exomem import find

    first = _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )
    second = _remember(
        tmp_path,
        content=_content("Second meeting."),
        title="Second meeting",
        slug="second-meeting",
    )
    assert "entity_candidate" in second

    after = commands.op_ask_memory(tmp_path, query=NAME, mode="keyword")
    after_hits = find.find(tmp_path, query=NAME, mode="keyword")

    assert isinstance(after, list)
    # Neither read surface exposes the write-time key.
    assert not any("entity_candidate" in row for row in after)
    assert not any(hasattr(hit, "entity_candidate") for hit in after_hits)
    # The query finds both notes -- unaffected by the other command's block.
    ask_paths = {row["path"] for row in after}
    find_paths = {hit.path for hit in after_hits}
    assert first["path"] in ask_paths
    assert second["path"] in ask_paths
    assert first["path"] in find_paths
    assert second["path"] in find_paths


def test_the_crossing_write_stays_inside_the_existing_commit_budget(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The candidate computation is bounded (one keyed graph read, at most
    sixteen already-in-hand page states, one filesystem probe at most -- task
    0.1/2.1) rather than merely fast by luck: the crossing write's own
    `commit_ms` -- the span `note.py` already measures and logs, which wraps
    `semantic_writes.commit_creation` end to end and so includes
    `_entity_candidate_block` beside `_capture_sweep_block` and the others --
    stays the same order of magnitude whether or not this write is the one
    that fires the block.
    """
    import logging
    import re

    caplog.set_level(logging.INFO, logger="exomem.note")

    def _commit_ms() -> float:
        records = [
            record
            for record in caplog.records
            if record.name == "exomem.note" and "note write timings" in record.getMessage()
        ]
        assert len(records) == 1, records
        match = re.search(r"commit_ms=([\d.]+)", records[0].getMessage())
        assert match, records[0].getMessage()
        return float(match.group(1))

    caplog.clear()
    _remember(
        tmp_path, content=_content("First meeting."), title="First meeting", slug="first-meeting"
    )
    no_candidate_ms = _commit_ms()

    caplog.clear()
    second = _remember(
        tmp_path,
        content=_content("Second meeting."),
        title="Second meeting",
        slug="second-meeting",
    )
    assert "entity_candidate" in second
    with_candidate_ms = _commit_ms()

    # A generous sanity ceiling, not a perf benchmark: it exists to catch a
    # gross regression (an accidental vault-wide walk), not to pin a number
    # this sandboxed environment's own variance would make flaky.
    assert with_candidate_ms < 2_000, with_candidate_ms
    assert with_candidate_ms < no_candidate_ms * 20 + 500, (
        with_candidate_ms,
        no_candidate_ms,
    )


def _candidate_leaf(*, guidance: str) -> dict:
    return {
        "creation": {
            "applicability": "full",
            "mutated": True,
            "entity_candidate": {
                "identities": [
                    {
                        "name": NAME,
                        "pages": ["a.md", "b.md"],
                        "near_matches": [],
                        "routes": ["resolve-entity", "create-entity"],
                    }
                ],
                "guidance": guidance,
            },
        }
    }


def _terminal_result(leaf: dict) -> dict:
    return {
        "_terminal": mutation_terminal._TERMINAL_MARKER,
        "version": mutation_terminal._TERMINAL_VERSION,
        "state": "committed",
        "ok": True,
        "warnings_count": 0,
        "leaf_result": leaf,
    }


def test_project_terminal_carries_it_at_compact_and_drops_it_at_legacy() -> None:
    """The terminal-level contract, independent of any one command's fixture."""
    result = _terminal_result(
        _candidate_leaf(guidance=capture_sweep.ENTITY_CANDIDATE_GUIDANCE)
    )

    compact = mutation_terminal.project_terminal(result, "compact")
    assert compact["entity_candidate"]["identities"][0]["name"] == NAME
    assert compact["entity_candidate"]["guidance"] == capture_sweep.ENTITY_CANDIDATE_GUIDANCE

    legacy = mutation_terminal.project_terminal(result, "legacy")
    assert "entity_candidate" not in legacy


def test_project_terminal_drops_the_block_when_the_guidance_does_not_match() -> None:
    """The terminal re-validates the leaf's bytes rather than trusting them
    (same posture as `routes`): a leaf naming anything but the one fixed
    sentence is a malformed advisory, dropped rather than served."""
    result = _terminal_result(_candidate_leaf(guidance="Do whatever you like."))

    compact = mutation_terminal.project_terminal(result, "compact")
    assert "entity_candidate" not in compact


def test_project_terminal_drops_the_block_when_the_guidance_is_missing() -> None:
    leaf = _candidate_leaf(guidance=capture_sweep.ENTITY_CANDIDATE_GUIDANCE)
    del leaf["creation"]["entity_candidate"]["guidance"]
    result = _terminal_result(leaf)

    compact = mutation_terminal.project_terminal(result, "compact")
    assert "entity_candidate" not in compact
