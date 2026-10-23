"""Retained recall selection across publication, provider work, and body changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import freshness, lexstore
from exomem.governance.principal import library_scope


@library_scope()
@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_explicit_python_backend_keeps_managed_recall_available(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The supported kill switch must keep serving through the Python fallback."""
    from exomem import find as find_module

    page = vault / "Knowledge Base/Notes/python-fallback.md"
    page.write_text("# Fallback\n\npythonfallbackmarker\n", encoding="utf-8")
    warm_managed_cell(vault)
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")

    hits = find_module.find(
        vault,
        query="pythonfallbackmarker",
        scope="kb-only",
        mode=mode,
        graph=False,
        rerank=False,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/python-fallback.md"]


@library_scope()
def test_fts5_absence_keeps_the_normal_table_catalogue_available(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing content ranking must not disable maintained unit metadata."""
    from exomem import find as find_module

    page = vault / "Knowledge Base/Notes/normal-table.md"
    page.write_text(
        "---\ntype: insight\nexomem_id: 8483e916-87d0-41a6-a0d7-f8c5f390dc02\n---\n\n"
        "# Metadata\n\n- [config] ordinary table evidence ^normal-table-unit\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    warm_managed_cell(vault)

    hits = find_module.find(
        vault,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters={"unit.category": {"$eq": "config"}},
    )

    assert [hit.source_anchor for hit in hits] == ["normal-table-unit"]


def test_stale_selection_keeps_existing_recovery_after_releasing_its_view(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving a valid stale projection must still trigger convergence."""
    from exomem import find as find_module

    page = vault / "Knowledge Base/Notes/stale-recovery.md"
    page.write_text("# Recovery\n\nstalerecoverymarker\n", encoding="utf-8")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    freshness.clear()
    rebuilt: list[bool] = []

    def recover(root, **_kwargs):
        from exomem import file_watcher

        assert root == vault.resolve()
        assert lexstore._admitted_catalog_view(vault) is None
        file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
        rebuilt.append(store.rebuild_atomic())

    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", recover)
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query="stalerecoverymarker",
        scope="kb-only",
        mode="keyword",
        degraded_out=degraded,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/stale-recovery.md"]
    assert "recall_projection" in degraded
    assert rebuilt == [True]
    assert lexstore.runtime_retrieval_catalog_proof(vault, schedule_repair=False) is not None


def test_changed_projected_inputs_cannot_borrow_the_live_checkpoint(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lineage reuse must not bless content that the retained catalogue lacks."""
    from exomem import find as find_module

    stable = vault / "Knowledge Base/Notes/lag-stable.md"
    stable.write_text("# Stable\n\nrealprojectionlag\n", encoding="utf-8")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    published = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    later = vault / "Knowledge Base/Notes/lag-later.md"
    later.write_text("# Later\n\nrealprojectionlag\n", encoding="utf-8")
    freshness.on_files_changed(vault, changed=[later])
    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", lambda _root, **_kwargs: None)
    lag: list[str] = []
    proof: dict[str, object] = {}

    hits = find_module.find(
        vault,
        query="realprojectionlag",
        scope="kb-only",
        mode="keyword",
        degraded_out=lag,
        catalog_proof_out=proof,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/lag-stable.md"]
    assert proof == published
    assert "recall_projection" in lag


@library_scope()
@pytest.mark.parametrize("result_level", ["unit", "page"])
def test_encoder_cutover_cannot_cache_old_serving_results_under_the_new_key(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, result_level: str
) -> None:
    """A pointer switch during encoding must degrade and leave the answer uncached."""
    from exomem import embeddings, find_types, index_paths
    from exomem import find as find_module

    page = vault / "Knowledge Base/Notes/encoding-cutover.md"
    page.write_text(
        "---\ntype: insight\nexomem_id: 24219f8c-8ac7-4e6a-905b-54663351f79c\n---\n\n"
        "# Cutover\n\n- [config] encodingcutovermarker ^cutover-unit\n",
        encoding="utf-8",
    )
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    old_path = index_paths.sidecar_path(vault)
    encodings: list[bool] = []
    searched: list[Path] = []

    def encode(_texts, *, is_query):
        assert is_query and lexstore._admitted_catalog_view(vault) is None
        encodings.append(True)
        index_paths.publish_active_sidecar(vault, index_paths.space_sidecar_name("next-space"))
        return [[1.0, 0.0]]

    def search(index, *_args, **_kwargs):
        searched.append(index.path)
        return []

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    monkeypatch.setattr(embeddings.EmbeddingIndex, "search", search)
    monkeypatch.setattr(embeddings.EmbeddingIndex, "search_semantic_units", search)
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query="encodingcutovermarker",
        scope="kb-only",
        result_level=result_level,
        graph=False,
        temporal=False,
        rerank=False,
        degraded_out=degraded,
    )

    assert hits
    assert index_paths.sidecar_path(vault) != old_path
    assert "embeddings" in degraded
    assert old_path not in searched
    timings = find_types.FindTimings()
    assert find_module.find(
        vault,
        query="encodingcutovermarker",
        scope="kb-only",
        result_level=result_level,
        graph=False,
        temporal=False,
        rerank=False,
        timings=timings,
    )
    assert encodings == [True, True]
    assert timings.cache["hit"] is False


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_data_publication_preserves_an_admitted_recall(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A normal publication must not cancel unrelated admitted evidence."""
    from exomem import find as find_module

    stable = vault / "Knowledge Base" / "Notes" / "publication-stable.md"
    stable.write_text("# Stable\n\npublicationmarker\n", encoding="utf-8")
    warm_managed_cell(vault)
    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", lambda *_a, **_kw: None)
    store = lexstore.get_store(vault)
    admitted = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    original = store._serve_from_ready_catalog_result
    published = False

    def publish_before_query(*args, **kwargs):
        nonlocal published
        if not published:
            later = vault / "Knowledge Base" / "Notes" / "publication-later.md"
            later.write_text("# Later\n\npublicationmarker\n", encoding="utf-8")
            freshness.on_files_changed(vault, changed=[later])
            for scope in freshness.SCOPES:
                delta = freshness.recall_delta_since(vault, scope, admitted[scope])
                assert delta.complete and not delta.requires_source_proof
                store.apply_catalog_delta(scope, delta)
                assert store.published_recall_checkpoint(scope) == delta.to
            published = True
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_serve_from_ready_catalog_result", publish_before_query)
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query="publicationmarker",
        scope="kb-only",
        mode=mode,
        graph=False,
        rerank=False,
        degraded_out=degraded,
    )

    assert published
    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/publication-stable.md"]
    assert "recall_projection" in degraded


@library_scope()
@pytest.mark.parametrize("evidence", ["page", "frame"])
def test_changed_selected_ranking_page_is_omitted_and_the_partial_answer_is_not_cached(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    """Metadata ranking must not let later selected bytes borrow an admitted hash."""
    from exomem import find as find_module
    from exomem import find_candidates, find_types

    changed = vault / "Knowledge Base/Notes/ranking-changed.md"
    stable = vault / "Knowledge Base/Notes/ranking-stable.md"
    for path in (changed, stable):
        path.write_text("# Ranked\n\nrankedbindingmarker\n", encoding="utf-8")
    source = changed
    if evidence == "frame":
        changed.write_text("---\nmedia_type: video\n---\n\nParent transcript.\n", encoding="utf-8")
        source = vault / "Knowledge Base/Notes/ranking-frame.md"
        source.write_text(
            "---\nparent_media: Knowledge Base/Notes/ranking-changed\n"
            "evidence_file: Knowledge Base/Notes/ranking-frame\nframe_ts: 4.5\n---\n\n"
            "rankedbindingmarker\n", encoding="utf-8",
        )
    before = source.read_bytes()
    warm_managed_cell(vault)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "16")
    original = find_candidates.collect_candidates
    altered = False

    def change_selected(*args, **kwargs):
        nonlocal altered
        bundle = original(*args, **kwargs)
        if not altered:
            altered = True
            source.write_bytes(before + b"Later canonical bytes.\n")
        return bundle

    monkeypatch.setattr(find_candidates, "collect_candidates", change_selected)
    kwargs = dict(query="rankedbindingmarker", scope="kb-only", graph=False, rerank=False)
    degraded = []
    hits = find_module.find(vault, degraded_out=degraded, **kwargs)
    assert [hit.path for hit in hits] == [stable.relative_to(vault).as_posix()]
    assert "recall_projection" in degraded

    source.write_bytes(before)
    timings = find_types.FindTimings()
    recovered = find_module.find(vault, timings=timings, **kwargs)
    assert {hit.path for hit in recovered} == {
        changed.relative_to(vault).as_posix(), stable.relative_to(vault).as_posix(),
    }
    assert timings.cache["hit"] is False


@pytest.mark.parametrize(
    "mode,query",
    [
        ("keyword", "hashbindingmarker"),
        ("hybrid", "hashbindingmarker"),
        ("keyword", ""),
    ],
)
def test_publication_omits_changed_canonical_evidence_without_blanking_recall(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, mode: str, query: str
) -> None:
    """A page whose tags change cannot borrow the admitted catalogue's old eligibility."""
    from exomem import find as find_module

    stable = vault / "Knowledge Base/Notes/hash-stable.md"
    changed = vault / "Knowledge Base/Notes/hash-changed.md"
    for path in (stable, changed):
        path.write_text(
            f"---\ntags: [selected]\n---\n\n# {path.stem}\n\nhashbindingmarker\n",
            encoding="utf-8",
        )
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    before = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    original = store._serve_from_ready_catalog_result
    published = False

    def publish_changed_page(*args, **kwargs):
        nonlocal published
        if not published:
            published = True
            changed.write_text(
                "---\ntags: [other]\n---\n\n# Changed\n\nhashbindingmarker NEWBYTES\n",
                encoding="utf-8",
            )
            freshness.on_files_changed(vault, changed=[changed])
            for scope in freshness.SCOPES:
                store.apply_catalog_delta(
                    scope, freshness.recall_delta_since(vault, scope, before[scope])
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_serve_from_ready_catalog_result", publish_changed_page)
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query=query,
        scope="kb-only",
        mode=mode,
        tags=["selected"],
        graph=False,
        degraded_out=degraded,
    )

    assert published
    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/hash-stable.md"]
    assert "recall_projection" in degraded


@library_scope()
@pytest.mark.parametrize("registered,exact", [(False, False), (True, False), (True, True)])
def test_changed_unit_parent_reduces_coverage_without_poisoning_the_cache(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, registered: bool, exact: bool
) -> None:
    """Missed events and published updates both preserve unaffected unit recall."""
    from exomem import find as find_module

    paths = [
        vault / "Knowledge Base/Notes/unit-stable.md",
        vault / "Knowledge Base/Notes/unit-changed.md",
    ]
    for index, path in enumerate(paths):
        path.write_text(
            "---\ntype: insight\n"
            f"exomem_id: 563a38ad-0867-4c9d-b0aa-7be0b535eb7{index}\n---\n\n"
            f"# Unit\n\n- [config] unitguardmarker ^{path.stem}\n",
            encoding="utf-8",
        )
    changed = paths[1]
    original_bytes = changed.read_bytes()
    warm_managed_cell(vault)
    filters = {"unit.category": {"$eq": "config"}} if exact else None
    prior_score = None
    if registered:
        monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
        baseline = find_module.find(
            vault,
            query="unitguardmarker",
            scope="kb-only",
            result_level="unit",
            filters=filters,
        )
        prior_score = next(hit.bm25_score for hit in baseline if hit.source_anchor == "unit-stable")
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *_args, **_kwargs: None)
    store = lexstore.get_store(vault)
    before = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    original = find_module._find_semantic_units
    altered = False

    def change_before_hydration(*args, **kwargs):
        nonlocal altered
        if not altered:
            altered = True
            changed.write_bytes(
                original_bytes.replace(b"unitguardmarker", b"unitguardmarker CHANGED")
            )
            if registered:
                later = vault / "Knowledge Base/Notes/unit-later.md"
                later.write_text(
                    "---\ntype: insight\nexomem_id: c936d7d6-6ff1-4c49-af9a-a809fd2b179a\n---\n\n"
                    "# Later\n\n- [config] unitguardmarker ^unit-later\n",
                    encoding="utf-8",
                )
                freshness.on_files_changed(vault, changed=[changed, later])
                for scope in freshness.SCOPES:
                    store.apply_catalog_delta(
                        scope, freshness.recall_delta_since(vault, scope, before[scope])
                    )
        return original(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_semantic_units", change_before_hydration)
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query="unitguardmarker",
        scope="kb-only",
        result_level="unit",
        filters=filters,
        degraded_out=degraded,
    )

    assert altered
    assert [hit.source_anchor for hit in hits] == ["unit-stable"]
    assert "recall_projection" in degraded
    if registered and not exact:
        assert hits[0].bm25_score == prior_score
    if not registered:
        changed.write_bytes(original_bytes)
        recovered = find_module.find(
            vault,
            query="unitguardmarker",
            scope="kb-only",
            result_level="unit",
        )
        assert {hit.source_anchor for hit in recovered} == {"unit-stable", "unit-changed"}


def test_hybrid_cache_miss_releases_the_catalogue_around_providers(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider work cannot hold publication back or reuse the discarded lookup."""
    from exomem import embeddings, find_types
    from exomem import find as find_module

    stable = vault / "Knowledge Base/Notes/provider-stable.md"
    stable.write_text("# Stable\n\nproviderboundary\n", encoding="utf-8")
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    store = lexstore.get_store(vault)
    calls: list[str] = []

    class Index:
        identity = None

        def search(self, *_args, **_kwargs):
            return [(stable.relative_to(vault).as_posix(), 0, "providerboundary", 0.8)]

    def encode(_texts, *, is_query):
        assert is_query
        assert lexstore._admitted_catalog_view(vault) is None
        later = vault / "Knowledge Base/Notes/provider-later.md"
        later.write_text("# Later\n\nproviderboundary\n", encoding="utf-8")
        freshness.on_files_changed(vault, changed=[later])
        assert store.rebuild_atomic() is True
        calls.append("encode")
        return [[1.0, 0.0]]

    def rerank(_query, passages):
        assert lexstore._admitted_catalog_view(vault) is None
        assert store.rebuild_atomic() is True
        calls.append("rerank")
        return [float(index) for index in range(len(passages))]

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: Index())
    monkeypatch.setattr(embeddings, "embed_texts", encode)
    monkeypatch.setattr(embeddings, "rerank_pairs", rerank)
    timings = find_types.FindTimings()
    proof: dict = {}
    hits = find_module.find(
        vault,
        query="providerboundary",
        scope="kb-only",
        graph=False,
        temporal=False,
        rerank=True,
        result_level="page",
        timings=timings,
        catalog_proof_out=proof,
    )

    assert calls == ["encode", "rerank"]
    assert {hit.path for hit in hits} == {
        "Knowledge Base/Notes/provider-stable.md",
        "Knowledge Base/Notes/provider-later.md",
    }
    assert proof == {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    assert timings.parent_conflicts() == {}
    assert lexstore._admitted_catalog_view(vault) is None


def test_main_unit_annotations_keep_the_selected_bytes_through_reranking(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later canonical edit cannot replace selected unit material during annotation."""
    from exomem import embeddings
    from exomem import find as find_module

    parent = vault / "Knowledge Base/Notes/annotation-source.md"
    authored = (
        "---\ntype: insight\nexomem_id: 2355362a-6a6c-4399-bb6a-58f278670660\n---\n\n"
        "# Annotation\n\n- [config] annotationmarker OLD-MATERIAL ^annotation-source\n"
    )
    parent.write_text(authored, encoding="utf-8")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    before = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}

    def rerank(_query, passages):
        assert lexstore._admitted_catalog_view(vault) is None
        parent.write_text(authored.replace("OLD-MATERIAL", "NEW-MATERIAL"), encoding="utf-8")
        freshness.on_files_changed(vault, changed=[parent])
        for scope in freshness.SCOPES:
            store.apply_catalog_delta(
                scope, freshness.recall_delta_since(vault, scope, before[scope])
            )
        return [1.0] * len(passages)

    monkeypatch.setattr(embeddings, "rerank_pairs", rerank)
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query="annotationmarker",
        result_level="page",
        scope="kb-only",
        graph=False,
        rerank=True,
        filters={"unit.category": {"$eq": "config"}},
        degraded_out=degraded,
    )

    assert len(hits) == 1
    assert len(hits[0].matched_units) == 1
    assert "OLD-MATERIAL" in hits[0].matched_units[0]["excerpt"]
    assert "NEW-MATERIAL" not in hits[0].matched_units[0]["excerpt"]
    assert "recall_projection" in degraded


@pytest.mark.parametrize(
    "catalogue_available", [True, False], ids=["managed-catalogue", "offline-held-only"],
)
def test_selected_pending_annotations_survive_global_retirement(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, catalogue_available: bool,
) -> None:
    """Retirement preserves held bytes and leading blanks, including without a catalogue."""
    from exomem import embeddings, pending_recall, readiness
    from exomem import find as find_module

    parent = vault / "Knowledge Base/Notes/pending-annotation.md"
    authored = (
        "---\ntype: insight\nexomem_id: 74e7c051-bfba-4781-b47f-c5eb59ab3ae2\n"
        "metadata: {priority: 3}\n---\n\n\n"
        "# Pending annotation\n\n- [config] BEFORE #held ^pending-annotation\n"
    )
    parent.write_text(authored, encoding="utf-8")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    before = {scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES}
    parent.write_text(authored.replace("BEFORE", "pendingannotationmarker HELD"), encoding="utf-8")
    page = find_module._CACHE.get(parent, vault)
    assert page.body.startswith("\n")
    overlay = pending_recall.PendingOverlay(
        "ready",
        None,
        1,
        {
            page.rel_path: pending_recall.PendingRow(
                page.rel_path,
                "pending-generation",
                "pending-batch",
                page.snapshot_hash,
                page,
                "exomem://memory/74e7c051-bfba-4781-b47f-c5eb59ab3ae2",
            )
        },
    )
    current = [overlay]
    monkeypatch.setattr(freshness, "recall_pending_coverage", lambda _root: current[0])
    filters = {"unit.category": {"$eq": "config"}}
    if not catalogue_available:
        monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
        monkeypatch.setattr(readiness, "runtime_managed", lambda: False)
        filters = {"$and": [
            {"unit.tags": {"$contains": "held"}},
            {"page.frontmatter:/metadata/priority": {"$eq": 3}},
        ]}

    def rerank(_query, passages):
        assert lexstore._admitted_catalog_view(vault) is None
        if catalogue_available:
            freshness.on_files_changed(vault, changed=[parent])
            for scope in freshness.SCOPES:
                store.apply_catalog_delta(
                    scope, freshness.recall_delta_since(vault, scope, before[scope])
                )
        else:
            parent.write_text(authored.replace("BEFORE", "pendingannotationmarker LATER"), encoding="utf-8")
        current[0] = pending_recall.PendingOverlay("ready", None, 2, {})
        return [1.0] * len(passages)

    monkeypatch.setattr(embeddings, "rerank_pairs", rerank)
    hits = find_module.find(
        vault,
        query="pendingannotationmarker",
        graph=False,
        rerank=True,
        result_level="page",
        filters=filters,
    )

    assert current[0].empty
    assert len(hits) == 1 and len(hits[0].matched_units) == 1
    assert "pendingannotationmarker HELD" in hits[0].matched_units[0]["excerpt"]


def test_delayed_widening_omits_bytes_changed_during_reranking(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening cannot fetch B under A's saved eligibility and hash."""
    from exomem import embeddings
    from exomem import find as find_module

    inside = vault / "Knowledge Base/Notes/widen-main.md"
    outside = vault / "Reference/widen-outside.md"
    outside.parent.mkdir(exist_ok=True)
    for path in (inside, outside):
        path.write_text("# Widen\n\nlatewidenmarker ORIGINAL\n", encoding="utf-8")
    warm_managed_cell(vault)

    def rerank(_query, passages):
        assert lexstore._admitted_catalog_view(vault) is None
        outside.write_text("# Changed\n\nlatewidenmarker NEWBYTES\n", encoding="utf-8")
        return [1.0] * len(passages)

    monkeypatch.setattr(embeddings, "rerank_pairs", rerank)
    degraded: list[str] = []
    hits = find_module.find(
        vault,
        query="latewidenmarker",
        graph=False,
        rerank=True,
        widen_outside_kb=True,
        degraded_out=degraded,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/widen-main.md"]
    assert "recall_projection" in degraded


@pytest.mark.parametrize("identity", ["policy", "semantic"])
def test_interpretation_changes_during_reranking_still_refuse(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    """A retained data view grants no continuing policy or interpretation authority."""
    from exomem import embeddings
    from exomem import find as find_module

    parent = vault / "Knowledge Base/Notes/identity-final.md"
    parent.write_text("# Identity\n\nfinalidentitymarker\n", encoding="utf-8")
    warm_managed_cell(vault)
    before = lexstore.catalog_semantic_identity(vault)

    def rerank(_query, passages):
        assert lexstore._admitted_catalog_view(vault) is None
        if identity == "policy":
            (vault / "Knowledge Base/_access.yaml").write_text(
                "excluded:\n  - Private\n",
                encoding="utf-8",
            )
        else:
            monkeypatch.setattr(
                lexstore, "catalog_semantic_identity", lambda _root: before + "-new"
            )
        return [1.0] * len(passages)

    monkeypatch.setattr(embeddings, "rerank_pairs", rerank)
    with pytest.raises(find_module.RetrievalIndexWarming) as failure:
        find_module.find(vault, query="finalidentitymarker", graph=False, rerank=True)
    assert failure.value.site == "catalog_proof_incomplete"


@library_scope()
def test_hot_hybrid_cache_does_not_prepare_models(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hot answer must not reload or encode a query before its cache lookup."""
    from exomem import embeddings, find_types
    from exomem import find as find_module

    stable = vault / "Knowledge Base/Notes/hot-provider.md"
    stable.write_text("# Stable\n\nhotproviderboundary\n", encoding="utf-8")
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    calls: list[str] = []

    class Index:
        identity = None

        def search(self, *_args, **_kwargs):
            return [(stable.relative_to(vault).as_posix(), 0, "hotproviderboundary", 0.8)]

    def get_index(_root):
        assert lexstore._admitted_catalog_view(vault) is None
        calls.append("index")
        return Index()

    def encode(_texts, *, is_query):
        assert is_query
        assert lexstore._admitted_catalog_view(vault) is None
        calls.append("encode")
        return [[1.0, 0.0]]

    monkeypatch.setattr(embeddings, "get_embedding_index", get_index)
    monkeypatch.setattr(embeddings, "embed_texts", encode)
    first = find_module.find(
        vault,
        query="hotproviderboundary",
        scope="kb-only",
        graph=False,
        temporal=False,
        rerank=False,
        result_level="page",
    )
    timings = find_types.FindTimings()
    second = find_module.find(
        vault,
        query="hotproviderboundary",
        scope="kb-only",
        graph=False,
        temporal=False,
        rerank=False,
        result_level="page",
        timings=timings,
    )

    assert calls == ["index", "encode"]
    assert [hit.as_dict() for hit in second] == [hit.as_dict() for hit in first]
    assert timings.cache["hit"] is True
    assert lexstore._admitted_catalog_view(vault) is None


@pytest.mark.parametrize("result_level", ["unit", "mixed"])
def test_managed_unit_and_page_lanes_prepare_one_query_outside_selection(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, result_level: str
) -> None:
    """Mixed retrieval must not encode again under its page or unit SQL view."""
    from exomem import embeddings
    from exomem import find as find_module

    parent = vault / "Knowledge Base/Notes/prepared-unit.md"
    parent.write_text(
        "---\ntype: insight\nexomem_id: 563a38ad-0867-4c9d-b0aa-7be0b535eb7e\n---\n\n"
        "# Prepared unit\n\n- [config] unitpreparationmarker ^prepared-unit\n",
        encoding="utf-8",
    )
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    calls: list[str] = []

    class Index:
        identity = None

        def search(self, *_args, **_kwargs):
            return [(parent.relative_to(vault).as_posix(), 0, "unitpreparationmarker", 0.8)]

        def search_semantic_units(self, *_args, **_kwargs):
            return []

    def encode(_texts, *, is_query):
        assert is_query
        assert lexstore._admitted_catalog_view(vault) is None
        calls.append("encode")
        return [[1.0, 0.0]]

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: Index())
    monkeypatch.setattr(embeddings, "embed_texts", encode)
    hits = find_module.find(
        vault,
        query="unitpreparationmarker",
        scope="kb-only",
        graph=False,
        temporal=False,
        rerank=False,
        result_level=result_level,
    )

    assert calls == ["encode"]
    assert any(getattr(hit, "source_anchor", None) == "prepared-unit" for hit in hits)
    assert lexstore._admitted_catalog_view(vault) is None


def test_mixed_recall_prepares_each_distinct_serving_and_build_space(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration may need two encoders; page preparation cannot borrow the unit space."""
    from exomem import embeddings, recall_migration, recall_space
    from exomem import find as find_module

    parent = vault / "Knowledge Base/Notes/build-space.md"
    parent.write_text(
        "---\ntype: insight\nexomem_id: 4c697c5e-e20a-42ed-a532-7a484f280c6f\n---\n\n"
        "# Build space\n\n- [config] buildspacemarker ^build-space\n",
        encoding="utf-8",
    )
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    encodings = []
    searches = []

    class Index:
        def __init__(self, model, dim):
            self.identity = recall_space.SpaceIdentity(model, None, dim)

        def encoding(self):
            assert lexstore._admitted_catalog_view(vault) is None
            return recall_space.selecting(self.identity.model)

        def search(self, vector, *_args, **_kwargs):
            searches.append((self.identity.model, len(vector)))
            return [(parent.relative_to(vault).as_posix(), 0, "buildspacemarker", 0.8)]

        def search_semantic_units(self, vector, *_args, **_kwargs):
            searches.append((self.identity.model, len(vector)))
            return []

        def file_mtimes(self, paths):
            return {path: (vault / path).stat().st_mtime for path in paths}

    serving = Index("serving-test-model", 2)
    building = Index("building-test-model", 3)

    def encode(_texts, *, is_query):
        assert is_query and lexstore._admitted_catalog_view(vault) is None
        model = recall_space.selected_model()
        encodings.append(model)
        return [[1.0] * (2 if model == serving.identity.model else 3)]

    def get_building(_root, _path):
        assert lexstore._admitted_catalog_view(vault) is None
        return building

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: serving)
    monkeypatch.setattr(embeddings, "get_building_index", get_building)
    monkeypatch.setattr(embeddings, "embed_texts", encode)
    monkeypatch.setattr(recall_migration, "building_sidecar", lambda _root: vault / "build.sqlite")
    hits = find_module.find(
        vault,
        query="buildspacemarker",
        scope="kb-only",
        graph=False,
        temporal=False,
        rerank=False,
        result_level="mixed",
    )

    assert encodings == [serving.identity.model, building.identity.model]
    assert searches == [(serving.identity.model, 2), (building.identity.model, 3)]
    assert any(getattr(hit, "source_anchor", None) == "build-space" for hit in hits)
    assert lexstore._admitted_catalog_view(vault) is None


@pytest.mark.parametrize("result_level", ["page", "unit"])
def test_prepared_dense_busy_keeps_its_existing_outcome(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, result_level: str
) -> None:
    """Moving dense preparation must not turn model contention into a lexical success."""
    from types import SimpleNamespace

    from exomem import embeddings, runtime_resources
    from exomem import find as find_module

    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    calls = []

    def encode(_texts, *, is_query):
        assert is_query and lexstore._admitted_catalog_view(vault) is None
        calls.append("encode")
        raise runtime_resources.ModelBusyError("model compute is busy")

    monkeypatch.setattr(
        embeddings, "get_embedding_index", lambda _root: SimpleNamespace(identity=None)
    )
    monkeypatch.setattr(embeddings, "embed_texts", encode)
    with pytest.raises(runtime_resources.ModelBusyError, match="model compute is busy"):
        find_module.find(
            vault,
            query="memory",
            scope="kb-only",
            graph=False,
            result_level=result_level,
        )

    assert calls == ["encode"]
    assert lexstore._admitted_catalog_view(vault) is None


@pytest.mark.parametrize("busy", [False, True])
def test_prepared_image_failure_keeps_its_existing_outcome(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    """Image dependency failure is optional; model contention retains its typed refusal."""
    from exomem import embeddings, runtime_resources
    from exomem import find as find_module

    parent = vault / "Knowledge Base/Notes/image-fallback.md"
    parent.write_text("# Image fallback\n\nimagefallbackmarker\n", encoding="utf-8")
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP")
    failure = (
        runtime_resources.ModelBusyError("model compute is busy")
        if busy
        else embeddings.ClipUnavailable("image encoder unavailable")
    )
    calls = []

    def encode(_query):
        assert lexstore._admitted_catalog_view(vault) is None
        calls.append("encode")
        raise failure

    class ImageIndex:
        def has_vectors(self):
            assert lexstore._admitted_catalog_view(vault) is None
            return True

    monkeypatch.setattr(embeddings, "get_clip_index", lambda _root: ImageIndex())
    monkeypatch.setattr(embeddings, "embed_clip_text", encode)
    failed: list[str] = []
    if busy:
        with pytest.raises(runtime_resources.ModelBusyError, match="model compute is busy"):
            find_module.find(
                vault,
                query="imagefallbackmarker",
                graph=False,
                failed_out=failed,
            )
    else:
        hits = find_module.find(
            vault,
            query="imagefallbackmarker",
            graph=False,
            failed_out=failed,
        )
        assert [hit.path for hit in hits] == ["Knowledge Base/Notes/image-fallback.md"]
        assert failed == ["clip"]

    assert calls == ["encode"]
    assert lexstore._admitted_catalog_view(vault) is None


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_selection_releases_its_catalogue(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    """Errors and cancellation must not leave a WAL reader blocking the publisher."""
    from exomem import find as find_module

    warm_managed_cell(vault)
    retained = []

    def fail_during_selection(*_args, **_kwargs):
        view = lexstore._admitted_catalog_view(vault)
        assert view is not None and view.connection is not None
        retained.append(view)
        raise failure("selection interrupted")

    monkeypatch.setattr(find_module, "_find_semantic", fail_during_selection)
    with pytest.raises(failure, match="selection interrupted"):
        find_module.find(vault, query="memory", scope="kb-only", graph=False)

    assert len(retained) == 1 and retained[0].connection is None
    assert lexstore._admitted_catalog_view(vault) is None
    assert lexstore.get_store(vault).rebuild_atomic() is True


def test_nested_recall_restores_its_callers_catalogue(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inner recall sees new data without replacing the outer selection."""
    from exomem import find as find_module

    stable = vault / "Knowledge Base/Notes/nested-stable.md"
    stable.write_text("# Stable\n\nnestedpublication\n", encoding="utf-8")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    original = store._serve_from_ready_catalog_result
    nested_hits = []
    published = False

    def publish_and_recall(*args, **kwargs):
        nonlocal published
        if not published:
            published = True
            before = dict(lexstore._admitted_catalog_view(vault).checkpoints)
            later = vault / "Knowledge Base/Notes/nested-later.md"
            later.write_text("# Later\n\nnestedpublication\n", encoding="utf-8")
            freshness.on_files_changed(vault, changed=[later])
            for scope in freshness.SCOPES:
                delta = freshness.recall_delta_since(vault, scope, before[scope])
                store.apply_catalog_delta(scope, delta)
            nested_hits.extend(
                find_module.find(
                    vault,
                    query="nestedpublication",
                    scope="kb-only",
                    mode="keyword",
                )
            )
            assert dict(lexstore._admitted_catalog_view(vault).checkpoints) == before
            assert lexstore.runtime_retrieval_catalog_proof(vault, schedule_repair=False) == {
                scope: store.published_recall_checkpoint(scope) for scope in freshness.SCOPES
            }
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_serve_from_ready_catalog_result", publish_and_recall)
    hits = find_module.find(
        vault,
        query="nestedpublication",
        scope="kb-only",
        mode="keyword",
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/nested-stable.md"]
    assert {hit.path for hit in nested_hits} == {
        "Knowledge Base/Notes/nested-stable.md",
        "Knowledge Base/Notes/nested-later.md",
    }
    assert lexstore._admitted_catalog_view(vault) is None


def test_copied_context_cannot_transfer_a_sqlite_view_to_another_thread(
    vault: Path, warm_managed_cell
) -> None:
    """A worker that inherits context must open its own connection rather than borrow A."""
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    parent = vault / "Knowledge Base/Notes/thread-source.md"
    parent.write_text("# Thread\n\nBEFORE\n", encoding="utf-8")
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)
    rel = parent.relative_to(vault).as_posix()
    with lexstore.admitted_catalog_scope(vault):
        with lexstore.catalog_selection_scope(vault) as admitted:
            before = store.page_content_hashes([rel])
            parent.write_text("# Thread\n\nAFTER\n", encoding="utf-8")
            freshness.on_files_changed(vault, changed=[parent])
            for scope in freshness.SCOPES:
                store.apply_catalog_delta(
                    scope, freshness.recall_delta_since(vault, scope, admitted.checkpoints[scope])
                )
            inherited = copy_context()
            with ThreadPoolExecutor(max_workers=1) as worker:
                after = worker.submit(inherited.run, store.page_content_hashes, [rel]).result(5)
            assert after[rel] is not None and after != before
            assert store.page_content_hashes([rel]) == before
