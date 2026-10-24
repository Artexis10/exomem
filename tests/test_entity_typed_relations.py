"""Typed relations authored when an entity is first created.

`markdown-relations` "Entity creation can author typed relations": a caller that
knows what an entity means to another page states the predicate at birth, and
bare `connections` keep their conservative `relates_to` rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands, graph_sync
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

    created = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name="Harbour Reconciliation",
        summary="A synthetic method that checks and belongs to the tidal ledger.",
        relations=[
            {"kind": "supports", "target": target},
            {"kind": "part_of", "target": target},
            {"kind": "supports", "target": target},
        ],
        connections=[target],
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
