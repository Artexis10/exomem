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


def test_query_tokens_match_in_any_order(vault: Path) -> None:
    """A multi-word query matches even when the words appear in different order.

    Regression: previously the matcher required the exact phrase as substring,
    so `metabolism curriculum` would miss a page about "curriculum on metabolism".
    """
    hits_a = find_module.find(vault, query="metabolism curriculum")
    hits_b = find_module.find(vault, query="curriculum metabolism")
    paths_a = {h.path for h in hits_a}
    paths_b = {h.path for h in hits_b}
    # Both queries should find the same set of pages.
    assert paths_a == paths_b
    # And both should include the curriculum research-note.
    assert any("metabolic-literacy-curriculum" in p for p in paths_a)


def test_query_all_tokens_required(vault: Path) -> None:
    """Every token must appear; missing one token → no match."""
    # `metabolism` appears in fixtures; the made-up token does not.
    hits = find_module.find(vault, query="metabolism zzzzzzznotfoundzzzzz")
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


# ---------------- scope: kb vs vault ----------------


def test_scope_kb_does_not_reach_curated_trees(vault: Path) -> None:
    """Default scope is KB-only; Cognitive Core/ marker should not surface."""
    hits = find_module.find(vault, query="cognitive-core-marker-xyz")
    assert hits == []


def test_scope_vault_reaches_curated_trees(vault: Path) -> None:
    """scope='vault' walks the full vault and surfaces curated-tree content."""
    hits = find_module.find(
        vault, query="cognitive-core-marker-xyz", scope="vault"
    )
    assert len(hits) == 1
    assert hits[0].path == "Cognitive Core/sample-curated.md"


def test_scope_vault_excludes_schema_and_trash(vault: Path) -> None:
    """_Schema/ and _trash/ must stay excluded even under scope='vault'."""
    # Place a marker file under each excluded dir
    schema_extra = vault / "Knowledge Base" / "_Schema" / "marker-vault-find.md"
    schema_extra.write_text(
        "---\ntags: []\n---\n# Schema marker\n\nfind-vault-skip-marker-abc\n",
        encoding="utf-8",
    )
    trash_extra = vault / "Knowledge Base" / "_trash" / "2026-05-25"
    trash_extra.mkdir(parents=True, exist_ok=True)
    (trash_extra / "scratch.md").write_text(
        "---\ntags: []\n---\n# Trash\n\nfind-vault-skip-marker-abc\n",
        encoding="utf-8",
    )
    # find_vault_skip_marker_abc would match if either dir leaked through
    find_module.clear_cache()
    hits = find_module.find(
        vault, query="find-vault-skip-marker-abc", scope="vault"
    )
    assert hits == []


def test_scope_default_is_kb(vault: Path) -> None:
    """No scope arg → behaves like scope='kb' (backward compat)."""
    hits_default = find_module.find(vault, query="cognitive-core-marker-xyz")
    hits_kb = find_module.find(
        vault, query="cognitive-core-marker-xyz", scope="kb"
    )
    assert hits_default == hits_kb


def test_scope_unknown_value_raises(vault: Path) -> None:
    with pytest.raises(ValueError, match="scope must be"):
        find_module.find(vault, query="x", scope="bogus")


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
