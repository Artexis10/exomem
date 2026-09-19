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

from exomem import commands, graph_sync, mutation_terminal, writer_lease

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


def test_project_terminal_carries_it_at_compact_and_drops_it_at_legacy() -> None:
    """The terminal-level contract, independent of any one command's fixture."""
    leaf = {
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
                ]
            },
        }
    }
    result = {
        "_terminal": mutation_terminal._TERMINAL_MARKER,
        "version": mutation_terminal._TERMINAL_VERSION,
        "state": "committed",
        "ok": True,
        "warnings_count": 0,
        "leaf_result": leaf,
    }

    compact = mutation_terminal.project_terminal(result, "compact")
    assert compact["entity_candidate"]["identities"][0]["name"] == NAME

    legacy = mutation_terminal.project_terminal(result, "legacy")
    assert "entity_candidate" not in legacy
