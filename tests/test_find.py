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


def test_scope_kb_auto_widens_to_curated_trees(vault: Path) -> None:
    """Default scope='kb' now auto-widens to the vault when KB has no hits.

    Previously this returned []; the curated marker lives outside Knowledge
    Base/ and was structurally invisible. Auto-widen surfaces it, tagged
    outside_kb so the caller knows it came from beyond the KB.
    """
    hits = find_module.find(vault, query="cognitive-core-marker-xyz")
    assert len(hits) == 1
    assert hits[0].path == "Cognitive Core/sample-curated.md"
    assert hits[0].outside_kb is True


def test_scope_kb_only_stays_strict(vault: Path) -> None:
    """scope='kb-only' is the strict opt-out: never widens to curated trees."""
    hits = find_module.find(
        vault, query="cognitive-core-marker-xyz", scope="kb-only"
    )
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


# ---------------- auto-widen: terse out-of-KB files (the X3 tracker case) ----


def _make_x3_tracker(vault: Path) -> str:
    """Write a terse, frontmatter-less tracker into a sibling (non-KB) folder.

    Mirrors Hugo's real `Tracking/X3 Full Reps.md`: no frontmatter, mostly
    numbers, one distinctive token. Returns its vault-relative path.
    """
    d = vault / "Tracking"
    d.mkdir(parents=True, exist_ok=True)
    (d / "X3 Full Reps.md").write_text(
        "# X3 Full Reps\n\n"
        "Overhead press (white short): 17 18 19 20 21 22 23\n"
        "Chest press (grey short): 18 20 22 24 26 28\n"
        "Split squat left (grey short): 18 17 21 24 23\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return "Tracking/X3 Full Reps.md"


def test_kb_scope_auto_widens_for_out_of_kb_file(vault: Path) -> None:
    """A file in a sibling folder surfaces under the default scope via widening."""
    rel = _make_x3_tracker(vault)
    hits = find_module.find(vault, query="X3")
    assert any(h.path == rel for h in hits)
    x3 = next(h for h in hits if h.path == rel)
    assert x3.outside_kb is True


def test_auto_widen_relaxed_gate_partial_token_match(vault: Path) -> None:
    """Out-of-KB hit survives even when not every query token is present.

    'progression'/'tracking' aren't in the terse file; 'x3' is. The strict
    all-tokens gate would drop it — the relaxed gate for out-of-KB BM25 hits
    keeps it.
    """
    rel = _make_x3_tracker(vault)
    hits = find_module.find(vault, query="X3 rep progression tracking")
    assert any(h.path == rel for h in hits)


def test_kb_only_scope_does_not_widen_to_tracker(vault: Path) -> None:
    rel = _make_x3_tracker(vault)
    hits = find_module.find(vault, query="X3", scope="kb-only")
    assert all(h.path != rel for h in hits)


def test_outside_kb_field_omitted_for_kb_hits(vault: Path) -> None:
    """KB hits carry outside_kb=False and omit it from as_dict()."""
    hits = find_module.find(vault, query="metabolism")
    assert hits
    kb_hit = next(h for h in hits if h.path.startswith("Knowledge Base/"))
    assert kb_hit.outside_kb is False
    assert "outside_kb" not in kb_hit.as_dict()


def test_reserve_surfaces_out_of_kb_even_when_kb_has_matches(vault: Path) -> None:
    """A reserved slot surfaces an out-of-KB match even when the KB fills `limit`.

    Uses a distinctive shared token (so BM25's IDF stays positive on the tiny
    fixture corpus). Two KB files match it literally and could fill limit=2 on
    their own; the reserve guarantees the out-of-KB file still appears, while
    the KB keeps the majority.
    """
    from kb_mcp import bm25
    token = "zzreservetokenzz"
    ins = vault / "Knowledge Base" / "Notes" / "Insights"
    (ins / "reserve-a.md").write_text(
        f"---\ntags: []\n---\n# Reserve A\n\n{token} alpha\n", encoding="utf-8"
    )
    (ins / "reserve-b.md").write_text(
        f"---\ntags: []\n---\n# Reserve B\n\n{token} beta\n", encoding="utf-8"
    )
    out = vault / "Reference"
    out.mkdir(parents=True, exist_ok=True)
    (out / "reserve-out.md").write_text(
        f"# Reserve Out\n\n{token} gamma outside the kb\n", encoding="utf-8"
    )
    find_module.clear_cache()
    bm25.clear_cache()
    hits = find_module.find(vault, query=token, limit=2)
    # Out-of-KB note reserved a slot (recall guarantee)...
    assert any(h.path == "Reference/reserve-out.md" and h.outside_kb for h in hits)
    # ...and the KB still keeps a slot.
    assert any(h.path.startswith("Knowledge Base/") for h in hits)


def test_no_reserve_consumed_when_nothing_outside_matches(vault: Path) -> None:
    """When no out-of-KB file matches, results stay pure KB (no reserve waste)."""
    hits = find_module.find(vault, query="metabolism", limit=5)
    assert hits
    assert all(h.path.startswith("Knowledge Base/") for h in hits)
    assert all(not h.outside_kb for h in hits)


def test_sync_conflict_files_excluded_from_results(vault: Path) -> None:
    """Obsidian `*.sync-conflict-*.md` duplicates never appear in find results.

    They are transient conflict copies (of log.md, notes, etc.) — indexing them
    pollutes results and wastes slots. They must be skipped under every scope.
    """
    from kb_mcp import bm25
    token = "zzsyncconflicttokenzz"
    p = vault / "Knowledge Base" / "log.sync-conflict-20260602-005505-ABC123.md"
    p.write_text(
        f"---\ntags: []\n---\n# Conflict copy\n\n{token}\n", encoding="utf-8"
    )
    find_module.clear_cache()
    bm25.clear_cache()
    hits = find_module.find(vault, query=token)
    assert all(".sync-conflict-" not in h.path for h in hits)
    # Nothing else contains the token, so the result is empty.
    assert hits == []


def test_cache_invalidation_on_mtime_change(vault: Path) -> None:
    """Editing a file mid-process should yield fresh results next call."""
    p = vault / "Knowledge Base" / "Notes" / "Insights" / "progressive-disclosure-without-mode-fragmentation.md"
    # Single distinctive token with no overlap against any fixture content —
    # so neither KB scope nor the vault auto-widen can surface it before we
    # write it. (A hyphenated sentinel would share sub-tokens with fixtures.)
    sentinel = "zzcacheinvalidationsentinelzz"
    pre = find_module.find(vault, query=sentinel)
    assert pre == []
    # Touch the file with a new marker; bump mtime
    text = p.read_text(encoding="utf-8")
    p.write_text(text + f"\n{sentinel}\n", encoding="utf-8")
    import os, time
    # Force a different mtime in case the resolution is coarse
    future = time.time() + 1
    os.utime(p, (future, future))
    post = find_module.find(vault, query=sentinel)
    assert any("progressive-disclosure" in h.path for h in post)


# ---------------- file-type filters (scoping; default = allow all) ----------------


def _mk_page(fm: dict):
    return find_module.ParsedPage(
        path=Path("x.md"), rel_path="Knowledge Base/x.md",
        frontmatter=fm, body="", title="t", mtime=0.0,
    )


def test_file_kind_classification() -> None:
    assert _mk_page({}).file_kind == "note"
    assert _mk_page({"type": "insight"}).file_kind == "note"
    assert _mk_page({"media_type": "pdf"}).file_kind == "pdf"
    assert _mk_page({"media_type": "image"}).file_kind == "image"
    assert _mk_page({"type": "dataset", "format": "csv"}).file_kind == "csv"
    assert _mk_page({"type": "dataset"}).file_kind == "dataset"


def test_passes_filters_default_allows_all_kinds() -> None:
    # No file-type filter → every kind passes. Search must never hide a type by default.
    for fm in ({}, {"media_type": "pdf"}, {"type": "dataset", "format": "csv"}):
        assert find_module._passes_filters(_mk_page(fm), types=None, projects=None, tags=None)


def test_passes_filters_file_types_include_and_exclude() -> None:
    pdf = _mk_page({"media_type": "pdf"})
    note = _mk_page({"type": "insight"})
    ds = _mk_page({"type": "dataset", "format": "csv"})
    # include: only listed kinds pass
    assert find_module._passes_filters(pdf, types=None, projects=None, tags=None, file_types=["pdf"])
    assert not find_module._passes_filters(note, types=None, projects=None, tags=None, file_types=["pdf"])
    # exclude: listed kinds drop, others pass
    assert not find_module._passes_filters(ds, types=None, projects=None, tags=None, exclude_file_types=["csv"])
    assert find_module._passes_filters(note, types=None, projects=None, tags=None, exclude_file_types=["csv"])
