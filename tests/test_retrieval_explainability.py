from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from exomem import commands
from exomem import embeddings
from exomem import epistemic_graph
from exomem import readiness


def _write_page(root: Path, *, name: str, updated: str, priority: int) -> str:
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
        "Private page content must not leak into compact explanations.\n",
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
    assert profile["fusion"] == {"algorithm": "weighted_rrf", "k": 60}

    first = explained["hits"][0]["ranking_explanation"]
    assert set(first["lanes"]) == {"bm25", "keyword"}
    assert first["lanes"]["bm25"]["raw_score"] != 0
    assert first["lanes"]["keyword"] == {
        "rank": first["lanes"]["keyword"]["rank"],
        "weight": 1.0,
        "rrf_k": 60,
        "rrf_contribution": pytest.approx(
            1.0 / (60 + first["lanes"]["keyword"]["rank"])
        ),
    }
    assert first["fusion"]["rrf_sum"] == pytest.approx(
        sum(lane["rrf_contribution"] for lane in first["lanes"].values())
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
    assert vector_lane["rrf_contribution"] == pytest.approx(1.0 / 61.0)
    assert "vector" not in hits[lexical_only_path]["ranking_explanation"]["lanes"]


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
        {"name": "status", "factor": 1.0, "before": ranking["lanes"]["bm25"]["raw_score"], "after": ranking["lanes"]["bm25"]["raw_score"]}
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
    assert graph["rrf_contribution"] == pytest.approx(1.0 / 61.0)


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
        assert mixed["contribution"] == pytest.approx(
            1.0 / (60 + mixed["lane_rank"])
        )
        assert ranking["final_sort_tuple"] == [
            mixed["contribution"],
            0 if lane_name == "page" else 1,
            hit.get("path") or hit["unit_ref"],
        ]


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
