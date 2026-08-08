"""Corpus-health scorer: three honesty tiers, unsupported is never zero.

- Tier 1 — the provider declares :data:`Capability.NATIVE_HEALTH_AUDIT` and
  supplies its own audit counts; each metric with a planted expectation is a
  deterministic reported-vs-expected gate.
- Tier 2 — the provider declares :data:`Capability.STATE_EXPORT`; the scorer
  derives what it honestly can from the exported pages: near-duplicates via
  8-word shingling with Jaccard >= 0.8, and wikilink-style orphans (pages no
  other page links to). Contradictions/staleness cannot be derived from a bare
  page export and stay ``unsupported``.
- Tier 3 — neither capability: EVERY health metric is emitted with
  ``gate="unsupported"`` and a ``None`` measurement — never converted to a
  zero (the same contract as :mod:`membench.scoring.gates`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import combinations

from membench.adapters.base import Capability, StateExport
from membench.scoring.gates import GateStatus, ScoreItem

HEALTH_METRICS: tuple[str, ...] = ("duplicates", "contradictions", "orphans", "stale")
HEALTH_DIMENSION = "health"
HEALTH_QUERY_ID = "corpus-health"

SHINGLE_WORDS = 8
DUPLICATE_JACCARD = 0.8

_WORD_RE = re.compile(r"[a-z0-9]+")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n|]+?)(?:\|[^\[\]\n]*)?\]\]")


@dataclass(frozen=True)
class HealthReport:
    """Per-metric gate items plus honest measurements (None = not derivable)."""

    tier: int
    items: tuple[ScoreItem, ...]
    measurements: dict[str, int | None] = field(default_factory=dict)


def _shingles(text: str) -> frozenset[tuple[str, ...]]:
    words = _WORD_RE.findall(text.lower())
    if len(words) < SHINGLE_WORDS:
        return frozenset({tuple(words)} if words else set())
    return frozenset(
        tuple(words[i : i + SHINGLE_WORDS])
        for i in range(len(words) - SHINGLE_WORDS + 1)
    )


def count_duplicate_pairs(export: StateExport) -> int:
    """Near-duplicate page pairs: shingle-set Jaccard >= the 0.8 ceiling."""

    shingled = [(page.path, _shingles(page.text)) for page in export.pages]
    pairs = 0
    for (_pa, sa), (_pb, sb) in combinations(shingled, 2):
        if not sa or not sb:
            continue
        union = len(sa | sb)
        if union and len(sa & sb) / union >= DUPLICATE_JACCARD:
            pairs += 1
    return pairs


def _link_keys(path: str) -> set[str]:
    """Normalized identities a wikilink may use to reference ``path``."""

    stem = path[:-3] if path.endswith(".md") else path
    stem = stem.lower()
    keys = {stem}
    keys.add(stem.rsplit("/", 1)[-1])  # bare basename form
    return keys


def count_orphans(export: StateExport) -> int:
    """Pages with no inbound wikilink from any OTHER exported page."""

    outbound: dict[str, set[str]] = {}
    for page in export.pages:
        targets: set[str] = set()
        for match in _WIKILINK_RE.finditer(page.text):
            target = match.group(1).strip().lower()
            if target.endswith(".md"):
                target = target[:-3]
            if target:
                targets.add(target)
                targets.add(target.rsplit("/", 1)[-1])
        outbound[page.path] = targets
    orphans = 0
    for page in export.pages:
        keys = _link_keys(page.path)
        inbound = any(
            keys & targets
            for source, targets in outbound.items()
            if source != page.path
        )
        if not inbound:
            orphans += 1
    return orphans


def _item(
    metric: str, status: GateStatus, evidence: str | None = None, *, query_id: str
) -> ScoreItem:
    return ScoreItem(query_id, f"health_{metric}", HEALTH_DIMENSION, status, evidence)


def _score_measurements(
    measurements: Mapping[str, int | None],
    expected_counts: Mapping[str, int] | None,
    *,
    tier: int,
    query_id: str,
) -> HealthReport:
    expected = dict(expected_counts or {})
    items: list[ScoreItem] = []
    for metric in HEALTH_METRICS:
        measured = measurements.get(metric)
        if measured is None:
            items.append(
                _item(
                    metric,
                    GateStatus.UNSUPPORTED,
                    f"not derivable at tier {tier}; measurement is None, never zero",
                    query_id=query_id,
                )
            )
            continue
        if metric not in expected:
            items.append(
                _item(
                    metric,
                    GateStatus.NOT_APPLICABLE,
                    f"no planted expectation; measured {measured}",
                    query_id=query_id,
                )
            )
            continue
        want = expected[metric]
        if measured == want:
            items.append(
                _item(metric, GateStatus.PASS, f"reported {measured}", query_id=query_id)
            )
        else:
            items.append(
                _item(
                    metric,
                    GateStatus.FAIL,
                    f"reported {measured}, expected {want}",
                    query_id=query_id,
                )
            )
    return HealthReport(
        tier=tier,
        items=tuple(items),
        measurements={metric: measurements.get(metric) for metric in HEALTH_METRICS},
    )


def score_health(
    capabilities: frozenset[Capability],
    *,
    expected_counts: Mapping[str, int] | None = None,
    audit_counts: Mapping[str, int] | None = None,
    export: StateExport | None = None,
    query_id: str = HEALTH_QUERY_ID,
) -> HealthReport:
    """Score corpus health at the best tier the adapter honestly supports.

    ``expected_counts`` are the planted counts the corpus/template provides
    (a metric without one is informational — ``not_applicable``).
    """

    if Capability.NATIVE_HEALTH_AUDIT in capabilities and audit_counts is not None:
        measurements: dict[str, int | None] = {
            metric: (
                int(audit_counts[metric]) if metric in audit_counts else None
            )
            for metric in HEALTH_METRICS
        }
        return _score_measurements(
            measurements, expected_counts, tier=1, query_id=query_id
        )

    if Capability.STATE_EXPORT in capabilities and export is not None:
        measurements = {
            "duplicates": count_duplicate_pairs(export),
            "contradictions": None,  # not derivable from a bare page export
            "orphans": count_orphans(export),
            "stale": None,  # wall-clock staleness is not in the export
        }
        return _score_measurements(
            measurements, expected_counts, tier=2, query_id=query_id
        )

    return _score_measurements(
        {metric: None for metric in HEALTH_METRICS},
        expected_counts,
        tier=3,
        query_id=query_id,
    )
