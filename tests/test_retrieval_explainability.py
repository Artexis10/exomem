from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    bm25,
    commands,
    embeddings,
    epistemic_graph,
    find_candidates,
    readiness,
)
from exomem import (
    find as find_module,
)
from exomem.ranking_config import RankingConfig
from exomem.retrieval_explain import RetrievalTrace, attach_hit_explanations


def _write_page(
    root: Path,
    *,
    name: str,
    updated: str,
    priority: int,
    body: str = "Private page content must not leak into compact explanations.",
) -> str:
    rel = f"Knowledge Base/Notes/Insights/{name}.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {name}\n"
        "status: active\n"
        f"updated: {updated}\n"
        "metadata:\n"
        f"  priority: {priority}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return rel


def _write_unit_page(root: Path) -> str:
    rel = "Knowledge Base/Notes/Insights/explained-units.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Explained units\n"
        f"exomem_id: {uuid.uuid4()}\n"
        "status: active\n"
        "updated: 2026-07-16\n"
        "---\n\n"
        "# Explained units\n\n"
        "## Observations\n"
        "- [config] Session lifetime is thirty days ^session-life\n"
        "- [config] Refresh follows expiry ^refresh-rule\n",
        encoding="utf-8",
    )
    return rel


def test_explain_is_opt_in_and_filter_only_profile_is_truthful(tmp_path: Path) -> None:
    older = _write_page(tmp_path, name="older", updated="2026-07-14", priority=3)
    newer = _write_page(tmp_path, name="newer", updated="2026-07-16", priority=3)
    request = {
        "query": "",
        "mode": "hybrid",
        "scope": "kb-only",
        "detail": "compact",
        "filters": {"page.frontmatter:/metadata/priority": {"$eq": 3}},
    }

    omitted = commands.op_ask_memory(tmp_path, **request)
    explicitly_false = commands.op_ask_memory(tmp_path, explain=False, **request)

    assert json.dumps(explicitly_false, ensure_ascii=False) == json.dumps(
        omitted, ensure_ascii=False
    )
    assert [hit["path"] for hit in omitted] == [newer, older]
    assert all("ranking_explanation" not in hit for hit in omitted)

    explained = commands.op_ask_memory(tmp_path, explain=True, **request)

    assert [hit["path"] for hit in explained["hits"]] == [newer, older]
    profile = explained["retrieval_profile"]
    assert profile["schema_version"] == 1
    assert profile["intent"] == "filter_only"
    assert profile["requested_mode"] == "hybrid"
    assert profile["effective_mode"] == "filter_only"
    assert profile["requested_result_level"] == "auto"
    assert profile["effective_result_level"] == "page"
    assert profile["normalized_filters"] == request["filters"]
    assert profile["compute"] == {
        "mode": "normal",
        "preload_models": False,
        "retain_cpu_caches": False,
        "defer_expensive_indexes": False,
        "release_when_idle": True,
    }
    assert "fusion" not in profile
    assert profile["lanes"]["vector"]["status"] == "non_applicable"
    assert profile["lanes"]["vector"]["reason"] == "empty_query"

    for final_rank, hit in enumerate(explained["hits"], start=1):
        assert "excerpt" not in hit
        assert "content" not in hit
        ranking = hit["ranking_explanation"]
        assert ranking["lanes"] == {
            "filtered_most_recent": {"rank": final_rank}
        }
        assert "fusion" not in ranking
        assert ranking["final_sort_tuple"] == [hit["updated"], hit["path"]]
        assert ranking["final_rank"] == final_rank


def test_keyword_explanation_has_one_real_lane_and_no_fusion(tmp_path: Path) -> None:
    older = _write_page(tmp_path, name="keyword-older", updated="2026-07-14", priority=3)
    newer = _write_page(tmp_path, name="keyword-newer", updated="2026-07-16", priority=3)

    explained = commands.op_ask_memory(
        tmp_path,
        query="private page content",
        mode="keyword",
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    assert [hit["path"] for hit in explained["hits"]] == [newer, older]
    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "keyword"
    assert profile["lanes"]["keyword"]["status"] == "participated"
    assert profile["lanes"]["keyword"]["metric"] == {
        "name": "rank",
        "direction": "lower",
        "rounding": "none",
    }
    assert profile["lanes"]["vector"] == {
        "status": "non_applicable",
        "reason": "requested_mode_keyword",
    }
    assert "fusion" not in profile
    for rank, hit in enumerate(explained["hits"], start=1):
        ranking = hit["ranking_explanation"]
        assert ranking["lanes"] == {"keyword": {"rank": rank}}
        assert "fusion" not in ranking
        assert ranking["final_sort_tuple"] == [hit["updated"], hit["path"]]


def test_disabled_embeddings_explain_lexical_fusion_without_loading_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="lexical-alpha", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    def forbidden_embed(*_args, **_kwargs):
        raise AssertionError("disabled explanations must not load the embedding model")

    monkeypatch.setattr(embeddings, "embed_texts", forbidden_embed)
    explained = commands.op_ask_memory(
        tmp_path,
        query="private page content",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "hybrid_lexical"
    assert profile["lanes"]["vector"] == {
        "status": "disabled",
        "reason": "embeddings_disabled",
        "model": "BAAI/bge-base-en-v1.5",
    }
    assert profile["lanes"]["bm25"]["metric"]["name"] == "raw_bm25_score"
    assert profile["lanes"]["bm25"]["metric"]["direction"] == "higher"
    assert profile["fusion"] == {
        "algorithm": "weighted_rrf",
        "k": 60,
        "weights": {"bm25": 1.0, "keyword": 1.0},
    }

    first = explained["hits"][0]["ranking_explanation"]
    assert set(first["lanes"]) == {"bm25", "keyword"}
    assert first["lanes"]["bm25"]["raw_score"] != 0
    assert first["lanes"]["keyword"] == {
        "rank": first["lanes"]["keyword"]["rank"],
        "weight": 1.0,
        "rrf_k": 60,
        "rrf_contribution": round(
            1.0 / (60 + first["lanes"]["keyword"]["rank"]), 6
        ),
    }
    assert first["fusion"]["rrf_sum"] == pytest.approx(
        round(
            sum(
                lane["weight"] / (lane["rrf_k"] + lane["rank"])
                for lane in first["lanes"].values()
            ),
            6,
        )
    )
    assert [step["name"] for step in first["multipliers"]] == ["type", "status"]
    assert first["final_sort_tuple"][1] == explained["hits"][0]["path"]


def test_available_vector_lane_is_reported_only_on_hits_it_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector_path = _write_page(
        tmp_path, name="vector-participant", updated="2026-07-16", priority=3
    )
    lexical_only_path = _write_page(
        tmp_path, name="lexical-only", updated="2026-07-15", priority=3
    )

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            assert k > 0
            assert allowed_paths is None
            return [(vector_path, 0, "bounded chunk", 0.73)]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )

    explained = commands.op_ask_memory(
        tmp_path,
        query="private page content",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["lanes"]["vector"]["status"] == "participated"
    assert profile["lanes"]["vector"]["model"] == "BAAI/bge-base-en-v1.5"
    hits = {hit["path"]: hit for hit in explained["hits"]}
    vector_lane = hits[vector_path]["ranking_explanation"]["lanes"]["vector"]
    assert vector_lane["rank"] == 1
    assert vector_lane["cosine"] == pytest.approx(0.73)
    assert vector_lane["rrf_contribution"] == round(1.0 / 61.0, 6)
    assert "vector" not in hits[lexical_only_path]["ranking_explanation"]["lanes"]


def test_available_hybrid_vector_reports_effective_mode_and_two_lane_fusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_page(
        tmp_path,
        name="hybrid-regulator",
        updated="2026-07-16",
        priority=3,
        body="A regulator controls the system.",
    )

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            assert k > 0
            assert allowed_paths is None
            return [(path, 0, "A regulator controls the system.", 0.731234567)]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )

    explained = commands.op_ask_memory(
        tmp_path,
        query="regulation",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "hybrid"
    assert profile["fusion"] == {
        "algorithm": "weighted_rrf",
        "k": 60,
        "weights": {"vector": 1.0, "bm25": 1.0},
    }
    lanes = explained["hits"][0]["ranking_explanation"]["lanes"]
    assert set(lanes) == {"vector", "bm25"}
    assert lanes["vector"]["rrf_contribution"] == round(1.0 / 61.0, 6)
    assert lanes["bm25"]["rrf_contribution"] == round(1.0 / 61.0, 6)


def test_fusion_uses_raw_contributions_until_the_serialization_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a_path = _write_page(tmp_path, name="a-rounding", updated="2026-07-16", priority=3)
    z_path = _write_page(tmp_path, name="z-rounding", updated="2026-07-16", priority=3)

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            assert k > 0
            assert allowed_paths is None
            return [
                (z_path, 0, "semantic z", 0.9),
                (a_path, 0, "semantic a", 0.8),
            ]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )
    monkeypatch.setattr(
        bm25,
        "search",
        lambda *_args, **_kwargs: [(a_path, 2.0), (z_path, 1.0)],
    )
    config = RankingConfig(
        rrf_k=1,
        intent_weights_conceptual=(0.010001, 0.01, 0.0, 0.0, 0.0, 0.0),
    )
    trace = RetrievalTrace(
        requested_mode="hybrid",
        requested_result_level="page",
        rerank_requested=False,
        auto_rerank=False,
    )

    hits = find_module.find(
        tmp_path,
        query="rounding adversary",
        mode="hybrid",
        graph=False,
        rerank=False,
        temporal=False,
        scope="kb-only",
        limit=2,
        intent="conceptual",
        config=config,
        retrieval_trace=trace,
    )
    serialized = [hit.as_compact_dict() for hit in hits]
    attach_hit_explanations(trace, serialized)

    assert [hit.path for hit in hits] == [z_path, a_path]
    z_ranking = serialized[0]["ranking_explanation"]
    a_ranking = serialized[1]["ranking_explanation"]
    raw_z = 0.010001 / 2 + 0.01 / 3
    assert z_ranking["fusion"]["rrf_sum"] == round(raw_z, 6) == 0.008334
    assert sum(
        lane["rrf_contribution"] for lane in z_ranking["lanes"].values()
    ) == pytest.approx(0.008333)
    assert z_ranking["multipliers"][0]["before"] == round(raw_z, 6)
    assert z_ranking["multipliers"][0]["after"] == round(raw_z * 1.15, 6)
    assert z_ranking["final_sort_tuple"][0] == a_ranking["final_sort_tuple"][0]
    assert (z_ranking["final_rank"], a_ranking["final_rank"]) == (1, 2)


def test_unit_trace_uses_the_runtime_fused_score_without_recomputation() -> None:
    trace = RetrievalTrace(
        requested_mode="hybrid",
        requested_result_level="unit",
        rerank_requested=False,
        auto_rerank=False,
    )
    unit_ref = "unit-runtime-score"
    formula_score = 0.010001 * (1.0 / 2) + 0.01 * (1.0 / 3)
    runtime_score = math.nextafter(formula_score, math.inf)

    trace.record_unit_ranked(
        records={
            unit_ref: (
                SimpleNamespace(status="active", rel_path="unit-parent.md"),
                SimpleNamespace(unit_ref=unit_ref),
                0,
            )
        },
        lexical_ranking=[unit_ref],
        lexical_scores={unit_ref: 1.0},
        lexical_backend="fixture",
        vector_ranking=[unit_ref],
        vector_scores={unit_ref: 0.9},
        vector_profile={"status": "participated"},
        final_ranking=[unit_ref],
        raw_fused_score_by_ref={unit_ref: runtime_score},
        weights=(0.010001, 0.01),
        rrf_k=1,
        prefer_active=True,
        superseded_penalty=0.5,
        lexical_used=True,
        vector_used=True,
    )

    evidence = trace.evidence_by_id[unit_ref]
    assert evidence["fusion"]["rrf_sum"] == runtime_score
    assert evidence["multipliers"][0]["before"] == runtime_score
    assert evidence["final_sort_tuple"][0] == runtime_score


def test_vector_only_page_is_single_lane_without_fusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_page(
        tmp_path, name="vector-only", updated="2026-07-16", priority=3
    )

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            return [(path, 0, "bounded chunk", 0.812345678)]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )

    explained = commands.op_ask_memory(
        tmp_path,
        query="semantic-only-query",
        mode="vector",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "vector"
    assert "fusion" not in profile
    ranking = explained["hits"][0]["ranking_explanation"]
    assert set(ranking["lanes"]) == {"vector"}
    assert ranking["lanes"]["vector"] == {
        "rank": 1,
        "cosine": pytest.approx(0.812346),
    }
    assert "fusion" not in ranking


def test_auto_widened_hit_has_outside_lane_and_final_merge_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(
        tmp_path,
        name="kb-scope-match-one",
        updated="2026-07-16",
        priority=3,
        body="scopewidener token",
    )
    _write_page(
        tmp_path,
        name="kb-scope-match-two",
        updated="2026-07-15",
        priority=3,
        body="scopewidener token",
    )
    outside_path = tmp_path / "Reference" / "scopewidener.md"
    outside_path.parent.mkdir(parents=True)
    outside_path.write_text(
        "# Outside scopewidener\n\nscopewidener token\n", encoding="utf-8"
    )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    explained = commands.op_ask_memory(
        tmp_path,
        query="scopewidener",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb",
        limit=2,
        detail="compact",
        explain=True,
    )

    assert [hit["outside_kb"] for hit in explained["hits"] if hit.get("outside_kb")] == [
        True
    ]
    outside = next(hit for hit in explained["hits"] if hit.get("outside_kb"))
    ranking = outside["ranking_explanation"]
    assert ranking["lanes"]["outside_bm25"]["rank"] == 1
    assert ranking["lanes"]["outside_bm25"]["raw_score"] != 0
    assert any(
        stage["stage"] == "scope_kb_auto_widen"
        and stage["segment"] == "outside"
        for stage in ranking["ordering_path"]
    )
    widening = next(
        stage
        for stage in explained["retrieval_profile"]["final_ordering"]["pipeline"]
        if stage["stage"] == "scope_kb_auto_widen"
    )
    assert widening["reserve"] == 1
    assert widening["kb_keep"] == 1
    assert explained["retrieval_profile"]["final_ordering"]["pipeline"][-2:] == [
        {
            "stage": "date_filter",
            "active": False,
            "updated_after": None,
            "updated_before": None,
            "recency_days": None,
            "preserves_order": True,
        },
        {"stage": "final_emit", "count": 2, "preserves_order": True},
    ]
    assert ranking["ordering_path"][-1] == {
        "stage": "final_emit",
        "rank": 2,
    }


def test_auto_widen_does_not_overwrite_primary_vector_evidence_for_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = "Reference/duplicate-widener.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text("# Duplicate widener\n\nduplicate widener\n", encoding="utf-8")

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            assert k > 0
            assert allowed_paths is None
            return [(rel, 0, "duplicate widener", 0.91)]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )

    explained = commands.op_ask_memory(
        tmp_path,
        query="duplicate widener",
        mode="vector",
        graph=False,
        rerank=False,
        scope="kb",
        limit=2,
        detail="compact",
        explain=True,
    )

    assert [hit["path"] for hit in explained["hits"]] == [rel]
    ranking = explained["hits"][0]["ranking_explanation"]
    assert set(ranking["lanes"]) == {"vector"}
    assert ranking["lanes"]["vector"] == {
        "rank": 1,
        "cosine": pytest.approx(0.91),
    }


def test_unit_vector_success_marks_lexical_non_applicable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_unit_page(tmp_path)

    class UnitVectorHit:
        def __init__(self, unit_ref: str, cosine: float) -> None:
            self.unit_ref = unit_ref
            self.cosine = cosine

    class FakeUnitIndex:
        def search_semantic_units(
            self, _query_vector, *, k: int, allowed_unit_refs: set[str]
        ):
            unit_ref = sorted(allowed_unit_refs)[0]
            return [UnitVectorHit(unit_ref, 0.876543219)]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings, "get_embedding_index", lambda _root: FakeUnitIndex()
    )
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )

    explained = commands.op_ask_memory(
        tmp_path,
        query="semantic vector query",
        categories=["config"],
        result_level="unit",
        mode="vector",
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "vector"
    assert profile["lanes"]["bm25"] == {
        "status": "non_applicable",
        "reason": "requested_mode_vector",
    }
    assert "fusion" not in profile
    ranking = explained["hits"][0]["ranking_explanation"]
    assert set(ranking["lanes"]) == {"vector"}
    assert ranking["lanes"]["vector"] == {
        "rank": 1,
        "cosine": pytest.approx(0.876543),
    }


def test_unit_vector_failure_reports_lexical_fallback_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_unit_page(tmp_path)

    class FailingUnitIndex:
        def search_semantic_units(self, *_args, **_kwargs):
            raise RuntimeError("unit vector backend failed")

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings, "get_embedding_index", lambda _root: FailingUnitIndex()
    )
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )

    explained = commands.op_ask_memory(
        tmp_path,
        query="session lifetime",
        categories=["config"],
        result_level="unit",
        mode="vector",
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "vector_lexical_fallback"
    assert profile["lanes"]["vector"]["status"] == "failed"
    assert profile["lanes"]["vector"]["reason"] == "search_failed"
    assert profile["lanes"]["bm25"]["status"] == "participated"
    ranking = explained["hits"][0]["ranking_explanation"]
    assert set(ranking["lanes"]) == {"bm25"}
    assert "fusion" not in ranking


def _rerank_request(
    root: Path,
    *,
    rerank: bool | None,
) -> dict:
    return commands.op_ask_memory(
        root,
        query="private page content",
        mode="hybrid",
        graph=False,
        rerank=rerank,
        scope="kb-only",
        detail="compact",
        explain=True,
    )


def test_rerank_hard_disabled_reason_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="rerank-hard-off", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: False)

    profile = _rerank_request(tmp_path, rerank=True)["retrieval_profile"]

    assert profile["rerank"]["decision"] == "skipped"
    assert profile["rerank"]["reason"] == "hard_disabled"


def test_rerank_warming_reason_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="rerank-warming", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(
        readiness, "should_defer", lambda component: component == "reranker"
    )
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("warming reranker must not execute")
        ),
    )

    profile = _rerank_request(tmp_path, rerank=True)["retrieval_profile"]

    assert profile["rerank"]["decision"] == "deferred"
    assert profile["rerank"]["reason"] == "model_warming"


def test_rerank_runtime_failure_reason_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="rerank-runtime", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    profile = _rerank_request(tmp_path, rerank=True)["retrieval_profile"]

    assert profile["rerank"]["decision"] == "failed"
    assert profile["rerank"]["reason"] == "runtime_failure"


def test_rerank_auto_declined_reason_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="rerank-auto", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(find_module, "auto_rerank_allowed_by_policy", lambda: True)
    monkeypatch.setattr(find_module, "should_rerank", lambda *_args, **_kwargs: False)

    profile = _rerank_request(tmp_path, rerank=None)["retrieval_profile"]

    assert profile["rerank"]["decision"] == "skipped"
    assert profile["rerank"]["reason"] == "auto_policy_declined"


def test_rerank_explicit_false_and_no_hits_have_distinct_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="rerank-explicit-off", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    explicit = _rerank_request(tmp_path, rerank=False)["retrieval_profile"]["rerank"]
    empty = commands.op_ask_memory(
        tmp_path,
        query="no-match-anywhere",
        mode="hybrid",
        graph=False,
        rerank=True,
        scope="kb-only",
        detail="compact",
        explain=True,
    )["retrieval_profile"]["rerank"]

    assert (explicit["decision"], explicit["reason"]) == ("skipped", "explicit_false")
    assert (empty["decision"], empty["reason"]) == ("skipped", "no_hits")


def test_rerank_dependency_unavailable_reason_is_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="rerank-unavailable", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.setattr(
        embeddings,
        "rerank_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ImportError("optional dependency absent")
        ),
    )

    profile = _rerank_request(tmp_path, rerank=True)["retrieval_profile"]

    assert profile["rerank"]["decision"] == "unavailable"
    assert profile["rerank"]["reason"] == "dependency_unavailable"


def test_temporal_lane_reports_rank_contribution_and_recency_multiplier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(
        tmp_path,
        name="recent-regulator",
        updated="2026-07-16",
        priority=3,
        body="A regulator controls the system.",
    )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    explained = commands.op_ask_memory(
        tmp_path,
        query="recent regulator",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["lanes"]["temporal"]["status"] == "participated"
    assert profile["fusion"]["weights"]["temporal"] > 0
    ranking = explained["hits"][0]["ranking_explanation"]
    assert ranking["lanes"]["temporal"]["rank"] == 1
    assert ranking["lanes"]["temporal"]["rrf_contribution"] > 0


def test_available_clip_lane_is_metric_labelled_without_optional_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = "Knowledge Base/Evidence/fixture.jpg.md"
    sidecar = tmp_path / rel
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        "---\ntype: source\nstatus: active\nupdated: 2026-07-16\n"
        "media_type: image\nmedia_file: Knowledge Base/Evidence/fixture.jpg\n---\n\n"
        "# Fixture image\n",
        encoding="utf-8",
    )

    class FakeClipIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            return [(rel.removesuffix(".md"), None, 0.654321987)]

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "get_clip_index", lambda _root: FakeClipIndex())
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda _query: [0.1, 0.2])
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)

    explained = commands.op_ask_memory(
        tmp_path,
        query="purely visual fixture",
        mode="vector",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["lanes"]["clip"]["status"] == "participated"
    assert profile["lanes"]["clip"]["metric"]["name"] == "cosine_similarity"
    ranking = explained["hits"][0]["ranking_explanation"]
    assert ranking["lanes"]["clip"] == {
        "rank": 1,
        "cosine": pytest.approx(0.654322),
    }
    assert "fusion" not in ranking


def test_explanation_free_rerank_does_not_build_multiplier_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="cheap-rerank", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.setattr(
        embeddings, "rerank_pairs", lambda _query, passages: [0.5] * len(passages)
    )

    hits = find_module.find(
        tmp_path,
        query="private page content",
        mode="hybrid",
        graph=False,
        rerank=True,
        scope="kb-only",
    )

    assert hits
    assert all(hit.rerank_multiplier_chain == [] for hit in hits)


def test_explanation_free_candidates_do_not_allocate_multiplier_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="cheap-candidates", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    captured: list[find_candidates.CandidateBundle] = []
    collect_candidates = find_candidates.collect_candidates

    def capture_bundle(*args, **kwargs):
        bundle = collect_candidates(*args, **kwargs)
        captured.append(bundle)
        return bundle

    monkeypatch.setattr(find_candidates, "collect_candidates", capture_bundle)

    hits = find_module.find(
        tmp_path,
        query="private page content",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
    )

    assert hits
    assert captured[0].multiplier_chain_by_path is None


def test_unit_filter_only_explanation_uses_parent_date_and_source_order(
    tmp_path: Path,
) -> None:
    parent_path = _write_unit_page(tmp_path)

    explained = commands.op_ask_memory(
        tmp_path,
        query="",
        categories=["config"],
        result_level="unit",
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    assert [hit["source_anchor"] for hit in explained["hits"]] == [
        "session-life",
        "refresh-rule",
    ]
    profile = explained["retrieval_profile"]
    assert profile["effective_result_level"] == "unit"
    assert profile["effective_mode"] == "filter_only"
    assert "fusion" not in profile
    for source_order, hit in enumerate(explained["hits"]):
        ranking = hit["ranking_explanation"]
        assert ranking["lanes"] == {
            "filtered_most_recent": {"rank": source_order + 1}
        }
        assert ranking["final_sort_tuple"] == [
            "2026-07-16",
            parent_path,
            source_order,
        ]


def test_unit_lexical_fallback_has_raw_score_without_invented_fusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_unit_page(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    explained = commands.op_ask_memory(
        tmp_path,
        query="session lifetime",
        categories=["config"],
        result_level="unit",
        mode="hybrid",
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "hybrid_lexical"
    assert "fusion" not in profile
    assert profile["lanes"]["vector"]["status"] == "disabled"
    ranking = explained["hits"][0]["ranking_explanation"]
    assert set(ranking["lanes"]) == {"bm25"}
    assert ranking["lanes"]["bm25"]["raw_score"] != 0
    assert "fusion" not in ranking
    assert ranking["multipliers"] == [
        {
            "name": "status",
            "factor": 1.0,
            "before": ranking["lanes"]["bm25"]["raw_score"],
            "after": ranking["lanes"]["bm25"]["raw_score"],
        }
    ]


def test_reranker_explanation_preserves_raw_and_adjusted_ordering_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = _write_page(
        tmp_path, name="rerank-winner", updated="2026-07-14", priority=3
    )
    _write_page(tmp_path, name="rerank-other", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)

    def fake_rerank(_query: str, passages: list[str]):
        return [0.9 if "rerank-winner" in passage else 0.1 for passage in passages]

    monkeypatch.setattr(embeddings, "rerank_pairs", fake_rerank)
    explained = commands.op_ask_memory(
        tmp_path,
        query="private page content",
        mode="hybrid",
        graph=False,
        rerank=True,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    assert explained["hits"][0]["path"] == winner
    profile = explained["retrieval_profile"]
    assert profile["rerank"]["ran"] is True
    assert profile["rerank"]["model"] == embeddings.RERANKER_NAME
    assert profile["rerank"]["metric"]["direction"] == "higher"
    ranking = explained["hits"][0]["ranking_explanation"]
    assert ranking["reranker"]["raw_score"] == pytest.approx(0.9)
    assert ranking["reranker"]["adjusted_score"] == pytest.approx(0.9 * 1.15)
    assert [step["name"] for step in ranking["reranker"]["multipliers"]] == [
        "type",
        "status",
    ]
    assert ranking["final_sort_tuple"] == [
        pytest.approx(0.9 * 1.15),
        ranking["reranker"]["input_rank"],
    ]


def test_typed_graph_explanation_names_seed_relation_direction_and_hop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = "Knowledge Base/Notes/Insights/graph-seed.md"
    target = "Knowledge Base/Notes/Insights/graph-target.md"
    for rel, body in (
        (
            seed,
            "# Graph seed\n\nchlorophyllneedle chlorophyllneedle\n\n"
            "## Relations\n\n"
            "- supports [[Knowledge Base/Notes/Insights/graph-target]]\n",
        ),
        (target, "# Graph target\n\nNo lexical overlap lives here.\n"),
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: insight\nstatus: active\nupdated: 2026-07-16\n---\n\n"
            + body,
            encoding="utf-8",
        )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    explained = commands.op_ask_memory(
        tmp_path,
        query="chlorophyllneedle",
        mode="hybrid",
        graph=True,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    hit = next(
        (item for item in explained["hits"] if item["path"] == target),
        None,
    )
    assert hit is not None, explained
    graph = hit["ranking_explanation"]["lanes"]["graph"]
    assert graph["rank"] == 1
    assert graph["provenance"] == {
        "seed": seed,
        "relation_type": "supports",
        "direction": "outbound",
        "hop": 1,
    }
    assert graph["rrf_contribution"] == round(1.0 / 61.0, 6)


def test_mixed_explanation_reports_the_actual_page_unit_result_fusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_unit_page(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    explained = commands.op_ask_memory(
        tmp_path,
        query="session lifetime",
        categories=["config"],
        result_level="mixed",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    profile = explained["retrieval_profile"]
    assert profile["effective_result_level"] == "mixed"
    assert profile["result_fusion"] == {
        "algorithm": "weighted_rrf",
        "k": 60,
        "weights": {"page": 1.0, "unit": 1.0},
        "unit_parent_cap": 3,
    }
    assert {hit["result_type"] for hit in explained["hits"]} == {
        "page",
        "semantic_unit",
    }
    for hit in explained["hits"]:
        ranking = hit["ranking_explanation"]
        lane_name = "page" if hit["result_type"] == "page" else "unit"
        mixed = ranking["result_fusion"]
        assert mixed["lane"] == lane_name
        assert mixed["contribution"] == round(
            1.0 / (60 + mixed["lane_rank"]), 6
        )
        assert ranking["final_sort_tuple"] == [
            mixed["contribution"],
            0 if lane_name == "page" else 1,
            hit.get("path") or hit["unit_ref"],
        ]


def test_mixed_explanation_preserves_divergent_page_and_unit_plans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_path = _write_unit_page(tmp_path)

    class FakeIndex:
        def search(self, _query_vector, *, k: int, allowed_paths=None):
            assert k > 0
            assert allowed_paths == {page_path}
            return [(page_path, 0, "Session lifetime is thirty days", 0.82)]

        def search_semantic_units(self, *_args, **_kwargs):
            raise RuntimeError("unit vector backend failed")

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FakeIndex())
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda _texts, *, is_query: [[0.1, 0.2]]
    )

    explained = commands.op_ask_memory(
        tmp_path,
        query="session lifetime",
        categories=["config"],
        result_level="mixed",
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    plans = explained["retrieval_profile"]["result_plans"]
    assert set(plans) == {"page", "unit"}
    assert plans["page"]["effective_mode"] == "hybrid"
    assert plans["page"]["lanes"]["vector"]["status"] == "participated"
    assert plans["page"]["fusion"]["weights"] == {
        "vector": 1.0,
        "bm25": 1.0,
        "keyword": 1.0,
    }
    assert plans["page"]["rerank"]["reason"] == "explicit_false"
    assert plans["unit"]["effective_mode"] == "hybrid_lexical"
    assert plans["unit"]["lanes"]["vector"] == {
        "status": "failed",
        "reason": "search_failed",
        "model": "BAAI/bge-base-en-v1.5",
    }
    assert plans["unit"]["lanes"]["bm25"]["status"] == "participated"
    assert "fusion" not in plans["unit"]
    assert plans["unit"]["rerank"]["reason"] == "result_level_unit"
    assert plans["page"]["final_ordering"] != plans["unit"]["final_ordering"]


def test_disabled_vector_mode_explains_its_useful_keyword_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(tmp_path, name="vector-fallback", updated="2026-07-16", priority=3)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    explained = commands.op_ask_memory(
        tmp_path,
        query="private page content",
        mode="vector",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )

    assert explained["hits"]
    profile = explained["retrieval_profile"]
    assert profile["effective_mode"] == "keyword_fallback"
    assert profile["lanes"]["vector"]["status"] == "disabled"
    assert profile["lanes"]["keyword"]["status"] == "participated"
    assert "fusion" not in profile
    assert set(explained["hits"][0]["ranking_explanation"]["lanes"]) == {
        "keyword"
    }
