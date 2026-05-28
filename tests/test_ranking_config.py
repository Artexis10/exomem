"""RankingConfig is the tuning seam for the eval harness.

These guard the one invariant that makes the seam safe to add: the DEFAULT
config must reproduce the pre-refactor ranking exactly, while a non-default
config must actually change ranking (proving the knobs are wired, not ignored).

Runs under the suite-wide KB_MCP_DISABLE_EMBEDDINGS — hybrid mode degrades to
BM25 + keyword + graph (the fixture vault has no sidecar), which still exercises
candidate_k / graph_seed_cap / rrf_k / type-boost, all of which the config feeds.
"""

from __future__ import annotations

from pathlib import Path

from kb_mcp import find as find_module


def test_default_config_matches_legacy_constants() -> None:
    cfg = find_module.RankingConfig()
    # The field defaults must equal the historical literals so DEFAULT is a
    # faithful no-op. _COMPILED_BOOST / _SOURCE_PENALTY are kept as the
    # canonical source values; this binds them together.
    assert cfg.compiled_boost == find_module._COMPILED_BOOST
    assert cfg.source_penalty == find_module._SOURCE_PENALTY
    assert cfg.rrf_k == 60
    assert cfg.candidate_multiplier == 5
    assert cfg.candidate_floor == 50
    assert cfg.graph_seed_cap == 20
    assert find_module.DEFAULT_RANKING == cfg


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
