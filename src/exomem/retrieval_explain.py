"""Bounded, opt-in serialization for retrieval-plan and hit evidence."""

from __future__ import annotations

import copy
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
    rerank_candidate_limit_requested: int | None = None
    rerank_candidate_limit_effective: int = 0
    rerank_candidate_limit_hard_max: int = 0
    rerank_scorer_input_count: int = 0
    rerank_unscored_tail_count: int = 0
    intent: str = "conceptual"
    effective_mode: str = "hybrid"
    effective_result_level: str = "page"
    normalized_filters: dict[str, Any] = field(default_factory=dict)
    compute_profile: dict[str, str | bool] = field(default_factory=dict)
    lane_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    fusion_profile: dict[str, Any] | None = None
    final_ordering: dict[str, Any] = field(default_factory=dict)
    rerank_profile: dict[str, Any] = field(default_factory=dict)
    result_fusion_profile: dict[str, Any] | None = None
    result_plans: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record_rerank_bound(
        self,
        *,
        requested: int | None,
        effective: int,
        hard_max: int,
    ) -> None:
        self.rerank_candidate_limit_requested = requested
        self.rerank_candidate_limit_effective = effective
        self.rerank_candidate_limit_hard_max = hard_max

    def _rerank_candidate_profile(self) -> dict[str, int | None]:
        return {
            "candidate_limit_requested": self.rerank_candidate_limit_requested,
            "candidate_limit_effective": self.rerank_candidate_limit_effective,
            "candidate_limit_hard_max": self.rerank_candidate_limit_hard_max,
            "scorer_input_count": self.rerank_scorer_input_count,
            "unscored_tail_count": self.rerank_unscored_tail_count,
        }

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
        self.fusion_profile = None
        self.lane_profiles = _base_lane_profiles(reason=reason)
        self.lane_profiles[lane] = {
            "status": "participated",
            "metric": (
                {
                    "name": "updated_timestamp",
                    "direction": "descending",
                    "rounding": "none",
                }
                if filter_only
                else {"name": "rank", "direction": "lower", "rounding": "none"}
            ),
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
            **self._rerank_candidate_profile(),
            "ran": False,
            "decision": "skipped",
            "reason": "empty_query" if filter_only else "requested_mode_keyword",
        }
        for rank, hit in enumerate(hits, start=1):
            self.evidence_by_id[hit.path] = {
                "lanes": {lane: {"rank": rank}},
                "ordering_path": [{"stage": lane, "rank": rank}],
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
        self.rerank_profile.update(
            decision="skipped",
            reason="no_hits" if not hits else "candidate_lanes_empty",
        )

    def record_page_candidates(
        self,
        bundle: Any,
        hits: list[Any],
        *,
        reranker_model: str,
        rerank_outcome: dict[str, Any],
        scorer_input_count: int,
        unscored_tail_count: int,
    ) -> None:
        self.effective_mode = self.requested_mode
        vector_status = bundle.lane_statuses.get("vector", {}).get("status")
        if self.requested_mode == "hybrid":
            self.effective_mode = (
                "hybrid"
                if vector_status in {"participated", "available_nonmatching"}
                else "hybrid_lexical"
            )
        self.lane_profiles = _base_lane_profiles(reason="not_requested")
        self.lane_profiles.update(bundle.lane_statuses)
        active_lanes = [name for name, ranking in bundle.lane_rankings.items() if ranking]
        fused = len(active_lanes) >= 2
        self.fusion_profile = None
        if fused:
            self.fusion_profile = {
                "algorithm": "weighted_rrf",
                "k": bundle.rrf_k,
                "weights": {
                    name: bundle.lane_weights[name] for name in active_lanes
                },
            }
        self.rerank_scorer_input_count = scorer_input_count
        self.rerank_unscored_tail_count = unscored_tail_count
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
            **self._rerank_candidate_profile(),
            "ran": rerank_ran,
            **rerank_outcome,
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
                }
                if fused:
                    lane.update(
                        {
                            "weight": bundle.lane_weights[lane_name],
                            "rrf_k": bundle.rrf_k,
                            "rrf_contribution": bundle.lane_weights[lane_name]
                            / (bundle.rrf_k + rank),
                        }
                    )
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
            multiplier_chains = bundle.multiplier_chain_by_path
            if multiplier_chains is None:
                raise RuntimeError("trace capture missing multiplier evidence storage")
            evidence: dict[str, Any] = {
                "lanes": lanes,
                "multipliers": multiplier_chains.get(hit.path, []),
                "ordering_path": [{"stage": "candidate_ranking"}],
                "final_sort_tuple": [bundle.adjusted_score_by_path[hit.path], hit.path],
                "tie_breaks": {"path": hit.path},
            }
            if fused:
                evidence["fusion"] = {
                    "rrf_sum": bundle.raw_fused_score_by_path[hit.path]
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

    def record_outside_candidates(
        self,
        hits: list[Any],
        *,
        scores: dict[str, float],
        backend: str,
    ) -> None:
        """Record the lexical lane used only by default-scope widening."""
        lane_name = "outside_keyword" if backend == "keyword_fallback" else "outside_bm25"
        metric = (
            {"name": "rank", "direction": "lower", "rounding": "none"}
            if lane_name == "outside_keyword"
            else {
                "name": "raw_bm25_score",
                "direction": "higher",
                "range": "backend_dependent",
                "rounding": 6,
                "caveat": "diagnostic; not comparable across backends or corpora",
            }
        )
        self.lane_profiles[lane_name] = {
            "status": "participated" if hits else "available_nonmatching",
            "backend": backend,
            "metric": metric,
        }
        for rank, hit in enumerate(hits, start=1):
            lane_evidence: dict[str, Any] = {"rank": rank}
            if lane_name == "outside_bm25":
                lane_evidence["raw_score"] = round(scores[hit.path], 6)
            self.evidence_by_id[hit.path] = {
                "lanes": {lane_name: lane_evidence},
                "ordering_path": [
                    {
                        "stage": lane_name,
                        "rank": rank,
                    }
                ],
            }

    def record_auto_widen(
        self,
        hits: list[Any],
        *,
        strong_paths: list[str],
        weak_paths: list[str],
        outside_paths: list[str],
        reserve: int,
        kb_keep: int,
    ) -> None:
        """Finalize the stable reserved-tail merge performed above page ranking."""
        prior_sort = self.final_ordering.get("sort", [])
        self.final_ordering = {
            "pipeline": [
                {"stage": "candidate_ranking", "sort": prior_sort},
                {
                    "stage": "scope_kb_auto_widen",
                    "policy": "reserved_tail",
                    "reserve": reserve,
                    "kb_keep": kb_keep,
                    "segments": ["strong_kb", "weak_kb", "outside"],
                    "stable_within_segment": True,
                },
            ],
            "sort": [
                {"field": "merge_segment", "direction": "ascending"},
                {"field": "stable_segment_rank", "direction": "ascending"},
            ],
        }
        segments = (
            ("strong_kb", strong_paths),
            ("weak_kb", weak_paths),
            ("outside", outside_paths),
        )
        position = {
            path: (segment_order, segment_name, rank)
            for segment_order, (segment_name, paths) in enumerate(segments)
            for rank, path in enumerate(paths, start=1)
        }
        for hit in hits:
            segment_order, segment_name, segment_rank = position[hit.path]
            evidence = self.evidence_by_id.setdefault(hit.path, {"lanes": {}})
            evidence.setdefault("ordering_path", []).append(
                {
                    "stage": "scope_kb_auto_widen",
                    "segment": segment_name,
                    "segment_rank": segment_rank,
                }
            )
            evidence["candidate_sort_tuple"] = evidence.get("final_sort_tuple")
            evidence["final_sort_tuple"] = [segment_order, segment_rank]
            evidence["tie_breaks"] = {"stable_segment_rank": segment_rank}

    def finalize_page_results(
        self,
        hits: list[Any],
        *,
        updated_after: str | None,
        updated_before: str | None,
        recency_days: int | None,
    ) -> None:
        """Finalize page evidence after widening and top-level date filtering."""
        pipeline = list(self.final_ordering.get("pipeline", []))
        if not pipeline:
            pipeline.append(
                {
                    "stage": "candidate_ranking",
                    "sort": self.final_ordering.get("sort", []),
                }
            )
        date_stage = {
            "stage": "date_filter",
            "active": any(
                value is not None
                for value in (updated_after, updated_before, recency_days)
            ),
            "updated_after": updated_after,
            "updated_before": updated_before,
            "recency_days": recency_days,
            "preserves_order": True,
        }
        pipeline.extend(
            (date_stage, {"stage": "final_emit", "count": len(hits), "preserves_order": True})
        )
        self.final_ordering["pipeline"] = pipeline
        for rank, hit in enumerate(hits, start=1):
            if hit.path not in self.evidence_by_id:
                raise RuntimeError(f"missing ranking evidence for emitted hit: {hit.path}")
            evidence = self.evidence_by_id[hit.path]
            ordering_path = evidence.setdefault("ordering_path", [])
            ordering_path.append(
                {
                    "stage": "date_filter",
                    "passed": True,
                    "active": date_stage["active"],
                }
            )
            ordering_path.append({"stage": "final_emit", "rank": rank})

    def record_unit_filter_only(
        self,
        ordered: list[tuple[Any, Any, int]],
    ) -> None:
        self.effective_mode = "filter_only"
        self.fusion_profile = None
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
            **self._rerank_candidate_profile(),
            "ran": False,
            "decision": "skipped",
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
        self.fusion_profile = None
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
            **self._rerank_candidate_profile(),
            "ran": False,
            "decision": "skipped",
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
        raw_fused_score_by_ref: dict[str, float],
        weights: tuple[float, float],
        rrf_k: int,
        prefer_active: bool,
        superseded_penalty: float,
        lexical_used: bool,
        vector_used: bool,
    ) -> None:
        self.fusion_profile = None
        self.lane_profiles = _base_lane_profiles(reason="not_requested")
        if lexical_used:
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
        elif self.requested_mode == "vector":
            self.lane_profiles["bm25"] = {
                "status": "non_applicable",
                "reason": "requested_mode_vector",
            }
        self.lane_profiles["vector"] = vector_profile
        self.rerank_profile = {
            "requested": self.rerank_requested,
            "auto_allowed": self.auto_rerank,
            **self._rerank_candidate_profile(),
            "ran": False,
            "decision": "skipped",
            "reason": "result_level_unit",
        }
        lexical_participated = bool(lexical_used and lexical_ranking)
        vector_participated = bool(vector_used and vector_ranking)
        fused = bool(
            vector_participated
            and lexical_participated
            and self.requested_mode == "hybrid"
        )
        if fused:
            self.effective_mode = "hybrid"
            self.fusion_profile = {
                "algorithm": "weighted_rrf",
                "k": rrf_k,
                "weights": {"vector": weights[0], "bm25": weights[1]},
            }
        elif vector_participated:
            self.effective_mode = "vector"
        elif lexical_participated and self.requested_mode == "vector":
            self.effective_mode = "vector_lexical_fallback"
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
            if lexical_used and unit_ref in lexical_rank:
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
            if vector_used and unit_ref in vector_rank:
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
                raw_score = raw_fused_score_by_ref[unit_ref]
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

    def snapshot_result_plan(self, result_type: str) -> None:
        """Preserve one mixed-result subpipeline before the shared trace advances."""
        plan = {
            "effective_mode": self.effective_mode,
            "lanes": copy.deepcopy(self.lane_profiles),
            "rerank": copy.deepcopy(self.rerank_profile),
            "final_ordering": copy.deepcopy(self.final_ordering),
        }
        if self.fusion_profile is not None:
            plan["fusion"] = copy.deepcopy(self.fusion_profile)
        self.result_plans[result_type] = plan

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
            "compute": self.compute_profile,
            "lanes": self.lane_profiles,
            "rerank": self.rerank_profile,
            "final_ordering": self.final_ordering,
        }
        if self.fusion_profile is not None:
            out["fusion"] = self.fusion_profile
        if self.result_fusion_profile is not None:
            out["result_fusion"] = self.result_fusion_profile
        if self.effective_result_level == "mixed" and self.result_plans:
            out["result_plans"] = self.result_plans
        return _round_public_metrics(out)


def attach_hit_explanations(
    trace: RetrievalTrace,
    hits: list[dict[str, Any]],
) -> None:
    """Attach bounded evidence by stable result identity after serialization."""
    for final_rank, hit in enumerate(hits, start=1):
        identity = str(hit.get("unit_ref") or hit.get("path") or "")
        if identity not in trace.evidence_by_id:
            raise RuntimeError(f"missing ranking evidence for emitted hit: {identity}")
        evidence = dict(trace.evidence_by_id[identity])
        evidence["final_rank"] = final_rank
        hit["ranking_explanation"] = _round_public_metrics(evidence)


def _round_public_metrics(value: Any) -> Any:
    """Apply the declared six-decimal precision at the serialization boundary."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_round_public_metrics(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_public_metrics(item) for key, item in value.items()}
    return value


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
