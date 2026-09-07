"""Optional graph lag must not discard an admitted ordinary lookup."""

from pathlib import Path

import pytest

from exomem import epistemic_graph, lexstore, readiness, warmup
from exomem import find as find_module


@pytest.fixture
def managed_recall(vault: Path, monkeypatch):
    monkeypatch.setenv("EXOMEM_EAGER_BOOT", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    lexstore.ensure_fresh(vault)
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        warmup.warm_retrieval_catalog(vault)
    finally:
        readiness.finish_warm()
    assert readiness.retrieval_admission(vault)["admitted"]
    yield vault
    readiness.reset()


@pytest.mark.parametrize("site", [
    "resolver_checkpoint_stale", "resolver_checkpoint_absent",
    "resolver_entries_unavailable", "resolver_build_wait",
])
@pytest.mark.parametrize("sidecar_available", [False, True])
def test_ordinary_hybrid_returns_direct_matches_during_optional_graph_lag(
    managed_recall: Path, monkeypatch, site: str, sidecar_available: bool,
):
    root = managed_recall
    baseline = find_module.find(root, query="metabolism", graph=False, rerank=False)
    assert baseline
    observed = []

    def lagging_resolver(*args, **kwargs):
        observed.append(kwargs["allow_fallback"])
        raise find_module.RetrievalIndexWarming(site=site)

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: sidecar_available)
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "indexed_paths", lambda self, paths: set())
    monkeypatch.setattr(find_module, "recall_resolver_snapshot", lagging_resolver)
    def unexpected_expansion(*args, **kwargs):
        pytest.fail("an unavailable resolver must not trigger implicit fallback expansion")
    monkeypatch.setattr(find_module, "_outbound_wikilink_paths", unexpected_expansion)
    warming = []
    hits = find_module.find(
        root, query="metabolism", graph=True, rerank=False, degraded_out=warming,
    )
    assert {hit.path for hit in hits} == {hit.path for hit in baseline}
    assert observed == [False]
    assert "graph" in warming


def test_recovered_resolver_is_retried_instead_of_caching_degraded_hits(managed_recall, monkeypatch):
    original = find_module.recall_resolver_snapshot
    ready = False
    calls = []

    def resolver(*args, **kwargs):
        calls.append(ready)
        if not ready:
            raise find_module.RetrievalIndexWarming(site="resolver_checkpoint_stale")
        return original(*args, **kwargs)

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: False)
    monkeypatch.setattr(find_module, "recall_resolver_snapshot", resolver)
    lag = []
    assert find_module.find(managed_recall, query="metabolism", graph=True, rerank=False, degraded_out=lag)
    assert "graph" in lag
    ready = True
    recovered = []
    assert find_module.find(managed_recall, query="metabolism", graph=True, rerank=False, degraded_out=recovered)
    assert "graph" not in recovered
    assert calls == [False, True]


def test_relation_predicate_still_refuses_unproven_graph(managed_recall: Path, monkeypatch):
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: False)
    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(managed_recall, query="", relations=["supports"], graph=False)


def test_non_resolver_failure_is_not_swallowed(managed_recall: Path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise find_module.RetrievalIndexWarming(site="pending_visibility_incomplete")

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: False)
    monkeypatch.setattr(find_module, "recall_resolver_snapshot", unavailable)
    with pytest.raises(find_module.RetrievalIndexWarming) as error:
        find_module.find(managed_recall, query="metabolism", graph=True, rerank=False)
    assert error.value.site == "pending_visibility_incomplete"
