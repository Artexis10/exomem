"""Self-tuning RankingConfig search — the closed feedback loop, desk-side.

Coordinate-descends the RankingConfig knobs (rrf_k, compiled_boost,
source_penalty, temporal_boost, and the per-intent lane weights) under a
LEXICOGRAPHIC objective `(pair_mrr, golden_ndcg)`:

- the 9 hand-authored golden queries are the trusted anchor, enforced as a HARD
  FLOOR — any candidate whose golden NDCG@10 drops more than EPSILON below the
  DEFAULT_RANKING baseline is infeasible and never selected;
- the mined `(query -> cited_path)` pairs (fresh-mined each run) are the
  improvement signal, scored as BINARY relevance via mean reciprocal rank — a
  cited doc is relevant, full stop; the mined `confidence` is only a filter
  (CONF_MIN), never a grade (grading by it would bake the incumbent rank in);
- below MIN_PAIRS distinct eligible pair queries the pairs term is OFF and the
  run reduces exactly to a golden-only tune.

It writes a reviewed CANDIDATE (`logs/ranking_config.candidate.json`) + a delta
REPORT — it NEVER auto-applies and never edits find.py. `--adopt` promotes the
candidate to the committed repo-root `ranking_config.json` (which find() loads
when no explicit config is passed), gated by the same golden floor. Reversal is
deleting / reverting that file.

Pure-substrate: deterministic coordinate descent over a fixed grid; relevance
labels come only from recorded usage; no model decides anything.

The real run needs torch + the live vault (force-enables embeddings via
eval_retrieval), so it is desk-side only:

    uv run python scripts/auto_tune_ranking.py            # mine -> tune -> candidate + report
    uv run python scripts/auto_tune_ranking.py --window-hours 6
    uv run python scripts/auto_tune_ranking.py --adopt    # promote candidate (floor-gated)

The pure pieces — `optimize()`, `pairs_to_eval()`, `pair_mrr()`,
`combined_score()`, `config_from_dict()`, `write_candidate()`, `adopt()` — are
torch-free and unit-tested in tests/test_auto_tune.py.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))  # sibling scripts (derive_relevance_pairs, eval_retrieval)

from exomem.find import (  # noqa: E402
    DEFAULT_RANKING,
    RankingConfig,
    ranking_config_from_jsonable,
    ranking_config_to_jsonable,
)

DEFAULT_GOLDEN = HERE.parent / "tests" / "golden" / "queries.yaml"
DEFAULT_PAIRS = HERE.parent / "logs" / "relevance_pairs.jsonl"
DEFAULT_CANDIDATE = HERE.parent / "logs" / "ranking_config.candidate.json"
DEFAULT_REPORT = HERE.parent / "logs" / "ranking_config.report.md"
ADOPTED_CONFIG = HERE.parent / "ranking_config.json"

# Objective guardrails (all CLI-overridable). See openspec/changes/close-auto-tune-loop.
EPSILON = 0.01     # max golden NDCG@10 the tuner may spend below baseline (hard floor)
MIN_PAIRS = 8      # distinct eligible pair queries before the pairs term turns on
CONF_MIN = 0.25    # mined-confidence floor — a FILTER on pairs, never a relevance grade


# --------------------------------------------------------------------------- #
# Pure, torch-free core: the coordinate-descent optimizer.
# --------------------------------------------------------------------------- #
def optimize(
    candidates: dict[str, list[Any]],
    evaluate_fn: Callable[[dict[str, Any]], float],
    *,
    start: dict[str, Any] | None = None,
    max_passes: int = 12,
) -> tuple[dict[str, Any], float]:
    """Coordinate-descent over `candidates` to maximize `evaluate_fn(config)`.

    `candidates` maps each knob name to its list of candidate values. Starting
    from `start` (or the first value of each axis), each pass walks the knobs in
    order, swapping in the single best value for each while holding the rest
    fixed. Stops when a full pass yields no improvement. Deterministic: ties keep
    the incumbent, so the same inputs always produce the same winner.

    Returns `(best_config, best_score)` where best_config is a knob->value dict.
    """
    if start is not None:
        current = dict(start)
    else:
        current = {knob: values[0] for knob, values in candidates.items() if values}
    best_score = evaluate_fn(current)
    for _ in range(max_passes):
        improved = False
        for knob, values in candidates.items():
            for value in values:
                if current.get(knob) == value:
                    continue
                trial = dict(current)
                trial[knob] = value
                score = evaluate_fn(trial)
                if score > best_score:
                    best_score = score
                    current = trial
                    improved = True
        if not improved:
            break
    return current, best_score


# --------------------------------------------------------------------------- #
# Loaders (torch-free).
# --------------------------------------------------------------------------- #
def load_relevance_pairs(path: Path) -> list[dict]:
    """Parse a `relevance_pairs.jsonl` feedback-loop log into dict rows.

    Skips blank lines and malformed JSON (the log is append-only and may be
    partially written). Mirrors the shape derive_relevance_pairs.py emits.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def load_golden(path: Path) -> list[dict]:
    """Load the golden query set into `{query, relevance, relevant}` rows.

    Same lenient parse as eval_retrieval._load_golden (graded or expect_any_of),
    re-implemented here so the loader stays torch-free and import-side-effect
    free for unit tests.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out: list[dict] = []
    for entry in raw:
        query = entry.get("query")
        if not query:
            continue
        if entry.get("graded"):
            relevance = {_canon(p): float(g) for p, g in entry["graded"].items()}
        else:
            relevance = {_canon(p): 1.0 for p in entry.get("expect_any_of", [])}
        if not relevance:
            continue
        relevant = {p for p, g in relevance.items() if g > 0}
        out.append({"query": query, "relevance": relevance, "relevant": relevant})
    return out


def _canon(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    if p.lower().endswith(".md"):
        p = p[:-3]
    if p.startswith("Knowledge Base/"):
        p = p[len("Knowledge Base/"):]
    return p.lower()


# --------------------------------------------------------------------------- #
# Knob <-> RankingConfig mapping + search space.
# --------------------------------------------------------------------------- #
def default_knobs() -> dict[str, Any]:
    """The DEFAULT_RANKING expressed as the flat knob dict the optimizer tunes."""
    return {
        "rrf_k": DEFAULT_RANKING.rrf_k,
        "compiled_boost": DEFAULT_RANKING.compiled_boost,
        "source_penalty": DEFAULT_RANKING.source_penalty,
        "temporal_boost": DEFAULT_RANKING.temporal_boost,
        "exact_lexical_weight": DEFAULT_RANKING.intent_weights_exact[1],
        "relationship_graph_weight": DEFAULT_RANKING.intent_weights_relationship[4],
        "temporal_lane_weight": DEFAULT_RANKING.intent_weights_temporal[5],
    }


def candidate_axes() -> dict[str, list[Any]]:
    """The coordinate-descent grid. Conceptual weights are intentionally NOT a
    knob — they stay neutral (all 1.0) so tuning never perturbs the common case."""
    return {
        "rrf_k": [30, 60, 100],
        "compiled_boost": [1.0, 1.15, 1.3],
        "source_penalty": [0.7, 0.85, 1.0],
        "temporal_boost": [1.0, 1.5, 2.0],
        "exact_lexical_weight": [1.0, 1.25, 1.5],
        "relationship_graph_weight": [1.0, 1.4, 1.8],
        "temporal_lane_weight": [1.0, 1.5, 2.0],
    }


def config_from_dict(knobs: dict[str, Any]) -> RankingConfig:
    """Build a RankingConfig from a knob dict (missing keys fall back to DEFAULT).

    The per-intent weight scalars expand into the full lane tuples: `exact`
    up-weights the lexical lanes (bm25+keyword), `relationship` the graph lane,
    `temporal` the recency lane. Conceptual stays neutral.
    """
    base = DEFAULT_RANKING
    exw = float(knobs.get("exact_lexical_weight", base.intent_weights_exact[1]))
    rgw = float(
        knobs.get("relationship_graph_weight", base.intent_weights_relationship[4])
    )
    tlw = float(knobs.get("temporal_lane_weight", base.intent_weights_temporal[5]))
    return dataclasses.replace(
        base,
        rrf_k=int(knobs.get("rrf_k", base.rrf_k)),
        compiled_boost=float(knobs.get("compiled_boost", base.compiled_boost)),
        source_penalty=float(knobs.get("source_penalty", base.source_penalty)),
        temporal_boost=float(knobs.get("temporal_boost", base.temporal_boost)),
        intent_weights_exact=(0.7, exw, exw, 1.0, 0.7, 1.0),
        intent_weights_relationship=(1.0, 1.0, 1.0, 1.0, rgw, 1.0),
        intent_weights_temporal=(1.0, 1.0, 1.0, 1.0, 1.0, tlw),
    )


# --------------------------------------------------------------------------- #
# Mined-pair scoring (torch-free): pairs are BINARY relevance labels.
# --------------------------------------------------------------------------- #
def pairs_to_eval(
    pairs: list[dict], golden_queries: set[str], *, conf_min: float
) -> list[dict]:
    """Group mined pairs into binary-relevance eval rows.

    Drops pairs below `conf_min` (confidence is a FILTER, never a grade) and any
    pair whose query already appears in `golden_queries` (lower/stripped), so the
    trusted golden signal isn't double-counted. Returns one row per remaining
    distinct query: `{"query": <raw>, "relevant": set(canon paths)}`.
    """
    by_query: dict[str, set[str]] = {}
    for p in pairs:
        if float(p.get("confidence", 0.0)) < conf_min:
            continue
        q = p.get("query")
        cited = p.get("cited_path")
        if not q or not cited:
            continue
        if q.strip().lower() in golden_queries:
            continue
        by_query.setdefault(q, set()).add(cited)
    return [{"query": q, "relevant": rel} for q, rel in by_query.items() if rel]


def pair_mrr(ranked_by_query: dict[str, list[str]], pair_rows: list[dict]) -> float:
    """Mean reciprocal rank of the first relevant (cited) path per eligible query.

    Binary relevance: the score depends only on WHERE a cited path lands in the
    ranking, never on the mined confidence — so a rank-1 pair is a guardrail, not a
    lever that would reward keeping the incumbent order.
    """
    if not pair_rows:
        return 0.0
    total = 0.0
    for row in pair_rows:
        ranked = ranked_by_query.get(row["query"], [])
        relevant = row["relevant"]
        rr = 0.0
        for idx, path in enumerate(ranked, start=1):
            if path in relevant:
                rr = 1.0 / idx
                break
        total += rr
    return total / len(pair_rows)


def pair_recall10(
    ranked_by_query: dict[str, list[str]], pair_rows: list[dict], *, k: int = 10
) -> float:
    """Mean fraction of a query's cited paths present in the top-k (report-only)."""
    if not pair_rows:
        return 0.0
    total = 0.0
    for row in pair_rows:
        ranked = ranked_by_query.get(row["query"], [])[:k]
        relevant = row["relevant"]
        if not relevant:
            continue
        total += sum(1 for p in relevant if p in ranked) / len(relevant)
    return total / len(pair_rows)


def combined_score(
    golden_ndcg: float,
    pair_mrr_value: float,
    *,
    baseline_golden: float,
    epsilon: float,
    n_eligible: int,
    min_pairs: int,
) -> tuple[float, float]:
    """The lexicographic objective value for one config.

    `optimize()` compares these tuples by `>` (lexicographic):
      - `(-1.0, g)`      golden regresses past the floor → infeasible (dominated);
      - `(0.0, g)`       too few eligible pairs          → golden-only (== today);
      - `(pair_mrr, g)`  otherwise                       → pairs primary, golden tiebreak.
    """
    if golden_ndcg < baseline_golden - epsilon:
        return (-1.0, golden_ndcg)
    if n_eligible < min_pairs:
        return (0.0, golden_ndcg)
    return (pair_mrr_value, golden_ndcg)


# --------------------------------------------------------------------------- #
# Candidate + report writers, and the floor-gated adopt step (torch-free).
# --------------------------------------------------------------------------- #
def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (tmp sibling + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def write_candidate(path: Path, cfg: RankingConfig, meta: dict) -> None:
    """Write the reviewed candidate: `{config: <full knobs>, meta: <measurements>}`.

    `config` is a full, find()-loadable RankingConfig; `meta` carries the measured
    golden/pairs values the adopt gate reads (so adoption needs no torch rerun).
    """
    payload = {"config": ranking_config_to_jsonable(cfg), "meta": meta}
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _knob_delta_rows(default_cfg: RankingConfig, best_cfg: RankingConfig) -> list[str]:
    d = ranking_config_to_jsonable(default_cfg)
    b = ranking_config_to_jsonable(best_cfg)
    return [f"| `{key}` | {d[key]} | {b[key]} |" for key in d if b[key] != d[key]]


def write_report(
    path: Path, default_cfg: RankingConfig, best_cfg: RankingConfig, meta: dict
) -> None:
    """Write a human-readable markdown delta report for review before `--adopt`."""
    deltas = _knob_delta_rows(default_cfg, best_cfg)
    lines = [
        "# Ranking auto-tune report",
        "",
        f"- golden NDCG@10: {meta['baseline_golden']} (baseline) "
        f"→ {meta['candidate_golden']} (candidate)",
        f"- pair-MRR: {meta['pair_mrr']} | pair-recall@10: {meta['pair_recall10']}",
        f"- eligible pair queries: {meta['n_eligible_pairs']} "
        f"(MIN_PAIRS={meta['min_pairs']}, guard_active={meta['guard_active']})",
        f"- window_hours={meta['window_hours']} epsilon={meta['epsilon']}",
        "",
        "## Knob changes vs DEFAULT_RANKING",
        "",
    ]
    if deltas:
        lines += ["| knob | default | candidate |", "|---|---|---|", *deltas]
    else:
        lines.append(
            "_No knob changed — DEFAULT_RANKING is already optimal under the "
            "current objective (expected while the pairs guard is active)._"
        )
    lines += [
        "",
        "## To adopt",
        "",
        "```",
        "uv run python scripts/auto_tune_ranking.py --adopt",
        "git add ranking_config.json && git commit -m 'tune: adopt ranking config'",
        "# deploy + restart the service — find() loads ranking_config.json at startup",
        "```",
        "",
        "Revert anytime by deleting `ranking_config.json` (or `git revert`).",
        "",
    ]
    _atomic_write_text(path, "\n".join(lines))


def adopt(
    candidate_path: Path, target_path: Path, *, force: bool, epsilon: float
) -> int:
    """Promote the candidate to the committed adopted config, gated by the floor.

    Refuses when the candidate's recorded golden NDCG@10 is more than `epsilon`
    below its recorded baseline (the same floor that governs tuning) unless
    `force`. Writes ONLY the raw config knobs to `target_path` so find() loads it
    directly.
    """
    if not candidate_path.exists():
        print(f"no candidate at {candidate_path}; run a tune first", file=sys.stderr)
        return 1
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    config_dict = payload.get("config", {})
    meta = payload.get("meta", {})
    # Validate the config loads (raises on a malformed/foreign file).
    ranking_config_from_jsonable(config_dict)
    bg, cg = meta.get("baseline_golden"), meta.get("candidate_golden")
    if bg is not None and cg is not None and cg < bg - epsilon and not force:
        print(
            f"refusing to adopt: candidate golden NDCG@10 {cg} is more than "
            f"{epsilon} below baseline {bg}. Re-run with --adopt --force to override.",
            file=sys.stderr,
        )
        return 1
    _atomic_write_text(
        target_path, json.dumps(config_dict, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"adopted -> {target_path}")
    print(f"  git add {target_path.name} && git commit -m 'tune: adopt ranking config'")
    print("  # then deploy + restart so find() reloads the config")
    return 0


# --------------------------------------------------------------------------- #
# Real (desk-side) combined objective — reuses the offline harness. Lazy-imported
# so the pure core above stays torch-free.
# --------------------------------------------------------------------------- #
def build_combined_evaluate_fn(
    vault_root: Path,
    golden: list[dict],
    pair_rows: list[dict],
    *,
    baseline_golden: float,
    epsilon: float,
    min_pairs: int,
    rerank: bool = False,
) -> Callable[[dict[str, Any]], tuple[float, float]]:
    """Return `knobs -> (pair_mrr, golden_ndcg)`, the lexicographic objective."""
    import eval_retrieval  # lazy: force-enables embeddings on import

    pair_queries = [r["query"] for r in pair_rows]
    n_eligible = len(pair_rows)

    def _evaluate(knobs: dict[str, Any]) -> tuple[float, float]:
        cfg = config_from_dict(knobs)
        g = eval_retrieval._evaluate(vault_root, golden, cfg, rerank=rerank)["ndcg10"]
        # Infeasible or guarded: the pairs term is irrelevant — skip ranking pairs.
        if g < baseline_golden - epsilon or n_eligible < min_pairs:
            pmrr = 0.0
        else:
            ranked = eval_retrieval.rank_queries(
                vault_root, pair_queries, cfg, rerank=rerank, k=10
            )
            pmrr = pair_mrr(ranked, pair_rows)
        return combined_score(
            g, pmrr, baseline_golden=baseline_golden, epsilon=epsilon,
            n_eligible=n_eligible, min_pairs=min_pairs,
        )

    return _evaluate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--window-hours", type=float, default=2.0,
                    help="mining window: max gap between a find() and a citing write")
    ap.add_argument("--rerank", action="store_true", help="evaluate with rerank on")
    ap.add_argument("--max-passes", type=int, default=12)
    ap.add_argument("--epsilon", type=float, default=EPSILON,
                    help="golden NDCG@10 floor below baseline (default 0.01)")
    ap.add_argument("--min-pairs", type=int, default=MIN_PAIRS,
                    help="distinct eligible pair queries before pairs term turns on")
    ap.add_argument("--conf-min", type=float, default=CONF_MIN,
                    help="mined-confidence filter on pairs (not a grade)")
    ap.add_argument("--adopt", action="store_true",
                    help="promote the existing candidate to ranking_config.json (floor-gated)")
    ap.add_argument("--force", action="store_true",
                    help="with --adopt, promote even if it regresses golden")
    args = ap.parse_args()

    if args.adopt:
        return adopt(args.candidate, ADOPTED_CONFIG, force=args.force, epsilon=args.epsilon)

    import derive_relevance_pairs as drp  # lazy (torch-free)

    from exomem.vault import resolve_vault  # lazy

    vault_root = resolve_vault()
    golden = load_golden(args.golden)
    if not golden:
        print(f"no golden queries loaded from {args.golden}", file=sys.stderr)
        return 1

    # Step 0: mine fresh so the objective reflects current usage (idempotent snapshot).
    pairs = drp.mine_pairs(args.window_hours * 3600, write=True)
    golden_queries = {g["query"].strip().lower() for g in golden}
    pair_rows = pairs_to_eval(pairs, golden_queries, conf_min=args.conf_min)
    guard_active = len(pair_rows) < args.min_pairs

    print(f"vault={vault_root}")
    print(f"golden: {len(golden)} queries | mined pairs: {len(pairs)} | "
          f"eligible pair queries: {len(pair_rows)} | pairs guard: "
          f"{'ON (golden-only)' if guard_active else 'off'}")

    import eval_retrieval  # lazy: force-enables embeddings
    baseline_golden = eval_retrieval._evaluate(
        vault_root, golden, DEFAULT_RANKING, rerank=args.rerank
    )["ndcg10"]

    evaluate_fn = build_combined_evaluate_fn(
        vault_root, golden, pair_rows,
        baseline_golden=baseline_golden, epsilon=args.epsilon,
        min_pairs=args.min_pairs, rerank=args.rerank,
    )
    start = default_knobs()
    best, _best_score = optimize(
        candidate_axes(), evaluate_fn, start=start, max_passes=args.max_passes
    )
    best_cfg = config_from_dict(best)

    # Final measured metrics for the chosen config (candidate meta + report).
    candidate_golden = eval_retrieval._evaluate(
        vault_root, golden, best_cfg, rerank=args.rerank
    )["ndcg10"]
    if guard_active:
        pmrr = prec = None
    else:
        ranked = eval_retrieval.rank_queries(
            vault_root, [r["query"] for r in pair_rows], best_cfg, rerank=args.rerank, k=10
        )
        pmrr = round(pair_mrr(ranked, pair_rows), 4)
        prec = round(pair_recall10(ranked, pair_rows), 4)

    meta = {
        "baseline_golden": round(baseline_golden, 4),
        "candidate_golden": round(candidate_golden, 4),
        "pair_mrr": pmrr,
        "pair_recall10": prec,
        "n_eligible_pairs": len(pair_rows),
        "guard_active": guard_active,
        "window_hours": args.window_hours,
        "epsilon": args.epsilon,
        "min_pairs": args.min_pairs,
    }
    write_candidate(args.candidate, best_cfg, meta)
    write_report(args.report, DEFAULT_RANKING, best_cfg, meta)

    changed = [k for k in best if best[k] != start.get(k)]
    print(f"\nDEFAULT golden NDCG@10 = {baseline_golden:.4f}")
    print(f"BEST    golden NDCG@10 = {candidate_golden:.4f}  "
          f"(delta {candidate_golden - baseline_golden:+.4f}; floor -{args.epsilon})")
    if not guard_active:
        print(f"pair-MRR = {pmrr}  pair-recall@10 = {prec}")
    print(f"changed knobs: {', '.join(changed) if changed else '(none — default is optimal)'}")
    print(f"candidate -> {args.candidate}")
    print(f"report    -> {args.report}")
    print("review the report, then: uv run python scripts/auto_tune_ranking.py --adopt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
