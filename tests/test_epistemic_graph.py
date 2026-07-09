"""Epistemic graph sidecar: derived, rebuildable, and propose-only."""

from __future__ import annotations

import builtins
from pathlib import Path

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


def test_graph_commands_are_registry_exposed_on_all_surfaces() -> None:
    from exomem import commands

    by_name = {cmd.name: cmd for cmd in commands.COMMANDS}
    for name in ("graph_context", "suggest_relations"):
        cmd = by_name[name]
        assert cmd.surfaces == frozenset({"mcp", "rest", "cli"})
        assert cmd.read_only is True
        assert cmd.cli_writes is False
        assert cmd.mcp_annotations.readOnlyHint is True
