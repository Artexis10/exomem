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
