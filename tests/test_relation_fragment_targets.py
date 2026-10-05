"""A relation target `[[Page#unit]]` lands on the unit, not silently on the page."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from exomem import (
    deferred_index,
    epistemic_graph,
    freshness,
    relation_census,
    semantic_language_registry,
)
from exomem import find as find_module
from exomem import vault as vault_module

KB = "Knowledge Base/Notes/Insights"
TARGET = f"{KB}/frag-target.md"
DUP_TARGET = f"{KB}/frag-dup.md"
SOURCE = f"{KB}/frag-source.md"
OTHER = f"{KB}/frag-other.md"
TARGET_ID = "44444444-4444-4444-8444-444444444444"
DUP_ID = "55555555-5555-4555-8555-555555555555"
SOURCE_ID = "66666666-6666-4666-8666-666666666666"
OTHER_ID = "77777777-7777-4777-8777-777777777777"


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _target_text(rich_body: str = "Rich target body.") -> str:
    return f"""\
---
type: insight
title: Frag target
exomem_id: {TARGET_ID}
---
# Frag target

## Observations
- [configuration] Compact target fact ^compact-1

## Decision
- category: config
- id: rich-1

{rich_body}
"""


def _source_text() -> str:
    return f"""\
---
type: insight
title: Frag source
exomem_id: {SOURCE_ID}
---
# Frag source

## Decision
- category: config
- id: src-rich
- relations: answers: [[{KB}/frag-target#rich-1]]

Source body.

## Relations
- supports [[{KB}/frag-target#compact-1]]
- depends_on [[{KB}/frag-target#compact-9]]
- contradicts [[{KB}/frag-dup#dup]]
- relates_to [[{KB}/frag-target]]
"""


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    registry = semantic_language_registry.registry_path(root)
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
    _write(root, TARGET, _target_text())
    _write(
        root,
        DUP_TARGET,
        f"---\ntype: insight\ntitle: Frag dup\nexomem_id: {DUP_ID}\n---\n# Frag dup\n\n"
        "## Observations\n- [configuration] First ^dup\n- [configuration] Second ^dup\n",
    )
    _write(root, SOURCE, _source_text())
    return root


def _built(tmp_path: Path) -> tuple[Path, epistemic_graph.EpistemicGraphIndex]:
    root = _vault(tmp_path)
    # Live freshness is what lets a refresh stay incremental instead of
    # falling back to a whole-vault rebuild that would hide a missed dependant.
    freshness.seed(
        root,
        "vault",
        ((str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(p), freshness.stat_signature(p))
            for p in find_module._walk_md(root / "Knowledge Base")
        ),
    )
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()
    return root, index


def _unit_key(unit_ref: str) -> str:
    return "unit:" + hashlib.sha256(unit_ref.encode("utf-8")).hexdigest()


def _node(index: epistemic_graph.EpistemicGraphIndex, path: str, anchor: str) -> dict:
    (node,) = [n for n in index.nodes(path=path) if n["anchor"] == anchor and n["kind"] != "file"]
    return node


def _authored(index: epistemic_graph.EpistemicGraphIndex, **match) -> dict:
    (edge,) = [
        e
        for e in index.edges(source_path=SOURCE)
        if all(e[k] == v for k, v in match.items())
    ]
    return edge


def test_note_level_fragment_lands_on_the_unit_and_a_bare_target_stays_on_the_page(
    tmp_path: Path,
) -> None:
    _root, index = _built(tmp_path)

    supports = _authored(index, origin="markdown_relation", relation_type="supports")
    compact = _node(index, TARGET, "compact-1")
    assert supports["dst_key"] == compact["node_key"] == _unit_key(compact["metadata"]["unit_ref"])
    assert supports["src_key"] == epistemic_graph._file_key(SOURCE)
    assert supports["source_anchor"].startswith("line-")
    assert supports["metadata"]["fragment_resolution"] == "unit"

    bare = _authored(index, origin="markdown_relation", relation_type="relates_to")
    assert bare["dst_key"] == epistemic_graph._file_key(TARGET)
    assert "fragment_resolution" not in bare["metadata"]


def test_relation_row_fragment_lands_on_the_rich_unit_and_keeps_its_source_anchor(
    tmp_path: Path,
) -> None:
    _root, index = _built(tmp_path)

    edge = _authored(index, origin="semantic_relation")
    rich = _node(index, TARGET, "rich-1")
    source_block = _node(index, SOURCE, "src-rich")
    assert edge["dst_key"] == rich["node_key"]
    assert edge["src_key"] == source_block["node_key"]
    assert edge["source_anchor"] == "src-rich"


def test_unresolvable_and_ambiguous_fragments_keep_the_page_edge_and_say_why(
    tmp_path: Path,
) -> None:
    root, index = _built(tmp_path)

    typo = _authored(index, origin="markdown_relation", relation_type="depends_on")
    ambiguous = _authored(index, origin="markdown_relation", relation_type="contradicts")
    assert typo["dst_key"] == epistemic_graph._file_key(TARGET)
    assert ambiguous["dst_key"] == epistemic_graph._file_key(DUP_TARGET)
    assert typo["metadata"]["fragment_resolution"] == "missing"
    assert ambiguous["metadata"]["fragment_resolution"] == "ambiguous"
    assert typo["metadata"]["target_fragment"] == "compact-9"

    metrics = relation_census.census(root)["metrics"]
    assert metrics["unresolved_fragment_edges"] == 1
    assert metrics["ambiguous_fragment_edges"] == 1


@pytest.mark.parametrize("path", ["refresh", "drain"])
def test_editing_a_rich_target_unit_rederives_the_source_edge(
    tmp_path: Path, path: str
) -> None:
    """The rich key hashes the body, so a target edit moves the destination key."""
    root, index = _built(tmp_path)
    before = _authored(index, origin="semantic_relation")["dst_key"]

    target = _write(root, TARGET, _target_text("Rewritten rich target body."))
    if path == "refresh":
        freshness.on_files_changed(root, changed=[target])
        find_module.on_resolver_files_changed(root, [TARGET], [])
        report = index.refresh_paths([target])
        assert "whole_vault" not in report
    else:
        deferred_index.add_graph(root, [TARGET])
        freshness.on_files_changed(root, changed=[target])
        find_module.on_resolver_files_changed(root, [TARGET], [])
        report = index.drain_paths([target])

    after = _authored(index, origin="semantic_relation")["dst_key"]
    assert after == _node(index, TARGET, "rich-1")["node_key"]
    assert after != before


def test_unit_destination_resolves_to_its_owning_page_for_every_reader(
    tmp_path: Path,
) -> None:
    _root, index = _built(tmp_path)

    participants = index.relation_participants(["supports"], anchor=SOURCE)
    assert participants.status == "available"
    assert TARGET in participants.paths

    neighbours = {(n.seed_rel, n.other_rel) for n in index.neighbors_for([TARGET])}
    assert (TARGET, SOURCE) in neighbours

    context = epistemic_graph.graph_context(index.vault_root, path=SOURCE, depth=1)
    paths = {node["path"] for node in context["nodes"]}
    assert TARGET in paths
    assert not any(node["kind"] == "unresolved" for node in context["nodes"])
    unit_edge = [e for e in context["edges"] if e["dst_key"] == _node(index, TARGET, "rich-1")["node_key"]]
    assert unit_edge


def test_two_pages_answering_the_same_unit_name_the_page_not_the_unit_key(
    tmp_path: Path,
) -> None:
    root, index = _built(tmp_path)
    _write(
        root,
        OTHER,
        f"---\ntype: insight\ntitle: Frag other\nexomem_id: {OTHER_ID}\n---\n# Frag other\n\n"
        f"## Decision\n- category: config\n- id: other-rich\n"
        f"- relations: answers: [[{KB}/frag-target#rich-1]]\n\nOther body.\n",
    )
    index.rebuild_all()

    conn = sqlite3.connect(index.path)
    try:
        candidates = epistemic_graph._shared_resolution_target_candidates(
            conn, SOURCE, epistemic_graph._file_key(SOURCE)
        )
    finally:
        conn.close()
    (candidate,) = [c for c in candidates if c["method"] == "shared_resolution_target"]
    assert candidate["to"] == OTHER
    assert [m["target"] for m in candidate["evidence"]["matches"]] == [TARGET]
