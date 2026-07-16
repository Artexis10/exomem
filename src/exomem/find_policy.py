"""Ranking, intent, temporal, and rerank policy for find()."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date, timedelta

from .find_types import Hit
from .ranking_config import DEFAULT_RANKING, RankingConfig

COMPILED_TYPES = frozenset(
    {
        "insight",
        "pattern",
        "failure",
        "research-note",
        "entity",
        "production-log",
        "experiment",
    }
)
SOURCE_TYPES = frozenset({"source"})
COMPILED_BOOST = 1.15
SOURCE_PENALTY = 0.85
SUPERSEDED_PENALTY = 0.5

TEMPORAL_MARKERS = re.compile(
    r"\b(recent|recently|latest|newest|today|yesterday|tonight|"
    r"week|weeks|month|months|year|years|"
    r"when|before|after|since|until|ago|"
    r"20\d\d|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
RELATIONSHIP_MARKERS = re.compile(
    r"\b(links?|linked|relate[sd]?|related|relationship|"
    r"connect(?:s|ed|ion|ions)?|cite[sd]?|citations?|"
    r"mention(?:s|ed)?)\b",
    re.IGNORECASE,
)
EXACT_LEADING = re.compile(r"^(who|whose|what|which)\b", re.IGNORECASE)

PageOf = Callable[[str], object | None]


def type_multiplier(
    page_type: str | None, config: RankingConfig = DEFAULT_RANKING
) -> float:
    if page_type in COMPILED_TYPES:
        return config.compiled_boost
    if page_type in SOURCE_TYPES:
        return config.source_penalty
    return 1.0


def status_multiplier(
    status: str | None, config: RankingConfig = DEFAULT_RANKING
) -> float:
    """Demote superseded tombstones; everything else is neutral."""
    if status == "superseded":
        return config.superseded_penalty
    return 1.0


def apply_type_boost(
    fused: list[tuple[str, float]],
    page_of: PageOf,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    """Re-sort fused `(path, score)` pairs after applying per-type multipliers."""
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = page_of(path)
        if page is not None and getattr(page, "media_type", None):
            mult = 1.0
        else:
            mult = type_multiplier(
                getattr(page, "page_type", None) if page is not None else None,
                config,
            )
        adjusted.append((path, score * mult))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def apply_status_demotion(
    fused: list[tuple[str, float]],
    page_of: PageOf,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    """Re-sort fused `(path, score)` pairs after demoting superseded pages."""
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = page_of(path)
        mult = status_multiplier(getattr(page, "status", None), config)
        adjusted.append((path, score * mult))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def apply_post_rrf_multipliers(
    fused: list[tuple[str, float]],
    query: str,
    config: RankingConfig,
    *,
    prefer_compiled: bool,
    prefer_active: bool,
    temporal: bool,
    page_of: PageOf,
    usage_map: dict[str, float] | None = None,
    evidence_out: dict[str, list[dict[str, float | str]]] | None = None,
) -> list[tuple[str, float]]:
    """All post-RRF multiplicative boosts in one pass with one final sort."""
    temporal_active = temporal and config.temporal_boost != 1.0 and is_temporal_query(query)
    usage_active = bool(usage_map)
    if not (prefer_compiled or prefer_active or temporal_active or usage_active):
        return fused
    if usage_active:
        from . import usage as usage_module
    today = date.today() if temporal_active else None
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = page_of(path)
        chain: list[dict[str, float | str]] | None = (
            [] if evidence_out is not None else None
        )
        if prefer_compiled:
            if page is not None and getattr(page, "media_type", None):
                factor = 1.0
            else:
                factor = type_multiplier(
                    getattr(page, "page_type", None) if page is not None else None,
                    config,
                )
            if chain is None:
                score *= factor
            else:
                before = score
                score *= factor
                chain.append(
                    {
                        "name": "type",
                        "factor": factor,
                        "before": before,
                        "after": score,
                    }
                )
        if prefer_active:
            factor = status_multiplier(getattr(page, "status", None), config)
            if chain is None:
                score *= factor
            else:
                before = score
                score *= factor
                chain.append(
                    {
                        "name": "status",
                        "factor": factor,
                        "before": before,
                        "after": score,
                    }
                )
        if temporal_active:
            d = parse_date(getattr(page, "updated", None)) if page else None
            if d is not None:
                factor = recency_multiplier(
                    max(0.0, float((today - d).days)), config
                )
                if chain is None:
                    score *= factor
                else:
                    before = score
                    score *= factor
                    chain.append(
                        {
                            "name": "recency",
                            "factor": factor,
                            "before": before,
                            "after": score,
                        }
                    )
        if usage_active:
            b = usage_map.get(usage_module.canon(path))
            if b is not None:
                factor = usage_module.usage_multiplier(b, config)
                if chain is None:
                    score *= factor
                else:
                    before = score
                    score *= factor
                    chain.append(
                        {
                            "name": "usage",
                            "factor": factor,
                            "before": before,
                            "after": score,
                        }
                    )
        if evidence_out is not None:
            assert chain is not None
            evidence_out[path] = chain
        adjusted.append((path, score))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def is_temporal_query(query: str) -> bool:
    """True when the query carries a recency/time marker."""
    if not query:
        return False
    return TEMPORAL_MARKERS.search(query) is not None


def classify_intent(query: str) -> str:
    """Deterministic intent label: exact | temporal | relationship | conceptual."""
    q = (query or "").strip()
    if not q:
        return "conceptual"
    if '"' in q or "[[" in q:
        return "exact"
    if EXACT_LEADING.match(q):
        return "exact"
    if is_temporal_query(q):
        return "temporal"
    if RELATIONSHIP_MARKERS.search(q):
        return "relationship"
    return "conceptual"


def parse_date(value: str | None) -> date | None:
    """Best-effort ISO date parse (YYYY-MM-DD prefix); None when unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def recency_multiplier(
    days_old: float, config: RankingConfig = DEFAULT_RANKING
) -> float:
    """Gaussian recency weight: peaks at temporal_boost for a brand-new page."""
    if config.temporal_boost == 1.0:
        return 1.0
    sigma = config.temporal_sigma_days or 1.0
    return 1.0 + (config.temporal_boost - 1.0) * math.exp(
        -(days_old ** 2) / (2.0 * sigma ** 2)
    )


def apply_temporal_boost(
    fused: list[tuple[str, float]],
    query: str,
    page_of: PageOf,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    """Re-sort fused `(path, score)` after a Gaussian recency multiplier."""
    if not is_temporal_query(query) or config.temporal_boost == 1.0:
        return fused
    today = date.today()
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = page_of(path)
        d = parse_date(getattr(page, "updated", None)) if page else None
        mult = 1.0 if d is None else recency_multiplier(
            max(0.0, float((today - d).days)), config
        )
        adjusted.append((path, score * mult))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def recency_ranking(candidate_paths: list[str], page_of: PageOf, cap: int) -> list[str]:
    """Candidate paths ordered most-recently-updated first."""
    dated: list[tuple[date, str]] = []
    seen: set[str] = set()
    for p in candidate_paths:
        if p in seen:
            continue
        seen.add(p)
        page = page_of(p)
        if page is None:
            continue
        d = parse_date(getattr(page, "updated", None))
        if d is not None:
            dated.append((d, p))
    dated.sort(key=lambda t: (-t[0].toordinal(), t[1]))
    return [p for _, p in dated][:cap]


def filter_by_date(
    hits: list[Hit],
    *,
    updated_after: str | None = None,
    updated_before: str | None = None,
    recency_days: int | None = None,
) -> list[Hit]:
    """Drop hits whose updated date falls outside the requested window."""
    after = parse_date(updated_after)
    before = parse_date(updated_before)
    floor: date | None = None
    if recency_days is not None and recency_days >= 0:
        floor = date.today() - timedelta(days=recency_days)
    if after is None and before is None and floor is None:
        return hits
    out: list[Hit] = []
    for h in hits:
        d = parse_date(h.updated)
        if d is None:
            continue
        if after is not None and d < after:
            continue
        if before is not None and d > before:
            continue
        if floor is not None and d < floor:
            continue
        out.append(h)
    return out


def should_rerank(
    hits: list[Hit], query: str, config: RankingConfig = DEFAULT_RANKING
) -> bool:
    """Heuristic: is this query worth the reranker's model-load cost?"""
    if len((query or "").split()) >= 5:
        return True
    vec = [
        h.path
        for h in sorted(
            (h for h in hits if h.vector_rank is not None),
            key=lambda h: h.vector_rank,  # type: ignore[arg-type,return-value]
        )
    ][:3]
    bm = [
        h.path
        for h in sorted(
            (h for h in hits if h.bm25_rank is not None),
            key=lambda h: h.bm25_rank,  # type: ignore[arg-type,return-value]
        )
    ][:3]
    if not vec or not bm:
        return False
    overlap = len(set(vec) & set(bm))
    disagreement = 1.0 - overlap / max(len(vec), len(bm))
    return disagreement > 0.5
