"""Semantic-unit nodes in the rebuildable epistemic graph sidecar."""

from __future__ import annotations

from pathlib import Path

from exomem import (
    commands,
    embeddings,
    epistemic_graph,
    semantic_index,
    semantic_language_registry,
)

_PAGE_ID = "33333333-3333-4333-8333-333333333333"
_SOURCE = "Knowledge Base/Notes/Insights/unit-source.md"
_TARGET = "Knowledge Base/Notes/Insights/unit-target.md"


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _unit_context_fixture(tmp_path: Path):
    registry = semantic_language_registry.registry_path(tmp_path)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        "schema_version: 1\n"
        "categories:\n"
        "  config:\n"
        "    description: Configuration facts\n"
        "    aliases: [configuration]\n"
        "kinds: {}\n",
        encoding="utf-8",
    )
    _write(tmp_path, _TARGET, "---\ntype: insight\n---\n# Target\n")
    source = _write(
        tmp_path,
        _SOURCE,
        f"""\
---
type: insight
title: Unit graph source
exomem_id: {_PAGE_ID}
---
# Unit graph source

## Observations
- [configuration] Compact links [[Knowledge Base/Notes/Insights/unit-target]] ^compact-1

## Decision
- category: config
- id: rich-1
- relations: supports: [[Knowledge Base/Notes/Insights/unit-target]]

Use the indexed semantic language.
""",
    )
    state = semantic_index.build_parent_index_state(tmp_path, source)
    compact, rich = state.document.units
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    return source, compact, rich


def _seed_refs(context: dict) -> list[str]:
    return [seed["metadata"]["unit_ref"] for seed in context["seeds"]]


def test_graph_context_seeds_exact_compact_and_rich_unit_refs_without_inference(
    tmp_path: Path,
) -> None:
    _source, compact, rich = _unit_context_fixture(tmp_path)

    compact_context = epistemic_graph.graph_context(
        tmp_path, unit_ref=compact.unit_ref, depth=1
    )
    rich_context = epistemic_graph.graph_context(
        tmp_path, unit_ref=rich.unit_ref, depth=1
    )

    assert compact_context["unit_status"] == "found"
    assert _seed_refs(compact_context) == [compact.unit_ref]
    compact_key = compact_context["seeds"][0]["node_key"]
    assert {
        edge["relation_type"]
        for edge in compact_context["edges"]
        if compact_key in (edge["src_key"], edge["dst_key"])
    } == {"derived_from"}

    assert rich_context["unit_status"] == "found"
    assert _seed_refs(rich_context) == [rich.unit_ref]
    rich_key = rich_context["seeds"][0]["node_key"]
    assert {
        edge["relation_type"]
        for edge in rich_context["edges"]
        if edge["src_key"] == rich_key
    } == {"derived_from", "supports"}


def test_graph_context_resolves_category_aliases_and_keeps_kind_distinct(
    tmp_path: Path,
) -> None:
    _source, compact, rich = _unit_context_fixture(tmp_path)

    by_alias = epistemic_graph.graph_context(
        tmp_path, categories=["configuration"], depth=0
    )
    compact_only = epistemic_graph.graph_context(
        tmp_path, kinds=["observation"], depth=0
    )
    rich_only = epistemic_graph.graph_context(
        tmp_path, categories=["configuration"], kinds=["decision"], depth=0
    )

    assert set(_seed_refs(by_alias)) == {compact.unit_ref, rich.unit_ref}
    assert _seed_refs(compact_only) == [compact.unit_ref]
    assert _seed_refs(rich_only) == [rich.unit_ref]


def test_graph_context_reports_when_exact_unit_is_excluded_by_unit_filters(
    tmp_path: Path,
) -> None:
    _source, compact, _rich = _unit_context_fixture(tmp_path)

    context = epistemic_graph.graph_context(
        tmp_path,
        unit_ref=compact.unit_ref,
        categories=["rule"],
        kinds=["observation"],
        depth=0,
    )

    assert context["unit_status"] == "found"
    assert context["unit_filter_status"] == "excluded"
    assert context["seeds"] == []


def test_graph_context_reports_stale_and_missing_unit_refs_explicitly(
    tmp_path: Path,
) -> None:
    source, compact, _rich = _unit_context_fixture(tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8").replace("Compact links", "Changed compact"),
        encoding="utf-8",
    )

    stale = epistemic_graph.graph_context(tmp_path, unit_ref=compact.unit_ref, depth=1)
    missing = epistemic_graph.graph_context(
        tmp_path, unit_ref="exomem://memory/missing#not-a-unit", depth=1
    )

    assert stale["unit_status"] == "stale"
    assert stale["seeds"] == []
    assert any(
        warning["code"] == "semantic_unit_index_drift"
        for warning in stale["warnings"]
    )
    assert missing["unit_status"] == "missing"
    assert missing["seeds"] == []


def test_graph_context_reports_ambiguous_unit_ref_without_selecting_a_collision(
    tmp_path: Path,
) -> None:
    first = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/first.md",
        f"---\ntype: insight\nexomem_id: {_PAGE_ID}\n---\n"
        "# First\n\n- [config] First unit ^shared\n",
    )
    _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/second.md",
        f"---\ntype: insight\nexomem_id: {_PAGE_ID}\n---\n"
        "# Second\n\n- [config] Second unit ^shared\n",
    )
    unit_ref = semantic_index.build_parent_index_state(tmp_path, first).document.units[
        0
    ].unit_ref
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    context = epistemic_graph.graph_context(tmp_path, unit_ref=unit_ref, depth=1)

    assert context["unit_status"] == "ambiguous"
    assert context["seeds"] == []


def test_graph_context_applies_freshness_before_the_unit_seed_cap(
    tmp_path: Path,
) -> None:
    stale_path = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/a-stale.md",
        "---\ntype: insight\n---\n# Stale\n\n- [config] Old indexed unit ^stale\n",
    )
    fresh_path = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/b-fresh.md",
        "---\ntype: insight\n---\n# Fresh\n\n- [config] Current indexed unit ^fresh\n",
    )
    fresh_ref = semantic_index.build_parent_index_state(
        tmp_path, fresh_path
    ).document.units[0].unit_ref
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    stale_path.write_text(
        stale_path.read_text(encoding="utf-8").replace("Old indexed", "Changed"),
        encoding="utf-8",
    )

    context = epistemic_graph.graph_context(
        tmp_path, categories=["config"], depth=0, max_nodes=1
    )

    assert _seed_refs(context) == [fresh_ref]
    assert any(
        warning["code"] == "semantic_unit_index_drift"
        for warning in context["warnings"]
    )


def test_graph_context_unit_filters_work_with_embeddings_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _source, compact, rich = _unit_context_fixture(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    def forbidden_embed(*_args, **_kwargs):
        raise AssertionError("graph context must not load an embedding model")

    monkeypatch.setattr(embeddings, "embed_texts", forbidden_embed)

    context = epistemic_graph.graph_context(
        tmp_path, categories=["configuration"], depth=0
    )

    assert set(_seed_refs(context)) == {compact.unit_ref, rich.unit_ref}


def test_graph_context_unit_controls_are_registry_generated_on_all_surfaces(
    tmp_path: Path,
) -> None:
    _source, compact, _rich = _unit_context_fixture(tmp_path)

    leaf = commands.op_graph_context(tmp_path, unit_ref=compact.unit_ref, depth=0)
    product = commands.op_connect_memory(
        tmp_path,
        operation="graph-context",
        unit_ref=compact.unit_ref,
        categories=["configuration"],
        kinds=["observation"],
        depth=0,
    )
    registry_commands = {
        "graph_context": next(
            command for command in commands.COMMANDS if command.name == "graph_context"
        ),
        "connect_memory": next(
            command
            for command in commands.PRODUCT_COMMANDS
            if command.name == "connect_memory"
        ),
    }
    for command in registry_commands.values():
        assert {"unit_ref", "categories", "kinds"} <= {
            param.name for param in command.params
        }
        assert command.surfaces == frozenset({"mcp", "rest", "cli"})

    assert leaf["unit_status"] == "found"
    assert _seed_refs(leaf) == [compact.unit_ref]
    assert product["graph"]["unit_status"] == "found"
    assert _seed_refs(product["graph"]) == [compact.unit_ref]


def test_graph_indexes_compact_and_rich_units_once_with_shared_generation(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _TARGET, "---\ntype: insight\n---\n# Target\n")
    source = _write(
        tmp_path,
        _SOURCE,
        """\
---
type: insight
title: Unit graph source
exomem_id: 33333333-3333-4333-8333-333333333333
---
# Unit graph source

## Observations
- [config] Compact links [[Knowledge Base/Notes/Insights/unit-target]] ^compact-1

## Decision
- category: config
- id: rich-1
- relations: supports: [[Knowledge Base/Notes/Insights/unit-target]]

Use the indexed semantic language.
""",
    )
    state = semantic_index.build_parent_index_state(tmp_path, source)
    page = epistemic_graph.find_module._parse_page(source, source.stat().st_mtime, tmp_path)
    assert page is not None
    compact, rich = state.document.units
    idx = epistemic_graph.EpistemicGraphIndex(tmp_path)

    report = idx.rebuild_all()

    assert report["indexed_files"] == 2
    nodes = idx.nodes(path=_SOURCE)
    unit_nodes = [node for node in nodes if node["metadata"].get("record_type") == "semantic_unit"]
    assert len(unit_nodes) == 2
    by_ref = {node["metadata"]["unit_ref"]: node for node in unit_nodes}
    assert set(by_ref) == {compact.unit_ref, rich.unit_ref}
    assert by_ref[compact.unit_ref]["kind"] == "observation"
    assert by_ref[compact.unit_ref]["metadata"]["category"] == "config"
    assert by_ref[rich.unit_ref]["node_key"] == epistemic_graph._block_key(page, rich)
    assert (
        len([node for node in nodes if node["node_key"] == epistemic_graph._block_key(page, rich)])
        == 1
    )
    for node in unit_nodes:
        assert node["source_hash"] == state.parent_source_hash
        assert node["metadata"]["parent_generation"] == state.parent_generation
        assert node["metadata"]["parent_source_hash"] == state.parent_source_hash
        assert node["metadata"]["parser_version"] == state.parser_version

    edges = idx.edges(source_path=_SOURCE)
    file_key = epistemic_graph._file_key(_SOURCE)
    derived = [
        edge
        for edge in edges
        if edge["relation_type"] == "derived_from"
        and edge["src_key"] in {node["node_key"] for node in unit_nodes}
    ]
    assert {(edge["src_key"], edge["dst_key"]) for edge in derived} == {
        (by_ref[compact.unit_ref]["node_key"], file_key),
        (by_ref[rich.unit_ref]["node_key"], file_key),
    }
    assert all(edge["metadata"]["parent_generation"] == state.parent_generation for edge in derived)
    rich_edges = [edge for edge in edges if edge["src_key"] == by_ref[rich.unit_ref]["node_key"]]
    assert {edge["relation_type"] for edge in rich_edges} == {
        "derived_from",
        "supports",
    }
    compact_edges = [
        edge for edge in edges if edge["src_key"] == by_ref[compact.unit_ref]["node_key"]
    ]
    assert [edge["relation_type"] for edge in compact_edges] == ["derived_from"]
    assert compact_edges[0]["origin"] == "semantic_unit"
    assert compact_edges[0]["registry_status"] == "core"


def test_graph_refresh_replaces_old_unit_generation_without_duplicates(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path,
        _SOURCE,
        f"---\ntype: insight\nexomem_id: {_PAGE_ID}\n---\n# Unit generation fixture\n\n"
        "- [config] old graph unit ^old\n",
    )
    idx = epistemic_graph.EpistemicGraphIndex(tmp_path)
    idx.rebuild_all()
    before = semantic_index.build_parent_index_state(tmp_path, source)

    source = _write(
        tmp_path,
        _SOURCE,
        f"---\ntype: insight\nexomem_id: {_PAGE_ID}\n---\n# Unit generation fixture\n\n"
        "- [rule] current graph unit ^current\n",
    )
    after = semantic_index.build_parent_index_state(tmp_path, source)
    idx.refresh_paths([source])

    nodes = [
        node
        for node in idx.nodes(path=_SOURCE)
        if node["metadata"].get("record_type") == "semantic_unit"
    ]
    assert len(nodes) == 1
    assert nodes[0]["metadata"]["unit_ref"] == after.document.units[0].unit_ref
    assert nodes[0]["metadata"]["parent_generation"] == after.parent_generation
    assert after.parent_generation != before.parent_generation
    assert "old graph unit" not in {node["text"] for node in nodes}
