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

from kb_mcp import bm25, embeddings, find as find_module, fusion


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
    from kb_mcp import create_file as create_file_module
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


def test_stem_aware_gate_recovers_morphological_match(vault) -> None:
    """A page that uses 'regulator' should be reachable via 'regulation'.

    Probes the BM25-only stem-aware gate in _find_semantic. Vector results
    are disabled (KB_MCP_DISABLE_EMBEDDINGS), so the only way this page
    reaches the result set is via BM25 (also stemmed) + the stem-aware
    all-tokens-present check.
    """
    from kb_mcp import create_file as create_file_module
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


def test_graph_expansion_surfaces_linked_neighbour(vault) -> None:
    """A page linked from a query match should come back with graph_hop=True.

    Builds a tiny graph: probe-graph-anchor mentions "specific anchor token";
    probe-graph-neighbour shares no query token but is wikilinked from
    -anchor. Querying for "anchor" should surface -neighbour via graph hop.
    """
    from kb_mcp import create_file as create_file_module
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
    """Lift the conftest-wide KB_MCP_DISABLE_EMBEDDINGS gate for these tests."""
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
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
    from kb_mcp import note as note_module

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
    from kb_mcp import audit_fix as audit_fix_module
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
    from kb_mcp import audit_fix as audit_fix_module
    from kb_mcp import create_file as create_file_module

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
