"""Unit tests for the desk-side ranking auto-tuner (torch-free).

Exercises the PURE pieces: the coordinate-descent `optimize()` with a stub
evaluator (known optimum), the relevance-pairs loader, the golden loader, and
the knob<->RankingConfig mapping. The real NDCG run needs torch + the live
vault and is deliberately NOT exercised here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import auto_tune_ranking as at  # noqa: E402

from kb_mcp.find import DEFAULT_RANKING  # noqa: E402


# --------------------------------------------------------------------------- #
# optimize() — coordinate descent finds a known optimum with a stub evaluator
# --------------------------------------------------------------------------- #
def test_optimize_finds_separable_optimum() -> None:
    candidates = {"a": [0, 1, 2], "b": [0, 1, 2]}
    # Concave, coordinate-separable: optimum at a=2, b=1, score 0.
    def evaluate(cfg: dict) -> float:
        return -((cfg["a"] - 2) ** 2 + (cfg["b"] - 1) ** 2)

    best, score = at.optimize(candidates, evaluate, start={"a": 0, "b": 0})
    assert best == {"a": 2, "b": 1}
    assert score == 0.0


def test_optimize_default_start_is_first_values() -> None:
    candidates = {"x": [3, 1, 2]}
    calls: list[dict] = []

    def evaluate(cfg: dict) -> float:
        calls.append(dict(cfg))
        return float(cfg["x"])

    best, score = at.optimize(candidates, evaluate)
    # First evaluated config uses the first candidate of each axis.
    assert calls[0] == {"x": 3}
    # Maximizing x → picks 3 (the largest candidate).
    assert best == {"x": 3}
    assert score == 3.0


def test_optimize_never_regresses_below_start() -> None:
    candidates = {"a": [0, 5, 10], "b": [0, 5, 10]}

    def evaluate(cfg: dict) -> float:
        # A single sharp peak the descent should climb to.
        return -(abs(cfg["a"] - 5) + abs(cfg["b"] - 10))

    start = {"a": 0, "b": 0}
    base = evaluate(start)
    best, score = at.optimize(candidates, evaluate, start=start)
    assert score >= base
    assert best == {"a": 5, "b": 10}
    assert score == 0.0


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def test_load_relevance_pairs_parses_fixture(tmp_path: Path) -> None:
    pairs_file = tmp_path / "relevance_pairs.jsonl"
    rows = [
        {"query": "binding problem", "cited_path": "notes/insights/binding",
         "confidence": 0.5, "rank_in_results": 1},
        {"query": "elimination diet", "cited_path": "sources/books/elim",
         "confidence": 0.25, "rank_in_results": 2},
    ]
    with pairs_file.open("w", encoding="utf-8") as f:
        f.write(json.dumps(rows[0]) + "\n")
        f.write("\n")               # blank line — must be skipped
        f.write("{not valid json\n")  # malformed — must be skipped
        f.write(json.dumps(rows[1]) + "\n")

    loaded = at.load_relevance_pairs(pairs_file)
    assert len(loaded) == 2
    assert loaded[0]["query"] == "binding problem"
    assert loaded[1]["cited_path"] == "sources/books/elim"


def test_load_relevance_pairs_missing_file(tmp_path: Path) -> None:
    assert at.load_relevance_pairs(tmp_path / "nope.jsonl") == []


def test_load_golden_parses_repo_set() -> None:
    golden = at.load_golden(at.DEFAULT_GOLDEN)
    assert golden, "repo golden set should be non-empty"
    for row in golden:
        assert row["query"]
        assert row["relevance"]
        assert row["relevant"] <= set(row["relevance"])


# --------------------------------------------------------------------------- #
# knob <-> RankingConfig mapping
# --------------------------------------------------------------------------- #
def test_default_knobs_roundtrip_to_default_config() -> None:
    cfg = at.config_from_dict(at.default_knobs())
    assert cfg == DEFAULT_RANKING


def test_config_from_dict_expands_intent_weight_scalars() -> None:
    cfg = at.config_from_dict({
        "exact_lexical_weight": 1.5,
        "relationship_graph_weight": 1.8,
        "temporal_lane_weight": 2.0,
        "temporal_boost": 1.5,
    })
    # exact scalar drives the bm25(1) + keyword(2) lanes.
    assert cfg.intent_weights_exact[1] == 1.5
    assert cfg.intent_weights_exact[2] == 1.5
    # relationship scalar drives the graph(4) lane.
    assert cfg.intent_weights_relationship[4] == 1.8
    # temporal scalar drives the temporal(5) lane.
    assert cfg.intent_weights_temporal[5] == 2.0
    assert cfg.temporal_boost == 1.5
    # conceptual stays neutral regardless of tuning.
    assert cfg.intent_weights_conceptual == (1.0, 1.0, 1.0, 1.0, 1.0, 1.0)


def test_candidate_axes_excludes_conceptual_weights() -> None:
    axes = at.candidate_axes()
    assert "rrf_k" in axes
    assert "temporal_boost" in axes
    # No knob should tune the conceptual lane weights — they must stay neutral.
    assert not any("conceptual" in knob for knob in axes)
