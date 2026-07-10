"""Epistemic graph sidecar: derived, rebuildable, and propose-only."""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

from exomem import epistemic_graph

SOURCE = "Knowledge Base/Sources/Articles/2026-07-08-source.md"
EVIDENCE = "Knowledge Base/Evidence/Cases/receipt.md"
OLD = "Knowledge Base/Notes/Insights/old-view.md"
CURRENT = "Knowledge Base/Notes/Insights/current-view.md"
RELATED = "Knowledge Base/Notes/Insights/related-view.md"


def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _seed_graph_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    _write(
        vault,
        SOURCE,
        """\
---
type: source
source_type: article
captured: 2026-07-08
ingested_into: []
---
# Source: Graph Source

## Capture

Source material.
""",
    )
    _write(
        vault,
        EVIDENCE,
        """\
---
type: evidence
status: active
---
# Receipt Evidence

Proof artifact.
""",
    )
    _write(
        vault,
        OLD,
        """\
---
type: insight
status: superseded
superseded_by: "[[Knowledge Base/Notes/Insights/current-view]]"
sources:
  - "[[Knowledge Base/Sources/Articles/2026-07-08-source]]"
---
# Old View

## Claim

The old claim.
""",
    )
    _write(
        vault,
        RELATED,
        """\
---
type: insight
status: active
sources:
  - "[[Knowledge Base/Sources/Articles/2026-07-08-source]]"
---
# Related View

## Claim

A related claim.
""",
    )
    _write(
        vault,
        CURRENT,
        """\
---
type: insight
status: active
supersedes: "[[Knowledge Base/Notes/Insights/old-view]]"
sources:
  - "[[Knowledge Base/Sources/Articles/2026-07-08-source]]"
evidence:
  - "[[Knowledge Base/Evidence/Cases/receipt]]"
---
# Current View

## Findings

The current graph finding cites [[Knowledge Base/Notes/Insights/related-view]].

- supports [[Knowledge Base/Notes/Insights/related-view]]
- supports [[Knowledge Base/Notes/Insights/future-view]]
- made_up_relation [[Knowledge Base/Notes/Insights/old-view]]
""",
    )
    return vault


def test_rebuild_indexes_files_blocks_and_core_edges(tmp_path: Path) -> None:
    vault = _seed_graph_vault(tmp_path)
    idx = epistemic_graph.EpistemicGraphIndex(vault)

    report = idx.rebuild_all()
    nodes = idx.nodes()
    edges = idx.edges()

    assert report["indexed_files"] >= 5
    assert any(n["kind"] == "file" and n["path"] == CURRENT for n in nodes)
    assert any(n["kind"] == "finding" and n["path"] == CURRENT for n in nodes)
    edge_types = {(e["relation_type"], e["source_path"]) for e in edges}
    assert ("derived_from", CURRENT) in edge_types
    assert ("supersedes", CURRENT) in edge_types
    assert ("links_to", CURRENT) in edge_types
    assert ("evidenced_by", CURRENT) in edge_types


def test_sidecar_can_be_deleted_and_rebuilt_equivalently(tmp_path: Path) -> None:
    vault = _seed_graph_vault(tmp_path)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    first = epistemic_graph.graph_context(vault, path=CURRENT, depth=1)

    epistemic_graph.sidecar_path(vault).unlink()
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    second = epistemic_graph.graph_context(vault, path=CURRENT, depth=1)

    assert second["available"] is True
    assert first == second


def test_edge_provenance_and_unsupported_relation_labels(tmp_path: Path) -> None:
    vault = _seed_graph_vault(tmp_path)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    edges = idx.edges(source_path=CURRENT)
    supports = [e for e in edges if e["relation_type"] == "supports"]
    assert supports
    assert supports[0]["origin"] == "semantic_relation"
    assert supports[0]["source_path"] == CURRENT
    assert supports[0]["source_anchor"].startswith("line-")
    assert all(e["relation_type"] != "made_up_relation" for e in edges)


def test_default_graph_indexing_imports_no_reasoning_model(
    tmp_path: Path, monkeypatch
) -> None:
    vault = _seed_graph_vault(tmp_path)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(("torch", "sentence_transformers", "transformers"))
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()


def test_optional_model_suggestion_failure_does_not_break_context(tmp_path: Path) -> None:
    vault = _seed_graph_vault(tmp_path)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    suggestions = epistemic_graph.suggest_relations(
        vault, path=CURRENT, include_model_suggestions=True
    )
    context = epistemic_graph.graph_context(vault, path=CURRENT, depth=1)

    assert context["available"] is True
    assert suggestions["model_suggestions_available"] is False
    assert any("unavailable" in warning for warning in suggestions["warnings"])
    assert any(c["method"] == "wikilink" for c in suggestions["candidates"])

def test_command_leaves_return_graph_context_and_suggestions(tmp_path: Path) -> None:
    from exomem import commands

    vault = _seed_graph_vault(tmp_path)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    context = commands.op_graph_context(vault, path=CURRENT, depth=1)
    suggestions = commands.op_suggest_relations(vault, path=CURRENT)

    assert context["available"] is True
    assert any(edge["relation_type"] == "supports" for edge in context["edges"])
    assert suggestions["mutated"] is False
    assert suggestions["candidates"]


def test_graph_context_unavailable_soft_fails(tmp_path: Path) -> None:
    from exomem import commands

    vault = _seed_graph_vault(tmp_path)
    context = commands.op_graph_context(vault, path=CURRENT)

    assert context == {
        "available": False,
        "reason": "graph sidecar unavailable",
        "seeds": [],
        "nodes": [],
        "edges": [],
        "truncation": [],
    }


def test_graph_context_keeps_unresolved_relation_as_placeholder(tmp_path: Path) -> None:
    vault = _seed_graph_vault(tmp_path)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    context = epistemic_graph.graph_context(vault, path=CURRENT, depth=1)

    placeholder = next(
        node for node in context["nodes"] if node["path"].endswith("future-view.md")
    )
    assert placeholder["kind"] == "unresolved"
    assert placeholder["metadata"] == {
        "placeholder": True,
        "resolution": "unresolved",
    }
    assert any(
        edge["dst_key"] == placeholder["node_key"]
        and edge["relation_type"] == "supports"
        for edge in context["edges"]
    )


def test_unified_context_matches_quality_golden_and_is_markdown_read_only(
    tmp_path: Path,
) -> None:
    import yaml

    from exomem import commands

    vault = _seed_graph_vault(tmp_path)
    _write(
        vault,
        "Knowledge Base/log.md",
        "## [2026-07-09] edit | Notes/Insights/current-view\n\n"
        "why: clarified the active finding\n",
    )
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    before = {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
        for path in vault.rglob("*.md")
    }
    golden = yaml.safe_load(
        (Path(__file__).parent / "golden" / "context_quality.yaml").read_text(
            encoding="utf-8"
        )
    )["current_view"]

    context = commands.op_connect_memory(
        vault,
        operation="context",
        path=CURRENT,
        depth=golden["depth"],
    )
    alias = commands.op_connect_memory(
        vault,
        operation="graph-context",
        path=CURRENT,
        depth=golden["depth"],
    )
    shorthand = commands.op_connect_memory(
        vault,
        operation="context",
        path=CURRENT.removeprefix("Knowledge Base/"),
        depth=golden["depth"],
    )

    node_paths = {node["path"] for node in context["graph"]["nodes"]}
    relation_types = {edge["relation_type"] for edge in context["graph"]["edges"]}
    assert set(golden["expected_paths"]).issubset(node_paths)
    assert set(golden["expected_relation_types"]).issubset(relation_types)
    assert sum(map(len, context["semantic_blocks"].values())) >= golden["min_blocks"]
    assert context["provenance"][0]["sources"]
    assert context["provenance"][0]["evidence"]
    assert context["supersession"][0]["supersedes"]
    assert context["history"][CURRENT]
    assert any(node["kind"] == "unresolved" for node in context["graph"]["nodes"])
    assert alias == context
    assert shorthand == context
    after = {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
        for path in vault.rglob("*.md")
    }
    assert after == before


def test_unified_context_reports_cross_seed_merge_truncation(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import memory_context

    calls = {"count": 0}

    def fake_graph_context(*args, **kwargs):
        calls["count"] += 1
        suffix = str(calls["count"])
        return {
            "available": True,
            "reason": None,
            "seeds": [{"node_key": f"seed:{suffix}"}],
            "nodes": [
                {"node_key": f"node:{suffix}:a"},
                {"node_key": f"node:{suffix}:b"},
            ],
            "edges": [
                {"edge_key": f"edge:{suffix}:a"},
                {"edge_key": f"edge:{suffix}:b"},
            ],
            "truncation": [],
        }

    monkeypatch.setattr(epistemic_graph, "graph_context", fake_graph_context)
    result = memory_context._merge_graph_contexts(
        tmp_path,
        [SimpleNamespace(rel_path="one.md"), SimpleNamespace(rel_path="two.md")],
        depth=1,
        relation_types=None,
        node_types=None,
        max_nodes=2,
        max_edges=2,
    )

    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 2
    assert any("merged nodes capped" in item for item in result["truncation"])
    assert any("merged edges capped" in item for item in result["truncation"])


def test_graph_commands_are_registry_exposed_on_all_surfaces() -> None:
    from exomem import commands

    by_name = {cmd.name: cmd for cmd in commands.COMMANDS}
    for name in ("graph_context", "suggest_relations"):
        cmd = by_name[name]
        assert cmd.surfaces == frozenset({"mcp", "rest", "cli"})
        assert cmd.read_only is True
        assert cmd.cli_writes is False
        assert cmd.mcp_annotations.readOnlyHint is True
