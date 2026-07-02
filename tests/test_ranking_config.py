"""RankingConfig is the tuning seam for the eval harness.

These guard the one invariant that makes the seam safe to add: the DEFAULT
config must reproduce the pre-refactor ranking exactly, while a non-default
config must actually change ranking (proving the knobs are wired, not ignored).

Runs under the suite-wide EXOMEM_DISABLE_EMBEDDINGS — hybrid mode degrades to
BM25 + keyword + graph (the fixture vault has no sidecar), which still exercises
candidate_k / graph_seed_cap / rrf_k / type-boost, all of which the config feeds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import find as find_module


def test_default_config_matches_legacy_constants() -> None:
    cfg = find_module.RankingConfig()
    # The field defaults must equal the historical literals so DEFAULT is a
    # faithful no-op. _COMPILED_BOOST / _SOURCE_PENALTY are kept as the
    # canonical source values; this binds them together.
    assert cfg.compiled_boost == find_module._COMPILED_BOOST
    assert cfg.source_penalty == find_module._SOURCE_PENALTY
    assert cfg.superseded_penalty == find_module._SUPERSEDED_PENALTY
    assert cfg.rrf_k == 60
    assert cfg.candidate_multiplier == 5
    assert cfg.candidate_floor == 50
    assert cfg.graph_seed_cap == 20
    assert find_module.DEFAULT_RANKING == cfg


def test_default_config_temporal_fields_are_off() -> None:
    """Temporal boost defaults to OFF (1.0) so recency never perturbs a default
    ranking; sigma is the documented 60-day Gaussian width."""
    cfg = find_module.RankingConfig()
    assert cfg.temporal_boost == 1.0
    assert cfg.temporal_sigma_days == 60.0


def test_default_conceptual_weights_are_neutral() -> None:
    """The common-case intent must be fully neutral (all 1.0) so weighted RRF
    reproduces the unweighted default exactly."""
    cfg = find_module.RankingConfig()
    assert cfg.intent_weights_conceptual == (1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    # Every intent tuple has one weight per fusion lane (LANE_ORDER length).
    assert len(find_module.LANE_ORDER) == 6
    for intent in ("conceptual", "exact", "relationship", "temporal"):
        assert len(cfg.intent_weights(intent)) == 6
    # Non-conceptual intents DO diverge (that's the feature).
    assert cfg.intent_weights("exact") != cfg.intent_weights_conceptual
    assert cfg.intent_weights("relationship") != cfg.intent_weights_conceptual
    assert cfg.intent_weights("temporal") != cfg.intent_weights_conceptual
    # Unknown intent falls back to neutral.
    assert cfg.intent_weights("bogus") == cfg.intent_weights_conceptual


def test_intent_weights_lane_emphasis() -> None:
    """Each non-conceptual intent up-weights its signature lane."""
    cfg = find_module.RankingConfig()
    # exact favours the lexical lanes (bm25 idx1, keyword idx2) over vector (idx0).
    assert cfg.intent_weights("exact")[1] > cfg.intent_weights("exact")[0]
    assert cfg.intent_weights("exact")[2] > cfg.intent_weights("exact")[0]
    # relationship favours the graph lane (idx4).
    assert cfg.intent_weights("relationship")[4] > 1.0
    # temporal favours the recency lane (idx5).
    assert cfg.intent_weights("temporal")[5] > 1.0


def test_intent_override_does_not_change_conceptual_default(vault: Path) -> None:
    """Forcing intent='conceptual' must match the auto-classified conceptual
    default for a conceptual query — i.e. the override is a no-op there."""
    for query in ("metabolism", "progressive disclosure"):
        auto = find_module.find(vault, query=query, mode="hybrid")
        forced = find_module.find(vault, query=query, mode="hybrid", intent="conceptual")
        assert [h.path for h in auto] == [h.path for h in forced]


def test_default_config_reproduces_no_config(vault: Path) -> None:
    """Passing config=DEFAULT_RANKING must be identical to passing nothing."""
    for query in ("metabolism", "progressive disclosure", "EGCG"):
        baseline = find_module.find(vault, query=query, mode="hybrid")
        explicit = find_module.find(
            vault, query=query, mode="hybrid",
            config=find_module.DEFAULT_RANKING,
        )
        assert [h.path for h in baseline] == [h.path for h in explicit]
        assert [h.as_dict() for h in baseline] == [h.as_dict() for h in explicit]


def test_type_boost_honors_config() -> None:
    """_apply_type_boost must read multipliers from the passed config."""
    # Two repo-fixture paths at equal base score: a compiled insight + a raw source.
    insight = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    source = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
    fixtures = Path(__file__).resolve().parents[0] / "fixtures"  # read-only
    fused = [(insight, 1.0), (source, 1.0)]

    # DEFAULT: compiled x1.15 > source x0.85 → insight ranks first.
    default_order = [p for p, _ in find_module._apply_type_boost(fused, fixtures)]
    assert default_order[0] == insight

    # Penalize compiled heavily → source overtakes the insight, proving the
    # config value (not the module constant) drives the multiplier.
    penalize = find_module.RankingConfig(compiled_boost=0.1)
    flipped = [
        p for p, _ in find_module._apply_type_boost(fused, fixtures, penalize)
    ]
    assert flipped[0] == source


def test_ranking_config_jsonable_roundtrip() -> None:
    """to_jsonable → json → from_jsonable must reproduce the config exactly, with
    the intent-weight fields surviving as tuples (the frozen dataclass needs
    hashable fields)."""
    default = find_module.DEFAULT_RANKING
    rt = find_module.ranking_config_from_jsonable(
        json.loads(json.dumps(find_module.ranking_config_to_jsonable(default)))
    )
    assert rt == default

    tuned = find_module.RankingConfig(
        rrf_k=30,
        compiled_boost=1.3,
        intent_weights_exact=(0.5, 2.0, 2.0, 1.0, 0.5, 1.0),
    )
    rt2 = find_module.ranking_config_from_jsonable(
        json.loads(json.dumps(find_module.ranking_config_to_jsonable(tuned)))
    )
    assert rt2 == tuned
    assert isinstance(rt2.intent_weights_exact, tuple)


def test_adopted_config_applies_then_reverts(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """find() with no `config` loads an adopted file; deleting it (with a cache
    reset) restores byte-identical DEFAULT behavior. The reversibility guard."""
    monkeypatch.delenv("EXOMEM_DISABLE_RANKING_CONFIG", raising=False)
    tuned = find_module.RankingConfig(compiled_boost=0.1)  # heavy compiled penalty
    cfg_path = tmp_path / "ranking_config.json"
    cfg_path.write_text(
        json.dumps(find_module.ranking_config_to_jsonable(tuned)), encoding="utf-8"
    )
    monkeypatch.setenv("EXOMEM_RANKING_CONFIG", str(cfg_path))
    find_module.reset_active_ranking_cache()

    queries = ("egcg", "metabolism", "progressive disclosure")
    # The seam applies the adopted file when no config is passed.
    for q in queries:
        adopted = [h.path for h in find_module.find(vault, query=q, mode="hybrid")]
        explicit = [
            h.path for h in find_module.find(vault, query=q, mode="hybrid", config=tuned)
        ]
        assert adopted == explicit
    # ...and the adopted config genuinely perturbs ranking vs DEFAULT somewhere.
    changed = any(
        [h.path for h in find_module.find(vault, query=q, mode="hybrid", config=tuned)]
        != [
            h.path
            for h in find_module.find(
                vault, query=q, mode="hybrid", config=find_module.DEFAULT_RANKING
            )
        ]
        for q in queries
    )
    assert changed, "the tuned config should reorder at least one fixture query"

    # Revert: delete the file + reset cache → no-config find() == DEFAULT path.
    cfg_path.unlink()
    find_module.reset_active_ranking_cache()
    for q in queries:
        reverted = [h.path for h in find_module.find(vault, query=q, mode="hybrid")]
        default = [
            h.path
            for h in find_module.find(
                vault, query=q, mode="hybrid", config=find_module.DEFAULT_RANKING
            )
        ]
        assert reverted == default
