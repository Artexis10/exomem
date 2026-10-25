"""Managed ranking keeps its admitted generation without reading candidate pages."""

from __future__ import annotations

import json
import platform
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from recall_structural_counts import RequestCounts
from test_recall_integration import _PageReadSentinel

from exomem import find as find_module
from exomem import find_candidates, find_policy, find_results, lexstore
from exomem.governance.principal import library_scope
from exomem.ranking_config import DEFAULT_RANKING
from exomem.vault import kb_dirname


@pytest.mark.parametrize("principal", ["owner", "admitted"])
def test_public_recall_work_does_not_scale_with_unrelated_pages(
    tmp_path: Path, monkeypatch, principal: str, record_property,
) -> None:
    """Catch whole-catalogue SQL and candidate hydration through both public read commands."""
    from conftest import initialize_vault_state_offline
    from test_governance_egress import write_rule, write_scope

    from exomem import (
        bm25,
        commands,
        epistemic_graph,
        file_watcher,
        memory_refs,
        readiness,
        warmup,
        working_set_index,
    )
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
    from exomem.vault import walk_vault_md

    runtime = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version}
    runtime_evidence = json.dumps(runtime, sort_keys=True)
    record_property("execution_versions", runtime_evidence)
    print(runtime)
    root = tmp_path / "generated"
    root.mkdir()
    initialize_vault_state_offline(root, source="structural recall fixture")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    monkeypatch.setenv("EXOMEM_EAGER_BOOT", "1")
    monkeypatch.setenv("EXOMEM_MODEL_OFFLINE", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    write_scope(root, paths="**")
    write_rule(root, ceiling=6, audience="structural-reader")
    reader = owner_principal() if principal == "owner" else RequestPrincipal(
        audience_id="structural-reader", surface="mcp",
    )
    queries = {"hybrid": "rankcatalogue latest", "keyword": "rankcatalogue latest",
               "activate": "quillon vantry window"}
    matching = {_write(root, f"match-{index:03d}",
                       f"type: insight\nstatus: active\nupdated: 2026-10-{index % 20 + 1:02d}\n")
                for index in range(24)}
    parent = f"{kb_dirname()}/Notes/match-000"
    _write(root, "match-000", "type: source\nmedia_type: video\nupdated: 2026-10-20\n")
    _write(root, "match-001", f"type: source\nparent_media: {parent}\nframe_ts: 7.5\n"
           "updated: 2026-10-20\n")
    carried = _write(root, "quillon-vantry-window", "type: note\nstatus: active\n",
                     "# Quillon vantry window\n\n## Summary\n\n"
                     "- [decision] The quillon vantry window uses a copper latch. ^latch")
    expected = {"hybrid": matching, "keyword": matching, "activate": {carried}}
    counts = RequestCounts(root, monkeypatch)
    observations = {}
    for size in (400, 1600):
        start = 0 if size == 400 else 375
        stop = 375 if size == 400 else 1575
        for index in range(start, stop):
            # Other-script titles, paths, body and links share no query trigrams.
            relative = f"{kb_dirname()}/余白/資料{index:04d}.md"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            text = (f"---\ntitle: 資料{index:04d}\ntype: note\nstatus: active\n---\n"
                    f"静かな資料を保存する。[[余白/資料{(index + 1) % 375:04d}]]\n")
            assert all(token[offset:offset + 3] not in relative + text
                       for query in queries.values() for token in query.split()
                       for offset in range(len(token) - 2))
            path.write_text(text, encoding="utf-8")
        with library_scope():
            file_watcher.FileWatcher(root)._reconcile_once(seed=True)
            lexstore.ensure_fresh(root)
            readiness.manage_runtime()
            readiness.begin_warm()
            try:
                assert warmup.warm_retrieval_catalog(root)
            finally:
                readiness.finish_warm()
            assert readiness.retrieval_admission(root)["admitted"]
            memory_refs.ReferenceIndex(root).rebuild_all()
            epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
            warmup.prime_recall_resolver(root)
            index = working_set_index.WorkingSetIndex(root)
            try:
                index.rebuild()
            finally:
                index.close()
        # Prove the generated provider and stored catalogue agree before measurement.
        provider = {path.relative_to(root).as_posix() for path in walk_vault_md(root)}
        with sqlite3.connect(lexstore.get_store(root).path) as connection:
            stored = {row[0] for row in connection.execute("SELECT path FROM pages WHERE in_kb=1")}
        assert len(provider) == len(stored) == size
        assert provider == stored
        for operation, query in queries.items():
            with request_scope(reader):
                store = lexstore.get_store(root)
                terms = bm25.tokenize(query, query=True)
                matched = store.term_document_paths(terms, "kb", None, limit=size)
                assert matched.readiness.complete
                bm25_paths = set().union(*map(set, matched.value.values()))
                keyword = store.search_substring_result(query.split(), "kb", None)
                assert keyword.readiness.complete
                assert bm25_paths == set(keyword.value) == expected[operation]
                if operation == "keyword":
                    assert len(keyword.value) > 4
                def invoke(operation=operation, query=query):
                    if operation == "activate":
                        return commands.op_activate_context(root, turn=query, include_timings=True)
                    return commands.op_ask_memory(
                        root, query=query, mode=operation, scope="kb", result_level="page",
                        limit=4, graph=True, rerank=False, include_timings=True,
                    )

                # Maintained indexes alone do not warm the caller's application caches.
                # The result cache stays disabled so the measured call still executes its queries.
                assert "warming" not in invoke()
                with counts.request():
                    result = invoke()
                sample = counts.snapshot()
            assert "warming" not in result, result
            rows = result.get("units", []) if operation == "activate" else result.get("hits", [])
            paths = {row["provenance"]["path"] if operation == "activate" else row["path"]
                     for row in rows}
            assert paths, result
            sources = paths | {sample["sources"][path] for path in paths
                               if path in sample["sources"]}
            sources |= {sample["sources"][path] for path in tuple(sources)
                        if path in sample["sources"]}
            sample["selected"] = paths
            sample["matched"] = bm25_paths
            sample["read_bound"] = len(sources)
            sample["unique_page_reads"] = len(set(sample["read_paths"]))
            sample["outside_output_reads"] = sorted(
                set(sample["read_paths"]) - {str(root / path) for path in sources}
            )
            assert sample["steps"] and sample["connections"] and sample["proofs"]
            assert sample["candidates"], "the request must execute real candidate queries"
            observations[size, operation] = sample

    reports, failures = [], []
    for operation in queries:
        small, large = (observations[size, operation] for size in (400, 1600))
        assert small["matched"] == large["matched"]
        assert small["candidates"] == large["candidates"]
        assert small["selected"] == large["selected"]
        # Aggregate across selections, but retain their connection/proof rows in the report.
        steps = []
        for sample in (small, large):
            total = Counter()
            for (_selection, store, scope, stage), value in sample["steps"].items():
                total[store, scope, stage] += value
            steps.append(total)
        ratios = {}
        for key in steps[0].keys() | steps[1].keys():
            first, second = (value.get(key, 0) for value in steps)
            # Posting-row work is normalized even though these premises hold rows fixed.
            normalizer = len(small["matched"]) if key[2].endswith((
                "/_bm25_query", "/_substring_query", "/_keyword_groups_query")) else 1
            ratio = (second / normalizer) / (first / normalizer) if first else None
            ratios["|".join(key)] = ratio
            if ratio is None or ratio > 1.5:
                failures.append((operation, key, ratio))
        for size, sample in ((400, small), (1600, large)):
            if sample["outside_output_reads"]:
                failures.append((operation, size, "Markdown exceeds selected sources"))
        read_growth = large["page_reads"] / small["page_reads"] if small["page_reads"] else None
        if (read_growth is None and large["page_reads"]) or (read_growth is not None and read_growth > 1.5):
            failures.append((operation, "Markdown read growth", read_growth))
        reports.append({
            "operation": operation, "ratios": ratios, "page_read_ratio": read_growth,
            "sizes": {size: {
                "steps": {str(key): value for key, value in sample["steps"].items()},
                "connections": {str(key): value for key, value in sample["connections"].items()},
                "proofs": {str(key): value for key, value in sample["proofs"].items()},
                "totals": {"steps": sum(sample["steps"].values()),
                           "connections": sum(sample["connections"].values()),
                           "proofs": sum(sample["proofs"].values()),
                           "page_reads": sample["page_reads"]},
                "matched_rows": len(sample["matched"]),
                "unique_page_reads": sample["unique_page_reads"],
                "selected_sources": sample["read_bound"],
            } for size, sample in ((400, small), (1600, large))},
        })
    report = json.dumps({**runtime, "principal": principal, "bounds_gating": principal == "owner",
                         "bounds_exceeded": failures, "requests": reports}, sort_keys=True)
    record_property("structural_recall", report)
    print(report)
    # Decision 9 reports admitted bounds until admission stops sizing the candidate pool.
    if principal == "owner":
        assert not failures, report


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
@pytest.mark.parametrize("mode", ["hybrid", "keyword"])
def test_warm_page_recall_hydrates_only_output_after_corpus_growth(
    vault: Path, warm_managed_cell, monkeypatch, walk_sentinel, mode: str,
) -> None:
    """Temporal ranking and multipliers must not hydrate every lexical candidate."""
    for index in range(60):
        _write(vault, f"match-{index:03d}", f"type: insight\nupdated: 2026-10-{1 + index % 20:02d}\n")
    warm_managed_cell(vault)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    walks = walk_sentinel(vault, current_thread_only=True)
    kwargs = dict(query="rankcatalogue latest", scope="kb-only", limit=4, graph=False, rerank=False, mode=mode)
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
    keyword = find_module.find(
        vault, query="rankcatalogue latest", mode="keyword", scope="kb-only", graph=False,
    )
    assert [hit.path for hit in keyword] == [child]


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
@pytest.mark.parametrize("mode", ["hybrid", "keyword"])
def test_many_matching_frames_hydrate_only_the_selected_parent_and_frame(
    vault: Path, warm_managed_cell, monkeypatch, mode: str,
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
        vault, query="rankcatalogue latest", scope="kb-only", graph=False, rerank=False, mode=mode,
    )
    assert [hit.path for hit in hits] == [parent]
    assert hits[0].scene_frame in [child[:-3] for child in children]
    assert "rankcatalogue latest" in hits[0].excerpt
    assert reads.count == 2, reads.report()


@library_scope()
@pytest.mark.parametrize("parent_first", [True, False], ids=["parent-nearest-frame", "separate-children"])
def test_keyword_group_sources_keep_exhaustive_frame_precedence(
    vault: Path, warm_managed_cell, monkeypatch, parent_first: bool,
) -> None:
    from exomem import readiness, scene_frames

    video = f"{kb_dirname()}/Evidence/group.mp4"
    frame_dir = vault / (video + ".frames")
    frame_dir.mkdir(parents=True, exist_ok=True)
    for index, timestamp in enumerate((5.0, 75.0)):
        (frame_dir / scene_frames.frame_filename(index, timestamp)).write_bytes(b"frame")
    annotation = video + ".frames/" + scene_frames.frame_filename(1, 75.0)
    parent = _write(
        vault, "group-parent",
        f"media_type: video\nevidence_file: {video}\nupdated: 2026-10-02\n",
        "[00:05] groupneedle parent text." if parent_first else "Parent transcript.",
    )
    first = _write(
        vault, "group-first", f"parent_media: {parent[:-3]}\nupdated: 2026-10-01\n",
        "groupneedle first child excerpt.",
    )
    later = _write(
        vault, "group-later", f"parent_media: {parent[:-3]}\nevidence_file: {annotation}\n"
        "frame_ts: 75.0\nupdated: 2026-09-30\n", "groupneedle later child annotation.",
    )
    warm_managed_cell(vault)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    kwargs = dict(query="groupneedle", mode="keyword", scope="kb-only", graph=False, limit=1)
    with monkeypatch.context() as oracle:
        oracle.setattr(readiness, "runtime_managed", lambda: False)
        expected = [hit.as_dict() for hit in find_module.find(vault, **kwargs)]
    find_module.reset_page_and_result_caches()
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)

    actual = [hit.as_dict() for hit in find_module.find(vault, **kwargs)]

    assert actual == expected
    assert actual[0]["path"] == parent
    assert actual[0]["scene_match_at"] == ("0:05" if parent_first else "1:15")
    assert ("parent text" if parent_first else "first child excerpt") in actual[0]["excerpt"]
    assert reads.count == (1 if parent_first else 3), reads.report()
    assert {Path(path).relative_to(vault).as_posix() for path, _caller in reads.read} == (
        {parent} if parent_first else {parent, first, later}
    )


@library_scope()
def test_keyword_child_can_emit_an_authorized_parent_outside_the_raw_scope(
    vault: Path, warm_managed_cell,
) -> None:
    parent = "Reference/outside-video.md"
    path = vault / parent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nmedia_type: video\nupdated: 2026-10-01\n---\n\nParent transcript.\n")
    _write(vault, "inside-frame", f"parent_media: {parent[:-3]}\n", "outsideparentneedle child excerpt.")
    warm_managed_cell(vault)

    hits = find_module.find(vault, query="outsideparentneedle", mode="keyword", scope="kb-only", graph=False)

    assert [hit.path for hit in hits] == [parent]
    assert "child excerpt" in hits[0].excerpt


@library_scope()
@pytest.mark.parametrize("selected_parent", [False, True], ids=["unread-parent", "selected-parent"])
def test_keyword_parent_deletion_keeps_the_admitted_groups_without_refill(
    vault: Path, warm_managed_cell, monkeypatch, selected_parent: bool,
) -> None:
    from exomem import find_types

    parent = _write(
        vault, "drift-parent", f"updated: {'2026-12-01' if selected_parent else '2026-01-01'}\n",
        "Parent transcript.",
    )
    child = _write(
        vault, "drift-child", f"parent_media: {parent[:-3]}\nupdated: 2026-12-02\n",
        "groupdriftmarker child evidence.",
    )
    ordinary = _write(vault, "drift-ordinary", "updated: 2026-10-01\n", "groupdriftmarker ordinary evidence.")
    before = (vault / parent).read_bytes()
    warm_managed_cell(vault)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "16")
    store = lexstore.get_store(vault)
    select = store._keyword_groups_query
    changed = False

    def delete_after_selection(*args, **kwargs):
        nonlocal changed
        groups = select(*args, **kwargs)
        if not changed:
            changed = True
            (vault / parent).unlink()
        return groups

    monkeypatch.setattr(store, "_keyword_groups_query", delete_after_selection)
    reads = _PageReadSentinel(vault)
    reads.install(monkeypatch)
    degraded = []
    kwargs = dict(query="groupdriftmarker", mode="keyword", scope="kb-only", graph=False, limit=1)
    hits = find_module.find(vault, degraded_out=degraded, **kwargs)

    assert [hit.path for hit in hits] == ([] if selected_parent else [ordinary])
    assert all(Path(path).relative_to(vault).as_posix() != child for path, _caller in reads.read)
    if selected_parent:
        assert "recall_projection" in degraded
        (vault / parent).write_bytes(before)
        timings = find_types.FindTimings()
        recovered = find_module.find(vault, timings=timings, **kwargs)
        assert [hit.path for hit in recovered] == [parent]
        assert timings.cache["hit"] is False


@library_scope()
@pytest.mark.parametrize("changed_source", ["excerpt", "annotation"])
def test_cached_keyword_hit_checks_both_disclosed_child_sources(
    vault: Path, warm_managed_cell, monkeypatch, changed_source: str,
) -> None:
    from exomem import find_types

    parent = _write(vault, "cached-parent", "updated: 2026-10-03\n", "Parent transcript.")
    excerpt = _write(
        vault, "cached-excerpt", f"parent_media: {parent[:-3]}\nupdated: 2026-10-02\n",
        "cachechildmarker excerpt evidence.",
    )
    annotation = _write(
        vault, "cached-annotation", f"parent_media: {parent[:-3]}\nupdated: 2026-10-01\n"
        "evidence_file: Evidence/frame.jpg\nframe_ts: 75.0\n", "cachechildmarker annotation evidence.",
    )
    warm_managed_cell(vault)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "16")
    kwargs = dict(query="cachechildmarker", mode="keyword", scope="kb-only", graph=False, limit=1)
    first = find_module.find(vault, **kwargs)
    assert [hit.path for hit in first] == [parent]
    assert first[0].as_dict()["scene_frame"] == "Evidence/frame.jpg"
    assert "excerpt evidence" in first[0].excerpt

    path = vault / (excerpt if changed_source == "excerpt" else annotation)
    path.write_text(path.read_text() + "Later bytes without a corpus event.\n")
    timings = find_types.FindTimings()
    degraded = []
    second = find_module.find(vault, timings=timings, degraded_out=degraded, **kwargs)

    assert timings.cache["hit"] is True
    assert second == []
    assert "recall_projection" in degraded
    assert "evidence_sources" not in first[0].as_dict()


@library_scope()
@pytest.mark.parametrize("parent_exists", [False, True], ids=["missing-parent", "present-parent"])
def test_keyword_admission_denies_a_child_with_a_denied_referenced_parent(
    vault: Path, warm_managed_cell, parent_exists: bool,
) -> None:
    parent = f"{kb_dirname()}/Notes/denied-parent.md"
    if parent_exists:
        _write(vault, "denied-parent", body="Parent transcript.")
    _write(vault, "denied-child", f"parent_media: {parent[:-3]}\n", "deniedparentmarker child evidence.")
    warm_managed_cell(vault)

    hits = find_module.find(
        vault, query="deniedparentmarker", mode="keyword", scope="kb-only", graph=False,
        admit_path=lambda path: path != parent,
    )

    assert hits == []
