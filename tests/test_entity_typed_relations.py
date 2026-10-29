"""Typed relations authored when an entity is first created.

`markdown-relations` "Entity creation can author typed relations": a caller that
knows what an entity means to another page states the predicate at birth, and
bare `connections` keep their conservative `relates_to` rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands, graph_sync, memory_refs
from exomem.__main__ import main as cli_main

ENTITIES = "Knowledge Base/Entities/Concepts"


def _relations_section(page: Path) -> list[str]:
    body = page.read_text(encoding="utf-8")
    section = body.split("## Relations\n", 1)[1]
    return [line for line in section.splitlines() if line.startswith("- ")]


def _create_target(vault: Path) -> str:
    created = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name="Tidal Ledger",
        summary="A synthetic accounting concept other entities point to.",
    )
    return created["path"].removesuffix(".md")


def test_create_entity_keeps_each_authored_predicate_in_markdown_and_graph(vault: Path) -> None:
    target = _create_target(vault)
    target_ref = memory_refs.ref_from_markdown((vault / f"{target}.md").read_text())

    created = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name="Harbour Reconciliation",
        summary="A synthetic method that checks and belongs to the tidal ledger.",
        relations=[
            {"kind": "supports", "target": target_ref},
            {"kind": "part_of", "target": target},
            {"kind": "supports", "target": target},
        ],
        connections=[target_ref],
    )

    page = vault / created["path"]
    assert _relations_section(page) == [
        f"- supports [[{target}]]",
        f"- part_of [[{target}]]",
        f"- relates_to [[{target}]]",
    ]
    assert graph_sync.drain_active_rebuilds(timeout=60.0)
    graph = commands.op_connect_memory(
        vault, operation="graph-context", path=created["path"], depth=1
    )["graph"]
    assert graph["available"] is True
    outgoing = {
        (edge["relation_type"], edge["dst_key"])
        for edge in graph["edges"]
        if edge["source_path"] == created["path"]
    }
    assert outgoing == {
        ("supports", f"file:{target}.md"),
        ("part_of", f"file:{target}.md"),
        ("relates_to", f"file:{target}.md"),
    }


@pytest.mark.parametrize(
    "relation",
    [
        pytest.param({"kind": "supports"}, id="missing-target"),
        pytest.param(
            {"kind": "supports [[Elsewhere]]\n- cites", "target": f"{ENTITIES}/Tidal Ledger"},
            id="kind-adds-a-line",
        ),
    ],
)
def test_malformed_relation_refuses_before_writing(vault: Path, relation: dict) -> None:
    with pytest.raises(ValueError, match=r"INVALID_LINK: relations\[0\]"):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="concept",
            name="Quay Inventory",
            summary="A synthetic concept whose relation input is malformed.",
            relations=[relation],
        )

    assert not (vault / ENTITIES / "Quay Inventory.md").exists()


def test_unregistered_kind_is_written_with_the_registry_warning(vault: Path) -> None:
    target = _create_target(vault)

    created = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name="Buoy Schedule",
        summary="A synthetic concept whose relation kind is not registered.",
        relations=[{"kind": "steers_toward", "target": target}],
    )

    assert _relations_section(vault / created["path"]) == [f"- steers_toward [[{target}]]"]
    findings = created["creation"]["contract_result"]["warnings"]
    assert [finding["code"] for finding in findings] == ["unregistered_relation"]


def test_relations_outside_create_entity_are_refused(vault: Path) -> None:
    with pytest.raises(ValueError, match="INVALID_SCHEMA_ARGUMENT: relations requires create-entity"):
        commands.op_connect_memory(
            vault,
            operation="context",
            query="tidal ledger",
            relations=[{"kind": "supports", "target": f"{ENTITIES}/Tidal Ledger"}],
        )


def test_cli_passes_relations_as_json_objects(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _create_target(vault)
    relations = json.dumps([{"kind": "part_of", "target": target}])

    try:
        code = cli_main([
            "connect_memory", "--operation", "create-entity", "--entity-type", "concept",
            "--name", "Pier Audit", "--summary", "A synthetic audit inside the tidal ledger.",
            "--relations", relations, "--json",
        ])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = capsys.readouterr().out
    assert code == 0, out
    page = vault / json.loads(out)["data"]["path"]
    assert _relations_section(page) == [f"- part_of [[{target}]]"]


def test_bare_title_target_resolves_and_collapses_like_connections(vault: Path) -> None:
    target = _create_target(vault)

    created = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name="Mooring Census",
        summary="A synthetic count that supports the tidal ledger.",
        relations=[
            {"kind": "supports", "target": "Tidal Ledger"},
            {"kind": "supports", "target": target},
        ],
    )

    assert _relations_section(vault / created["path"]) == [f"- supports [[{target}]]"]


@pytest.mark.parametrize("failure", ["invalid", "missing", "ambiguous", "excluded"])
def test_unavailable_reference_refuses_before_creating_an_entity(vault: Path, failure: str) -> None:
    target = _create_target(vault)
    page = vault / f"{target}.md"
    ref = memory_refs.ref_from_markdown(page.read_text())
    expected = "REFERENCE_NOT_FOUND"
    if failure == "invalid":
        ref = memory_refs.REF_PREFIX + "invalid"
        expected = "INVALID_REFERENCE"
    elif failure == "missing":
        ref = memory_refs.memory_ref(memory_refs.new_id())
    elif failure == "ambiguous":
        page.with_name("Second Ledger.md").write_text(page.read_text())
        expected = "AMBIGUOUS_REFERENCE"
    else:
        relative = target.removeprefix("Knowledge Base/")
        (vault / "Knowledge Base/_access.yaml").write_text(f"excluded:\n  - {relative}.md\n")
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")}

    with pytest.raises(ValueError, match=expected):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="concept",
            name="Quay Inventory",
            summary="A synthetic inventory with an unavailable relation target.",
            relations=[{"kind": "supports", "target": ref}],
        )

    assert {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*.md")} == before


def test_an_unclosed_fence_in_the_summary_cannot_hide_authored_edges(vault: Path) -> None:
    target = _create_target(vault)

    with pytest.raises(ValueError, match="INVALID_LINK: .*close every code fence"):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="concept",
            name="Slipway Notes",
            summary="A synthetic concept whose summary opens a fence:\n```text\nnever closed",
            relations=[{"kind": "part_of", "target": target}],
        )

    assert not (vault / ENTITIES / "Slipway Notes.md").exists()


def test_a_connection_with_a_line_break_cannot_author_a_second_bullet(vault: Path) -> None:
    target = _create_target(vault)

    with pytest.raises(ValueError, match=r"INVALID_LINK: connections\[0\]"):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="concept",
            name="Quay Inventory",
            summary="A synthetic concept whose connection smuggles a second bullet.",
            connections=[f"[[{target}]]\n- cites [[Elsewhere]]"],
        )

    assert not (vault / ENTITIES / "Quay Inventory.md").exists()


@pytest.mark.parametrize("field", ["summary", "why_in_kb"])
def test_free_text_cannot_open_a_second_relations_section(vault: Path, field: str) -> None:
    target = _create_target(vault)
    text = "A synthetic concept.\n\n## relations\n- cites [[Elsewhere]]"

    with pytest.raises(ValueError, match=rf"INVALID_LINK: .*{field}"):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="concept",
            name="Slipway Notes",
            **{"summary": "A synthetic concept.", "why_in_kb": None, field: text},
            relations=[{"kind": "part_of", "target": target}],
        )

    assert not (vault / ENTITIES / "Slipway Notes.md").exists()


def test_relations_must_be_a_list(vault: Path) -> None:
    with pytest.raises(ValueError, match=r"INVALID_LINK: relations must be a list"):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="concept",
            name="Anchor Register",
            summary="A synthetic concept sent the REST object shape by mistake.",
            relations={},
        )


def test_read_operation_refuses_relations_unless_empty(vault: Path) -> None:
    with pytest.raises(ValueError, match="INVALID_SCHEMA_ARGUMENT: relations requires create-entity"):
        commands.op_connect_memory(vault, operation="context", query="tidal ledger", relations={})
    # An empty list is the unset value, so a read operation still accepts it.
    commands.op_connect_memory(vault, operation="context", query="tidal ledger", relations=[])


def test_malformed_connection_keeps_the_relation_finding(vault: Path) -> None:
    with pytest.raises(ValueError, match="malformed_relation"):
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="concept",
            name="Dock Ledger",
            summary="A synthetic concept whose connection packs two links into one value.",
            connections=["[[Tidal Ledger]], [[Quay Inventory]]"],
        )


def test_curation_plan_authors_typed_relations_through_the_entity_leaf(vault: Path) -> None:
    from exomem import curation

    target = _create_target(vault)
    proposed = curation.propose(
        vault,
        {
            "version": 1,
            "title": "Typed entity curation",
            "steps": [
                {
                    "step_id": "entity",
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "concept",
                        "name": "Ballast Review",
                        "summary": "A synthetic review that belongs to the tidal ledger.",
                        "relations": [{"kind": "part_of", "target": target}],
                    },
                }
            ],
        },
    )
    result = curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved exact plan.",
    )

    assert _relations_section(vault / result["step"]["path"]) == [f"- part_of [[{target}]]"]
