"""Offline retrieval-quality eval harness for kb-mcp.

Runs `find()` against the REAL vault (embeddings ENABLED) over a golden query
set and reports NDCG@5/@10, MRR, recall@10 — so every ranking change becomes a
number that goes up or down instead of a vibe. `--sweep` walks the RankingConfig
knobs and prints a ranked comparison plus a markdown table you can file as a
governance pattern note.

Usage:
    uv run python scripts/eval_retrieval.py                  # baseline (DEFAULT config)
    uv run python scripts/eval_retrieval.py --sweep          # rrf_k x compiled_boost grid
    uv run python scripts/eval_retrieval.py --sweep --include-rerank --markdown

This is a dev/eval tool: it imports kb_mcp directly and needs the bge model
(it force-enables embeddings). It writes nothing to the vault.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The eval MUST run with live vectors — undo any inherited test/service disable.
os.environ.pop("KB_MCP_DISABLE_EMBEDDINGS", None)

from kb_mcp import eval_metrics as metrics  # noqa: E402
from kb_mcp import find as find_module  # noqa: E402
from kb_mcp.vault import resolve_vault  # noqa: E402

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
    k_max: int = 10,
) -> dict:
    """Run every golden query under `config`; return mean metrics + per-query rows."""
    rows: list[dict] = []
    for g in golden:
        hits = find_module.find(
            vault_root,
            query=g["query"],
            limit=k_max,
            mode="hybrid",
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
        print(f"| config | NDCG@5 | NDCG@10 | MRR | recall@10 |")
        print(f"|---|---|---|---|---|")
        for label, res, _cfg, _rr in results:
            print(
                f"| {label} | {res['ndcg5']:.4f} | {res['ndcg10']:.4f} | "
                f"{res['mrr']:.4f} | {res['recall10']:.4f} |"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--sweep", action="store_true", help="grid-search the ranking knobs")
    ap.add_argument("--include-rerank", action="store_true", help="add the rerank axis to --sweep (slow)")
    ap.add_argument("--rerank", action="store_true", help="baseline run with rerank on")
    ap.add_argument("--markdown", action="store_true", help="emit a markdown results table")
    args = ap.parse_args()

    vault_root = resolve_vault()
    golden = _load_golden(args.golden)
    if not golden:
        print(f"no golden queries loaded from {args.golden}", file=sys.stderr)
        return 1
    print(f"vault={vault_root}")
    print(f"golden set: {len(golden)} queries from {args.golden}")

    if args.sweep:
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
