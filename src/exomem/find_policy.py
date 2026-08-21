"""Ranking, intent, temporal, and rerank policy for find()."""

from __future__ import annotations

import heapq
import math
import re
from collections.abc import Callable
from datetime import date, timedelta

from .find_types import Hit
from .ranking_config import DEFAULT_RANKING, RankingConfig
# Names imported directly rather than the `temporal` module: this file already
# has a parameter called `temporal` in `apply_post_rrf_multipliers`, and a
# module of the same name would sit one refactor away from being shadowed.
from .temporal import Moment, Order, compare as compare_moments, parse as parse_moment

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


def multiplier_bounds(
    config: RankingConfig,
    *,
    prefer_compiled: bool,
    prefer_active: bool,
    temporal_active: bool,
    usage_active: bool,
) -> tuple[float, float]:
    """(min, max) of the product of every ACTIVE post-RRF multiplier.

    Each factor is bounded by a declared config value, which is what makes the
    bounded pass below exact rather than approximate:

    * ``type_multiplier``   -> ``compiled_boost`` high, ``source_penalty`` low
    * ``status_multiplier`` -> 1.0 high, ``superseded_penalty`` low (penalty only)
    * ``recency_multiplier``-> ``temporal_boost`` at zero days, 1.0 at infinity
    * ``usage_multiplier``  -> ``usage_boost`` high, 1.0 low (never a penalty)

    An inactive factor contributes exactly 1.0 to both ends.
    """
    low = high = 1.0
    if prefer_compiled:
        low *= min(config.compiled_boost, config.source_penalty, 1.0)
        high *= max(config.compiled_boost, config.source_penalty, 1.0)
    if prefer_active:
        low *= min(config.superseded_penalty, 1.0)
        high *= max(config.superseded_penalty, 1.0)
    if temporal_active:
        low *= min(config.temporal_boost, 1.0)
        high *= max(config.temporal_boost, 1.0)
    if usage_active:
        low *= min(config.usage_boost, 1.0)
        high *= max(config.usage_boost, 1.0)
    return low, high


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
    top_n: int | None = None,
) -> list[tuple[str, float]]:
    """All post-RRF multiplicative boosts in one pass with one final sort.

    Every factor needs the candidate's page, and `page_of` on a cold page is a
    file read plus a policy admission check — so an unbounded pass loads the
    whole fused set to rank it. On a 2.4k-page vault that measured ~500 pages
    loaded to return 10 results, and was the single largest read-path term
    (#283).

    `top_n` bounds that work WITHOUT changing the answer. Candidates arrive in
    descending RRF order, so once `top_n` results are in hand, a candidate whose
    best possible adjusted score cannot reach the current `top_n`-th score can
    be skipped — and so can every candidate after it, because their raw scores
    are no lower. `multiplier_bounds` supplies "best possible" exactly.

    The first `top_n` entries of the result are therefore byte-identical to an
    unbounded pass. Entries beyond `top_n` keep raw RRF order and raw scores,
    since by construction nothing there can enter the top. Pass `top_n=None`
    (the default) for the full pass — every existing caller keeps its behaviour.
    """
    temporal_active = temporal and config.temporal_boost != 1.0 and is_temporal_query(query)
    usage_active = bool(usage_map)
    if not (prefer_compiled or prefer_active or temporal_active or usage_active):
        return fused
    if usage_active:
        from . import usage as usage_module
    today = date.today() if temporal_active else None
    # Evidence must describe every candidate a trace will show, so an explain
    # request takes the full pass. Bounding it would leave the tail with no
    # multiplier chain and make the trace lie by omission.
    bounded = top_n is not None and top_n > 0 and evidence_out is None
    low_mult = high_mult = 1.0
    if bounded:
        low_mult, high_mult = multiplier_bounds(
            config,
            prefer_compiled=prefer_compiled,
            prefer_active=prefer_active,
            temporal_active=temporal_active,
            usage_active=usage_active,
        )
    cutoff: list[float] = []  # min-heap of the best `top_n` adjusted scores
    adjusted: list[tuple[str, float]] = []
    for index, (path, score) in enumerate(fused):
        if bounded and len(cutoff) >= top_n:
            # Upper bound of `score * m` over m in [low, high]. Written as a max
            # of both products rather than `score * high` so a negative raw
            # score (a negative lane weight would produce one) bounds correctly
            # instead of inverting.
            reachable = max(score * low_mult, score * high_mult)
            # Strict `<` only: on equality the loser could still win the
            # `(-score, path)` tie-break, so an equal candidate must be scored.
            if reachable < cutoff[0]:
                adjusted.sort(key=lambda t: (-t[1], t[0]))
                return adjusted + list(fused[index:])
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
        if bounded:
            # `cutoff[0]` is the weakest of the best `top_n` adjusted scores
            # seen so far. It only ever rises, which is what makes the early
            # return above safe for every remaining candidate rather than only
            # the next one.
            if len(cutoff) < top_n:
                heapq.heappush(cutoff, score)
            elif score > cutoff[0]:
                heapq.heapreplace(cutoff, score)
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
    """Best-effort parse of a recorded date; None when unparseable.

    Recency scoring buckets pages by whole days, so a timestamp collapses to
    its day. Delegating to `temporal` also picks up the quoted and
    space-separated forms that prefix-slicing to 10 characters used to reject.
    """
    moment = parse_moment(value)
    return moment.day if moment is not None else None


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


def _bound_verdict(
    recorded: Moment, bound: str | None, *, keep_when: tuple[Order, ...]
) -> tuple[bool, bool]:
    """Apply one bound to one recorded value. Returns `(keep, indeterminate)`.

    The bound's own precision decides the granularity. A day-scoped bound asks
    a day-scoped question, which every page can answer; an instant bound cannot
    be answered by a page recorded only to the day it falls on.
    """
    parsed = parse_moment(bound)
    if parsed is None:
        return True, False
    if parsed.precise:
        order = compare_moments(recorded, parsed)
        if order is Order.INDETERMINATE:
            # Undecidable: keep it and say so, rather than dropping it silently
            # (the defect this change exists to fix) or passing a guess off as
            # a match.
            return True, True
    else:
        order = compare_moments(
            Moment(recorded.day), Moment(parsed.day)
        )
        if order is Order.INDETERMINATE:
            order = Order.SAME
    return order in keep_when, False


def filter_by_date(
    hits: list[Hit],
    *,
    updated_after: str | None = None,
    updated_before: str | None = None,
    recency_days: int | None = None,
) -> list[Hit]:
    """Drop hits whose recorded time falls outside the requested window.

    `updated_after`/`updated_before` accept an instant as well as a day.
    `recency_days` stays day-scoped: it is a window of whole days by
    construction. A hit kept on a bound that could not actually be decided is
    marked in `Hit.order_indeterminate` rather than presented as a clean match.
    """
    floor: date | None = None
    if recency_days is not None and recency_days >= 0:
        floor = date.today() - timedelta(days=recency_days)
    if updated_after is None and updated_before is None and floor is None:
        return hits
    after_keep = (Order.AFTER, Order.SAME)
    before_keep = (Order.BEFORE, Order.SAME)
    out: list[Hit] = []
    for h in hits:
        recorded = parse_moment(h.updated)
        if recorded is None:
            continue
        if floor is not None and recorded.day < floor:
            continue
        keep_after, vague_after = _bound_verdict(
            recorded, updated_after, keep_when=after_keep
        )
        if not keep_after:
            continue
        keep_before, vague_before = _bound_verdict(
            recorded, updated_before, keep_when=before_keep
        )
        if not keep_before:
            continue
        vague = [
            name
            for name, flag in (
                ("updated_after", vague_after),
                ("updated_before", vague_before),
            )
            if flag
        ]
        if vague:
            h.order_indeterminate = vague
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
