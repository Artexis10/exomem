"""The historical golden scopes keep their meaning after opt-in widening."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evaluator(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(
        "eval_retrieval_scope_test", ROOT / "scripts" / "eval_retrieval.py"
    )
    module = importlib.util.module_from_spec(spec)
    # The standalone evaluator enables embeddings on import. Keep that change
    # local to the import so a lean test cannot alter later tests' environment.
    with monkeypatch.context() as env:
        env.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("scope", [None, "kb-only", "kb", "vault"])
@pytest.mark.parametrize("operation", ["quality", "latency"])
def test_golden_scope_explicitly_requests_its_historical_reserve(
    evaluator, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope, operation
) -> None:
    calls = []

    def find(_root, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(path="Knowledge Base/Notes/example.md")]

    monkeypatch.setattr(evaluator.find_module, "find", find)
    entry = {
        "query": "example",
        "relevance": {"notes/example": 1.0},
        "relevant": {"notes/example"},
    }
    if scope is not None:
        entry["scope"] = scope
    config = evaluator.find_module.DEFAULT_RANKING
    if operation == "quality":
        result = evaluator._evaluate(tmp_path, [entry], config, rerank=False)
        assert result["recall10"] == 1.0
    else:
        assert len(evaluator._sample_latencies(
            tmp_path, [entry], config, mode="hybrid", rerank=False, repeat=2
        )) == 2

    assert calls
    for call in calls:
        assert call["scope"] == (scope or "kb-only")
        assert call.get("widen_outside_kb", False) is (scope == "kb")


def test_existing_outside_kb_golden_target_is_still_retrieved(evaluator, vault: Path) -> None:
    from exomem import lexstore

    golden = evaluator._load_golden(ROOT / "tests" / "golden" / "queries.yaml")
    reserve_cases = [entry for entry in golden if entry["scope"] == "kb"]
    assert reserve_cases, "the golden corpus must retain its outside-KB reserve case"
    lexstore.ensure_fresh(vault)
    result = evaluator._evaluate(
        vault, reserve_cases, evaluator.find_module.DEFAULT_RANKING,
        rerank=False, mode="keyword",
    )
    assert all(row["recall10"] > 0 for row in result["rows"])
