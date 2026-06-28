"""Self-tuning RankingConfig search — desk-side tooling, NO server changes.

Coordinate-descends the RankingConfig knobs (rrf_k, compiled_boost,
source_penalty, temporal_boost, and the per-intent lane weights) to MAXIMIZE
NDCG@10 over the golden query set, reusing the same NDCG evaluator the offline
harness uses (`scripts/eval_retrieval.py`). It PROPOSES (prints) the winning
config and its delta vs DEFAULT_RANKING — it never writes defaults back. The
operator reviews and edits `find.py` by hand (visibility over auto-mutation).

Pure-substrate: the optimizer is deterministic coordinate descent over a fixed
candidate grid. No model decides anything.

The real run needs torch + the live vault (it force-enables embeddings via
eval_retrieval), so it is desk-side only:

    uv run python scripts/auto_tune_ranking.py
    uv run python scripts/auto_tune_ranking.py --window-hours 6

The pure pieces — `optimize()`, `load_relevance_pairs()`, `load_golden()`,
`config_from_dict()` — are torch-free and unit-tested in tests/test_auto_tune.py.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kb_mcp.find import DEFAULT_RANKING, RankingConfig  # noqa: E402

DEFAULT_GOLDEN = HERE.parent / "tests" / "golden" / "queries.yaml"
DEFAULT_PAIRS = HERE.parent / "logs" / "relevance_pairs.jsonl"


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
# Real (desk-side) NDCG evaluator — reuses the offline harness. Lazy-imported so
# the pure core above stays torch-free.
# --------------------------------------------------------------------------- #
def build_evaluate_fn(
    vault_root: Path, golden: list[dict], *, rerank: bool = False
) -> Callable[[dict[str, Any]], float]:
    """Return `knobs -> mean NDCG@10` over the golden set, on the live vault."""
    import eval_retrieval  # lazy: force-enables embeddings on import

    def _evaluate(knobs: dict[str, Any]) -> float:
        cfg = config_from_dict(knobs)
        return eval_retrieval._evaluate(
            vault_root, golden, cfg, rerank=rerank
        )["ndcg10"]

    return _evaluate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS)
    ap.add_argument("--rerank", action="store_true", help="evaluate with rerank on")
    ap.add_argument("--max-passes", type=int, default=12)
    args = ap.parse_args()

    from kb_mcp.vault import resolve_vault  # lazy

    vault_root = resolve_vault()
    golden = load_golden(args.golden)
    if not golden:
        print(f"no golden queries loaded from {args.golden}", file=sys.stderr)
        return 1
    pairs = load_relevance_pairs(args.pairs)
    print(f"vault={vault_root}")
    print(f"golden: {len(golden)} queries | relevance_pairs: {len(pairs)} rows")

    evaluate_fn = build_evaluate_fn(vault_root, golden, rerank=args.rerank)
    start = default_knobs()
    base_score = evaluate_fn(start)
    best, best_score = optimize(
        candidate_axes(), evaluate_fn, start=start, max_passes=args.max_passes
    )

    print(f"\nDEFAULT NDCG@10 = {base_score:.4f}")
    print(f"BEST    NDCG@10 = {best_score:.4f}  (delta {best_score - base_score:+.4f})")
    print("\n=== PROPOSED config (review, then hand-edit find.py defaults) ===")
    for knob, value in best.items():
        changed = " *" if value != start.get(knob) else ""
        print(f"  {knob:<28} {value}{changed}")
    cfg = config_from_dict(best)
    print("\nRankingConfig(")
    print(f"    rrf_k={cfg.rrf_k}, compiled_boost={cfg.compiled_boost}, ")
    print(f"    source_penalty={cfg.source_penalty}, temporal_boost={cfg.temporal_boost},")
    print(f"    intent_weights_exact={cfg.intent_weights_exact},")
    print(f"    intent_weights_relationship={cfg.intent_weights_relationship},")
    print(f"    intent_weights_temporal={cfg.intent_weights_temporal},")
    print(")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
