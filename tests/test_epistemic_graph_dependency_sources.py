"""Bare-name link-dependency lookup on the typed graph sidecar.

Covers `EpistemicGraphIndex.dependency_sources_for_bare_name` -- the read the
write-time `entity_candidate` block (`capture-identities-at-write-time`) uses
to find, without a vault walk, which pages already link an unresolved bare
name. It is deliberately conservative: it matches only the SAME bare,
unfoldered spelling (casefolded), never a folder-qualified or differently
normalised one, and shares the never-false-empty status contract
`relation_participants`/`relation_edges` already use.
"""

from __future__ import annotations

from pathlib import Path

from exomem import epistemic_graph

A = "Knowledge Base/Notes/a.md"
B = "Knowledge Base/Notes/b.md"
DEEP = "Knowledge Base/Notes/deep.md"


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _seed(vault: Path) -> None:
    _write(vault, A, "---\ntype: insight\n---\n# A\n\nMet [[Harbour Studio]].\n")
    _write(vault, B, "---\ntype: insight\n---\n# B\n\nAlso [[Harbour Studio]].\n")
    # A folder-qualified spelling of the same identity: a DIFFERENT lookup key
    # by design (design D5) -- the audit family's job, not this lookup's.
    _write(
        vault,
        DEEP,
        "---\ntype: insight\n---\n# Deep\n\nSee [[Notes/People/Harbour Studio]].\n",
    )


def _built(vault: Path) -> epistemic_graph.EpistemicGraphIndex:
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    return idx


def test_bare_name_sources_match_only_the_same_bare_spelling(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    result = idx.dependency_sources_for_bare_name("Harbour Studio")
    assert result.status == "available"
    assert result.sources == frozenset({A, B})


def test_a_name_nobody_links_is_authoritative_empty(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    result = idx.dependency_sources_for_bare_name("Nobody Here")
    assert result.status == "available"
    assert result.sources == frozenset()


def test_empty_name_is_available_noop(tmp_path: Path) -> None:
    idx = _built(tmp_path / "vault")
    assert idx.dependency_sources_for_bare_name("").status == "available"
    assert idx.dependency_sources_for_bare_name("").sources == frozenset()


def test_disabled_index_is_temporarily_unavailable(tmp_path: Path, monkeypatch) -> None:
    _built(tmp_path / "vault")
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    result = epistemic_graph.EpistemicGraphIndex(
        tmp_path / "vault"
    ).dependency_sources_for_bare_name("Harbour Studio")
    assert result.status == "temporarily_unavailable"
    assert result.reason == "graph_index_disabled"
    assert result.sources == frozenset()


def test_missing_sidecar_is_warming(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    # No rebuild -- the sidecar does not exist yet.
    result = epistemic_graph.EpistemicGraphIndex(vault).dependency_sources_for_bare_name(
        "Harbour Studio"
    )
    assert result.status == "warming"
