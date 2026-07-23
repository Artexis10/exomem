"""Relation-filtered recall wired into the find pipeline.

Covers the find()-level integration: participant intersection into the
eligibility seam (composes with structured filters and empty-query recall),
direction and anchor, unknown-key rejection, graph=false still filtering, and
the never-false-empty degrade matrix (available / warming / temporarily
unavailable).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import epistemic_graph
from exomem import find as find_module
from exomem.cli_ops import OpError
from exomem.find import RetrievalIndexWarming

A = "Knowledge Base/Notes/Insights/a.md"
B = "Knowledge Base/Notes/Insights/b.md"
C = "Knowledge Base/Notes/Insights/c.md"


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _vault(tmp_path: Path, *, build: bool = True) -> Path:
    vault = tmp_path / "vault"
    _write(
        vault,
        A,
        "---\ntype: insight\n---\n# A\n\n## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Insights/b]]\n"
        "- contradicts [[Knowledge Base/Notes/Insights/c]]\n",
    )
    _write(vault, B, "---\ntype: insight\n---\n# B\n\nBody.\n")
    _write(vault, C, "---\ntype: insight\n---\n# C\n\nBody.\n")
    if build:
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return vault


def _paths(vault: Path, **kw) -> set[str]:
    return {h.path for h in find_module.find(vault, query="", limit=15, **kw)}


def test_relation_filter_selects_participants(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"]) == {A, B}


def test_relation_direction_filters(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"], relation_direction="outbound") == {A}
    assert _paths(vault, relations=["supports"], relation_direction="inbound") == {B}


def test_relation_of_anchor(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"], relation_of=A) == {B}


def test_symmetric_relation_ignores_direction(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["contradicts"], relation_direction="outbound") == {A, C}


def test_relation_of_alone_matches_all_edges(tmp_path: Path) -> None:
    # An anchor with no relation keys must match every typed edge touching it,
    # never a silent empty (A supports B, A contradicts C).
    vault = _vault(tmp_path)
    assert _paths(vault, relation_of=A) == {B, C}


def test_mixed_result_level_applies_relation_filter_to_units(tmp_path: Path) -> None:
    # In mixed mode the unit half must be gated by the relation filter too — a unit
    # whose parent page does not participate must not leak through.
    vault = _vault(tmp_path)
    hits = find_module.find(
        vault, query="", relations=["supports"], result_level="mixed", limit=15
    )
    parents = set()
    for h in hits:
        parents.add(getattr(h, "parent_path", None) or getattr(h, "path", None))
    # Only A and B participate in a supports edge; C must not appear via any unit.
    assert C not in parents


def test_filter_still_applies_with_graph_disabled_lane(tmp_path: Path) -> None:
    # graph=False turns off the graph ranking lane, but the relation filter is
    # eligibility, not lane fusion — it must still apply.
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"], graph=False) == {A, B}


def test_authoritative_empty_no_matching_edge(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["mitigates"]) == set()


def test_unknown_relation_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with pytest.raises(OpError) as excinfo:
        find_module.find(vault, query="", relations=["implments"], limit=15)
    assert excinfo.value.code == "INVALID_RELATION_FILTER"
    assert "implements" in excinfo.value.details.get("suggestions", [])


def test_invalid_direction_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with pytest.raises(OpError) as excinfo:
        find_module.find(vault, query="", relations=["supports"], relation_direction="sideways")
    assert excinfo.value.code == "INVALID_RELATION_FILTER"


def test_disabled_index_raises_temporarily_unavailable(tmp_path: Path, monkeypatch) -> None:
    vault = _vault(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    with pytest.raises(RetrievalIndexWarming) as excinfo:
        find_module.find(vault, query="", relations=["supports"], limit=15)
    assert excinfo.value.status == "temporarily_unavailable"


def test_missing_sidecar_raises_warming(tmp_path: Path) -> None:
    vault = _vault(tmp_path, build=False)
    with pytest.raises(RetrievalIndexWarming) as excinfo:
        find_module.find(vault, query="", relations=["supports"], limit=15)
    assert excinfo.value.status == "warming"


def test_stale_sidecar_raises_warming(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    conn = sqlite3.connect(epistemic_graph.sidecar_path(vault))
    conn.execute("UPDATE graph_meta SET value = '6' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RetrievalIndexWarming) as excinfo:
        find_module.find(vault, query="", relations=["supports"], limit=15)
    assert excinfo.value.status == "warming"


def test_no_relation_filter_is_unaffected(tmp_path: Path) -> None:
    # Absent filter: every page is eligible (empty-query recall returns all three).
    vault = _vault(tmp_path)
    assert _paths(vault) == {A, B, C}


def test_op_ask_memory_passes_relation_filter(tmp_path: Path) -> None:
    from exomem import commands

    vault = _vault(tmp_path)
    hits = commands.op_ask_memory(vault, query="", relations=["supports"], limit=15)
    # compact detail projects hits as dicts; the relation_match annotation rides through.
    assert {h["path"] for h in hits} == {A, B}
    assert any(h.get("relation_match", {}).get("relation_type") == "supports" for h in hits)


def test_op_find_rejects_unknown_relation(tmp_path: Path) -> None:
    from exomem import commands

    vault = _vault(tmp_path)
    with pytest.raises(OpError) as excinfo:
        commands.op_find(vault, query="", relations=["contradcts"], limit=15)
    assert excinfo.value.code == "INVALID_RELATION_FILTER"


def test_relation_match_annotation_is_additive(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    hits = find_module.find(vault, query="", relations=["supports"], limit=15)
    by_path = {h.path: h for h in hits}
    assert by_path[A].relation_match is not None
    assert by_path[A].relation_match["relation_type"] == "supports"
    assert by_path[A].relation_match["direction"] == "outbound"
    assert by_path[A].relation_match["counterpart"] == B
    assert by_path[A].relation_match["matched"] == "self"
    assert by_path[B].relation_match["direction"] == "inbound"
    # Hits without a relation filter carry no annotation.
    plain = find_module.find(vault, query="", limit=15)
    assert all(h.relation_match is None for h in plain)
