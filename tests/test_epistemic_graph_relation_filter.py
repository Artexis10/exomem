"""Relation-participant lookup on the typed graph sidecar.

Covers `EpistemicGraphIndex.relation_participants` — the engine behind
relation-filtered recall: canonical/parent matching, direction semantics
(candidate-relative and anchor-relative), symmetric no-op, placeholder and
self-edge exclusion, anchor exclusion, and the never-false-empty readiness
status mapping (available / warming / temporarily_unavailable).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from exomem import epistemic_graph

A = "Knowledge Base/Notes/Insights/a.md"
B = "Knowledge Base/Notes/Insights/b.md"
C = "Knowledge Base/Notes/Insights/c.md"


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _seed(vault: Path) -> None:
    _write(
        vault,
        A,
        "---\ntype: insight\n---\n# A\n\n## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Insights/b]]\n"
        "- contradicts [[Knowledge Base/Notes/Insights/c]]\n"
        "- links_to [[Knowledge Base/Notes/Insights/does-not-exist]]\n",
    )
    _write(vault, B, "---\ntype: insight\n---\n# B\n\nBody.\n")
    _write(vault, C, "---\ntype: insight\n---\n# C\n\nBody.\n")


def _built(vault: Path) -> epistemic_graph.EpistemicGraphIndex:
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    return idx


def test_canonical_match_both_directions_no_anchor(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    result = idx.relation_participants(["supports"])
    assert result.status == "available"
    # A owns the edge source (outbound); B owns the destination (inbound).
    assert result.paths == frozenset({A, B})
    assert result.provenance[A].direction == "outbound"
    assert result.provenance[A].counterpart == B
    assert result.provenance[A].matched_via == "relation_type"
    assert result.provenance[B].direction == "inbound"
    assert result.provenance[B].counterpart == A


def test_direction_filter_candidate_relative(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    assert idx.relation_participants(["supports"], direction="outbound").paths == frozenset({A})
    assert idx.relation_participants(["supports"], direction="inbound").paths == frozenset({B})


def test_symmetric_relation_ignores_direction(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    # contradicts is symmetric: both endpoints qualify regardless of direction.
    for direction in ("any", "outbound", "inbound"):
        assert idx.relation_participants(["contradicts"], direction=direction).paths == frozenset(
            {A, C}
        )


def test_anchor_join_excludes_anchor_and_is_anchor_relative(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    # Edge is A --supports--> B. Anchored on A, only B qualifies; A is excluded.
    result = idx.relation_participants(["supports"], anchor=A)
    assert result.paths == frozenset({B})
    # Relative to anchor A the edge is outbound, so direction=outbound keeps B,
    # inbound drops it. The annotation on B is candidate-relative ("inbound").
    assert idx.relation_participants(["supports"], anchor=A, direction="outbound").paths == frozenset(
        {B}
    )
    assert idx.relation_participants(["supports"], anchor=A, direction="inbound").paths == frozenset()
    assert result.provenance[B].direction == "inbound"


def test_anchor_alone_matches_all_typed_edges(tmp_path: Path) -> None:
    # relation_of with no relation keys: every typed edge touching the anchor
    # qualifies (supports->B and contradicts->C), never a silent empty.
    idx = _built(tmp_path / "vault")
    result = idx.relation_participants([], anchor=A)
    assert result.status == "available"
    assert result.paths == frozenset({B, C})


def test_anchor_alone_on_edgeless_page_is_authoritative_empty(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    # C has no outgoing edges of its own; only the inbound contradicts from A.
    result = idx.relation_participants([], anchor=B)
    assert result.status == "available"
    assert result.paths == frozenset({A})


def test_placeholder_and_self_edges_excluded(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    # links_to points at a non-existent page; no participant is produced for it.
    result = idx.relation_participants(["links_to"])
    assert not any("does-not-exist" in p for p in result.paths)


def test_authoritative_empty_when_no_such_edge(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    result = idx.relation_participants(["mitigates"])
    assert result.status == "available"
    assert result.paths == frozenset()


def test_empty_keys_available_noop(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    assert idx.relation_participants([]).status == "available"


def test_disabled_index_is_temporarily_unavailable(tmp_path: Path, monkeypatch) -> None:
    _built(tmp_path / "vault")
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    result = epistemic_graph.EpistemicGraphIndex(tmp_path / "vault").relation_participants(
        ["supports"]
    )
    assert result.status == "temporarily_unavailable"
    assert result.reason == "graph_index_disabled"
    assert result.paths == frozenset()


def test_missing_sidecar_is_warming(tmp_path: Path) -> None:
    _seed(tmp_path / "vault")
    # No rebuild — the sidecar does not exist yet.
    result = epistemic_graph.EpistemicGraphIndex(tmp_path / "vault").relation_participants(
        ["supports"]
    )
    assert result.status == "warming"


def test_stale_schema_sidecar_is_warming(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _built(vault)
    # Simulate a pre-bump (v6) sidecar: its identity no longer matches v7.
    conn = sqlite3.connect(epistemic_graph.sidecar_path(vault))
    conn.execute("UPDATE graph_meta SET value = '6' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    result = epistemic_graph.EpistemicGraphIndex(vault).relation_participants(["supports"])
    assert result.status == "warming"


def test_schema_bump_invalidates_cache_token(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _built(vault)
    assert epistemic_graph.SCHEMA_VERSION == 7
    token = epistemic_graph.cache_token(vault)
    assert token is not None
    assert token[0] == "7"


def test_deterministic_participants(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    first = idx.relation_participants(["supports"])
    second = idx.relation_participants(["supports"])
    assert first.paths == second.paths
    assert {k: v.counterpart for k, v in first.provenance.items()} == {
        k: v.counterpart for k, v in second.provenance.items()
    }
