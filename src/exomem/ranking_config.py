"""Ranking configuration and adopted-config loading for ``find``."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankingConfig:
    """The tunable knobs of the hybrid ranker, in one place.

    Every value here was historically a hardcoded literal scattered through
    `_find_semantic`/`fusion`/`_type_multiplier`. Bundling them lets the
    offline eval harness (`scripts/eval_retrieval.py`) sweep them against a
    golden set and pick winners by NDCG/MRR instead of intuition. The
    field defaults reproduce the pre-refactor behaviour byte-for-byte — see
    `tests/test_ranking_config.py`, which guards that invariant.

    Intentionally NOT exposed on the MCP `find` tool signature: claude.ai
    needs no knobs API. It's an internal seam for measurement + tuning.
    """

    rrf_k: int = 60  # Cormack/Clarke/Buettcher 2009 default; fusion.py
    compiled_boost: float = 1.15  # must equal _COMPILED_BOOST
    source_penalty: float = 0.85  # must equal _SOURCE_PENALTY
    superseded_penalty: float = 0.5  # must equal _SUPERSEDED_PENALTY
    candidate_multiplier: int = 5  # candidate_k = max(limit*mult, floor)
    candidate_floor: int = 50
    graph_seed_cap: int = 20  # per-ranker fanout cap for 1-hop expansion

    # ---- Temporal lane (Gaussian recency) ----
    # `temporal_boost` is the peak multiplier a brand-new page gets on a
    # temporal query: 1.0 = OFF (the default), so recency NEVER perturbs a
    # non-temporal ranking. The post-RRF boost only fires when both
    # `_is_temporal_query(query)` is true AND `temporal_boost != 1.0`.
    temporal_boost: float = 1.0
    temporal_sigma_days: float = 60.0  # Gaussian width: ~halflife of "recent"

    # ---- Usage-activation boost (opt-in via find(prefer_used=true)) ----
    # Bounded multiplicative post-boost from ACT-R activation over the JSONL
    # access logs (see usage.py). `usage_boost` is the CEILING of the
    # multiplier — deliberately below `compiled_boost` so usage can break
    # ties but never override the epistemic hierarchy, and low enough that
    # a superseded tombstone at max usage still loses to its active
    # successor (0.5 × 1.10 < 1.0). `usage_w_surfaced` defaults to 0: being
    # surfaced by find is not a choice anyone made (rich-get-richer guard);
    # reads and citations are genuine selection acts.
    usage_boost: float = 1.10
    usage_decay: float = 0.5
    usage_horizon_days: float = 90.0
    usage_w_surfaced: float = 0.0
    usage_w_read: float = 1.0
    usage_w_cited: float = 2.0

    # ---- Intent-adaptive weighted RRF ----
    # One weight per fusion lane, aligned positionally to LANE_ORDER:
    #   (vector, bm25, keyword, clip, graph, temporal)
    # `conceptual` is fully neutral (all 1.0) so the common case reproduces the
    # pre-feature unweighted RRF byte-for-byte; only the non-conceptual intents
    # diverge. The adaptivity is the feature, not a global ranking change.
    intent_weights_conceptual: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    # exact: literal lookups — favour the lexical lanes (bm25 + keyword), damp
    # the semantic/connectivity lanes that float topical-but-inexact matches.
    intent_weights_exact: tuple[float, ...] = (0.7, 1.5, 1.5, 1.0, 0.7, 1.0)
    # relationship: "what links/cites/relates to X" — favour the graph lane.
    intent_weights_relationship: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.8, 1.0)
    # temporal: up-weight the recency lane so newer matches surface first.
    intent_weights_temporal: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0, 2.0)

    def intent_weights(self, intent: str) -> tuple[float, ...]:
        """Lane-weight tuple for a classified intent; conceptual (neutral) default."""
        return {
            "exact": self.intent_weights_exact,
            "temporal": self.intent_weights_temporal,
            "relationship": self.intent_weights_relationship,
            "conceptual": self.intent_weights_conceptual,
        }.get(intent, self.intent_weights_conceptual)


# Fusion lane order — the canonical alignment for the per-intent weight tuples.
# MUST match the order lanes are assembled into the weighted RRF in
# `_find_semantic` (see `lane_rankings`).
LANE_ORDER = ("vector", "bm25", "keyword", "clip", "graph", "temporal")

DEFAULT_RANKING = RankingConfig()


# --------------------------------------------------------------------------- #
# Adopted-config seam: the auto-tuner writes a reviewed `ranking_config.json`;
# `find()` loads it once per process when no explicit `config` is passed. Absent
# file (or any parse error) → DEFAULT_RANKING, byte-identical to the in-code
# baseline. The live `op_find` calls find() WITHOUT config (consults disk); the
# eval harnesses pass config= explicitly (hermetic, never touch disk).
# --------------------------------------------------------------------------- #
# src/exomem/find.py → parents[2] is the repo (or deploy-checkout) root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_ACTIVE_RANKING: RankingConfig | None = None
_ACTIVE_RANKING_LOADED = False


def ranking_config_to_jsonable(cfg: RankingConfig) -> dict[str, Any]:
    """Plain-dict form of a RankingConfig (tuples render as JSON arrays)."""
    return dataclasses.asdict(cfg)


def ranking_config_from_jsonable(data: dict[str, Any]) -> RankingConfig:
    """Build a RankingConfig from a parsed JSON dict, field by field.

    Unknown keys are ignored (schema-drift-safe) and missing keys fall back to
    the dataclass default. Scalar fields are coerced to int/float per their
    default; the four `intent_weights_*` fields are coerced to float tuples and
    MUST have exactly `len(LANE_ORDER)` entries. Raises on an uncoercible value
    or a wrong-length lane tuple so the caller can fail loud.
    """
    field_names = {f.name for f in dataclasses.fields(RankingConfig)}
    unknown = set(data) - field_names
    if unknown:
        log.warning(
            "ranking config: ignoring unknown knob(s): %s", ", ".join(sorted(unknown))
        )
    n_lanes = len(LANE_ORDER)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(RankingConfig):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name.startswith("intent_weights_"):
            tup = tuple(float(x) for x in value)
            if len(tup) != n_lanes:
                raise ValueError(
                    f"{f.name}: expected {n_lanes} lane weights, got {len(tup)}"
                )
            kwargs[f.name] = tup
        elif isinstance(f.default, bool):
            kwargs[f.name] = bool(value)
        elif isinstance(f.default, int):
            kwargs[f.name] = int(value)
        else:
            kwargs[f.name] = float(value)
    return RankingConfig(**kwargs)


def _load_adopted_ranking() -> RankingConfig:
    """Resolve the active RankingConfig from disk; DEFAULT_RANKING on absence/error.

    Resolution order:
      1. ``EXOMEM_DISABLE_RANKING_CONFIG`` set → DEFAULT_RANKING (hermetic; the
         test suite sets this so a committed file never pollutes the suite).
      2. ``EXOMEM_RANKING_CONFIG=<path>`` → that path.
      3. ``<repo_root>/ranking_config.json`` if present.
      4. else DEFAULT_RANKING.

    A malformed / wrong-typed / bad-lane-length file fails LOUD (``log.error``)
    and falls back to DEFAULT_RANKING — it never crashes the server and never
    applies a partially-parsed config.
    """
    if os.environ.get("EXOMEM_DISABLE_RANKING_CONFIG"):
        return DEFAULT_RANKING
    override = os.environ.get("EXOMEM_RANKING_CONFIG")
    path = Path(override) if override else _REPO_ROOT / "ranking_config.json"
    if not path.exists():
        return DEFAULT_RANKING
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ranking config must be a JSON object")
        return ranking_config_from_jsonable(data)
    except Exception as exc:  # noqa: BLE001 — degrade to known-good, loudly.
        log.error(
            "adopted ranking config %s invalid (%s); using DEFAULT_RANKING", path, exc
        )
        return DEFAULT_RANKING


def _active_ranking() -> RankingConfig:
    """The adopted RankingConfig, loaded once per process (memoized)."""
    global _ACTIVE_RANKING, _ACTIVE_RANKING_LOADED
    if not _ACTIVE_RANKING_LOADED:
        _ACTIVE_RANKING = _load_adopted_ranking()
        _ACTIVE_RANKING_LOADED = True
    return _ACTIVE_RANKING if _ACTIVE_RANKING is not None else DEFAULT_RANKING


def reset_active_ranking_cache() -> None:
    """Drop the memoized adopted config (tests; desk-side adopt happens out of band)."""
    global _ACTIVE_RANKING, _ACTIVE_RANKING_LOADED
    _ACTIVE_RANKING = None
    _ACTIVE_RANKING_LOADED = False
