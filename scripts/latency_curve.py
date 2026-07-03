"""Per-lane retrieval latency vs. corpus size — the anti-"5ms lie" harness.

A fixture-scale (10-file) latency benchmark once reported a whole `find()` at
~5ms and hid a real ~14s graph-lane cost on the owner's ~1700-note vault. The
lesson: latency MUST be measured at realistic corpus scale AND broken out PER
LANE, or a single-lane blow-up disappears into an aggregate that stays small on
a toy corpus. This harness does exactly that.

For each corpus size it:
  1. generates a synthetic, densely-wikilinked vault (`scripts/synth_vault.py`),
  2. seeds the freshness registry the way the file watcher does (so the graph
     lane's resolver is live and warm, like production),
  3. warms every lane with one query, then
  4. times a fixed query set `--repeat` times with `find(include_timings=True)`,
     recording the PER-STAGE milliseconds `FindTimings` exposes,
and prints a markdown table of median / p90 PER LANE (vector, bm25, keyword,
graph, fusion, rerank) plus the end-to-end total, at each size.

Two measurement modes:
  * model-free (default): the vector/CLIP/rerank lanes are switched off so the
    run needs no GPU, no model download, and no embedding sidecar. It reports
    the lanes that scale with corpus AND don't need a model — bm25, keyword,
    graph, fusion — which is exactly where the ~14s graph regression lived. This
    mode is deterministic and reproducible on any machine.
  * `--embeddings`: builds a real embedding sidecar per synthetic vault and adds
    the vector lane (and `--rerank` the rerank lane). Needs `--extra embeddings`
    + the model cache; the sidecar build dominates wall-time at large sizes, so
    keep `--max-embeddings-size` sane.

Usage:
    uv run python scripts/latency_curve.py
    uv run python scripts/latency_curve.py --sizes 100,500,1000,2000,5000 --repeat 5
    uv run --extra embeddings python scripts/latency_curve.py --embeddings --rerank \
        --sizes 100,500,1000,2000

It writes nothing to any real vault — every corpus lives in a throwaway temp dir
that is removed after the size is measured.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

# --- Isolate this benchmark from any host/service config BEFORE importing exomem.
# Every measured find() must run all lanes (no hot-cache short-circuit) and must
# not spawn the warm thread / file watcher / load a committed ranking config.
os.environ.setdefault("EXOMEM_FIND_CACHE_SIZE", "0")  # measure lanes, not the cache
os.environ.setdefault("EXOMEM_DISABLE_WARMUP", "1")
os.environ.setdefault("EXOMEM_DISABLE_FILE_WATCHER", "1")
os.environ.setdefault("EXOMEM_DISABLE_RANKING_CONFIG", "1")
os.environ.setdefault("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
os.environ.setdefault("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")

# Emit UTF-8 regardless of the host console codepage, so the em-dash for an
# absent lane survives redirection into the markdown report on Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from synth_vault import gen_dense_vault  # noqa: E402

from exomem import find as find_module  # noqa: E402
from exomem import freshness  # noqa: E402
from exomem.vault import walk_vault_md  # noqa: E402

DEFAULT_SIZES = [100, 500, 1000, 2000, 5000]

# Fixed query set, run identically at every size. A spread of shapes so more than
# one lane is exercised: a long natural-language query, a short topical one, and
# a bare multi-term query. They intentionally hit the synthetic corpus's shared
# vocabulary ("topic", "note", "related") so every lane produces candidates.
DEFAULT_QUERIES = [
    "topic prose paragraph related context",
    "note about synthetic dense graph",
    "realistically sized body text for ranking",
    "related links between insight pattern notes",
    "concept people sources experiments topic",
]

# The lanes we report, in a fixed column order. Names must match FindTimings'
# stage keys (see find.FindTimings / the _span(...) call sites).
LANES = ("vector", "bm25", "keyword", "graph", "fusion", "rerank")


class _NoEmbeddingIndex:
    """Sentinel: make find()'s vector lane take its lean-deployment path.

    find() treats an ImportError from the embedding getter as a DEPLOYMENT SHAPE
    (no embeddings extra), not a failure — it logs and falls back to BM25/keyword
    without recording degradation. Raising it here is the cleanest way to switch
    the vector (and, with CLIP disabled, the CLIP) lane OFF for a model-free run,
    regardless of whether torch happens to be importable on this machine.
    """


def _install_model_free() -> None:
    """Force the vector + CLIP lanes off for a model-free measurement."""
    os.environ["EXOMEM_DISABLE_CLIP"] = "1"  # clip_enabled() -> False, lane skipped

    def _raise(*_a, **_k):
        raise ImportError("model-free latency_curve run: vector lane disabled")

    # find() does `from . import embeddings` inside its recall function, then
    # calls embeddings.get_embedding_index(...) first in the vector span; an
    # ImportError there sends it down the intended lean/BM25 fallback. Patch the
    # module attribute so that lookup resolves to the raiser.
    from exomem import embeddings as embeddings_module
    embeddings_module.get_embedding_index = _raise  # type: ignore[assignment]


def _seed_freshness_live(vault: Path) -> None:
    """Seed the event-maintained freshness registry the way the watcher does,
    so the graph lane's resolver is live and warm (production shape)."""
    freshness.seed(
        vault, "vault",
        ((str(p), p.stat().st_mtime_ns) for p in walk_vault_md(vault)),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault, "kb",
        ((str(p), p.stat().st_mtime_ns) for p in find_module._walk_md(kb)),
    )


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (dependency-free)."""
    if not sorted_vals:
        return 0.0
    rank = math.ceil(pct / 100.0 * len(sorted_vals))
    idx = min(max(rank, 1), len(sorted_vals)) - 1
    return sorted_vals[idx]


def measure_size(
    n: int,
    *,
    queries: list[str],
    repeat: int,
    links_per_note: int,
    limit: int,
    embeddings_on: bool,
    rerank: bool,
) -> dict:
    """Generate an n-note vault and return per-lane + total latency aggregates.

    Returns `{"n", "chunks", "lanes": {lane: {"median","p90","samples"}},
    "total": {"median","p90"}}`. Lanes that never produced a timing (skipped /
    errored, e.g. vector in model-free mode) are absent from `lanes`.
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"exomem-latcurve-{n}-"))
    chunks = 0
    try:
        vault = tmp / "vault"
        gen_dense_vault(vault, n, links_per_note=links_per_note)
        _seed_freshness_live(vault)

        find_module.clear_cache()
        if embeddings_on:
            # Real sidecar so the vector lane searches actual chunks at scale.
            from exomem import embeddings as embeddings_module
            embeddings_module.clear_embedding_indexes()
            chunks = embeddings_module.get_embedding_index(vault).rebuild_all()

        lane_samples: dict[str, list[float]] = {lane: [] for lane in LANES}
        total_samples: list[float] = []

        # Warm every lane once (bm25 corpus, resolver, model/sidecar) so the
        # measured passes reflect steady-state, not first-touch build cost.
        for q in queries:
            find_module.find(
                vault, query=q, limit=limit, mode="hybrid",
                graph=True, rerank=rerank,
            )

        for _ in range(max(repeat, 1)):
            for q in queries:
                t = find_module.FindTimings()
                find_module.find(
                    vault, query=q, limit=limit, mode="hybrid",
                    graph=True, rerank=rerank, timings=t,
                )
                d = t.as_dict()
                total_samples.append(d["total_ms"])
                for lane in LANES:
                    stage = d["stages"].get(lane, {})
                    # A lane's `_span` finally-block records `ms` even when the
                    # lane body raised (e.g. the model-free vector ImportError),
                    # so exclude any lane that errored or was skipped — it did not
                    # actually run to completion and its ~0ms is not a lane cost.
                    if "ms" in stage and "error" not in stage and "skipped" not in stage:
                        lane_samples[lane].append(stage["ms"])

        lanes: dict[str, dict] = {}
        for lane, vals in lane_samples.items():
            if not vals:
                continue
            sv = sorted(vals)
            lanes[lane] = {
                "median": statistics.median(sv),
                "p90": _percentile(sv, 90),
                "samples": len(sv),
            }
        total_sorted = sorted(total_samples)
        return {
            "n": n,
            "chunks": chunks,
            "lanes": lanes,
            "total": {
                "median": statistics.median(total_sorted) if total_sorted else 0.0,
                "p90": _percentile(total_sorted, 90),
            },
        }
    finally:
        find_module.clear_cache()
        freshness.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _fmt_cell(agg: dict | None) -> str:
    """A `median / p90` cell in ms, or an em-dash when the lane didn't run."""
    if not agg:
        return "—"
    return f"{agg['median']:.1f} / {agg['p90']:.1f}"


def render_markdown(results: list[dict], *, embeddings_on: bool, rerank: bool) -> str:
    """Render the per-lane latency-vs-size curve as a markdown table (ms, median / p90)."""
    lines: list[str] = []
    mode = "hybrid + embeddings" if embeddings_on else "model-free (BM25/keyword/graph/fusion)"
    if embeddings_on and rerank:
        mode += " + rerank"
    lines.append(f"Per-lane latency (ms, median / p90) — mode: {mode}")
    lines.append("")
    header = ["Notes", *LANES, "total"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in results:
        cells = [str(r["n"])]
        for lane in LANES:
            cells.append(_fmt_cell(r["lanes"].get(lane)))
        cells.append(_fmt_cell(r["total"]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sizes", type=str, default=",".join(str(s) for s in DEFAULT_SIZES),
        help="comma-separated note counts (default 100,500,1000,2000,5000)",
    )
    ap.add_argument("--repeat", type=int, default=5, help="measured passes of the query set per size")
    ap.add_argument("--links-per-note", type=int, default=25, help="outbound wikilinks per synthetic note")
    ap.add_argument("--limit", type=int, default=10, help="find() result limit")
    ap.add_argument(
        "--embeddings", action="store_true",
        help="build a real sidecar per size and measure the vector lane (needs --extra embeddings)",
    )
    ap.add_argument("--rerank", action="store_true", help="also measure the rerank lane (implies model use)")
    ap.add_argument(
        "--max-embeddings-size", type=int, default=2000,
        help="skip sizes above this when --embeddings (sidecar build is expensive); default 2000",
    )
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    if args.embeddings:
        sizes = [s for s in sizes if s <= args.max_embeddings_size]
        if not sizes:
            print("no sizes <= --max-embeddings-size; nothing to measure", file=sys.stderr)
            return 1
    else:
        _install_model_free()

    print(
        f"latency curve: sizes={sizes} repeat={args.repeat} "
        f"links/note={args.links_per_note} queries={len(DEFAULT_QUERIES)} "
        f"embeddings={'on' if args.embeddings else 'off'} rerank={'on' if args.rerank else 'off'}",
        file=sys.stderr,
    )
    results: list[dict] = []
    for n in sizes:
        t0 = time.perf_counter()
        r = measure_size(
            n,
            queries=DEFAULT_QUERIES,
            repeat=args.repeat,
            links_per_note=args.links_per_note,
            limit=args.limit,
            embeddings_on=args.embeddings,
            rerank=args.rerank or args.embeddings,
        )
        results.append(r)
        chunk_note = f" chunks={r['chunks']}" if args.embeddings else ""
        print(
            f"  n={n:>5}{chunk_note}  graph={_fmt_cell(r['lanes'].get('graph'))}  "
            f"total={_fmt_cell(r['total'])}  ({time.perf_counter() - t0:.1f}s)",
            file=sys.stderr,
        )

    print()
    print(render_markdown(results, embeddings_on=args.embeddings, rerank=args.rerank or args.embeddings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
