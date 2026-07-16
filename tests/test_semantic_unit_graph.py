"""Semantic-unit nodes in the rebuildable epistemic graph sidecar."""

from __future__ import annotations

from pathlib import Path

from exomem import epistemic_graph, semantic_index

_PAGE_ID = "33333333-3333-4333-8333-333333333333"
_SOURCE = "Knowledge Base/Notes/Insights/unit-source.md"
_TARGET = "Knowledge Base/Notes/Insights/unit-target.md"


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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
