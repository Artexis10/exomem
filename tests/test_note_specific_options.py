"""Specific relation options for a generic edge between two notes, end to end (task 2.7).

A write that commits a generic edge between two notes is not stopped. Its receipt offers the
relations whose declared endpoints fit both notes, each with one hash-guarded edit that swaps
the generic predicate in place. Every case goes through the public tools over an initialized
vault; the restricted-caller case calls the same writer in process.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_generic_relation_barrier import _anchored, _body, _create, _shown, _source
from test_governance_egress import _external, write_rule, write_scope
from test_relation_endpoints import _extension, _save_extension

from exomem import commands
from exomem.governance.principal import request_scope

# A core relation declares no note roles, so a vault declares the relations it wants offered.
COMPILED_PAIR = {"subject": ["role:compiled"], "object": ["role:compiled"]}
COMPILED_TO_SOURCE = {"subject": ["role:compiled"], "object": ["role:source"]}
SOURCED_BY = "vault.sourced_by"
TYPED_EDGE_WARNING = "RELATION_AUTHORED_ALL_GENERIC"
DISMISSAL = "intentional: these two notes are only loosely connected."


def _page(vault: Path, path: str) -> str:
    return (vault / path).read_text(encoding="utf-8")


def _relation_bullets(vault: Path, path: str) -> list[str]:
    section = _page(vault, path).split("## Relations", 1)[1]
    return [line for line in section.splitlines() if line.startswith("- ")]


def _offered(result: dict) -> list[dict]:
    return result.get("relation_advisory", {}).get("specific_options", [])


def _follow(call, option: dict) -> dict:
    route = option["route"]
    return call(route["tool"], route["args"])


def _declare(call, *keys: str, parent: str = "refines", endpoints: dict = COMPILED_PAIR) -> None:
    """Register relations that join the notes `endpoints` names."""
    for key in keys:
        saved = _save_extension(call, key, _extension(parent, endpoints))
        assert saved["state"] == "committed", saved


def _declare_sourced_by(call) -> None:
    _declare(call, SOURCED_BY, parent="evidenced_by", endpoints=COMPILED_TO_SOURCE)


def _linked(call, anchor: str, title: str) -> dict:
    """A note whose only relation is a generic edge to the anchor note."""
    written = _create(call, title, _source(call, title), f"relates_to [[{anchor}]]")
    assert written["state"] == "committed", written
    return written


def _add_generic_edge(call, vault: Path, path: str, target: str) -> dict:
    """Edit a note so that it gains one generic edge, in its `## Relations` section."""
    page = _page(vault, path)
    last_line = page.rstrip("\n").splitlines()[-1]
    heading = "" if "## Relations" in page else "\n\n## Relations\n"
    return call(
        "edit_note",
        {
            "path": path,
            "why": "link the two notes",
            "operation": {
                "kind": "replace_string",
                "old_string": last_line,
                "new_string": f"{last_line}{heading}\n- relates_to [[{target}]]",
            },
        },
    )


def test_a_generic_edge_to_a_source_note_offers_its_fitting_relation_and_the_route_leaves_one_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, _anchor = _anchored(tmp_path, monkeypatch)
    _declare_sourced_by(call)
    cited = _source(call, "Crane").split("#")[0].removesuffix(".md")

    written = _create(call, "Crane", _source(call, "Cargo"), f"relates_to [[{cited}]]")

    assert written["state"] == "committed", written
    [option] = _offered(written)
    assert (option["target"], option["relation"]) == (f"{cited}.md", SOURCED_BY)
    assert _relation_bullets(vault, written["path"]) == [f"- relates_to [[{cited}]]"]

    swapped = _follow(call, option)

    assert swapped["state"] == "committed", swapped
    assert _relation_bullets(vault, written["path"]) == [f"- {SOURCED_BY} [[{cited}]]"]
    assert not _offered(swapped)


def test_an_edit_that_adds_a_generic_edge_offers_the_same_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    _declare_sourced_by(call)
    cited = _source(call, "Crane").split("#")[0].removesuffix(".md")
    cargo = _create(call, "Cargo", _source(call, "Cargo"), f"supports [[{anchor}]]")
    assert cargo["state"] == "committed", cargo

    edited = _add_generic_edge(call, vault, cargo["path"], cited)

    assert edited["state"] == "committed", edited
    [option] = _offered(edited)
    assert option["relation"] == SOURCED_BY
    assert _follow(call, option)["state"] == "committed"
    assert _relation_bullets(vault, cargo["path"]) == [
        f"- supports [[{anchor}]]",
        f"- {SOURCED_BY} [[{cited}]]",
    ]


def test_only_relations_whose_declared_endpoints_fit_both_notes_are_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    cited = _source(call, "Crane").split("#")[0].removesuffix(".md")
    edges = (f"relates_to [[{anchor}]]", f"relates_to [[{cited}]]")

    # No core relation declares note roles, so nothing is offered; the edges still commit and warn.
    bare = _create(call, "Bare", _source(call, "Bare"), *edges)
    assert bare["state"] == "committed", bare
    assert not _offered(bare)
    assert _relation_bullets(vault, bare["path"]) == [f"- {edge}" for edge in edges]
    assert TYPED_EDGE_WARNING in [f["code"] for f in bare["semantic_advisory"]["findings"]]

    _declare(call, "vault.corroborates")
    _declare_sourced_by(call)
    offered = _create(call, "Offered", _source(call, "Offered"), *edges)

    # Each relation is offered for the note pair it declares, and for no other pair.
    assert {(option["target"], option["relation"]) for option in _offered(offered)} == {
        (f"{anchor}.md", "vault.corroborates"),
        (f"{cited}.md", SOURCED_BY),
    }
    assert TYPED_EDGE_WARNING in [f["code"] for f in offered["semantic_advisory"]["findings"]]


def test_at_most_four_options_are_offered_however_many_edges_the_write_adds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call, anchor = _anchored(tmp_path, monkeypatch)
    second = _create(call, "Second", _source(call, "Second"), f"supports [[{anchor}]]")
    _declare(call, *(f"vault.fits_{n}" for n in range(6)))

    written = _create(
        call,
        "Crowded",
        _source(call, "Crowded"),
        f"relates_to [[{anchor}]]",
        f"relates_to [[{second['path'].removesuffix('.md')}]]",
    )

    options = _offered(written)
    assert len(options) == 4
    assert len({option["relation"] for option in options}) == 4


def test_a_swap_that_would_leave_two_identical_edges_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, _anchor = _anchored(tmp_path, monkeypatch)
    _declare_sourced_by(call)
    cited = _source(call, "Crane").split("#")[0].removesuffix(".md")

    written = _create(
        call, "Cargo", _source(call, "Cargo"), f"{SOURCED_BY} [[{cited}]]", f"relates_to [[{cited}]]"
    )

    assert written["state"] == "committed", written
    assert not _offered(written)
    assert len(_relation_bullets(vault, written["path"])) == 2


def test_a_dismissed_pair_is_not_offered_again_whichever_note_carries_the_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    _declare(call, "vault.corroborates")
    first = _linked(call, anchor, "Crane")
    [option] = _offered(first)
    anchor_path = f"{anchor}.md"

    dismissed = call(
        "triage_memory",
        {
            "ref": option["review"],
            "action": "dismiss",
            "expected_fingerprint": option["fingerprint"],
            "why": DISMISSAL,
        },
    )
    assert dismissed.get("state") == "dismissed", dismissed

    # The anchor now links back to the dismissed pair's other note: the same pair, the other way.
    back = _add_generic_edge(call, vault, anchor_path, first["path"].removesuffix(".md"))
    assert back["state"] == "committed", back
    assert not _offered(back)
    assert TYPED_EDGE_WARNING in [f["code"] for f in back["semantic_advisory"]["findings"]]

    # A different pair is still offered.
    assert _offered(_linked(call, anchor, "Hoist"))


def test_a_withheld_note_gives_a_restricted_writer_the_result_an_absent_one_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def written_by_restricted(name: str, *, source_exists: bool, withheld: bool):
        vault, call, _anchor = _anchored(tmp_path / name, monkeypatch)
        _declare_sourced_by(call)
        cited = _source(call, "Crane").split("#")[0] if source_exists else None
        cited = cited or "Knowledge Base/Sources/Sessions/2026-10-31-crane-source.md"
        if withheld:
            write_scope(vault, paths=cited, name="Crane source")
            write_rule(vault, ceiling=0)
        content = _body("Cargo", f"relates_to [[{cited.removesuffix('.md')}]]")
        with request_scope(_external()):
            try:
                result = commands.op_remember(
                    vault,
                    content=content,
                    title="Cargo",
                    note_type="insight",
                    sources=[_source(call, "Cargo")],
                )
            except ValueError as error:
                result = {"refused": str(error)}
        return result, _shown(result, vault)

    visible, visible_shown = written_by_restricted("visible", source_exists=True, withheld=False)
    _withheld, withheld = written_by_restricted("withheld", source_exists=True, withheld=True)
    _absent, absent = written_by_restricted("absent", source_exists=False, withheld=True)

    # The control: a restricted writer who may see the source is offered the relation.
    assert SOURCED_BY in visible_shown, visible
    assert withheld == absent
    assert SOURCED_BY not in withheld
