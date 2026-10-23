"""Managed ranking keeps its admitted generation without reading candidate pages."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_recall_integration import _PageReadSentinel

from exomem import find as find_module
from exomem import find_candidates, find_policy, find_results, lexstore
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope
from exomem.ranking_config import DEFAULT_RANKING
from exomem.vault import kb_dirname


def _write(vault: Path, name: str, metadata: str = "", body: str = "rankcatalogue latest") -> str:
    rel = f"{kb_dirname()}/Notes/{name}.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {name}\n{metadata}---\n\n{body}\n", encoding="utf-8")
    return rel


@library_scope()
def test_ranking_views_equal_parsed_pages_without_reading_markdown(
    vault: Path, warm_managed_cell, monkeypatch, walk_sentinel
) -> None:
    """Dates, raw status, search fields and frame attribution share one source generation."""
    paths = {
        _write(vault, "recorded", "type: insight\nstatus: active\nupdated: 2026-10-01\n"),
        _write(vault, "captured", "type: source\ncaptured: 2026-10-01T23:30:00+02:00\n"),
        _write(vault, "invalid-date", "type: note\nstatus: [active]\nupdated: tomorrow\n"),
        _write(vault, "media", "type: source\nmedia_type: video\nupdated: 2026-10-02\n"),
        _write(vault, "other-script", body="最新記録を調査する。"),
        _write(vault, "partial", body="rankcatalogue only"),
        _write(vault, "provenance", body="rankcatalogue latest\n`[[Hidden]]`\n[[recorded|shown]]\n"),
        _write(vault, "custom-status", "type: insight\nstatus: old-local\nupdated: 2026-10-01\n"),
    }
    status_path = find_module.lifecycle_statuses.registry_path(vault)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "schema_version: 1\nentries:\n  old-local:\n    attributes: {class: superseded}\n",
        encoding="utf-8",
    )
    warm_managed_cell(vault)
    parsed = {rel: find_module._CACHE.get(vault / rel, vault) for rel in paths}
    assert all(parsed.values())
    expected_recency = find_policy.recency_ranking(sorted(paths), parsed.get, 20)
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    walks = walk_sentinel(vault, current_thread_only=True)

    metadata = find_module._managed_page_metadata(
        vault, paths, scope="kb", freshness_key=find_module.FreshnessSnapshot(vault).kb(),
        pending=None, include_ranking=True,
    )
    ranked = {rel: row.ranking for rel, row in metadata.items()}

    assert reads.count == 0, reads.report()
    assert walks.count == 0, walks.report()
    assert find_policy.recency_ranking(sorted(paths), ranked.get, 20) == expected_recency
    for rel, page in parsed.items():
        view = ranked[rel]
        assert view is not None
        assert view.frontmatter.get("status") == page.frontmatter.get("status")
        assert view.page_type == page.page_type
        assert view.media_type == page.media_type
        assert view.stem_set == page.stem_set
        assert view.letter_script == page.letter_script
        assert find_results.literal_tokens_present(view, "rankcatalogue latest") == (
            find_results.make_excerpt(page, "rankcatalogue latest") is not None
        )
        assert find_results.stem_tokens_present(view, "rankcatalogue latest") == (
            find_results.stem_tokens_present(page, "rankcatalogue latest")
        )
        assert view.outbound_targets == find_results.wikilink_targets(page.body)
    # A malformed lifecycle declaration remains malformed for the policy owner.
    invalid = next(rel for rel in paths if rel.endswith("/invalid-date.md"))
    assert ranked[invalid].frontmatter["status"] == ["active"]
    valid_paths = sorted(paths)
    raw = [(rel, 1.0) for rel in valid_paths]
    kwargs = dict(
        prefer_compiled=True, prefer_active=True, temporal=True,
        status_basis=find_module.lifecycle_statuses.Basis(vault),
        type_basis=find_module.note_types.Basis(vault),
    )
    assert find_policy.apply_post_rrf_multipliers(
        raw, "latest", DEFAULT_RANKING, page_of=ranked.get, **kwargs,
    ) == find_policy.apply_post_rrf_multipliers(
        raw, "latest", DEFAULT_RANKING, page_of=parsed.get, **kwargs,
    )
    lead = next(rel for rel in paths if rel.endswith("/other-script.md"))
    guard = dict(
        query="rankcatalogue latest", vector_ranking=[lead, *valid_paths],
        lexical_rankings=(valid_paths, valid_paths), lane_weights=(1.0, 1.0, 1.0),
        rrf_k=60, window=50,
    )
    assert find_policy.lexical_visibility(**guard, view_of=ranked.get) == (
        find_policy.lexical_visibility(**guard, view_of=parsed.get)
    )


@library_scope()
def test_warm_hybrid_hydrates_only_output_after_corpus_growth(
    vault: Path, warm_managed_cell, monkeypatch, walk_sentinel
) -> None:
    """Temporal ranking and multipliers must not hydrate every lexical candidate."""
    for index in range(60):
        _write(vault, f"match-{index:03d}", f"type: insight\nupdated: 2026-10-{1 + index % 20:02d}\n")
    warm_managed_cell(vault)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    walks = walk_sentinel(vault, current_thread_only=True)
    kwargs = dict(query="rankcatalogue latest", scope="kb-only", limit=4, graph=False, rerank=False)
    first = find_module.find(vault, **kwargs)
    assert len(first) == 4
    assert reads.count <= 4, reads.report()
    assert walks.count == 0, walks.report()

    # The same matched rows and candidates survive four times as many unrelated pages.
    for index in range(180):
        _write(vault, f"filler-{index:03d}", body="disjoint background material")
    warm_managed_cell(vault)
    reads.reset()
    walks.reset()
    second = find_module.find(vault, **kwargs)
    assert [hit.as_dict() for hit in second] == [hit.as_dict() for hit in first]
    assert reads.count <= 4, reads.report()
    assert walks.count == 0, walks.report()


@library_scope()
def test_normal_table_ranking_does_not_depend_on_fts(
    vault: Path, warm_managed_cell, monkeypatch
) -> None:
    rel = _write(vault, "without-fts", "type: insight\ncaptured: 2026-10-01\n")
    monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    warm_managed_cell(vault)

    metadata = find_module._managed_page_metadata(
        vault, {rel}, scope="kb", freshness_key=find_module.FreshnessSnapshot(vault).kb(),
        pending=None, include_ranking=True,
    )

    assert metadata[rel].ranking.stem_set
    assert metadata[rel].ranking.updated == "2026-10-01"


@library_scope()
def test_unparseable_parent_keeps_the_same_generation_collapse(
    vault: Path, warm_managed_cell
) -> None:
    parent = _write(vault, "unparseable-parent")
    child = _write(vault, "readable-child", f"parent_media: {parent[:-3]}\n")
    (vault / parent).write_bytes(b"\xff")
    warm_managed_cell(vault, prebuild_refs=False)
    metadata = find_module._managed_page_metadata(
        vault, {child}, scope="kb", freshness_key=find_module.FreshnessSnapshot(vault).kb(),
        pending=None, include_ranking=True,
    )
    assert set(metadata) == {child, parent}
    assert metadata[parent].ranking is None
    expected = find_candidates.collapse_frame_children(
        [child], vault, lambda rel: find_module._CACHE.get(vault / rel, vault), {},
        recall_paths={child, parent},
    )
    actual = find_candidates.collapse_frame_children(
        [child], vault, lambda rel: metadata[rel].ranking, {},
        recall_paths={child, parent}, metadata_paths=metadata.keys(),
    )
    assert actual == expected == [parent]


@library_scope()
def test_missing_ranking_on_a_parsed_row_declines_instead_of_hydrating(
    vault: Path, warm_managed_cell, monkeypatch
) -> None:
    rel = _write(vault, "missing-ranking")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE pages SET ranking_json = NULL WHERE path = ?", (rel,))
    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", lambda *_a, **_kw: None)
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module._managed_page_metadata(
            vault, {rel}, scope="kb", freshness_key=find_module.FreshnessSnapshot(vault).kb(),
            pending=None, include_ranking=True,
        )
    assert reads.count == 0, reads.report()


@library_scope()
def test_pending_ranking_replaces_tombstones_and_discovers_a_new_parent(
    vault: Path, warm_managed_cell
) -> None:
    from exomem import find_corpus, pending_recall

    child = _write(vault, "frame", "updated: 2026-10-01\n")
    removed = _write(vault, "removed", "updated: 2026-10-01\n")
    warm_managed_cell(vault)
    parent = _write(vault, "new-parent", "media_type: video\nupdated: 2026-10-03\n")
    _write(vault, "frame", f"parent_media: {parent[:-3]}\nframe_ts: 7.5\nupdated: 2026-10-02\n")
    pages = {
        rel: find_corpus.parse_page(vault / rel, (vault / rel).stat().st_mtime, vault)
        for rel in (child, parent)
    }
    rows = {
        rel: pending_recall.PendingRow(rel, "generation", "batch", page.snapshot_hash, page, None)
        for rel, page in pages.items()
    }
    rows[removed] = pending_recall.PendingRow(removed, "generation", "batch", None, None, None)
    overlay = pending_recall.PendingOverlay("ready", None, 1, rows)
    metadata = find_module._managed_page_metadata(
        vault, {child, removed}, scope="kb", freshness_key=find_module.FreshnessSnapshot(vault).kb(),
        pending=overlay, include_ranking=True,
    )
    assert removed not in metadata
    assert metadata[child].ranking.updated == "2026-10-02"
    attribution = {}
    collapsed = find_candidates.collapse_frame_children(
        [child, removed], vault, lambda rel: metadata[rel].ranking if rel in metadata else None,
        attribution, recall_paths={child, parent}, metadata_paths=metadata.keys(),
    )
    assert collapsed == [parent]
    assert attribution[parent] == (child, 7.5)


@library_scope()
def test_unread_byte_drift_does_not_rerank_the_admitted_generation(
    vault: Path, warm_managed_cell, monkeypatch
) -> None:
    """An unselected candidate's later date cannot replace the admitted winner."""
    winner = _write(vault, "winner", "type: insight\nupdated: 2026-10-20\n")
    unread = _write(vault, "unread", "type: insight\nupdated: 2026-10-01\n")
    warm_managed_cell(vault)
    original = find_candidates.collect_candidates
    altered = False

    def drift(*args, **kwargs):
        nonlocal altered
        if not altered:
            altered = True
            _write(vault, "unread", "type: insight\nupdated: 2099-10-01\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(find_candidates, "collect_candidates", drift)
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    hits = find_module.find(
        vault, query="rankcatalogue latest", scope="kb-only", limit=1, graph=False, rerank=False,
    )
    assert altered
    assert [hit.path for hit in hits] == [winner]
    assert all(path != str(vault / unread) for path, _caller in reads.read)


@library_scope()
def test_managed_graph_and_fusion_equal_the_same_generation_parsed_oracle(
    vault: Path, warm_managed_cell, monkeypatch
) -> None:
    """An out-of-scope seed keeps legacy expansion without hydrating its Markdown."""
    from exomem import epistemic_graph

    target = _write(vault, "graph-target", "type: insight\n", "Connected evidence.")
    code_only = _write(vault, "code-target", "type: insight\n", "Literal code example.")
    seed = "Reference/catalogue-seed.md"
    (vault / seed).parent.mkdir(parents=True, exist_ok=True)
    (vault / seed).write_text(
        "---\ntitle: Seed\nupdated: 2026-10-01\n---\n\nrankcatalogue latest\n"
        "[[Notes/graph-target#Heading|Shown]]\n"
        "`[[Notes/code-target]]`\n```\n[[Notes/code-target]]\n```\n",
        encoding="utf-8",
    )
    _write(vault, "partial-seed", body="rankcatalogue only")
    warm_managed_cell(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    original_collect = find_candidates.collect_candidates
    original_indexed = epistemic_graph.EpistemicGraphIndex.indexed_paths
    seeds = []
    bundles = []
    oracle = False

    def record_seeds(index, paths):
        seeds.append(tuple(paths))
        return original_indexed(index, paths)

    def collect(*args, **kwargs):
        if oracle:
            kwargs["ranking_metadata"] = None
        bundle = original_collect(*args, **kwargs)
        bundles.append(bundle)
        return bundle

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "indexed_paths", record_seeds)
    monkeypatch.setattr(find_candidates, "collect_candidates", collect)
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    kwargs = dict(query="rankcatalogue latest", scope="vault", limit=5, graph=True, rerank=False)
    managed = find_module.find(vault, **kwargs)
    managed_reads = reads.count
    oracle = True
    expected = find_module.find(vault, **kwargs)

    assert seed in seeds[0]
    assert seeds[0] == seeds[1]
    assert target in {hit.path for hit in managed}
    assert code_only not in {hit.path for hit in managed}
    assert bundles[0].fused == bundles[1].fused
    assert bundles[0].temporal_ranking == bundles[1].temporal_ranking
    assert bundles[0].graph_ranking == bundles[1].graph_ranking
    assert bundles[0].lexical_votes_withheld == bundles[1].lexical_votes_withheld
    assert [hit.as_dict() for hit in managed] == [hit.as_dict() for hit in expected]
    assert managed_reads <= len(managed), reads.report()


@library_scope()
def test_many_matching_frames_hydrate_only_the_selected_parent_and_frame(
    vault: Path, warm_managed_cell, monkeypatch
) -> None:
    parent = _write(vault, "clip", "media_type: video\nupdated: 2026-10-01\n", "Parent transcript.")
    children = []
    for number in range(12):
        frame = f"{kb_dirname()}/Notes/frame-{number:02d}"
        children.append(_write(
            vault, f"frame-{number:02d}",
            f"parent_media: {parent[:-3]}\nevidence_file: {frame}\n"
            f"frame_ts: {number}.5\nupdated: 2026-10-01\n",
        ))
    warm_managed_cell(vault)
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    hits = find_module.find(
        vault, query="rankcatalogue latest", scope="kb-only", graph=False, rerank=False,
    )
    assert [hit.path for hit in hits] == [parent]
    assert hits[0].scene_frame in [child[:-3] for child in children]
    assert "rankcatalogue latest" in hits[0].excerpt
    assert reads.count == 2, reads.report()


@library_scope()
def test_restricted_parent_does_not_replace_an_admitted_frame(
    vault: Path, warm_managed_cell
) -> None:
    from test_vocabulary_registries import _govern, _reset_governance

    parent = _write(vault, "Withheld/restricted-parent", "media_type: video\n", "Private transcript.")
    child = _write(vault, "public-frame", f"parent_media: {parent[:-3]}\nframe_ts: 2.5\n")
    _govern(vault)
    _reset_governance()
    warm_managed_cell(vault)
    with request_scope(RequestPrincipal("external", surface="mcp")):
        hits = find_module.find(
            vault, query="rankcatalogue latest", scope="kb-only", graph=False, rerank=False,
        )
    assert [hit.path for hit in hits] == [child]
    assert hits[0].scene_frame is None
