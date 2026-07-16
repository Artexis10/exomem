"""Epistemic graph sidecar: derived, rebuildable, and propose-only."""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

from exomem import (
    corpus_aware,
    epistemic_graph,
    markdown_relations,
    semantic_blocks,
    semantic_units,
)

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


def test_graph_rich_units_keep_legacy_keys_nodes_and_parse_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = _seed_graph_vault(tmp_path)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    current_path = vault / CURRENT
    raw = current_path.read_text(encoding="utf-8")
    page = epistemic_graph.find_module._parse_page(
        current_path, current_path.stat().st_mtime, vault
    )
    legacy = semantic_blocks.parse_semantic_blocks(
        page.body, validate=False, registry=idx.registry
    )
    document = semantic_units.parse_semantic_units(
        page.body,
        path=page.rel_path,
        validate=False,
        language_registry=idx.language_registry,
        relation_registry=idx.registry,
        include_legacy_relations=True,
        retain_unknown_relations=True,
        page_type=page.page_type,
    )

    assert len(document.rich_units) == len(legacy.blocks)
    for block, unit in zip(legacy.blocks, document.rich_units, strict=True):
        expected_material = "\n".join(
            [
                page.rel_path,
                block.type,
                block.id or f"line-{block.line}",
                block.title,
                block.body,
            ]
        )
        assert epistemic_graph._block_key(page, unit) == (
            f"block:{epistemic_graph._hash(expected_material)}"
        )
        node = epistemic_graph._block_node(page, unit, raw)
        assert node.anchor == (
            block.id
            or semantic_blocks.normalize_label(block.title)
            or f"line-{block.line}"
        )
        assert node.source_hash == epistemic_graph.vault_module.content_hash(raw)
        assert node.line_start == block.line
        assert node.line_end == block.end_line
        assert node.metadata == {
            **block.metadata,
            "origin": "semantic_block",
            "level": block.level,
        }

    parse_calls = 0
    original_parse = epistemic_graph.semantic_units.parse_semantic_units

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(
        epistemic_graph.semantic_units, "parse_semantic_units", counted_parse
    )
    idx.rebuild_all()

    assert parse_calls == len(list(vault.rglob("*.md")))


def test_graph_edges_exactly_match_legacy_parser_oracle(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source_rel = "Knowledge Base/Notes/Insights/edge-source.md"
    rich_target = "Knowledge Base/Notes/Insights/rich-target"
    note_target = "Knowledge Base/Notes/Insights/note-target"
    generic_target = "Knowledge Base/Notes/Insights/generic-target"
    for target in (rich_target, note_target, generic_target):
        _write(vault, f"{target}.md", "---\ntype: insight\n---\n# Target\n")
    source_path = _write(
        vault,
        source_rel,
        f"""\
---
type: insight
status: active
---
# Edge Source

## Claim
- id: claim-1
- relations: supports: [[{rich_target}]]

The rich claim.

## Relations
- refines [[{note_target}]]

## Notes
See [[{generic_target}]].
""",
    )
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    page = epistemic_graph.find_module._parse_page(
        source_path, source_path.stat().st_mtime, vault
    )
    source_hash = epistemic_graph.vault_module.content_hash(page.body)
    legacy_block = semantic_blocks.parse_semantic_blocks(
        page.body, validate=False, registry=idx.registry
    ).blocks[0]
    legacy_note_relation = markdown_relations.parse_markdown_relations(
        page.body,
        include_legacy=True,
        relation_types=idx.registry.keys | frozenset(idx.registry.aliases),
        retain_unknown=True,
    ).relations[0]
    document = semantic_units.parse_semantic_units(
        page.body,
        path=page.rel_path,
        validate=False,
        language_registry=idx.language_registry,
        relation_registry=idx.registry,
        include_legacy_relations=True,
        retain_unknown_relations=True,
        page_type=page.page_type,
    )

    file_key = epistemic_graph._file_key(source_rel)
    block_key = "block:" + epistemic_graph._hash(
        "\n".join(
            [
                source_rel,
                legacy_block.type,
                legacy_block.id or f"line-{legacy_block.line}",
                legacy_block.title,
                legacy_block.body,
            ]
        )
    )
    block_relation = legacy_block.relations[0]
    expected = [
        epistemic_graph._edge(
            block_key,
            file_key,
            "derived_from",
            "semantic_block",
            source_path=source_rel,
            source_anchor="claim-1",
            metadata={"block_kind": "claim"},
            registry=idx.registry,
            page_type="insight",
            source_hash=source_hash,
        ),
        epistemic_graph._edge(
            block_key,
            epistemic_graph._file_key(f"{rich_target}.md"),
            block_relation.kind,
            "semantic_relation",
            source_path=source_rel,
            source_anchor="claim-1",
            raw_relation=block_relation.raw.split(":", 1)[0].strip(),
            registry=idx.registry,
            page_type="insight",
            source_kind="claim",
            target_kind="file",
            source_hash=source_hash,
            metadata={
                "block_kind": "claim",
                "line": block_relation.line,
                "raw": block_relation.raw,
                "target_resolution": "resolved",
            },
        ),
        epistemic_graph._edge(
            file_key,
            epistemic_graph._file_key(f"{rich_target}.md"),
            "links_to",
            "wikilink",
            source_path=source_rel,
            registry=idx.registry,
            page_type="insight",
            source_hash=source_hash,
        ),
        epistemic_graph._edge(
            file_key,
            epistemic_graph._file_key(f"{generic_target}.md"),
            "links_to",
            "wikilink",
            source_path=source_rel,
            registry=idx.registry,
            page_type="insight",
            source_hash=source_hash,
        ),
        epistemic_graph._edge(
            file_key,
            epistemic_graph._file_key(f"{note_target}.md"),
            legacy_note_relation.kind,
            "markdown_relation",
            source_path=source_rel,
            source_anchor=f"line-{legacy_note_relation.line}",
            raw_relation=legacy_note_relation.kind,
            registry=idx.registry,
            page_type="insight",
            source_kind="file",
            target_kind="file",
            source_hash=source_hash,
            metadata={
                "line": legacy_note_relation.raw,
                "canonical": True,
                "target_resolution": "resolved",
            },
        ),
    ]

    actual = epistemic_graph._edges_for_page(
        vault,
        page,
        document,
        registry=idx.registry,
        source_hash=source_hash,
    )

    assert [edge.as_dict() for edge in actual] == [
        edge.as_dict() for edge in expected
    ]


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


def test_canonical_note_relation_suppresses_redundant_generic_edge(tmp_path: Path) -> None:
    vault = _seed_graph_vault(tmp_path)
    canonical = "Knowledge Base/Notes/Insights/canonical.md"
    _write(
        vault,
        canonical,
        """\
---
type: insight
status: active
---
# Canonical Relations

## Relations
- refines [[Knowledge Base/Notes/Insights/old-view]]

## Finding
This also references [[Knowledge Base/Notes/Insights/related-view]].
""",
    )

    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    edges = idx.edges(source_path=canonical)

    old_edges = [edge for edge in edges if edge["dst_key"].endswith("old-view.md")]
    assert [(edge["relation_type"], edge["origin"]) for edge in old_edges] == [
        ("refines", "markdown_relation")
    ]
    assert any(
        edge["relation_type"] == "links_to"
        and edge["dst_key"].endswith("related-view.md")
        for edge in edges
    )


def test_inline_link_to_typed_target_keeps_generic_edge(tmp_path: Path) -> None:
    vault = _seed_graph_vault(tmp_path)
    canonical = "Knowledge Base/Notes/Insights/repeated-target.md"
    _write(
        vault,
        canonical,
        """\
---
type: insight
status: active
---
# Repeated Target

## Relations
- refines [[Knowledge Base/Notes/Insights/old-view]]

## Finding
The discussion also cites [[Knowledge Base/Notes/Insights/old-view]] generically.
""",
    )

    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    old_edges = [
        edge
        for edge in idx.edges(source_path=canonical)
        if edge["dst_key"].endswith("old-view.md")
    ]

    assert {(edge["relation_type"], edge["origin"]) for edge in old_edges} == {
        ("refines", "markdown_relation"),
        ("links_to", "wikilink"),
    }


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


def test_similarity_suggestions_are_neutral_evidenced_and_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    vault = _seed_graph_vault(tmp_path)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    before_markdown = {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
        for path in vault.rglob("*.md")
    }
    before_edges = index.edges()
    monkeypatch.setattr(
        corpus_aware,
        "_best_cosine_per_file",
        lambda *_args, **_kwargs: {RELATED: 0.91234},
    )

    suggestions = epistemic_graph.suggest_relations(vault, path=CURRENT, limit=30)
    candidates = suggestions["candidates"]
    shared = next(candidate for candidate in candidates if candidate["method"] == "shared_sources")
    proximity = next(
        candidate for candidate in candidates if candidate["method"] == "embedding_proximity"
    )
    wikilink = next(candidate for candidate in candidates if candidate["method"] == "wikilink")
    frontmatter = next(
        candidate for candidate in candidates if candidate["method"] == "frontmatter_sources"
    )

    assert shared["relation_type"] == "relates_to"
    assert shared["evidence"]["shared_source"].endswith("2026-07-08-source.md")
    assert proximity == {
        "from": CURRENT,
        "to": RELATED,
        "relation_type": "relates_to",
        "method": "embedding_proximity",
        "evidence": {"cosine": 0.9123},
    }
    assert wikilink["relation_type"] == "links_to"
    assert frontmatter["relation_type"] == "derived_from"
    assert suggestions["mutated"] is False
    assert index.edges() == before_edges
    assert {
        path.relative_to(vault).as_posix(): path.read_text(encoding="utf-8")
        for path in vault.rglob("*.md")
    } == before_markdown


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


def test_graph_context_reports_edge_truncation_only_on_actual_overflow(
    tmp_path: Path,
) -> None:
    vault = _seed_graph_vault(tmp_path)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    full = epistemic_graph.graph_context(
        vault, path=CURRENT, depth=1, max_nodes=80, max_edges=80
    )
    edge_count = len(full["edges"])
    assert 1 < edge_count < 80

    exact = epistemic_graph.graph_context(
        vault, path=CURRENT, depth=1, max_nodes=80, max_edges=edge_count
    )
    overflow = epistemic_graph.graph_context(
        vault, path=CURRENT, depth=1, max_nodes=80, max_edges=edge_count - 1
    )

    assert not any("edges capped" in item for item in exact["truncation"])
    assert any("edges capped" in item for item in overflow["truncation"])


def test_graph_context_bounds_raw_edge_inspection_by_public_limits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = _seed_graph_vault(tmp_path)
    current = vault / CURRENT
    links = "\n".join(
        f"- [[Knowledge Base/Notes/Insights/bounded-target-{index}]]"
        for index in range(100)
    )
    current.write_text(
        current.read_text(encoding="utf-8") + "\n## Bounded links\n" + links + "\n",
        encoding="utf-8",
    )
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    converted = 0
    original = epistemic_graph._edge_row_to_dict

    def counted(row):
        nonlocal converted
        converted += 1
        return original(row)

    monkeypatch.setattr(epistemic_graph, "_edge_row_to_dict", counted)
    context = epistemic_graph.graph_context(
        vault, path=CURRENT, depth=1, max_nodes=2, max_edges=1
    )

    assert converted <= epistemic_graph._edge_inspection_budget(
        max_nodes=2, max_edges=1
    )
    assert len(context["edges"]) == 1
    assert any("edges capped" in item for item in context["truncation"])


def test_graph_context_reports_edge_inspection_overflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = _seed_graph_vault(tmp_path)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    monkeypatch.setattr(epistemic_graph, "_edge_inspection_budget", lambda **_: 1)

    context = epistemic_graph.graph_context(
        vault, path=CURRENT, depth=1, max_nodes=80, max_edges=80
    )

    assert any("edge inspection capped at 1 record" in item for item in context["truncation"])


def test_neighbor_edge_query_uses_limit_plus_one_overflow_sentinel(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Result:
        def fetchall(self):
            return [{"edge_key": f"edge-{index}"} for index in range(4)]

    class Connection:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return Result()

    monkeypatch.setattr(epistemic_graph, "_edge_row_to_dict", lambda row: row)
    rows, overflow = epistemic_graph._neighbor_edges(
        Connection(), {"node:b", "node:a"}, set(), limit=3
    )

    assert str(captured["sql"]).endswith("ORDER BY edge_key LIMIT ?")
    assert captured["params"] == ("node:a", "node:b", "node:a", "node:b", 4)
    assert [row["edge_key"] for row in rows] == ["edge-0", "edge-1", "edge-2"]
    assert overflow is True


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
