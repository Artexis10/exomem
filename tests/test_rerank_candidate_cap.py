"""Caller-bounded reranker candidates stay optional, cache-safe, and observable."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from exomem import commands, embeddings, readiness, server, writer_lease
from exomem import find as find_module
from exomem.__main__ import main


@pytest.fixture(autouse=True)
def _reset_find_state() -> None:
    find_module.clear_cache()
    readiness.reset()
    writer_lease.reset_managers_for_tests()
    yield
    find_module.clear_cache()
    readiness.reset()
    writer_lease.reset_managers_for_tests()


def _write_candidates(root: Path, count: int = 15) -> list[str]:
    paths: list[str] = []
    notes = root / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        rel = f"Knowledge Base/Notes/Insights/rerank-candidate-{index:02d}.md"
        (root / rel).write_text(
            "---\n"
            "type: insight\n"
            f"title: Rerank candidate {index:02d}\n"
            "status: active\n"
            f"updated: 2026-07-{(index % 19) + 1:02d}\n"
            "---\n\n"
            f"# Rerank candidate {index:02d}\n\n"
            f"bounded reranker candidate needle {index:02d}\n",
            encoding="utf-8",
        )
        paths.append(rel)
    return paths


def _enable_stub_reranker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    scorer_counts: list[int] = []
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)

    def score(_query: str, passages: list[str]) -> list[float]:
        scorer_counts.append(len(passages))
        return [float(index) for index in range(len(passages))]

    monkeypatch.setattr(embeddings, "rerank_pairs", score)
    return scorer_counts


def _find_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "query": "bounded reranker candidate needle",
        "limit": 5,
        "mode": "hybrid",
        "scope": "kb-only",
        "graph": False,
        "rerank": True,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("invalid", [True, False, 4, 301, 5.0, "5", object()])
def test_invalid_rerank_candidate_cap_fails_before_model_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    _write_candidates(tmp_path)
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("validation must precede model invocation")
        ),
    )
    with pytest.raises(ValueError, match=r"rerank_max_candidates.*integer.*5.*300"):
        find_module.find(
            tmp_path,
            **_find_kwargs(rerank_max_candidates=invalid),
        )


def test_cap_validation_uses_the_effective_normalized_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_candidates(tmp_path, count=1)
    counts = _enable_stub_reranker(monkeypatch)

    hits = find_module.find(
        tmp_path,
        **_find_kwargs(limit=0, rerank_max_candidates=1),
    )

    assert len(hits) == 1
    assert counts == [1]


def test_cap_and_omission_control_exact_scorer_input_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_candidates(tmp_path)
    counts = _enable_stub_reranker(monkeypatch)

    find_module.find(tmp_path, **_find_kwargs(rerank_max_candidates=5))
    find_module.find(tmp_path, **_find_kwargs(rerank_max_candidates=None))

    assert counts == [5, 15]


def test_successful_prefix_ordering_preserves_the_fused_tail() -> None:
    hits = [
        SimpleNamespace(path="a", rerank_score=0.1),
        SimpleNamespace(path="b", rerank_score=0.9),
        SimpleNamespace(path="c", rerank_score=None),
        SimpleNamespace(path="d", rerank_score=None),
    ]

    ordered = find_module._order_reranked_prefix(hits, prefix_count=2)

    assert [hit.path for hit in ordered] == ["b", "a", "c", "d"]
    assert ordered[2] is hits[2]
    assert ordered[3] is hits[3]


def test_cap_does_not_enable_explicitly_disabled_reranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_candidates(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rerank=false must not invoke the scorer")
        ),
    )

    baseline = find_module.find(tmp_path, **_find_kwargs(rerank=False))
    capped = find_module.find(
        tmp_path,
        **_find_kwargs(rerank=False, rerank_max_candidates=5),
    )

    assert [hit.path for hit in capped] == [hit.path for hit in baseline]


def test_cap_does_not_change_auto_policy_decline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_candidates(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(find_module, "should_rerank", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a declined auto policy must not invoke the scorer")
        ),
    )

    baseline = find_module.find(tmp_path, **_find_kwargs(rerank=False))
    capped_auto = find_module.find(
        tmp_path,
        **_find_kwargs(
            rerank=None,
            auto_rerank=True,
            rerank_max_candidates=5,
        ),
    )

    assert [hit.path for hit in capped_auto] == [hit.path for hit in baseline]


@pytest.mark.parametrize(
    ("hard_disabled", "warming", "decision", "reason"),
    [
        (True, False, "skipped", "hard_disabled"),
        (False, True, "deferred", "model_warming"),
    ],
)
def test_pre_score_soft_failures_keep_fused_order_and_truthful_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hard_disabled: bool,
    warming: bool,
    decision: str,
    reason: str,
) -> None:
    _write_candidates(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    baseline = commands.op_ask_memory(
        tmp_path,
        **_find_kwargs(rerank=False),
        detail="compact",
    )
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: not hard_disabled)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: warming)
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pre-score soft failure must not invoke the scorer")
        ),
    )

    result = commands.op_ask_memory(
        tmp_path,
        **_find_kwargs(rerank_max_candidates=5),
        detail="compact",
        explain=True,
    )

    assert [hit["path"] for hit in result["hits"]] == [hit["path"] for hit in baseline]
    profile = result["retrieval_profile"]["rerank"]
    assert (profile["decision"], profile["reason"]) == (decision, reason)
    assert profile["scorer_input_count"] == 0
    assert profile["unscored_tail_count"] == 10


@pytest.mark.parametrize(
    ("failure", "decision", "reason"),
    [
        (ImportError("missing"), "unavailable", "dependency_unavailable"),
        (RuntimeError("boom"), "failed", "runtime_failure"),
    ],
)
def test_failed_bounded_reranking_keeps_fused_order_and_exact_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    decision: str,
    reason: str,
) -> None:
    _write_candidates(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    baseline = commands.op_ask_memory(
        tmp_path,
        **_find_kwargs(rerank=False),
        detail="compact",
    )
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    result = commands.op_ask_memory(
        tmp_path,
        **_find_kwargs(rerank_max_candidates=5),
        detail="compact",
        explain=True,
    )

    assert [hit["path"] for hit in result["hits"]] == [hit["path"] for hit in baseline]
    profile = result["retrieval_profile"]["rerank"]
    assert (profile["decision"], profile["reason"]) == (decision, reason)
    assert profile["candidate_limit_requested"] == 5
    assert profile["candidate_limit_effective"] == 5
    assert profile["scorer_input_count"] == 5
    assert profile["unscored_tail_count"] == 10


def test_score_application_failure_is_transactional_without_partial_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_candidates(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    baseline = commands.op_ask_memory(
        tmp_path,
        **_find_kwargs(rerank=False),
        detail="compact",
    )
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda _query, _passages: [0.9, object(), 0.7, 0.6, 0.5],
    )

    result = commands.op_ask_memory(
        tmp_path,
        **_find_kwargs(rerank_max_candidates=5),
        detail="compact",
        explain=True,
    )

    assert [hit["path"] for hit in result["hits"]] == [hit["path"] for hit in baseline]
    profile = result["retrieval_profile"]["rerank"]
    assert (profile["decision"], profile["reason"], profile["ran"]) == (
        "failed",
        "runtime_failure",
        False,
    )
    assert all("reranker" not in hit["ranking_explanation"] for hit in result["hits"])


def test_cap_diagnostics_report_successful_prefix_and_tail_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_candidates(tmp_path)
    counts = _enable_stub_reranker(monkeypatch)

    result = commands.op_ask_memory(
        tmp_path,
        **_find_kwargs(rerank_max_candidates=5),
        detail="compact",
        include_timings=True,
        explain=True,
    )

    assert counts == [5]
    expected = {
        "candidate_limit_requested": 5,
        "candidate_limit_effective": 5,
        "candidate_limit_hard_max": find_module.MAX_RERANK_CANDIDATES,
        "scorer_input_count": 5,
        "unscored_tail_count": 10,
    }
    rerank_profile = result["retrieval_profile"]["rerank"]
    timing_profile = result["timings"]["profile"]["rerank"]
    assert {key: rerank_profile[key] for key in expected} == expected
    assert {key: timing_profile[key] for key in expected} == expected
    assert rerank_profile["decision"] == "ran"
    assert timing_profile["decision"] == "ran"
    assert all(hit["ranking_explanation"]["reranker"]["input_rank"] <= 5 for hit in result["hits"])


def test_different_caps_are_isolated_in_the_hot_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_candidates(tmp_path)
    counts = _enable_stub_reranker(monkeypatch)
    semantic_calls = 0
    original = find_module._find_semantic

    def counting(*args: Any, **kwargs: Any):
        nonlocal semantic_calls
        semantic_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(find_module, "_find_semantic", counting)

    find_module.find(tmp_path, **_find_kwargs(rerank_max_candidates=5))
    find_module.find(tmp_path, **_find_kwargs(rerank_max_candidates=6))
    find_module.find(tmp_path, **_find_kwargs(rerank_max_candidates=5))

    assert semantic_calls == 2
    assert counts == [5, 6]


def _integer_branch(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "integer":
        return schema
    return next(branch for branch in schema["anyOf"] if branch.get("type") == "integer")


def test_product_and_canonical_mcp_schemas_are_strict_bounded_integers(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(vault.parent / "writer-lease-state"),
    )
    monkeypatch.setenv("EXOMEM_MCP_LEGACY_COMPAT", "1")
    mcp = server.build_server(require_auth=False)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools(run_middleware=False))}

    for name in ("ask_memory", "find"):
        schema = tools[name].to_mcp_tool().model_dump(mode="json")["inputSchema"]
        cap = _integer_branch(schema["properties"]["rerank_max_candidates"])
        assert cap["minimum"] == 1
        assert cap["maximum"] == find_module.MAX_RERANK_CANDIDATES
        for invalid in (False, True):
            with pytest.raises(Exception, match="rerank_max_candidates"):
                asyncio.run(
                    mcp.call_tool(
                        name,
                        {"query": "", "rerank_max_candidates": invalid},
                        run_middleware=False,
                    )
                )


def test_rest_cli_descriptors_and_product_forwarding_include_candidate_cap(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(vault.parent / "writer-lease-state"),
    )
    for command in (
        next(item for item in commands.COMMANDS if item.name == "find"),
        next(item for item in commands.PRODUCT_COMMANDS if item.name == "ask_memory"),
    ):
        param = next(item for item in command.params if item.name == "rerank_max_candidates")
        assert param.type == "int"
        assert param.required is False

    forwarded: list[int | None] = []

    def fake_find(_vault_root: Path, **kwargs: Any) -> list[Any]:
        forwarded.append(kwargs["rerank_max_candidates"])
        return []

    monkeypatch.setattr(commands, "op_find", fake_find)
    commands.op_ask_memory(vault, query="x", rerank_max_candidates=7)
    assert forwarded == [7]

    code = main(
        [
            "ask_memory",
            "metabolism",
            "--mode",
            "keyword",
            "--rerank-max-candidates",
            "5",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert json.loads(captured.out.strip().splitlines()[-1])["success"] is True
