"""Tests for propose_compilation — the backlog-drain scaffold (Pillar 3).

suggest_related (which calls find/hybrid → torch) is monkeypatched so these stay
fast, torch-free, and deterministic. The point under test is the scaffold: type
heuristic, source resolution, connection filtering, outline shape, no writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import compile_proposal as cp
from exomem import corpus_aware
from exomem import find as find_module

_ARTICLE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements"
_SESSION = "Knowledge Base/Sources/Sessions/2026-05-05-metabolism-curriculum-design"


def test_proposal_structure_and_no_write(vault: Path, monkeypatch) -> None:
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *a, **k: [])
    res = cp.propose_compilation(vault, sources=[_ARTICLE])
    assert res["suggested_sources"] == [_ARTICLE]
    assert res["suggested_note_type"] in ("insight", "research-note")
    out = res["outline_markdown"]
    assert out.startswith("# ")
    assert "## Relations" in out
    # It must not have written anything — the source's ingested_into stays empty.
    src = (vault / f"{_ARTICLE}.md").read_text(encoding="utf-8")
    assert "ingested_into: []" in src


def test_proposal_filters_source_connections(vault: Path, monkeypatch) -> None:
    fake = [
        corpus_aware.RelatedSuggestion(
            path="Knowledge Base/Notes/Insights/keep.md", title="Keep",
            type="insight", why="", excerpt="",
        ),
        corpus_aware.RelatedSuggestion(
            path="Knowledge Base/Sources/Articles/drop.md", title="Drop",
            type="source", why="", excerpt="",
        ),
    ]
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *a, **k: fake)
    res = cp.propose_compilation(vault, sources=[_ARTICLE])
    assert res["suggested_connections"] == ["Knowledge Base/Notes/Insights/keep.md"]
    assert "- relates_to [[Knowledge Base/Notes/Insights/keep.md]]" in res["outline_markdown"]


def test_session_source_suggests_research_note(vault: Path, monkeypatch) -> None:
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *a, **k: [])
    res = cp.propose_compilation(vault, sources=[_SESSION])
    assert res["suggested_note_type"] == "research-note"
    assert "project:" in res["outline_markdown"]  # reminder that research-note needs it


def test_errors(vault: Path) -> None:
    with pytest.raises(cp.ProposeError):
        cp.propose_compilation(vault, sources=[])
    with pytest.raises(cp.ProposeError):
        cp.propose_compilation(
            vault, sources=["Knowledge Base/Sources/Articles/does-not-exist-xyz"]
        )


# ---------------- audit 2-02: relation targets must be governed KB material --
#
# compile_source proposes `relates_to` edges via corpus_aware.suggest_related's
# candidate scan, which reuses find()'s scope="kb" auto-widen. Auto-widen is
# correct for find() (surfacing out-of-KB content is the point), but a relation
# TARGET must be governed, in-KB material — you can't act on a read-only or
# out-of-KB edge, so it just pollutes the graph. These are torch-free (no
# sentence_transformers needed) so they live here rather than
# test_corpus_aware.py, whose semantic section import-skips without the
# `embeddings` extra.


def _hit(path: str):
    return find_module.Hit(
        path=path, type="insight", scope=None, title=path.rsplit("/", 1)[-1],
        updated="", excerpt="ex",
    )


def test_suggest_related_excludes_out_of_kb_targets(monkeypatch) -> None:
    # Handbooks/ and Reference/ are sibling trees of Knowledge Base/ — read-only
    # INPUT surfaced by find()'s auto-widen, never governed notes a relates_to
    # edge can point at.
    fake = [
        _hit("Handbooks/incident-severity-levels.md"),
        _hit("Reference/sample-curated.md"),
        _hit("Knowledge Base/Notes/Insights/fresh.md"),
    ]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(Path("/unused"), title="t", body="b", limit=8)
    paths = {corpus_aware._canon(s.path) for s in out}
    assert "handbooks/incident-severity-levels" not in paths
    assert "reference/sample-curated" not in paths
    assert paths == {"notes/insights/fresh"}


def test_suggest_related_excludes_readonly_and_excluded_targets(
    vault: Path, monkeypatch
) -> None:
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly:\n  - Products\nexcluded:\n  - Private\n", encoding="utf-8"
    )
    fake = [
        _hit("Knowledge Base/Products/ro-note.md"),
        _hit("Knowledge Base/Private/secret.md"),
        _hit("Knowledge Base/Notes/Insights/fresh.md"),
    ]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(vault, title="t", body="b", limit=8)
    paths = {corpus_aware._canon(s.path) for s in out}
    assert "products/ro-note" not in paths
    assert "private/secret" not in paths
    assert paths == {"notes/insights/fresh"}


def test_suggest_related_excludes_out_of_kb_hits_end_to_end(vault: Path) -> None:
    """Real fixture vault, no monkeypatch: find()'s scope='kb' auto-widen would
    surface Reference/sample-curated.md (see test_find.py's
    test_scope_kb_auto_widens_to_curated_trees) — suggest_related must drop it.
    """
    out = corpus_aware.suggest_related(
        vault, title="reference-marker-xyz", body="reference-marker-xyz", limit=8
    )
    assert not any(s.path.startswith("Reference/") for s in out)


def test_propose_compilation_never_suggests_out_of_kb_connections(
    vault: Path,
) -> None:
    """End-to-end through op_compile_source's underlying call: on a cold-start
    KB, suggested_connections must never point at Handbooks/ or Reference/
    (audit finding 2-02) even though find()'s auto-widen would surface them
    for a plain query.
    """
    res = cp.propose_compilation(
        vault,
        sources=[_ARTICLE],
        suggested_title="reference-marker-xyz incident-severity-levels",
    )
    for conn in res["suggested_connections"]:
        assert not conn.startswith("Reference/"), res["suggested_connections"]
        assert not conn.startswith("Handbooks/"), res["suggested_connections"]
