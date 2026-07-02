"""Tests for hybrid search (BM25 + vector + RRF).

Light tests (chunking, RRF math, sqlite roundtrip) run without the
embedding model. Heavy tests (real semantic recall, writer hooks)
import-skip when sentence-transformers/torch aren't available, and
explicitly re-enable embeddings since the suite-wide conftest disables
them by default.
"""

from __future__ import annotations

import numpy as np
import pytest

from exomem import bm25, embeddings, fusion
from exomem import find as find_module

# ============================================================================
# Light tests — no model load
# ============================================================================


def test_chunk_text_splits_on_blank_lines_and_prepends_title() -> None:
    body = "First paragraph.\n\nSecond paragraph.\n\n\nThird paragraph."
    chunks = embeddings.chunk_text("My Doc", body)
    assert chunks == [
        "My Doc\n\nFirst paragraph.",
        "My Doc\n\nSecond paragraph.",
        "My Doc\n\nThird paragraph.",
    ]


def test_chunk_text_drops_empty_and_handles_no_body() -> None:
    assert embeddings.chunk_text("Title", "") == ["Title"]
    assert embeddings.chunk_text("", "  \n\n  ") == []
    assert embeddings.chunk_text("Title", "real\n\n   \n\nreal2") == [
        "Title\n\nreal",
        "Title\n\nreal2",
    ]


def test_chunk_text_truncates_long_paragraphs_at_word_boundary() -> None:
    long_para = " ".join(["word"] * 1000)
    chunks = embeddings.chunk_text("T", long_para)
    assert len(chunks) == 1
    # Title + 350 "word"s. Word count of the body part (after title prefix):
    body_words = chunks[0].split("\n\n", 1)[1].split()
    assert len(body_words) == embeddings.MAX_WORDS_PER_CHUNK


def test_rrf_known_ranks() -> None:
    # list A: [a, b, c] → ranks 1,2,3
    # list B: [c, b]    → ranks 1,2
    fused = fusion.reciprocal_rank_fusion([["a", "b", "c"], ["c", "b"]], k=60)
    # c: 1/63 + 1/61 ≈ 0.0323
    # b: 1/62 + 1/62 ≈ 0.0323 (a touch lower than c)
    # a: 1/61          ≈ 0.0164
    assert [p for p, _ in fused] == ["c", "b", "a"]
    cs = dict(fused)
    assert cs["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert cs["b"] == pytest.approx(2 / 62)
    assert cs["a"] == pytest.approx(1 / 61)


def test_rrf_dedupes_within_a_single_list() -> None:
    # Only the first occurrence in each list counts. Subsequent positions
    # are not compressed — "a" at position 2 is ignored, "b" stays at rank 3.
    fused = fusion.reciprocal_rank_fusion([["a", "a", "b"]], k=60)
    cs = dict(fused)
    assert cs["a"] == pytest.approx(1 / 61)
    assert cs["b"] == pytest.approx(1 / 63)


def test_audit_flags_embedding_drift(tmp_path) -> None:
    """When file mtime advances past the sidecar row's mtime, audit should flag it."""
    from exomem import audit as audit_module
    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "_Schema").mkdir(parents=True)
    # Minimal SKILL.md so resolve_vault wouldn't reject (we call audit directly).
    (vault / "Knowledge Base" / "_Schema" / "SKILL.md").write_text(
        "---\nname: knowledge-base\n---\n", encoding="utf-8"
    )
    page = vault / "Knowledge Base" / "Notes" / "Insights" / "probe.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-05-28\nupdated: 2026-05-28\ntags: []\n---\n\n# Probe\n\nbody\n",
        encoding="utf-8",
    )
    # Seed the sidecar with a row whose file_mtime is in the past.
    idx = embeddings.EmbeddingIndex(vault)
    fake_vec = np.zeros((1, embeddings.VECTOR_DIM), dtype=np.float32)
    idx.upsert_file(
        "Knowledge Base/Notes/Insights/probe.md",
        ["chunk"], fake_vec, mtime=0.0,
    )
    report = audit_module.audit(vault, categories=["embedding_drift"])
    paths = [f.path for f in report.findings if f.category == "embedding_drift"]
    assert any("probe.md" in p for p in paths), (
        f"drift should be flagged; got findings {report.findings}"
    )


def test_embedding_index_sqlite_roundtrip(tmp_path) -> None:
    """Exercise the sidecar without loading the actual model — fabricated vectors."""
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    idx = embeddings.EmbeddingIndex(vault)
    # Fake unit-norm vectors. Three files, three chunks total.
    v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    v3 = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    # Padding to a real 768-dim vector — sidecar doesn't actually enforce the
    # dimension at the schema level; search() will reshape from the blob.
    def pad(v):
        out = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
        out[: len(v)] = v
        return out
    idx.upsert_file("a.md", ["chunk-a"], np.stack([pad(v1)]), 1.0)
    idx.upsert_file("b.md", ["chunk-b1", "chunk-b2"], np.stack([pad(v2), pad(v3)]), 2.0)

    metadata, matrix = idx.all_vectors()
    assert len(metadata) == 3
    assert matrix.shape == (3, embeddings.VECTOR_DIM)

    # Query matching the first vector → "a.md" wins.
    hits = idx.search(pad(v1), k=2)
    assert hits[0][0] == "a.md"
    assert hits[0][3] == pytest.approx(1.0)

    # Delete b.md → only a.md remains.
    idx.delete_file("b.md")
    metadata, matrix = idx.all_vectors()
    assert [m[0] for m in metadata] == ["a.md"]


def test_keyword_mode_backward_compat(vault) -> None:
    """`find(mode="keyword")` must produce the original sort + filter behaviour."""
    # Same expectation as test_find.test_query_substring_hits_body, pinned to
    # mode="keyword" so we can prove that path stayed intact.
    hits = find_module.find(vault, query="EGCG", mode="keyword")
    assert any("egcg" in h.path.lower() for h in hits)
    egcg = next(h for h in hits if "egcg" in h.path.lower())
    assert egcg.type == "source"

    # Keyword mode sorts by `updated:` desc — assert that explicitly.
    hits = find_module.find(vault, query="metabolism", mode="keyword")
    updated_values = [h.updated for h in hits if h.updated]
    assert updated_values == sorted(updated_values, reverse=True)


def test_invalid_mode_raises(vault) -> None:
    with pytest.raises(ValueError, match="mode must be"):
        find_module.find(vault, query="x", mode="bogus")


def test_bm25_search_smoke(vault) -> None:
    """BM25 over fixture vault returns sensible top-k for a content keyword.

    Picks a query term with a small enough document frequency for IDF to be
    positive on the 14-doc fixture corpus. With BM25Okapi, terms that appear
    in roughly half the corpus get IDF≈0 (and thus score 0) — that's an
    artefact of the small fixture, not the algorithm; on the real 600-file
    vault, "metabolism" is plenty discriminative.
    """
    bm25.clear_cache()
    hits = bm25.search(vault, "insulin", k=5)
    assert hits, "BM25 returned no hits for 'insulin'"
    # `metabolic-literacy-curriculum.md` mentions insulin in its body.
    assert any("metabolic-literacy" in p for p, _ in hits)


def test_bm25_corpus_is_stemmed(vault) -> None:
    """BM25 tokenization runs Snowball — query "compounding" matches "compound"."""
    from exomem import create_file as create_file_module
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Notes/Insights/probe-stem-compound.md",
        content="# Probe stem compound\n\nThis page mentions the word compound exactly once and nothing else lexically tied to the query.",
        frontmatter={
            "type": "insight",
            "status": "active",
            "created": "2026-05-28",
            "updated": "2026-05-28",
            "tags": [],
        },
    )
    bm25.clear_cache()
    find_module.clear_cache()
    hits = bm25.search(vault, "compounding", k=5)
    assert any("probe-stem-compound" in p for p, _ in hits), (
        f"stemmed corpus should let 'compounding' match a page with only "
        f"'compound'; got {hits}"
    )


def _write_md(path, body: str) -> None:
    """Drop a minimal compiled note straight onto disk (no writer side effects
    like index refresh), so token-count assertions see exactly one new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-06-27\n"
        f"updated: 2026-06-27\ntags: []\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_bm25_incremental_build_only_tokenizes_changed_doc(vault) -> None:
    """After a single new write, the rebuild re-tokenizes ONLY the new doc.

    The whole point of the per-doc token cache: a write (which advances the
    vault's max mtime and so forces the next search to rebuild) must not
    re-stem the entire corpus — only the document that changed.
    """
    import os
    import time

    bm25.clear_cache()
    find_module.clear_cache()
    # Cold build over the whole fixture corpus.
    bm25.search(vault, "insulin", k=5)
    cold = bm25._INDEX.last_tokenized
    assert cold > 1, f"cold build should tokenize the whole corpus; got {cold}"
    assert bm25._INDEX.last_reused == 0, "nothing to reuse on a cold build"

    # Add ONE new note. Force its mtime past every fixture file so the next
    # search() definitely rebuilds (avoids any filesystem-resolution ambiguity).
    probe = vault / "Knowledge Base" / "Notes" / "Insights" / "probe-incremental-one.md"
    _write_md(probe, "# Probe incremental one\n\nA brand new note about insulin and glucose.")
    future = time.time() + 10_000
    os.utime(probe, (future, future))

    bm25.search(vault, "insulin", k=5)
    assert bm25._INDEX.last_tokenized == 1, (
        f"rebuild after one write should re-tokenize only the new doc; "
        f"got {bm25._INDEX.last_tokenized}"
    )
    # Every original doc came from the token cache instead of being re-stemmed.
    assert bm25._INDEX.last_reused == cold, (
        f"all {cold} original docs should be reused; got {bm25._INDEX.last_reused}"
    )


def test_bm25_edit_retokenizes_only_changed_file(vault) -> None:
    """Editing one existing file re-tokenizes only that file, not the corpus.

    Bumps the file's mtime explicitly via os.utime so the assertion doesn't
    depend on the filesystem's mtime resolution (a real edit advances mtime
    too — that's the cache key).
    """
    import os
    import time

    bm25.clear_cache()
    find_module.clear_cache()
    bm25.search(vault, "insulin", k=5)  # cold build

    kb = vault / "Knowledge Base"
    target = next(find_module._walk_md(kb))  # any indexed file
    future = time.time() + 10_000
    os.utime(target, (future, future))

    bm25.search(vault, "insulin", k=5)
    assert bm25._INDEX.last_tokenized == 1, (
        f"editing one file should re-tokenize only it; "
        f"got {bm25._INDEX.last_tokenized}"
    )


def test_bm25_cached_tokens_match_fresh_tokenization(vault) -> None:
    """Ranking parity: cached-token scores equal cold-rebuild scores exactly.

    Cached tokens are byte-identical to freshly stemmed tokens, so the BM25
    corpus — and therefore every score — must be identical whether a doc was
    reused from cache or re-tokenized from scratch. This is the deterministic
    parity proof the handoff asks for (stronger than NDCG drift on a golden set).
    """
    import os
    import time

    bm25.clear_cache()
    find_module.clear_cache()
    bm25.search(vault, "insulin", k=50)  # cold over the original corpus

    probe = vault / "Knowledge Base" / "Notes" / "Insights" / "probe-parity.md"
    _write_md(probe, "# Probe parity\n\nGlucose metabolism and insulin sensitivity.")
    future = time.time() + 10_000
    os.utime(probe, (future, future))

    # Incremental path: originals reused from cache, only the new doc stemmed.
    incremental = bm25.search(vault, "insulin", k=50)
    assert bm25._INDEX.last_tokenized == 1

    # Cold path: same file set, but every doc tokenized fresh.
    bm25.clear_cache()
    cold = bm25.search(vault, "insulin", k=50)
    assert bm25._INDEX.last_tokenized > 1

    assert incremental == cold, (
        "cached-token ranking must equal fresh-token ranking exactly"
    )


def test_stem_aware_gate_recovers_morphological_match(vault) -> None:
    """A page that uses 'regulator' should be reachable via 'regulation'.

    Probes the BM25-only stem-aware gate in _find_semantic. Vector results
    are disabled (EXOMEM_DISABLE_EMBEDDINGS), so the only way this page
    reaches the result set is via BM25 (also stemmed) + the stem-aware
    all-tokens-present check.
    """
    from exomem import create_file as create_file_module
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Notes/Insights/probe-stem-regulator.md",
        content=(
            "# Probe stem regulator\n\n"
            "The thyroid acts as a regulator of basal metabolism. "
            "Without that regulator, downstream tissues drift."
        ),
        frontmatter={
            "type": "insight",
            "status": "active",
            "created": "2026-05-28",
            "updated": "2026-05-28",
            "tags": [],
        },
    )
    bm25.clear_cache()
    find_module.clear_cache()
    hits = find_module.find(vault, query="regulator metabolism", mode="hybrid", limit=10)
    assert any("probe-stem-regulator" in h.path for h in hits), (
        "literal substring; sanity check failed"
    )
    hits = find_module.find(vault, query="regulation metabolism", mode="hybrid", limit=10)
    assert any("probe-stem-regulator" in h.path for h in hits), (
        "morphological match via stem-aware gate failed"
    )

    # Keyword mode must stay strict — should NOT find the page on 'regulation'.
    hits = find_module.find(vault, query="regulation metabolism", mode="keyword", limit=10)
    assert not any("probe-stem-regulator" in h.path for h in hits), (
        "keyword mode is supposed to be strict-substring; do not stem"
    )


def test_hit_signals_populated_in_hybrid(vault) -> None:
    """Hybrid mode should tag each hit with bm25_rank / vector_rank / etc."""
    bm25.clear_cache()
    find_module.clear_cache()
    hits = find_module.find(vault, query="insulin", mode="hybrid", limit=5)
    assert hits
    # At least one hit should carry a bm25_rank (we know BM25 finds 'insulin'
    # on the fixture). Vector ranks may be None if embeddings disabled.
    assert any(h.bm25_rank is not None for h in hits)
    d = hits[0].as_dict()
    # When signals are present, they're under "signals" key; keyword-mode
    # hits would omit it.
    if hits[0].bm25_rank is not None:
        assert "signals" in d
        assert d["signals"].get("bm25_rank") == hits[0].bm25_rank


def test_hit_signals_omitted_in_keyword(vault) -> None:
    """Keyword-mode hits must not carry the signals key (backward compat)."""
    hits = find_module.find(vault, query="EGCG", mode="keyword", limit=3)
    assert hits
    for h in hits:
        assert h.bm25_rank is None
        assert h.vector_rank is None
        assert "signals" not in h.as_dict()


def test_compiled_types_include_production_log_and_experiment() -> None:
    """production-log and experiment should boost the same as research-note etc."""
    assert "production-log" in find_module._COMPILED_TYPES
    assert "experiment" in find_module._COMPILED_TYPES
    # Sanity: the multiplier helper returns the boost for both.
    assert find_module._type_multiplier("production-log") == find_module._COMPILED_BOOST
    assert find_module._type_multiplier("experiment") == find_module._COMPILED_BOOST
    # Sources still get the penalty.
    assert find_module._type_multiplier("source") == find_module._SOURCE_PENALTY


def test_prefer_compiled_reorders_above_source(vault, source_schema) -> None:
    """Equal-scoring source vs insight should put the insight first when
    prefer_compiled=True, and either order is acceptable when False.

    Builds a probe pair: matching content on a `source` and an `insight`
    so BM25 scores them similarly. The type-weight boost is the only
    signal that can break the tie.
    """
    from exomem import add as add_module
    from exomem import create_file as create_file_module

    probe_body = "The distinctivetypeprobe word appears here exactly once."
    # Source via add() (Sources/ is append-only — must use the typed writer).
    add_module.add(
        vault, source_schema,
        content=probe_body,
        source_type="article",
        title="Probe distinctivetypeprobe source",
        url="https://example.com/probe-source",
    )
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Notes/Insights/probe-distinctivetypeprobe-insight.md",
        content=f"# Probe distinctivetypeprobe insight\n\n{probe_body}",
        frontmatter={
            "type": "insight",
            "status": "active",
            "created": "2026-05-28",
            "updated": "2026-05-28",
            "tags": [],
        },
    )
    bm25.clear_cache()
    find_module.clear_cache()

    hits_on = find_module.find(
        vault, query="distinctivetypeprobe", mode="hybrid",
        prefer_compiled=True, limit=5,
    )
    types_on = [h.type for h in hits_on if "distinctivetypeprobe" in h.path]
    # The insight must rank above (or equal to first occurrence of) the source.
    assert types_on, f"probes not surfaced: {[h.path for h in hits_on]}"
    insight_pos = next((i for i, t in enumerate(types_on) if t == "insight"), -1)
    source_pos = next((i for i, t in enumerate(types_on) if t == "source"), -1)
    assert insight_pos != -1 and source_pos != -1, (
        f"both types should surface; got {types_on}"
    )
    assert insight_pos < source_pos, (
        f"insight should rank above source with prefer_compiled=True; "
        f"got order {types_on}"
    )

    hits_off = find_module.find(
        vault, query="distinctivetypeprobe", mode="hybrid",
        prefer_compiled=False, limit=5,
    )
    # With boost off, fused order is the raw RRF — we don't assert a specific
    # order (depends on tie-breaking), only that both still surface.
    paths_off = {h.path for h in hits_off}
    assert any("source.md" in p for p in paths_off)
    assert any("insight.md" in p for p in paths_off)


def test_keyword_rank_populated_in_hybrid(vault) -> None:
    """Pages that pass keyword's all-tokens-present gate should carry keyword_rank.

    `EGCG` is a single-token query — every page that contains it as a
    substring is a keyword match. Hybrid mode must surface those with
    keyword_rank set, alongside whatever BM25/vector ranks they got.
    """
    bm25.clear_cache()
    find_module.clear_cache()
    hits = find_module.find(vault, query="EGCG", mode="hybrid", limit=10)
    assert hits
    with_kw = [h for h in hits if h.keyword_rank is not None]
    assert with_kw, (
        f"hybrid should surface keyword-matched paths with keyword_rank; "
        f"got hits {[(h.path, h.bm25_rank, h.vector_rank, h.keyword_rank) for h in hits]}"
    )
    # First keyword-ranked hit's signals dict should include keyword_rank.
    d = with_kw[0].as_dict()
    assert "signals" in d and d["signals"].get("keyword_rank") == with_kw[0].keyword_rank


def test_hybrid_is_strict_superset_of_keyword(vault) -> None:
    """Recall-floor invariant: hybrid never returns fewer paths than keyword.

    The motivating regression: BM25 + vector can bury a literal match under
    thematically-noisy hits, and keyword would surface it while hybrid
    dropped it. Adding keyword as a fourth ranker guarantees this can't
    happen — for any query, hybrid_paths ⊇ keyword_paths.
    """
    queries = ["EGCG", "metabolism", "engine", "insulin", "Lovelace"]
    for q in queries:
        bm25.clear_cache()
        find_module.clear_cache()
        # Match limits so the comparison is fair (hybrid limit constrains
        # results just like keyword's).
        kw = find_module.find(vault, query=q, mode="keyword", limit=20)
        hy = find_module.find(vault, query=q, mode="hybrid", limit=20)
        kw_paths = {h.path for h in kw}
        hy_paths = {h.path for h in hy}
        missing = kw_paths - hy_paths
        assert not missing, (
            f"hybrid missing keyword matches for {q!r}: {missing}"
        )


def test_graph_in_degree_counts_inbound_from_seeds(vault) -> None:
    """A page wikilinked from multiple strong matches should expose graph_in_degree.

    Note: graph_in_degree is non-zero ONLY when the seed pages are matched
    (vector OR stem-gated BM25). Builds three pages all linking to one hub
    plus mentioning a distinctive query term — the hub then has in-degree 3.
    """
    from exomem import create_file as create_file_module
    hub_path = "Knowledge Base/Notes/Insights/probe-indegree-hub.md"
    create_file_module.create_file(
        vault,
        path=hub_path,
        content="# Probe in-degree hub\n\nGenuinely unrelated content.",
        frontmatter={
            "type": "insight", "status": "active",
            "created": "2026-05-28", "updated": "2026-05-28", "tags": [],
        },
    )
    for i in (1, 2, 3):
        create_file_module.create_file(
            vault,
            path=f"Knowledge Base/Notes/Insights/probe-indegree-seed-{i}.md",
            content=(
                f"# Probe in-degree seed {i}\n\n"
                f"Contains the rareindegreemarker token. "
                f"See [[Knowledge Base/Notes/Insights/probe-indegree-hub]]."
            ),
            frontmatter={
                "type": "insight", "status": "active",
                "created": "2026-05-28", "updated": "2026-05-28", "tags": [],
            },
        )
    bm25.clear_cache()
    find_module.clear_cache()
    find_module._RESOLVER_CACHE.clear()

    hits = find_module.find(
        vault, query="rareindegreemarker", mode="hybrid", limit=10,
    )
    paths_by_basename = {h.path.rsplit("/", 1)[-1]: h for h in hits}
    # The seeds should all surface (they each contain the unique token).
    assert all(f"probe-indegree-seed-{i}.md" in paths_by_basename for i in (1, 2, 3))
    # And the hub should appear via graph expansion (or as a graph-in-degree
    # mention on a primary hit).
    hub_hit = paths_by_basename.get("probe-indegree-hub.md")
    if hub_hit is not None:
        # Hub came in via graph_ranking (it has no lexical match).
        assert hub_hit.graph_hop, "hub should be tagged graph_hop=True"
        assert hub_hit.graph_in_degree == 3, (
            f"hub should be linked from all 3 seeds, got "
            f"{hub_hit.graph_in_degree}"
        )
    # If the hub didn't make the top-10 (possible under aggressive limits),
    # at least one of the seeds should NOT carry a graph_in_degree marker —
    # in-degree on a primary hit only fires when other seeds link in. None do
    # in this fixture, so seed in-degree is 0. That's a sanity check.
    for i in (1, 2, 3):
        seed_hit = paths_by_basename[f"probe-indegree-seed-{i}.md"]
        assert seed_hit.graph_in_degree == 0


def test_graph_expansion_surfaces_linked_neighbour(vault) -> None:
    """A page linked from a query match should come back with graph_hop=True.

    Builds a tiny graph: probe-graph-anchor mentions "specific anchor token";
    probe-graph-neighbour shares no query token but is wikilinked from
    -anchor. Querying for "anchor" should surface -neighbour via graph hop.
    """
    from exomem import create_file as create_file_module
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Notes/Insights/probe-graph-neighbour.md",
        content="# Probe graph neighbour\n\nNo lexical overlap with the query at all.",
        frontmatter={
            "type": "insight",
            "status": "active",
            "created": "2026-05-28",
            "updated": "2026-05-28",
            "tags": [],
        },
    )
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Notes/Insights/probe-graph-anchor.md",
        content=(
            "# Probe graph anchor\n\n"
            "Specific anchor token used here for the query. See also "
            "[[Knowledge Base/Notes/Insights/probe-graph-neighbour]]."
        ),
        frontmatter={
            "type": "insight",
            "status": "active",
            "created": "2026-05-28",
            "updated": "2026-05-28",
            "tags": [],
        },
    )
    bm25.clear_cache()
    find_module.clear_cache()
    # Drop the resolver cache so the new files are visible.
    find_module._RESOLVER_CACHE.clear()
    hits = find_module.find(vault, query="specific anchor token", mode="hybrid",
                            graph=True, limit=10)
    paths = [h.path for h in hits]
    assert any("probe-graph-anchor" in p for p in paths), (
        f"anchor not in {paths}"
    )
    neighbour_hit = next(
        (h for h in hits if "probe-graph-neighbour" in h.path), None,
    )
    assert neighbour_hit is not None, (
        f"graph expansion should surface the linked neighbour; got {paths}"
    )
    assert neighbour_hit.graph_hop, (
        "neighbour should be tagged graph_hop=True (it isn't in bm25 or vector)"
    )

    # graph=False should NOT surface the neighbour.
    hits_no_graph = find_module.find(
        vault, query="specific anchor token", mode="hybrid", graph=False, limit=10,
    )
    assert not any("probe-graph-neighbour" in h.path for h in hits_no_graph)


# ============================================================================
# Heavy tests — load bge model. Gated by importorskip + env-var override.
# ============================================================================


pytest.importorskip("sentence_transformers")
pytest.importorskip("torch")


@pytest.fixture
def embeddings_enabled(monkeypatch):
    """Lift the conftest-wide EXOMEM_DISABLE_EMBEDDINGS gate for these tests."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    # Reset module-level import-failed flag in case earlier tests tripped it.
    embeddings._IMPORT_FAILED = False


def test_embed_query_and_passage_shapes(embeddings_enabled) -> None:
    qvec = embeddings.embed_texts(["metabolic health"], is_query=True)
    pvec = embeddings.embed_texts(["insulin sensitivity matters"], is_query=False)
    assert qvec.shape == (1, embeddings.VECTOR_DIM)
    assert pvec.shape == (1, embeddings.VECTOR_DIM)
    # Unit-norm after normalize_embeddings=True
    assert float(np.linalg.norm(qvec[0])) == pytest.approx(1.0, abs=1e-3)
    assert float(np.linalg.norm(pvec[0])) == pytest.approx(1.0, abs=1e-3)


def test_writer_updates_sidecar(vault, embeddings_enabled) -> None:
    """Calling note() should land chunks for the new file in the sidecar."""
    from exomem import note as note_module

    note_module.note(
        vault,
        content=(
            "# Glycemic variability and morning fog\n\n"
            "Postprandial spikes correlate with reduced clarity the following hour. "
            "n=1 over six weeks; no controls."
        ),
        note_type="insight",
        title="Glycemic variability and morning fog",
    )
    idx = embeddings.EmbeddingIndex(vault)
    metadata, matrix = idx.all_vectors()
    rel_paths = {m[0] for m in metadata}
    # The new insight should appear in the sidecar.
    assert any("glycemic-variability" in p for p in rel_paths), (
        f"new note not embedded; sidecar rows: {rel_paths}"
    )


def test_rerank_reorders_top_k(vault, embeddings_enabled) -> None:
    """rerank=True should at minimum populate Hit.rerank_score.

    Reranker model loads on first call (~30s). We assert reranker SCORES are
    attached, not a specific ordering — the relative ordering depends on
    bge-reranker-base's training, which is opaque to test for content this
    small. Smoke-level confidence is enough.
    """
    from exomem import audit_fix as audit_fix_module
    audit_fix_module.audit_fix(vault, rebuild_embeddings=True)
    hits = find_module.find(
        vault, query="metabolic disease", mode="hybrid",
        rerank=True, limit=5,
    )
    assert hits
    # At least one hit should carry a reranker score (None means the rerank
    # step was skipped or failed).
    assert any(h.rerank_score is not None for h in hits), (
        "rerank=True should populate rerank_score on at least one hit"
    )
    # Scores attached → they should be reflected in ordering: top hit has the
    # max rerank_score (after filtering out None).
    scored = [h for h in hits if h.rerank_score is not None]
    if len(scored) > 1:
        assert scored[0].rerank_score >= max(h.rerank_score for h in scored)


def test_hybrid_finds_semantic_match_keyword_misses(
    vault, embeddings_enabled
) -> None:
    """A natural-language query reaches a page whose body uses different words."""
    from exomem import audit_fix as audit_fix_module
    from exomem import create_file as create_file_module

    # Drop a probe page whose body contains the *concept* but not the literal
    # query tokens.
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Notes/Insights/blood-sugar-clarity-probe.md",
        content=(
            "Blood sugar control and afternoon clarity\n\n"
            "Sharp postprandial peaks tank cognitive sharpness within the next "
            "hour. Steadier glucose curves track with steadier focus."
        ),
        frontmatter={
            "type": "insight",
            "status": "active",
            "created": "2026-05-28",
            "updated": "2026-05-28",
            "tags": ["probe"],
        },
    )

    # Build the embedding index against the fixture vault.
    audit_fix_module.audit_fix(vault, rebuild_embeddings=True)

    # Query uses none of the probe page's literal words.
    query = "glucose stability and mental focus"

    keyword_hits = find_module.find(vault, query=query, mode="keyword", limit=10)
    hybrid_hits = find_module.find(vault, query=query, mode="hybrid", limit=10)

    keyword_paths = {h.path for h in keyword_hits}
    hybrid_paths = {h.path for h in hybrid_hits}

    probe_marker = "blood-sugar-clarity-probe"
    assert not any(probe_marker in p for p in keyword_paths), (
        "keyword mode should NOT find the semantic probe"
    )
    assert any(probe_marker in p for p in hybrid_paths), (
        f"hybrid mode should surface the probe; got {hybrid_paths}"
    )
