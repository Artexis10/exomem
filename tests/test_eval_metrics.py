"""Unit tests for the pure ranking metrics. No embeddings / torch needed."""

from __future__ import annotations

import math

import pytest

from kb_mcp import eval_metrics as m


def test_dcg_known_values() -> None:
    # rank1 discount = 1/log2(2) = 1; rank2 = 1/log2(3); rank3 = 1/log2(4)=0.5
    assert m.dcg([1.0]) == pytest.approx(1.0)
    assert m.dcg([0.0, 1.0]) == pytest.approx(1.0 / math.log2(3))
    assert m.dcg([3.0, 0.0, 1.0]) == pytest.approx(3.0 + 0.0 + 1.0 / 2.0)


def test_ndcg_perfect_ranking_is_one() -> None:
    rel = {"a": 3.0, "b": 1.0}
    assert m.ndcg_at_k(["a", "b"], rel, 2) == pytest.approx(1.0)


def test_ndcg_reversed_ranking_below_one() -> None:
    rel = {"a": 3.0, "b": 1.0}
    score = m.ndcg_at_k(["b", "a"], rel, 2)
    assert 0.0 < score < 1.0
    # exponential gain: actual = (2^1-1) + (2^3-1)/log2(3) = 1 + 7/1.585
    actual = 1.0 + 7.0 / math.log2(3)
    ideal = 7.0 + 1.0 / math.log2(3)
    assert score == pytest.approx(actual / ideal)


def test_ndcg_no_relevant_is_zero() -> None:
    assert m.ndcg_at_k(["x", "y"], {}, 10) == pytest.approx(0.0)


def test_ndcg_truncates_at_k() -> None:
    # The ideal is also truncated at k, so a relevant doc beyond k can't lift it.
    rel = {"a": 3.0, "b": 3.0, "c": 3.0}
    # only "a" in top-1, ideal@1 is one grade-3 doc → perfect
    assert m.ndcg_at_k(["a", "b", "c"], rel, 1) == pytest.approx(1.0)


def test_mrr_first_relevant_rank() -> None:
    assert m.mrr(["x", "a", "b"], {"a", "b"}) == pytest.approx(0.5)
    assert m.mrr(["a"], {"a"}) == pytest.approx(1.0)
    assert m.mrr(["x", "y"], {"a"}) == pytest.approx(0.0)


def test_recall_at_k() -> None:
    ranked = ["a", "x", "b", "y"]
    relevant = {"a", "b", "c"}
    assert m.recall_at_k(ranked, relevant, 2) == pytest.approx(1.0 / 3)  # only "a" in top-2
    assert m.recall_at_k(ranked, relevant, 4) == pytest.approx(2.0 / 3)  # a,b in top-4
    assert m.recall_at_k(ranked, set(), 4) == pytest.approx(0.0)


def test_mean() -> None:
    assert m.mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert m.mean([]) == pytest.approx(0.0)
