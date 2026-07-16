"""Bounded, opt-in serialization for retrieval-plan and hit evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 1
TEXT_MODEL_NAME = "BAAI/bge-base-en-v1.5"


@dataclass(slots=True)
class RetrievalTrace:
    """Request-scoped trace collector; never constructed for ordinary recall."""

    requested_mode: str
    requested_result_level: str
    rerank_requested: bool | None
    auto_rerank: bool
    intent: str = "conceptual"
    effective_mode: str = "hybrid"
    effective_result_level: str = "page"
    normalized_filters: dict[str, Any] = field(default_factory=dict)
    lane_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    fusion_profile: dict[str, Any] | None = None
    final_ordering: dict[str, Any] = field(default_factory=dict)
    rerank_profile: dict[str, Any] = field(default_factory=dict)
    result_fusion_profile: dict[str, Any] | None = None

    def record_plan(
        self,
        *,
        query: str,
        intent: str,
        effective_result_level: str,
        normalized_filters: dict[str, Any],
    ) -> None:
        self.intent = intent
        self.effective_mode = self.requested_mode
        self.effective_result_level = effective_result_level
        self.normalized_filters = normalized_filters
        if not query.strip():
            self.intent = "filter_only"
            self.effective_mode = "filter_only"

    def record_keyword_hits(self, hits: list[Any], *, filter_only: bool) -> None:
        lane = "filtered_most_recent" if filter_only else "keyword"
        reason = "empty_query" if filter_only else "requested_mode_keyword"
        self.effective_mode = "filter_only" if filter_only else "keyword"
        self.lane_profiles = _base_lane_profiles(reason=reason)
        self.lane_profiles[lane] = {
            "status": "participated",
            "metric": {
                "name": "updated_timestamp",
                "direction": "descending",
                "rounding": "none",
            },
        }
        self.final_ordering = {
            "sort": [
                {"field": "updated", "direction": "descending"},
                {"field": "path", "direction": "descending"},
            ]
        }
        self.rerank_profile = {
            "requested": self.rerank_requested,
            "auto_allowed": self.auto_rerank,
            "ran": False,
            "reason": "empty_query" if filter_only else "requested_mode_keyword",
        }
        for rank, hit in enumerate(hits, start=1):
            self.evidence_by_id[hit.path] = {
                "lanes": {lane: {"rank": rank}},
                "final_sort_tuple": [hit.updated, hit.path],
                "tie_breaks": {"path": hit.path},
            }

    def record_keyword_fallback(
        self,
        hits: list[Any],
        *,
        lane_profiles: dict[str, dict[str, Any]],
    ) -> None:
        self.record_keyword_hits(hits, filter_only=False)
        keyword_profile = self.lane_profiles["keyword"]
        self.lane_profiles.update(lane_profiles)
        self.lane_profiles["keyword"] = keyword_profile
        self.effective_mode = "keyword_fallback"
        self.rerank_profile["reason"] = "candidate_lanes_empty"

    def record_page_candidates(
        self,
        bundle: Any,
        hits: list[Any],
        *,
        reranker_model: str,
    ) -> None:
        self.effective_mode = self.requested_mode
        if (
            self.requested_mode == "hybrid"
            and bundle.lane_statuses.get("vector", {}).get("status") != "available"
        ):
            self.effective_mode = "hybrid_lexical"
        self.lane_profiles = _base_lane_profiles(reason="not_requested")
        self.lane_profiles.update(bundle.lane_statuses)
        active_lanes = [name for name, ranking in bundle.lane_rankings.items() if ranking]
        if active_lanes:
            self.fusion_profile = {
                "algorithm": "weighted_rrf",
                "k": bundle.rrf_k,
            }
        rerank_ran = any(hit.rerank_raw_score is not None for hit in hits)
        self.final_ordering = (
            {
                "sort": [
                    {"field": "adjusted_reranker_score", "direction": "descending"},
                    {"field": "reranker_input_rank", "direction": "ascending"},
                ]
            }
            if rerank_ran
            else {
                "sort": [
                    {"field": "adjusted_score", "direction": "descending"},
                    {"field": "path", "direction": "ascending"},
                ]
            }
        )
        self.rerank_profile = {
            "requested": self.rerank_requested,
            "auto_allowed": self.auto_rerank,
            "ran": rerank_ran,
            "reason": "ran" if rerank_ran else "request_disabled",
        }
        if rerank_ran:
            self.rerank_profile.update(
                {
                    "model": reranker_model,
                    "backend": "cross_encoder",
                    "metric": {
                        "name": "cross_encoder_score",
                        "direction": "higher",
                        "range": "model_dependent",
                        "rounding": 6,
                    },
                }
            )
        for hit in hits:
            lanes: dict[str, dict[str, Any]] = {}
            for lane_name, ranking in bundle.lane_rankings.items():
                try:
                    rank = ranking.index(hit.path) + 1
                except ValueError:
                    continue
                lane = {
                    "rank": rank,
                    "weight": bundle.lane_weights[lane_name],
                    "rrf_k": bundle.rrf_k,
                    "rrf_contribution": bundle.lane_weights[lane_name]
                    / (bundle.rrf_k + rank),
                }
                if lane_name == "bm25":
                    lane["raw_score"] = bundle.bm25_score_by_path[hit.path]
                elif lane_name in ("vector", "clip"):
                    scores = (
                        bundle.vector_score_by_path
                        if lane_name == "vector"
                        else bundle.clip_score_by_path
                    )
                    lane["cosine"] = scores[hit.path]
                if lane_name == "graph" and hit.graph_provenance is not None:
                    lane["provenance"] = {
                        "seed": hit.graph_provenance.seed,
                        "relation_type": hit.graph_provenance.relation_type,
                        "direction": hit.graph_provenance.direction,
                        "hop": 1,
                    }
                lanes[lane_name] = lane
            evidence: dict[str, Any] = {
                "lanes": lanes,
                "fusion": {"rrf_sum": bundle.raw_fused_score_by_path[hit.path]},
                "multipliers": bundle.multiplier_chain_by_path.get(hit.path, []),
                "final_sort_tuple": [bundle.adjusted_score_by_path[hit.path], hit.path],
                "tie_breaks": {"path": hit.path},
            }
            if hit.rerank_raw_score is not None:
                evidence["reranker"] = {
                    "raw_score": hit.rerank_raw_score,
                    "adjusted_score": hit.rerank_score,
                    "input_rank": hit.rerank_input_rank,
                    "multipliers": hit.rerank_multiplier_chain,
                }
                evidence["final_sort_tuple"] = [
                    hit.rerank_score,
                    hit.rerank_input_rank,
                ]
                evidence["tie_breaks"] = {
                    "reranker_input_rank": hit.rerank_input_rank
                }
            self.evidence_by_id[hit.path] = evidence

    def record_unit_filter_only(
        self,
        ordered: list[tuple[Any, Any, int]],
    ) -> None:
        self.effective_mode = "filter_only"
        self.lane_profiles = _base_lane_profiles(reason="empty_query")
        self.lane_profiles["filtered_most_recent"] = {
            "status": "participated",
            "metric": {
                "name": "parent_updated_timestamp",
                "direction": "descending",
                "rounding": "none",
            },
        }
        self.final_ordering = {
            "sort": [
                {"field": "parent_updated", "direction": "descending"},
                {"field": "parent_path", "direction": "descending"},
                {"field": "source_order", "direction": "ascending"},
            ]
        }
        self.rerank_profile = {
            "requested": self.rerank_requested,
            "auto_allowed": self.auto_rerank,
            "ran": False,
            "reason": "empty_query",
        }
        for rank, (page, unit, source_order) in enumerate(ordered, start=1):
            self.evidence_by_id[unit.unit_ref] = {
                "lanes": {"filtered_most_recent": {"rank": rank}},
                "final_sort_tuple": [page.updated, page.rel_path, source_order],
                "tie_breaks": {
                    "parent_path": page.rel_path,
                    "source_order": source_order,
                },
            }

    def record_unit_keyword(
        self,
        ordered: list[tuple[Any, Any, int]],
    ) -> None:
        self.effective_mode = "keyword"
        self.lane_profiles = _base_lane_profiles(reason="requested_mode_keyword")
        self.lane_profiles["keyword"] = {
            "status": "participated" if ordered else "available_nonmatching",
            "backend": "case_insensitive_substring",
            "metric": {"name": "rank", "direction": "lower", "rounding": "none"},
        }
        self.final_ordering = {
            "sort": [
                {"field": "active_status", "direction": "ascending"},
                {"field": "parent_updated", "direction": "descending"},
                {"field": "parent_path", "direction": "descending"},
                {"field": "source_order", "direction": "ascending"},
            ]
        }
        self.rerank_profile = {
            "requested": self.rerank_requested,
            "auto_allowed": self.auto_rerank,
            "ran": False,
            "reason": "requested_mode_keyword",
        }
        for rank, (page, unit, source_order) in enumerate(ordered, start=1):
            self.evidence_by_id[unit.unit_ref] = {
                "lanes": {"keyword": {"rank": rank}},
                "final_sort_tuple": [
                    page.status == "superseded",
                    page.updated,
                    page.rel_path,
                    source_order,
                ],
                "tie_breaks": {
                    "parent_path": page.rel_path,
                    "source_order": source_order,
                },
            }

    def record_unit_ranked(
        self,
        *,
        records: dict[str, tuple[Any, Any, int]],
        lexical_ranking: list[str],
        lexical_scores: dict[str, float],
        lexical_backend: str,
        vector_ranking: list[str],
        vector_scores: dict[str, float],
        vector_profile: dict[str, Any],
        final_ranking: list[str],
        weights: tuple[float, float],
        rrf_k: int,
        prefer_active: bool,
        superseded_penalty: float,
    ) -> None:
        self.lane_profiles = _base_lane_profiles(reason="not_requested")
        self.lane_profiles["bm25"] = {
            "status": "participated" if lexical_ranking else "available_nonmatching",
            "backend": lexical_backend,
            "metric": {
                "name": "raw_bm25_score",
                "direction": "higher",
                "range": "backend_dependent",
                "rounding": 6,
                "caveat": "diagnostic; not comparable across backends or corpora",
            },
        }
        self.lane_profiles["vector"] = vector_profile
        self.rerank_profile = {
            "requested": self.rerank_requested,
            "auto_allowed": self.auto_rerank,
            "ran": False,
            "reason": "result_level_unit",
        }
        fused = bool(vector_ranking and lexical_ranking and self.requested_mode == "hybrid")
        if fused:
            self.effective_mode = "hybrid"
            self.fusion_profile = {"algorithm": "weighted_rrf", "k": rrf_k}
        elif vector_ranking and self.requested_mode == "vector":
            self.effective_mode = "vector"
        else:
            self.effective_mode = "hybrid_lexical"
        self.final_ordering = {
            "sort": [
                {"field": "adjusted_score", "direction": "descending"},
                {"field": "superseded", "direction": "ascending"},
                {"field": "parent_path", "direction": "ascending"},
                {"field": "source_order", "direction": "ascending"},
                {"field": "unit_ref", "direction": "ascending"},
            ]
        }
        lexical_rank = {ref: rank for rank, ref in enumerate(lexical_ranking, 1)}
        vector_rank = {ref: rank for rank, ref in enumerate(vector_ranking, 1)}
        for unit_ref in final_ranking:
            page, _unit, source_order = records[unit_ref]
            lanes: dict[str, dict[str, Any]] = {}
            raw_score: float
            if unit_ref in lexical_rank and self.requested_mode != "vector":
                rank = lexical_rank[unit_ref]
                lane: dict[str, Any] = {
                    "rank": rank,
                    "raw_score": lexical_scores[unit_ref],
                }
                if fused:
                    lane.update(
                        {
                            "weight": weights[1],
                            "rrf_k": rrf_k,
                            "rrf_contribution": weights[1] / (rrf_k + rank),
                        }
                    )
                lanes["bm25"] = lane
            if unit_ref in vector_rank:
                rank = vector_rank[unit_ref]
                lane = {"rank": rank, "cosine": vector_scores[unit_ref]}
                if fused:
                    lane.update(
                        {
                            "weight": weights[0],
                            "rrf_k": rrf_k,
                            "rrf_contribution": weights[0] / (rrf_k + rank),
                        }
                    )
                lanes["vector"] = lane
            if fused:
                raw_score = sum(lane["rrf_contribution"] for lane in lanes.values())
            elif "vector" in lanes:
                raw_score = vector_scores[unit_ref]
            else:
                raw_score = lexical_scores[unit_ref]
            factor = 1.0
            if prefer_active and page.status == "superseded":
                factor = superseded_penalty if raw_score >= 0 else 1.0 / superseded_penalty
            adjusted_score = raw_score * factor
            evidence: dict[str, Any] = {
                "lanes": lanes,
                "multipliers": [
                    {
                        "name": "status",
                        "factor": factor,
                        "before": raw_score,
                        "after": adjusted_score,
                    }
                ]
                if prefer_active
                else [],
                "final_sort_tuple": [
                    adjusted_score,
                    bool(prefer_active and page.status == "superseded"),
                    page.rel_path,
                    source_order,
                    unit_ref,
                ],
                "tie_breaks": {
                    "superseded": bool(
                        prefer_active and page.status == "superseded"
                    ),
                    "parent_path": page.rel_path,
                    "source_order": source_order,
                    "unit_ref": unit_ref,
                },
            }
            if fused:
                evidence["fusion"] = {"rrf_sum": raw_score}
            self.evidence_by_id[unit_ref] = evidence

    def record_mixed(
        self,
        ranked_items: list[tuple[float, int, str, Any, int, str]],
        *,
        rrf_k: int,
        page_weight: float,
        unit_weight: float,
        unit_parent_cap: int,
    ) -> None:
        self.result_fusion_profile = {
            "algorithm": "weighted_rrf",
            "k": rrf_k,
            "weights": {"page": page_weight, "unit": unit_weight},
            "unit_parent_cap": unit_parent_cap,
        }
        self.final_ordering = {
            "sort": [
                {"field": "result_fusion_contribution", "direction": "descending"},
                {"field": "result_type_order", "direction": "ascending"},
                {"field": "result_identity", "direction": "ascending"},
            ]
        }
        for contribution, type_order, identity, _hit, lane_rank, lane_name in ranked_items:
            evidence = self.evidence_by_id.setdefault(identity, {})
            evidence["result_fusion"] = {
                "lane": lane_name,
                "lane_rank": lane_rank,
                "weight": page_weight if lane_name == "page" else unit_weight,
                "rrf_k": rrf_k,
                "contribution": contribution,
            }
            evidence["final_sort_tuple"] = [contribution, type_order, identity]
            evidence["tie_breaks"] = {
                "result_type_order": type_order,
                "result_identity": identity,
            }

    def profile(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "intent": self.intent,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "requested_result_level": self.requested_result_level,
            "effective_result_level": self.effective_result_level,
            "normalized_filters": self.normalized_filters,
            "lanes": self.lane_profiles,
            "rerank": self.rerank_profile,
            "final_ordering": self.final_ordering,
        }
        if self.fusion_profile is not None:
            out["fusion"] = self.fusion_profile
        if self.result_fusion_profile is not None:
            out["result_fusion"] = self.result_fusion_profile
        return out


def attach_hit_explanations(
    trace: RetrievalTrace,
    hits: list[dict[str, Any]],
) -> None:
    """Attach bounded evidence by stable result identity after serialization."""
    for final_rank, hit in enumerate(hits, start=1):
        identity = str(hit.get("unit_ref") or hit.get("path") or "")
        evidence = dict(trace.evidence_by_id.get(identity, {}))
        evidence["final_rank"] = final_rank
        hit["ranking_explanation"] = evidence


def _base_lane_profiles(*, reason: str) -> dict[str, dict[str, Any]]:
    return {
        lane: {"status": "non_applicable", "reason": reason}
        for lane in (
            "filtered_most_recent",
            "vector",
            "bm25",
            "keyword",
            "clip",
            "graph",
            "temporal",
        )
    }
