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


# --------------------------------------------------------------------------- #
# Mined-pair scoring — binary relevance, confidence is a filter not a grade
# --------------------------------------------------------------------------- #
def test_pairs_to_eval_filters_conf_and_dedups_golden() -> None:
    pairs = [
        {"query": "metabolism", "cited_path": "notes/a", "confidence": 0.9},
        {"query": "metabolism", "cited_path": "notes/b", "confidence": 0.3},
        {"query": "weak", "cited_path": "notes/c", "confidence": 0.1},  # below conf_min
        {"query": "In Golden", "cited_path": "notes/d", "confidence": 0.9},  # golden dupe
    ]
    rows = at.pairs_to_eval(pairs, {"in golden"}, conf_min=0.25)
    by_query = {r["query"]: r["relevant"] for r in rows}
    assert set(by_query) == {"metabolism"}  # weak filtered, golden-dup dropped
    assert by_query["metabolism"] == {"notes/a", "notes/b"}  # both ≥ conf_min, deduped per query


def test_pair_mrr_is_rank_based_not_confidence() -> None:
    pair_rows = [
        {"query": "q1", "relevant": {"p1"}},
        {"query": "q2", "relevant": {"p2"}},
    ]
    ranked = {"q1": ["x", "p1", "y"], "q2": ["p2", "z"]}
    # q1: p1 at rank 2 → 0.5 ; q2: p2 at rank 1 → 1.0 ; mean = 0.75
    assert at.pair_mrr(ranked, pair_rows) == 0.75
    # A missing query / no hit contributes 0 — no dependence on any confidence value.
    assert at.pair_mrr({"q1": ["nope"]}, pair_rows) == 0.0


def test_pair_recall10_fraction_in_topk() -> None:
    pair_rows = [{"query": "q1", "relevant": {"a", "b"}}]
    assert at.pair_recall10({"q1": ["a", "c", "d"]}, pair_rows) == 0.5


def test_combined_score_floor_guard_and_feasible() -> None:
    base = 0.9
    # Golden regresses past the floor → infeasible sentinel.
    assert at.combined_score(
        0.85, 0.5, baseline_golden=base, epsilon=0.01, n_eligible=10, min_pairs=8
    ) == (-1.0, 0.85)
    # Within floor but too few pairs → guard → golden-only (0.0, g).
    assert at.combined_score(
        0.9, 0.5, baseline_golden=base, epsilon=0.01, n_eligible=3, min_pairs=8
    ) == (0.0, 0.9)
    # Feasible with enough pairs → (pair_mrr, g).
    assert at.combined_score(
        0.9, 0.5, baseline_golden=base, epsilon=0.01, n_eligible=10, min_pairs=8
    ) == (0.5, 0.9)


def test_optimize_respects_golden_floor() -> None:
    """A candidate that boosts pairs but regresses golden past the floor is never
    selected over a feasible pairs improvement."""
    base = 0.9

    def evaluate(knobs: dict) -> tuple:
        scores = {
            0: (0.90, 0.1),  # start
            1: (0.90, 0.5),  # pairs improve, golden steady → feasible
            2: (0.80, 0.9),  # huge pairs gain but golden tanks → infeasible
        }
        g, pmrr = scores[knobs["k"]]
        return at.combined_score(
            g, pmrr, baseline_golden=base, epsilon=0.01, n_eligible=10, min_pairs=8
        )

    best, score = at.optimize({"k": [0, 1, 2]}, evaluate, start={"k": 0})
    assert best == {"k": 1}
    assert score == (0.5, 0.9)


def test_optimize_golden_breaks_pair_ties() -> None:
    """Equal pair-MRR → the higher golden NDCG wins (lexicographic tiebreak)."""
    base = 0.9

    def evaluate(knobs: dict) -> tuple:
        g = {0: 0.90, 1: 0.92, 2: 0.91}[knobs["k"]]
        return at.combined_score(
            g, 0.5, baseline_golden=base, epsilon=0.05, n_eligible=10, min_pairs=8
        )

    best, score = at.optimize({"k": [0, 1, 2]}, evaluate, start={"k": 0})
    assert best == {"k": 1}
    assert score == (0.5, 0.92)


# --------------------------------------------------------------------------- #
# Candidate / report / adopt — torch-free file ops + the floor-gated adopt
# --------------------------------------------------------------------------- #
def _meta(baseline: float = 0.9, candidate: float = 0.905, guard: bool = False) -> dict:
    return {
        "baseline_golden": baseline,
        "candidate_golden": candidate,
        "pair_mrr": None if guard else 0.5,
        "pair_recall10": None if guard else 0.4,
        "n_eligible_pairs": 3 if guard else 10,
        "guard_active": guard,
        "window_hours": 2.0,
        "epsilon": 0.01,
        "min_pairs": 8,
    }


def test_write_candidate_is_loadable_with_meta(tmp_path: Path) -> None:
    cfg = at.RankingConfig(rrf_k=30, compiled_boost=1.3)
    path = tmp_path / "cand.json"
    at.write_candidate(path, cfg, _meta())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["meta"]["candidate_golden"] == 0.905
    # The embedded config is a full, find()-loadable RankingConfig.
    assert at.ranking_config_from_jsonable(payload["config"]) == cfg


def test_write_report_lists_changed_knobs(tmp_path: Path) -> None:
    best = at.RankingConfig(rrf_k=30)
    path = tmp_path / "report.md"
    at.write_report(path, at.DEFAULT_RANKING, best, _meta())
    text = path.read_text(encoding="utf-8")
    assert "rrf_k" in text  # the changed knob is shown
    assert "--adopt" in text  # the adopt instructions are present


def test_adopt_refuses_golden_regression_then_forces(tmp_path: Path) -> None:
    cand = tmp_path / "cand.json"
    target = tmp_path / "ranking_config.json"
    at.write_candidate(cand, at.RankingConfig(rrf_k=30), _meta(candidate=0.80))  # regresses
    assert at.adopt(cand, target, force=False, epsilon=0.01) == 1
    assert not target.exists()  # blocked
    # --force overrides the floor gate.
    assert at.adopt(cand, target, force=True, epsilon=0.01) == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    # The adopted file is the RAW config (loadable by find() directly), not wrapped.
    assert "rrf_k" in written and "config" not in written
    assert at.ranking_config_from_jsonable(written).rrf_k == 30


def test_adopt_writes_raw_config_within_floor(tmp_path: Path) -> None:
    cand = tmp_path / "cand.json"
    target = tmp_path / "ranking_config.json"
    at.write_candidate(cand, at.RankingConfig(compiled_boost=1.3), _meta(candidate=0.905))
    assert at.adopt(cand, target, force=False, epsilon=0.01) == 0
    loaded = at.ranking_config_from_jsonable(json.loads(target.read_text(encoding="utf-8")))
    assert loaded.compiled_boost == 1.3
