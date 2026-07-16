"""Shared public response types for page, unit, mixed, and explained recall."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypeAlias

from typing_extensions import TypedDict


class MetricProfile(TypedDict):
    name: str
    direction: str
    rounding: int | str
    range: NotRequired[str | list[float]]
    caveat: NotRequired[str]


class LaneProfile(TypedDict):
    status: str
    reason: NotRequired[str]
    backend: NotRequired[str]
    model: NotRequired[str]
    metric: NotRequired[MetricProfile]


class RerankProfile(TypedDict):
    requested: bool | None
    auto_allowed: bool
    ran: bool
    decision: str
    reason: str
    model: NotRequired[str]
    backend: NotRequired[str]
    metric: NotRequired[MetricProfile]


class RetrievalSubplan(TypedDict):
    effective_mode: str
    lanes: dict[str, LaneProfile]
    fusion: NotRequired[dict[str, Any]]
    rerank: RerankProfile
    final_ordering: dict[str, Any]


class RetrievalResultPlans(TypedDict):
    page: RetrievalSubplan
    unit: RetrievalSubplan


class RetrievalProfile(TypedDict):
    schema_version: int
    intent: str
    requested_mode: str
    effective_mode: str
    requested_result_level: str
    effective_result_level: str
    normalized_filters: dict[str, Any]
    compute: dict[str, str | bool]
    lanes: dict[str, LaneProfile]
    fusion: NotRequired[dict[str, Any]]
    result_fusion: NotRequired[dict[str, Any]]
    result_plans: NotRequired[RetrievalResultPlans]
    rerank: RerankProfile
    final_ordering: dict[str, Any]


class LaneEvidence(TypedDict):
    rank: int
    raw_score: NotRequired[float]
    cosine: NotRequired[float]
    weight: NotRequired[float]
    rrf_k: NotRequired[int]
    rrf_contribution: NotRequired[float]
    provenance: NotRequired[dict[str, str | int]]


class MultiplierEvidence(TypedDict):
    name: str
    factor: float
    before: float
    after: float


class RankingExplanation(TypedDict):
    lanes: dict[str, LaneEvidence]
    fusion: NotRequired[dict[str, float]]
    multipliers: NotRequired[list[MultiplierEvidence]]
    reranker: NotRequired[dict[str, Any]]
    result_fusion: NotRequired[dict[str, Any]]
    ordering_path: NotRequired[list[dict[str, Any]]]
    candidate_sort_tuple: NotRequired[list[Any] | None]
    final_sort_tuple: list[Any]
    tie_breaks: dict[str, Any]
    final_rank: int


class PageHit(TypedDict):
    path: str
    type: str | None
    scope: str | None
    title: str
    updated: str
    result_type: NotRequired[Literal["page"]]
    ref: NotRequired[str]
    excerpt: NotRequired[str]
    outside_kb: NotRequired[bool]
    status: NotRequired[str]
    superseded_by: NotRequired[list[str]]
    signals: NotRequired[dict[str, Any]]
    graph: NotRequired[dict[str, str | None]]
    media_type: NotRequired[str]
    media_file: NotRequired[str]
    clip_match_at: NotRequired[str]
    scene_frame: NotRequired[str]
    scene_match_at: NotRequired[str]
    transcript_match_at: NotRequired[str]
    matched_units: NotRequired[list[dict[str, Any]]]
    matched_units_truncated: NotRequired[int]
    mixed_units_truncated: NotRequired[int]
    ranking_explanation: NotRequired[RankingExplanation]


class SemanticUnitHit(TypedDict):
    result_type: Literal["semantic_unit"]
    unit_ref: str
    category: str
    kind: str
    excerpt: str
    source_anchor: str | None
    parent_path: str
    parent_ref: str | None
    parent_title: str
    parent_type: str | None
    parent_status: str | None
    parent_updated: str
    form: NotRequired[str]
    category_raw: NotRequired[str]
    category_key: NotRequired[str]
    content: NotRequired[str]
    tags: NotRequired[list[str]]
    context: NotRequired[str | None]
    source_span: NotRequired[dict[str, int]]
    source_hash: NotRequired[str]
    parent_superseded_by: NotRequired[list[str]]
    signals: NotRequired[dict[str, Any]]
    mixed_units_truncated: NotRequired[int]
    ranking_explanation: NotRequired[RankingExplanation]


RetrievalHit: TypeAlias = PageHit | SemanticUnitHit


class FindEnvelope(TypedDict):
    hits: list[RetrievalHit]
    pack: NotRequired[dict[str, Any]]
    timings: NotRequired[dict[str, Any]]
    warming: NotRequired[dict[str, Any]]
    degraded: NotRequired[list[str]]
    retrieval_profile: NotRequired[RetrievalProfile]
