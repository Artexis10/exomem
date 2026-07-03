"""Offline retrieval-quality eval harness for exomem.

Runs `find()` against the REAL vault (embeddings ENABLED) over a golden query
set and reports NDCG@5/@10, MRR, recall@10 — so every ranking change becomes a
number that goes up or down instead of a vibe. `--sweep` walks the RankingConfig
knobs and prints a ranked comparison plus a markdown table you can file as a
governance pattern note.

Usage:
    uv run python scripts/eval_retrieval.py                  # baseline (DEFAULT config)
    uv run python scripts/eval_retrieval.py --sweep          # rrf_k x compiled_boost grid
    uv run python scripts/eval_retrieval.py --sweep --include-rerank --markdown

This is a dev/eval tool: it imports exomem directly and needs the bge model
(it force-enables embeddings). It writes nothing to the vault.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The eval MUST run with live vectors — undo any inherited test/service disable.
os.environ.pop("EXOMEM_DISABLE_EMBEDDINGS", None)

from exomem import __version__ as EXOMEM_VERSION  # noqa: E402
from exomem import eval_metrics as metrics  # noqa: E402
from exomem import eval_report  # noqa: E402
from exomem import find as find_module  # noqa: E402
from exomem.vault import resolve_vault  # noqa: E402

# Model names the harness runs against (the shipped default vector stack).
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
RERANKER_MODEL = "BAAI/bge-reranker-base"

DEFAULT_GOLDEN = HERE.parent / "tests" / "golden" / "queries.yaml"


def _canon(path: str) -> str:
    """Normalize a path so golden entries and find() results compare equal."""
    p = path.strip().replace("\\", "/")
    if p.lower().endswith(".md"):
        p = p[:-3]
    if p.startswith("Knowledge Base/"):
        p = p[len("Knowledge Base/"):]
    return p.lower()


def _load_golden(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out: list[dict] = []
    for entry in raw:
        query = entry.get("query")
        if not query:
            continue
        if "graded" in entry and entry["graded"]:
            relevance = {_canon(p): float(g) for p, g in entry["graded"].items()}
        else:
            relevance = {_canon(p): 1.0 for p in entry.get("expect_any_of", [])}
        if not relevance:
            continue
        relevant = {p for p, g in relevance.items() if g > 0}
        out.append({"query": query, "relevance": relevance, "relevant": relevant})
    return out


def _evaluate(
    vault_root: Path,
    golden: list[dict],
    config: find_module.RankingConfig,
    *,
    rerank: bool,
    mode: str = "hybrid",
    k_max: int = 10,
) -> dict:
    """Run every golden query under `config`; return mean metrics + per-query rows.

    `mode` defaults to "hybrid" so every existing caller (sweep, baseline) is
    byte-identical; the `--report markdown` path passes "keyword"/"hybrid".
    """
    rows: list[dict] = []
    for g in golden:
        hits = find_module.find(
            vault_root,
            query=g["query"],
            limit=k_max,
            mode=mode,
            # The golden targets are all KB paths, so evaluate KB-only: this skips
            # the scope="kb" auto-widen scan over the ~20-folder wider vault, which
            # dominates eval wall-time per query (the tuner makes hundreds of calls).
            scope="kb-only",
            rerank=rerank,
            config=config,
        )
        ranked = [_canon(h.path) for h in hits]
        rows.append({
            "query": g["query"],
            "ndcg5": metrics.ndcg_at_k(ranked, g["relevance"], 5),
            "ndcg10": metrics.ndcg_at_k(ranked, g["relevance"], 10),
            "mrr": metrics.mrr(ranked, g["relevant"]),
            "recall10": metrics.recall_at_k(ranked, g["relevant"], 10),
        })
    return {
        "ndcg5": metrics.mean(r["ndcg5"] for r in rows),
        "ndcg10": metrics.mean(r["ndcg10"] for r in rows),
        "mrr": metrics.mean(r["mrr"] for r in rows),
        "recall10": metrics.mean(r["recall10"] for r in rows),
        "n": len(rows),
        "rows": rows,
    }


def rank_queries(
    vault_root: Path,
    queries: list[str],
    config: find_module.RankingConfig,
    *,
    rerank: bool = False,
    k: int = 10,
) -> dict[str, list[str]]:
    """Return `{query: [canon'd ranked paths]}` for each query under `config`.

    The canonical find()+`_canon` path, reused by the auto-tuner's pair metrics so
    mined-pair scoring sees exactly what `_evaluate` scores for golden queries.
    """
    out: dict[str, list[str]] = {}
    for q in queries:
        hits = find_module.find(
            vault_root, query=q, limit=k, mode="hybrid",
            scope="kb-only",  # mined-pair targets are KB paths; skip the auto-widen scan
            rerank=rerank, config=config,
        )
        out[q] = [_canon(h.path) for h in hits]
    return out


def _config_label(cfg: find_module.RankingConfig, rerank: bool) -> str:
    return (
        f"rrf_k={cfg.rrf_k} boost={cfg.compiled_boost} "
        f"penalty={cfg.source_penalty} rerank={'on' if rerank else 'off'}"
    )


def _print_baseline(result: dict) -> None:
    print(f"\nPer-query (n={result['n']}):")
    print(f"  {'ndcg@5':>7} {'ndcg@10':>8} {'mrr':>6} {'rec@10':>7}  query")
    for r in result["rows"]:
        print(
            f"  {r['ndcg5']:7.3f} {r['ndcg10']:8.3f} {r['mrr']:6.3f} "
            f"{r['recall10']:7.3f}  {r['query'][:70]}"
        )
    print("\nMEANS:")
    print(
        f"  NDCG@5={result['ndcg5']:.4f}  NDCG@10={result['ndcg10']:.4f}  "
        f"MRR={result['mrr']:.4f}  recall@10={result['recall10']:.4f}"
    )


def _sweep(
    vault_root: Path, golden: list[dict], *, include_rerank: bool, markdown: bool
) -> None:
    rrf_ks = [30, 60, 100]
    boosts = [1.0, 1.15, 1.3]
    rerank_axis = [False, True] if include_rerank else [False]

    results: list[tuple[str, dict, find_module.RankingConfig, bool]] = []
    base = find_module.DEFAULT_RANKING
    for rerank in rerank_axis:
        for k in rrf_ks:
            for b in boosts:
                cfg = replace(base, rrf_k=k, compiled_boost=b)
                res = _evaluate(vault_root, golden, cfg, rerank=rerank)
                results.append((_config_label(cfg, rerank), res, cfg, rerank))
                print(
                    f"  {_config_label(cfg, rerank):<52} "
                    f"NDCG@10={res['ndcg10']:.4f} MRR={res['mrr']:.4f} "
                    f"rec@10={res['recall10']:.4f}"
                )

    results.sort(key=lambda t: -t[1]["ndcg10"])
    print("\n=== ranked by NDCG@10 ===")
    for label, res, _cfg, _rr in results:
        print(f"  NDCG@10={res['ndcg10']:.4f}  {label}")
    for metric in ("ndcg10", "mrr", "recall10"):
        winner = max(results, key=lambda t: t[1][metric])
        print(f"best {metric}: {winner[1][metric]:.4f}  [{winner[0]}]")

    if markdown:
        print("\n=== markdown (file via note(note_type='pattern', pattern_type='governance')) ===\n")
        print("| config | NDCG@5 | NDCG@10 | MRR | recall@10 |")
        print("|---|---|---|---|---|")
        for label, res, _cfg, _rr in results:
            print(
                f"| {label} | {res['ndcg5']:.4f} | {res['ndcg10']:.4f} | "
                f"{res['mrr']:.4f} | {res['recall10']:.4f} |"
            )


# Modes for the published benchmark: (label, find mode, rerank flag). The third
# is mode="hybrid" with rerank on — reranking is orthogonal to mode, not a mode.
_REPORT_MODES: tuple[tuple[str, str, bool], ...] = (
    ("keyword", "keyword", False),
    ("hybrid", "hybrid", False),
    ("hybrid+rerank", "hybrid", True),
)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (dependency-free).

    Nearest-rank: the value at ceil(pct/100 * N), 1-based, clamped into range.
    Cheap and adequate for a p90 over a few dozen latency samples; no interpolation.
    """
    if not sorted_vals:
        return 0.0
    rank = math.ceil(pct / 100.0 * len(sorted_vals))
    idx = min(max(rank, 1), len(sorted_vals)) - 1
    return sorted_vals[idx]


def _sample_latencies(
    vault_root: Path,
    golden: list[dict],
    config: find_module.RankingConfig,
    *,
    mode: str,
    rerank: bool,
    repeat: int,
    k: int = 10,
) -> list[float]:
    """Time every find() call over `repeat` passes of the golden set (ms each).

    Wraps each find() in time.perf_counter() — the same lightweight span pattern
    find.py uses internally — and returns a flat list of per-call latencies. No
    FindTimings recorder: the report needs end-to-end latency, not a per-stage
    breakdown, which a bare perf_counter delta already gives.
    """
    latencies: list[float] = []
    for _ in range(max(repeat, 1)):
        for g in golden:
            t0 = time.perf_counter()
            find_module.find(
                vault_root, query=g["query"], limit=k, mode=mode,
                scope="kb-only", rerank=rerank, config=config,
            )
            latencies.append((time.perf_counter() - t0) * 1000.0)
    return latencies


def _report_markdown(vault_root: Path, golden: list[dict], *, repeat: int) -> None:
    """Emit the aggregate-only benchmark report markdown to stdout."""
    per_mode: dict[str, dict] = {}
    for label, mode, rerank in _REPORT_MODES:
        res = _evaluate(
            vault_root, golden, find_module.DEFAULT_RANKING, rerank=rerank, mode=mode
        )
        lat = sorted(_sample_latencies(
            vault_root, golden, find_module.DEFAULT_RANKING,
            mode=mode, rerank=rerank, repeat=repeat,
        ))
        per_mode[label] = {
            "ndcg5": res["ndcg5"],
            "ndcg10": res["ndcg10"],
            "mrr": res["mrr"],
            "recall10": res["recall10"],
            "latency_median_ms": statistics.median(lat) if lat else 0.0,
            "latency_p90_ms": _percentile(lat, 90),
        }

    corpus = eval_report.count_corpus_stats(vault_root)
    meta = {
        "exomem_version": EXOMEM_VERSION,
        "embedding_model": EMBEDDING_MODEL,
        "reranker_model": RERANKER_MODEL,
        # Fill via EXOMEM_BENCH_HARDWARE at invocation; generic placeholder otherwise.
        "hardware": os.environ.get(
            "EXOMEM_BENCH_HARDWARE", "(fill in: CPU / GPU / RAM)"
        ),
        # Script-side date so the pure renderer never calls datetime.now().
        "date": date.today().isoformat(),
    }
    print()
    print(eval_report.render_benchmark_report(
        corpus=corpus, per_mode=per_mode, golden_n=len(golden), meta=meta
    ))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--sweep", action="store_true", help="grid-search the ranking knobs")
    ap.add_argument("--include-rerank", action="store_true", help="add the rerank axis to --sweep (slow)")
    ap.add_argument("--rerank", action="store_true", help="baseline run with rerank on")
    ap.add_argument("--markdown", action="store_true", help="emit a markdown results table")
    ap.add_argument(
        "--report", choices=["markdown"], default=None,
        help="emit the aggregate-only per-mode benchmark report (keyword/hybrid/"
             "hybrid+rerank) with latency percentiles and rounded corpus counts",
    )
    ap.add_argument(
        "--repeat", type=int, default=3,
        help="golden-set passes per mode for latency sampling under --report (default 3)",
    )
    args = ap.parse_args()

    vault_root = resolve_vault()
    golden = _load_golden(args.golden)
    if not golden:
        print(f"no golden queries loaded from {args.golden}", file=sys.stderr)
        return 1
    print(f"vault={vault_root}")
    print(f"golden set: {len(golden)} queries from {args.golden}")

    if args.report == "markdown":
        _report_markdown(vault_root, golden, repeat=args.repeat)
    elif args.sweep:
        _sweep(vault_root, golden, include_rerank=args.include_rerank, markdown=args.markdown)
    else:
        result = _evaluate(
            vault_root, golden, find_module.DEFAULT_RANKING, rerank=args.rerank
        )
        _print_baseline(result)
        if args.markdown:
            print("\n| metric | value |\n|---|---|")
            for m in ("ndcg5", "ndcg10", "mrr", "recall10"):
                print(f"| {m} | {result[m]:.4f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
