"""Heavy recall stages yield to what is left of the request budget.

Yielding removes stages; it never reorders. Every ordering assertion here
compares a budgeted call against the call that never asked for the stage at
all, because "we skipped the reranker" and "we half-applied the reranker" are
the two outcomes a user cannot tell apart from the hits alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, request_budget
from exomem import context_pack as context_pack_module


@pytest.fixture
def budget_free():
    """No budget bound, whatever a previous test left behind."""
    token = request_budget.set_current(None)
    try:
        yield
    finally:
        request_budget.reset_current(token)


@pytest.fixture
def bind_budget():
    tokens = []

    def _bind(seconds: float) -> request_budget.RequestBudget:
        budget = request_budget.RequestBudget(seconds=seconds)
        tokens.append(request_budget.set_current(budget))
        return budget

    try:
        yield _bind
    finally:
        for token in reversed(tokens):
            request_budget.reset_current(token)


@pytest.fixture
def reversing_reranker(monkeypatch: pytest.MonkeyPatch):
    """A reranker that visibly reorders, so a skipped one is distinguishable.

    Without this the suite's disabled embeddings make every rerank soft-fail
    into the fused order, and a test asserting "the order is the unreranked
    order" would pass even if the budget check never ran.
    """
    from exomem import embeddings

    def _scores(_query: str, passages: list[str]) -> list[float]:
        return [float(index) * 1000.0 for index in range(len(passages))]

    monkeypatch.setattr(embeddings, "rerank_pairs", _scores)


def _paths(result) -> list[str]:
    hits = result["hits"] if isinstance(result, dict) else result
    return [str(hit.get("path") or "") for hit in hits]


def _pack(result) -> dict:
    assert isinstance(result, dict)
    return result["pack"]


QUERY = "metabolism"


def test_the_reranker_reorders_when_it_is_afforded(
    vault: Path, budget_free, reversing_reranker
) -> None:
    """The control for every rerank assertion below: skipping is observable."""
    unreranked = _paths(commands.op_find(vault, query=QUERY, rerank=False, limit=5))
    reranked = _paths(commands.op_find(vault, query=QUERY, rerank=True, limit=5))

    assert len(unreranked) >= 2
    assert reranked != unreranked


def test_rerank_is_skipped_under_a_tight_budget(
    vault: Path, bind_budget, reversing_reranker
) -> None:
    unreranked = _paths(commands.op_find(vault, query=QUERY, rerank=False, limit=5))

    budget = bind_budget(0.001)
    result = commands.op_find(vault, query=QUERY, rerank=True, limit=5)

    assert budget.skipped == ["rerank"]
    assert _paths(result) == unreranked
    assert result["budget"]["applied"] is True
    assert result["budget"]["skipped"] == ["rerank"]


def test_a_cold_reranker_needs_the_larger_reserve(
    vault: Path, bind_budget, reversing_reranker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold load runs inside the request, so residency changes the reserve."""
    from exomem import embeddings

    monkeypatch.setattr(embeddings, "_RERANKER", None, raising=False)
    # Comfortably above the warm reserve, comfortably below the cold one.
    between = (
        request_budget.RERANK_RESERVE_SECONDS + request_budget.RERANK_COLD_RESERVE_SECONDS
    ) / 2.0
    assert (
        request_budget.RERANK_RESERVE_SECONDS
        < between
        < request_budget.RERANK_COLD_RESERVE_SECONDS
    )

    budget = bind_budget(between)
    commands.op_find(vault, query=QUERY, rerank=True, limit=5)

    assert budget.skipped == ["rerank"]


def test_skipped_rerank_does_not_cache_an_incomplete_result(
    vault: Path, bind_budget, reversing_reranker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings

    calls = []
    score = embeddings.rerank_pairs

    def counted_scores(query, passages):
        calls.append(query)
        return score(query, passages)

    monkeypatch.setattr(embeddings, "rerank_pairs", counted_scores)
    budget = bind_budget(0.001)
    skipped = commands.op_find(vault, query=QUERY, rerank=True, limit=5)
    assert budget.skipped == ["rerank"]
    assert calls == []

    bind_budget(3600.0)
    completed = commands.op_find(vault, query=QUERY, rerank=True, limit=5)
    assert calls == [QUERY]
    assert _paths(completed) != _paths(skipped)
    assert "budget" not in completed

    cached = commands.op_find(vault, query=QUERY, rerank=True, limit=5)
    assert calls == [QUERY]
    assert _paths(cached) == _paths(completed)


def test_a_warm_reranker_runs_inside_the_same_budget(
    vault: Path, bind_budget, reversing_reranker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings

    monkeypatch.setattr(embeddings, "_RERANKER", object(), raising=False)
    unreranked = _paths(commands.op_find(vault, query=QUERY, rerank=False, limit=5))
    between = (
        request_budget.RERANK_RESERVE_SECONDS + request_budget.RERANK_COLD_RESERVE_SECONDS
    ) / 2.0

    budget = bind_budget(between)
    result = commands.op_find(vault, query=QUERY, rerank=True, limit=5)

    assert budget.skipped == []
    assert _paths(result) != unreranked


def test_graph_enrich_is_skipped_when_only_the_pack_fits(
    vault: Path, bind_budget
) -> None:
    reserve = request_budget.PACK_RESERVE_SECONDS + (
        request_budget.GRAPH_ENRICH_RESERVE_SECONDS / 2.0
    )
    budget = bind_budget(reserve)

    result = commands.op_find(
        vault, query=QUERY, pack=True, graph_enrich=True, limit=5
    )

    assert budget.skipped == ["graph_enrich"]
    assert "graph" not in _pack(result)
    assert result["budget"]["skipped"] == ["graph_enrich"]


def test_the_deep_pack_is_skipped_when_it_cannot_be_afforded(
    vault: Path, bind_budget
) -> None:
    budget = bind_budget(0.001)

    result = commands.op_find(vault, query=QUERY, pack=True, limit=5)

    assert budget.skipped == ["pack"]
    assert result["pack"] is None
    assert result["budget"]["skipped"] == ["pack"]


@pytest.mark.parametrize("remaining_after_load", [0.0, 1.0])
def test_graph_enrichment_rechecks_its_reserve_after_pack_loading(
    vault: Path, bind_budget, monkeypatch: pytest.MonkeyPatch, remaining_after_load: float
) -> None:
    budget = bind_budget(3600.0)
    clock = [budget.deadline - 100.0]
    load_parent = context_pack_module._load_parent_snapshot
    enriched = []

    def slow_parent_load(*args, **kwargs):
        page = load_parent(*args, **kwargs)
        clock[0] = budget.deadline - remaining_after_load
        return page

    monkeypatch.setattr(context_pack_module, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(context_pack_module, "_load_parent_snapshot", slow_parent_load)
    monkeypatch.setattr(
        context_pack_module, "_graph_enrichment", lambda *_args: enriched.append(True)
    )

    result = commands.op_find(vault, query=QUERY, pack=True, graph_enrich=True, limit=5)

    assert enriched == []
    assert _pack(result)["packed_paths"]
    assert "graph" not in _pack(result)
    assert result["budget"]["skipped"] == ["graph_enrich"]


def test_the_deep_pack_truncates_at_the_deadline(
    vault: Path, bind_budget, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pack that started and ran out of clock is a prefix, and says so."""
    full = _pack(commands.op_find(vault, query=QUERY, pack=True, limit=5))
    assert len(full["packed_paths"]) >= 2

    budget = bind_budget(3600.0)
    calls = {"n": 0}

    def _clock() -> float:
        calls["n"] += 1
        # One group packed, then the deadline arrives.
        return budget.deadline - 1.0 if calls["n"] <= 1 else budget.deadline + 1.0

    monkeypatch.setattr(context_pack_module, "_monotonic", _clock)

    result = commands.op_find(vault, query=QUERY, pack=True, limit=5)
    packed = _pack(result)

    assert budget.truncated == ["pack"]
    assert packed["packed_paths"] == full["packed_paths"][: len(packed["packed_paths"])]
    assert len(packed["packed_paths"]) < len(full["packed_paths"])
    assert context_pack_module.DEADLINE_TRUNCATION in packed["truncation"]
    assert result["budget"]["truncated"] == ["pack"]


def test_a_call_that_fits_the_budget_is_unchanged(
    vault: Path, bind_budget, budget_free, reversing_reranker
) -> None:
    """Byte-identical to today's response: no budget key, same shape."""
    unbudgeted = commands.op_find(
        vault, query=QUERY, rerank=True, pack=True, graph_enrich=True, limit=5
    )

    budget = bind_budget(3600.0)
    budgeted = commands.op_find(
        vault, query=QUERY, rerank=True, pack=True, graph_enrich=True, limit=5
    )

    assert budget.skipped == []
    assert budget.truncated == []
    assert type(budgeted) is type(unbudgeted)
    assert set(budgeted) == set(unbudgeted)
    assert "budget" not in budgeted
    assert _paths(budgeted) == _paths(unbudgeted)


def test_a_tight_budget_with_nothing_to_skip_carries_no_budget_key(
    vault: Path, bind_budget, budget_free
) -> None:
    """The block is advisory, so a call that lost nothing must not carry it."""
    unbudgeted = commands.op_find(vault, query=QUERY, limit=5)

    budget = bind_budget(0.001)
    budgeted = commands.op_find(vault, query=QUERY, limit=5)

    assert budget.skipped == []
    assert budget.truncated == []
    assert isinstance(budgeted, list)
    assert budgeted == unbudgeted


def test_the_compact_recall_product_carries_the_block(
    vault: Path, bind_budget, reversing_reranker
) -> None:
    budget = bind_budget(0.001)

    result = commands.op_ask_memory(
        vault, query=QUERY, rerank=True, detail="compact", limit=5
    )

    assert budget.skipped == ["rerank"]
    assert isinstance(result, dict)
    assert result["budget"]["applied"] is True
    assert result["budget"]["skipped"] == ["rerank"]


def test_a_local_call_with_no_budget_skips_nothing(
    vault: Path, budget_free, reversing_reranker
) -> None:
    result = commands.op_find(
        vault, query=QUERY, rerank=True, pack=True, graph_enrich=True, limit=5
    )

    assert request_budget.current() is None
    assert "budget" not in (result if isinstance(result, dict) else {})
    assert "graph" in _pack(result)
