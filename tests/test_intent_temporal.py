"""Wave-1 search: intent classification, temporal lane, weighted RRF, smart rerank.

Pure / torch-free wherever possible — the markers, the weighted-fusion math, the
recency multiplier, and the rerank heuristic are all deterministic and don't need
embeddings. `_apply_temporal_boost` is exercised over a tiny seeded tmp vault.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from kb_mcp import find as find_module
from kb_mcp import fusion
from kb_mcp.find import DEFAULT_RANKING, Hit


# --------------------------------------------------------------------------- #
# _is_temporal_query
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query",
    [
        "what did I conclude recently",
        "the latest thinking on metabolism",
        "notes from today",
        "what happened yesterday",
        "decisions this week",
        "spending last month",
        "what changed this year",
        "when did I switch to cloudflare",
        "anything before the migration",
        "results after the refactor",
        "the 2024 plan",
        "entry on 2026-05-04",
    ],
)
def test_is_temporal_query_true(query: str) -> None:
    assert find_module._is_temporal_query(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "metabolism curriculum",
        "EGCG dose response",
        "reference-marker-xyz",  # 'reference' must NOT read as temporal
        "glucose stability and mental focus",
        "",
        "X3 rep progression tracking",
    ],
)
def test_is_temporal_query_false(query: str) -> None:
    assert find_module._is_temporal_query(query) is False


# --------------------------------------------------------------------------- #
# _classify_intent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query,expected",
    [
        # exact: quotes, wikilink, or leading interrogative
        ('"exact phrase match"', "exact"),
        ("see [[Envelope]]", "exact"),
        ("who is Karpathy", "exact"),
        ("what is the engine boundary", "exact"),
        ("which protocol drifts", "exact"),
        # temporal markers (and exact does NOT pre-empt a non-interrogative temporal)
        ("the latest curriculum", "temporal"),
        ("notes from yesterday", "temporal"),
        ("the 2025 retro", "temporal"),
        # relationship markers
        ("what links to the envelope concept", "exact"),  # leading 'what' wins
        ("pages that cite the envelope", "relationship"),
        ("how does this connect to insulin", "relationship"),
        ("related work on metabolism", "relationship"),
        ("mentions of EGCG", "relationship"),
        # conceptual default — none of the existing suite queries should divert
        ("metabolism curriculum", "conceptual"),
        ("glucose stability and mental focus", "conceptual"),
        ("reference-marker-xyz", "conceptual"),
        ("specific anchor token", "conceptual"),
        ("", "conceptual"),
    ],
)
def test_classify_intent(query: str, expected: str) -> None:
    assert find_module._classify_intent(query) == expected


def test_existing_suite_queries_classify_conceptual() -> None:
    """Every ordering-sensitive query the existing suite uses must stay
    conceptual (neutral weights) — that's what keeps the suite green."""
    for q in [
        "EGCG", "metabolism", "insulin", "metabolism curriculum",
        "curriculum metabolism", "SKILL", "engine", "Karpathy",
        "reference-marker-xyz", "find-vault-skip-marker-abc", "X3",
        "X3 rep progression tracking", "regulator metabolism",
        "regulation metabolism", "distinctivetypeprobe", "rareindegreemarker",
        "specific anchor token", "metabolic disease",
        "glucose stability and mental focus", "purple dinosaur costume",
        "whiteboard diagram", "water damage claim", "cockroach infestation",
        "Acme Plumbing", "zqxconflicttoken",
    ]:
        assert find_module._classify_intent(q) == "conceptual", q


# --------------------------------------------------------------------------- #
# reciprocal_rank_fusion_weighted (pure math)
# --------------------------------------------------------------------------- #
def test_weighted_fusion_neutral_equals_unweighted() -> None:
    """All-1.0 weights must reproduce the unweighted RRF byte-for-byte."""
    lists = [["a", "b", "c"], ["b", "c", "d"], ["x", "a"]]
    base = fusion.reciprocal_rank_fusion(lists, k=60)
    weighted = fusion.reciprocal_rank_fusion_weighted(lists, [1.0, 1.0, 1.0], k=60)
    assert base == weighted


def test_weighted_fusion_weight_biases_winner() -> None:
    """Up-weighting a lane lifts that lane's exclusive top item above a rival."""
    vector = ["v_top", "shared"]
    bm25 = ["b_top", "shared"]
    # Neutral: 'shared' wins (votes from both lanes); v_top and b_top tie on path.
    neutral = dict(fusion.reciprocal_rank_fusion_weighted([vector, bm25], [1.0, 1.0]))
    assert neutral["v_top"] == pytest.approx(neutral["b_top"])
    # Heavily up-weight the vector lane → v_top must outscore b_top.
    biased = dict(fusion.reciprocal_rank_fusion_weighted([vector, bm25], [5.0, 1.0]))
    assert biased["v_top"] > biased["b_top"]


def test_weighted_fusion_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        fusion.reciprocal_rank_fusion_weighted([["a"], ["b"]], [1.0])


# --------------------------------------------------------------------------- #
# recency multiplier + _apply_temporal_boost
# --------------------------------------------------------------------------- #
def test_recency_multiplier_off_by_default() -> None:
    # Default temporal_boost == 1.0 → always neutral, regardless of age.
    assert find_module._recency_multiplier(0.0) == 1.0
    assert find_module._recency_multiplier(1000.0) == 1.0


def test_recency_multiplier_peaks_at_zero_age() -> None:
    cfg = replace(DEFAULT_RANKING, temporal_boost=2.0, temporal_sigma_days=60.0)
    fresh = find_module._recency_multiplier(0.0, cfg)
    middling = find_module._recency_multiplier(60.0, cfg)
    old = find_module._recency_multiplier(600.0, cfg)
    assert fresh == pytest.approx(2.0)            # peak == temporal_boost
    assert 1.0 < middling < fresh                 # decays with age
    assert old == pytest.approx(1.0, abs=1e-3)    # far past sigma → neutral


def _write_page(root: Path, rel: str, updated: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: insight\nupdated: {updated}\n---\n# {Path(rel).stem}\nbody\n",
        encoding="utf-8",
    )


def test_apply_temporal_boost_noop_when_not_temporal(tmp_path: Path) -> None:
    find_module.clear_cache()
    _write_page(tmp_path, "Knowledge Base/old.md", "2020-01-01")
    _write_page(tmp_path, "Knowledge Base/new.md", date.today().isoformat())
    fused = [("Knowledge Base/old.md", 1.0), ("Knowledge Base/new.md", 0.9)]
    cfg = replace(DEFAULT_RANKING, temporal_boost=3.0)
    # Non-temporal query → unchanged even with boost configured.
    assert find_module._apply_temporal_boost(fused, tmp_path, "metabolism", cfg) == fused
    # Temporal query but boost==1.0 (default) → also unchanged.
    assert find_module._apply_temporal_boost(
        fused, tmp_path, "latest notes", DEFAULT_RANKING
    ) == fused


def test_apply_temporal_boost_reorders_recent_first(tmp_path: Path) -> None:
    find_module.clear_cache()
    _write_page(tmp_path, "Knowledge Base/old.md", "2020-01-01")
    _write_page(tmp_path, "Knowledge Base/new.md", date.today().isoformat())
    # old starts ahead on base score; a strong recency boost must flip new on top.
    fused = [("Knowledge Base/old.md", 1.0), ("Knowledge Base/new.md", 0.95)]
    cfg = replace(DEFAULT_RANKING, temporal_boost=5.0, temporal_sigma_days=30.0)
    out = find_module._apply_temporal_boost(fused, tmp_path, "latest notes", cfg)
    assert out[0][0] == "Knowledge Base/new.md"


def test_apply_temporal_boost_undated_keeps_score(tmp_path: Path) -> None:
    find_module.clear_cache()
    (tmp_path / "Knowledge Base").mkdir(parents=True)
    (tmp_path / "Knowledge Base" / "nodate.md").write_text(
        "---\ntype: insight\n---\n# nodate\nbody\n", encoding="utf-8"
    )
    fused = [("Knowledge Base/nodate.md", 0.7)]
    cfg = replace(DEFAULT_RANKING, temporal_boost=5.0)
    out = find_module._apply_temporal_boost(fused, tmp_path, "recent", cfg)
    assert out == [("Knowledge Base/nodate.md", 0.7)]  # mult 1.0


# --------------------------------------------------------------------------- #
# should_rerank
# --------------------------------------------------------------------------- #
def _hit(path: str, *, vector_rank=None, bm25_rank=None) -> Hit:
    return Hit(
        path=path, type=None, scope=None, title=path, updated="", excerpt="",
        vector_rank=vector_rank, bm25_rank=bm25_rank,
    )


def test_should_rerank_long_query() -> None:
    assert find_module.should_rerank([], "one two three four five") is True
    assert find_module.should_rerank([], "one two three four") is False


def test_should_rerank_high_disagreement() -> None:
    # Vector top-3 and bm25 top-3 share only 1 of 3 → >50% disagreement.
    hits = [
        _hit("a", vector_rank=1, bm25_rank=9),
        _hit("b", vector_rank=2, bm25_rank=8),
        _hit("shared", vector_rank=3, bm25_rank=1),
        _hit("x", bm25_rank=2),
        _hit("y", bm25_rank=3),
    ]
    assert find_module.should_rerank(hits, "short query") is True


def test_should_rerank_agreement_is_false() -> None:
    # Same top-3 in both rankers → no disagreement, short query → no rerank.
    hits = [
        _hit("a", vector_rank=1, bm25_rank=1),
        _hit("b", vector_rank=2, bm25_rank=2),
        _hit("c", vector_rank=3, bm25_rank=3),
    ]
    assert find_module.should_rerank(hits, "short query") is False


def test_should_rerank_needs_both_lanes() -> None:
    # Only a vector lane present, short query → can't disagree → False.
    hits = [_hit("a", vector_rank=1), _hit("b", vector_rank=2)]
    assert find_module.should_rerank(hits, "short query") is False
