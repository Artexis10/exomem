"""find tool tests against the fixture KB."""

from __future__ import annotations

from pathlib import Path

import pytest

from kb_mcp import find as find_module


def test_query_substring_hits_body(vault: Path) -> None:
    hits = find_module.find(vault, query="EGCG")
    assert any("egcg" in h.path.lower() for h in hits)
    egcg_hit = next(h for h in hits if "egcg" in h.path.lower())
    assert egcg_hit.type == "source"


def test_query_case_insensitive(vault: Path) -> None:
    upper = find_module.find(vault, query="METABOLISM")
    lower = find_module.find(vault, query="metabolism")
    assert {h.path for h in upper} == {h.path for h in lower}
    assert len(upper) >= 2


def test_empty_query_returns_most_recent(vault: Path) -> None:
    hits = find_module.find(vault, query="", limit=5)
    assert len(hits) <= 5
    # All hits should have a non-empty excerpt (first 200 chars)
    assert all(h.excerpt for h in hits)


def test_filter_by_type(vault: Path) -> None:
    hits = find_module.find(vault, query="", types=["source"])
    assert all(h.type == "source" for h in hits)
    assert len(hits) >= 3


def test_filter_by_project_singular(vault: Path) -> None:
    # research-note with project: endstate
    hits = find_module.find(vault, query="", projects=["endstate"])
    assert any(h.path.endswith("engine-architecture.md") for h in hits)
    # The insight has projects: [endstate, q] — should also match
    assert any("progressive-disclosure" in h.path for h in hits)


def test_filter_by_project_plural(vault: Path) -> None:
    hits = find_module.find(vault, query="", projects=["q"])
    # Only the insight has projects: [endstate, q]
    assert any("progressive-disclosure" in h.path for h in hits)


def test_filter_by_tag(vault: Path) -> None:
    hits = find_module.find(vault, query="", tags=["metabolism"])
    paths = {h.path for h in hits}
    # The session, the curriculum research note, and the production-log all carry it
    assert any("metabolism-curriculum-design" in p for p in paths)
    assert any("metabolic-literacy-curriculum" in p for p in paths)


def test_filter_combination(vault: Path) -> None:
    hits = find_module.find(
        vault, query="metabolism", types=["research-note"], projects=["health"]
    )
    assert len(hits) == 1
    assert hits[0].path.endswith("metabolic-literacy-curriculum.md")


def test_excerpt_centers_on_match(vault: Path) -> None:
    hits = find_module.find(vault, query="insulin")
    assert hits
    # All returned hits should mention insulin in the excerpt (or have it in title)
    for h in hits:
        text = (h.title + " " + h.excerpt).lower()
        assert "insulin" in text


def test_no_matches_returns_empty(vault: Path) -> None:
    hits = find_module.find(vault, query="zzzzzzznotfoundzzzzz")
    assert hits == []


def test_excludes_schema_dir(vault: Path) -> None:
    hits = find_module.find(vault, query="SKILL")
    # _Schema/SKILL.md should not appear (excluded by walker)
    assert not any("_Schema" in h.path for h in hits)


def test_hit_carries_scope_for_different_types(vault: Path) -> None:
    # research-note → scope = project
    research_hits = find_module.find(vault, query="engine", types=["research-note"])
    assert research_hits[0].scope == "endstate"

    # production-log → scope = medium
    prod_hits = find_module.find(vault, query="metabolism", types=["production-log"])
    assert prod_hits and prod_hits[0].scope == "reels"

    # entity → scope = entity_type
    entity_hits = find_module.find(vault, query="Karpathy", types=["entity"])
    assert entity_hits and entity_hits[0].scope == "person"


def test_limit_respected(vault: Path) -> None:
    hits = find_module.find(vault, query="", limit=2)
    assert len(hits) == 2


def test_results_sorted_by_updated_desc(vault: Path) -> None:
    hits = find_module.find(vault, query="")
    dates = [h.updated for h in hits if h.updated]
    assert dates == sorted(dates, reverse=True)


def test_cache_invalidation_on_mtime_change(vault: Path) -> None:
    """Editing a file mid-process should yield fresh results next call."""
    p = vault / "Knowledge Base" / "Notes" / "Insights" / "progressive-disclosure-without-mode-fragmentation.md"
    pre = find_module.find(vault, query="brand-new-marker-string")
    assert pre == []
    # Touch the file with a new marker; bump mtime
    text = p.read_text(encoding="utf-8")
    p.write_text(text + "\nbrand-new-marker-string\n", encoding="utf-8")
    import os, time
    # Force a different mtime in case the resolution is coarse
    future = time.time() + 1
    os.utime(p, (future, future))
    post = find_module.find(vault, query="brand-new-marker-string")
    assert any("progressive-disclosure" in h.path for h in post)
