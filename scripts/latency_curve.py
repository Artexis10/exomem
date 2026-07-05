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
# stage keys (see find.FindTimings / the _span(...) call sites). NOTE:
# `outside_kb` (the scope="kb" auto-widen) costs ~0 on this synthetic corpus —
# every generated note lives inside Knowledge Base/ — but the column keeps the
# lane visible; on a real vault it was a per-query cost the harness silently
# omitted (see docs/benchmarks.md, real-vault section).
LANES = ("vector", "bm25", "keyword", "graph", "outside_kb", "fusion", "rerank")


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


# Vector-lane backend passes (--vec-backend). "binary" = sqlite-vec + binary
# quantization (EXOMEM_VEC_QUANT=binary). "numpy" is the reference pass: when
# present it runs first and the other backends report top-10 overlap against it.
# numpy is ALSO the product default now (vec0 is opt-in) — there is no `auto`
# value — so this harness always sets EXOMEM_VEC_BACKEND explicitly for each pass
# and must name both `numpy` and `sqlite-vec` to keep a published comparison honest.
VEC_BACKENDS = ("numpy", "sqlite-vec", "binary")


def _apply_vec_backend(name: str) -> None:
    """Point the vector lane at one backend via the env seam `search()` reads.

    Always sets EXOMEM_VEC_BACKEND to an explicit value (`numpy` or `sqlite-vec`);
    the harness never relies on the product default, so the pass measures exactly
    the named backend regardless of what production defaults to.
    """
    if name == "binary":
        os.environ["EXOMEM_VEC_BACKEND"] = "sqlite-vec"
        os.environ["EXOMEM_VEC_QUANT"] = "binary"
    else:
        os.environ["EXOMEM_VEC_BACKEND"] = name
        os.environ.pop("EXOMEM_VEC_QUANT", None)


# Lexical-lane backend passes (--lexical-backend): fts5 = the .lexical.sqlite
# sidecar (product default); python = the in-process rank-bm25 + substring
# scan (kill-switch shape, the before-side of the FTS5 change's evidence).
LEX_BACKENDS = ("fts5", "python", "auto")


def _apply_lexical_backend(name: str) -> None:
    """Point the bm25/keyword lanes at one lexical backend and reset the
    per-process lexstore state so each pass starts clean."""
    from exomem import lexstore

    os.environ["EXOMEM_LEXICAL_BACKEND"] = name
    lexstore.reset_memo()
    lexstore.clear_stores()


def _rss_mb() -> float | None:
    """Resident set size in MB via psutil, or None when psutil is absent."""
    try:
        import psutil  # noqa: PLC0415 — optional, desk-side diagnostics only
    except ImportError:
        return None
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _sidecar_chunk_count(vault: Path) -> int:
    """Row count of an existing sidecar (corpus-cache reuse path)."""
    import sqlite3

    from exomem import embeddings as embeddings_module

    sidecar = embeddings_module.sidecar_path(vault)
    if not sidecar.exists():
        return 0
    conn = sqlite3.connect(sidecar)
    try:
        return conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _measure_pass(
    vault: Path,
    *,
    queries: list[str],
    repeat: int,
    limit: int,
    rerank: bool,
) -> tuple[dict[str, dict], dict, list[list[str]]]:
    """Warm every lane, then time the query set. Returns `(lanes, total, top10s)`
    where `top10s` is the ordered top-10 path list per query (for cross-backend
    overlap)."""
    lane_samples: dict[str, list[float]] = {lane: [] for lane in LANES}
    total_samples: list[float] = []

    # Warm every lane once (bm25 corpus, resolver, model/sidecar/vec tables) so
    # the measured passes reflect steady-state, not first-touch build cost.
    for q in queries:
        find_module.find(
            vault, query=q, limit=limit, mode="hybrid",
            graph=True, rerank=rerank,
        )

    top10s: list[list[str]] = []
    for q in queries:
        hits = find_module.find(vault, query=q, limit=10, mode="hybrid", graph=True)
        top10s.append([h.path for h in hits])

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
    total = {
        "median": statistics.median(total_sorted) if total_sorted else 0.0,
        "p90": _percentile(total_sorted, 90),
    }
    return lanes, total, top10s


def _overlap_at_10(reference: list[list[str]], candidate: list[list[str]]) -> float:
    """Mean per-query overlap fraction of two top-10 path lists."""
    if not reference or len(reference) != len(candidate):
        return 0.0
    fracs = []
    for ref, cand in zip(reference, candidate, strict=True):
        denom = max(len(ref), 1)
        fracs.append(len(set(ref) & set(cand)) / denom)
    return sum(fracs) / len(fracs)


def measure_size(
    n: int,
    *,
    queries: list[str],
    repeat: int,
    links_per_note: int,
    limit: int,
    embeddings_on: bool,
    rerank: bool,
    vec_backends: list[str] | None = None,
    lexical_backends: list[str] | None = None,
    corpus_cache: Path | None = None,
) -> dict:
    """Measure an n-note vault; per-lane + total latency, per vector backend.

    Returns `{"n", "chunks", "backends": {backend: {"lanes", "total",
    "overlap10", "rss_mb"}}}`. Model-free mode uses the single pseudo-backend
    `"off"`. `overlap10` is the mean top-10 overlap vs the `numpy` pass (None
    for the reference itself and when numpy wasn't requested).

    With `corpus_cache`, the generated vault (and its embedding sidecar) persists
    under `corpus_cache/n<k>-l<links>-s7/` and is reused on the next run — the
    embed-once/measure-many contract that makes 100k-note tiers re-runnable.
    Without it, the corpus lives in a throwaway temp dir, as before.
    """
    backends = list(vec_backends or ["numpy"]) if embeddings_on else ["off"]
    if "numpy" in backends:  # reference pass first
        backends.sort(key=lambda b: b != "numpy")

    if corpus_cache is not None:
        root = corpus_cache / f"n{n}-l{links_per_note}-s7"
        root.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        root = Path(tempfile.mkdtemp(prefix=f"exomem-latcurve-{n}-"))
        cleanup = True

    chunks = 0
    try:
        vault = root / "vault"
        # A marker guards cache reuse: a run killed mid-generation must not leave a
        # partial vault that a later run silently measures as the full corpus.
        marker = root / "vault.ok"
        if not (vault.exists() and marker.exists()):
            if vault.exists():
                shutil.rmtree(vault, ignore_errors=True)
            gen_dense_vault(vault, n, links_per_note=links_per_note)
            marker.write_text(str(n), encoding="utf-8")

        find_module.clear_cache()
        if embeddings_on:
            from exomem import embeddings as embeddings_module
            embeddings_module.clear_embedding_indexes()
            chunks = _sidecar_chunk_count(vault)
            if not chunks:
                # Real sidecar so the vector lane searches actual chunks at scale.
                chunks = embeddings_module.get_embedding_index(vault).rebuild_all()

        lex_backends = list(lexical_backends or ["fts5"])
        results: dict[str, dict] = {}
        reference_top10: list[list[str]] | None = None
        for lex in lex_backends:
            _apply_lexical_backend(lex)
            for backend in backends:
                if backend != "off":
                    _apply_vec_backend(backend)
                    from exomem import embeddings as embeddings_module
                    embeddings_module.clear_embedding_indexes()
                find_module.clear_cache()
                # AFTER the cache clear (clear_cache wipes the freshness
                # registry): the seed is the production shape — a live watcher
                # keeps these triples event-maintained. Seeding before the
                # clear silently measured every lane registry-COLD, adding a
                # per-query O(N) full-vault stat walk that landed in whichever
                # lane derived its triple first (the historical 2k/10k/50k
                # graph "wall" of 226ms/1.1s/7.8s was exactly this walk).
                _seed_freshness_live(vault)
                lanes, total, top10s = _measure_pass(
                    vault, queries=queries, repeat=repeat, limit=limit, rerank=rerank
                )
                key = backend if len(lex_backends) == 1 else f"{backend}+{lex}"
                overlap = None
                if reference_top10 is None:
                    reference_top10 = top10s
                else:
                    overlap = _overlap_at_10(reference_top10, top10s)
                results[key] = {
                    "lanes": lanes,
                    "total": total,
                    "overlap10": overlap,
                    "rss_mb": _rss_mb(),
                }
        return {"n": n, "chunks": chunks, "backends": results}
    finally:
        os.environ.pop("EXOMEM_VEC_BACKEND", None)
        os.environ.pop("EXOMEM_VEC_QUANT", None)
        os.environ.pop("EXOMEM_LEXICAL_BACKEND", None)
        find_module.clear_cache()
        freshness.clear()
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)


def _fmt_cell(agg: dict | None) -> str:
    """A `median / p90` cell in ms, or an em-dash when the lane didn't run."""
    if not agg:
        return "—"
    return f"{agg['median']:.1f} / {agg['p90']:.1f}"


def render_markdown(results: list[dict], *, embeddings_on: bool, rerank: bool) -> str:
    """Render the per-lane latency-vs-size curve as markdown tables (ms, median / p90).

    One per-lane table per vector backend, plus (when more than one backend ran) a
    vector-lane summary table comparing backends side by side with top-10 overlap
    vs the numpy reference and process RSS after each pass.
    """
    lines: list[str] = []
    mode = "hybrid + embeddings" if embeddings_on else "model-free (BM25/keyword/graph/fusion)"
    if embeddings_on and rerank:
        mode += " + rerank"
    backends = list(results[0]["backends"].keys()) if results else []
    for backend in backends:
        label = f" — vector backend: {backend}" if backend != "off" else ""
        lines.append(f"Per-lane latency (ms, median / p90) — mode: {mode}{label}")
        lines.append("")
        header = ["Notes", *LANES, "total"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for r in results:
            b = r["backends"][backend]
            cells = [str(r["n"])]
            for lane in LANES:
                cells.append(_fmt_cell(b["lanes"].get(lane)))
            cells.append(_fmt_cell(b["total"]))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    if len(backends) > 1:
        lines.append("Vector lane by backend (ms, median / p90; overlap@10 vs numpy; RSS MB)")
        lines.append("")
        header = ["Notes", "chunks"]
        for backend in backends:
            header.append(f"vector {backend}")
            if backend != "numpy":
                header.append(f"overlap@10 {backend}")
        header.append("RSS " + "/".join(backends))
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for r in results:
            cells = [str(r["n"]), str(r["chunks"])]
            for backend in backends:
                b = r["backends"][backend]
                cells.append(_fmt_cell(b["lanes"].get("vector")))
                if backend != "numpy":
                    ov = b["overlap10"]
                    cells.append(f"{ov:.2f}" if ov is not None else "—")
            rss = [r["backends"][b]["rss_mb"] for b in backends]
            cells.append(
                " / ".join(f"{v:.0f}" if v is not None else "—" for v in rss)
            )
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
    ap.add_argument(
        "--vec-backend", type=str, default="numpy",
        help="comma-separated vector backends to measure per size: "
             "numpy,sqlite-vec,binary (binary = sqlite-vec + EXOMEM_VEC_QUANT=binary). "
             "Only meaningful with --embeddings; numpy runs first as the overlap reference.",
    )
    ap.add_argument(
        "--lexical-backend", type=str, default="fts5",
        help="comma-separated lexical backends to measure per size: fts5,python "
             "(python = the in-process rank-bm25 + substring scan, the FTS5 "
             "change's before-side). Works model-free and with --embeddings.",
    )
    ap.add_argument(
        "--corpus-cache", type=str, default=None,
        help="directory to persist generated vaults+sidecars per (size, links, seed) and "
             "reuse across runs — embed once, measure many (the 100k-tier contract). "
             "Omit for throwaway temp corpora.",
    )
    args = ap.parse_args()

    vec_backends = [b.strip() for b in args.vec_backend.split(",") if b.strip()]
    bad = [b for b in vec_backends if b not in VEC_BACKENDS]
    if bad:
        print(f"unknown --vec-backend value(s): {bad} (choose from {VEC_BACKENDS})",
              file=sys.stderr)
        return 1
    lexical_backends = [b.strip() for b in args.lexical_backend.split(",") if b.strip()]
    bad = [b for b in lexical_backends if b not in LEX_BACKENDS]
    if bad:
        print(f"unknown --lexical-backend value(s): {bad} (choose from {LEX_BACKENDS})",
              file=sys.stderr)
        return 1
    if not args.embeddings and vec_backends != ["numpy"]:
        print("--vec-backend requires --embeddings (the vector lane is off without it)",
              file=sys.stderr)
        return 1
    corpus_cache = Path(args.corpus_cache) if args.corpus_cache else None

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
        f"embeddings={'on' if args.embeddings else 'off'} rerank={'on' if args.rerank else 'off'} "
        f"vec-backends={vec_backends if args.embeddings else 'n/a'} "
        f"lexical-backends={lexical_backends} "
        f"corpus-cache={corpus_cache or 'off'}",
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
            vec_backends=vec_backends,
            lexical_backends=lexical_backends,
            corpus_cache=corpus_cache,
        )
        results.append(r)
        chunk_note = f" chunks={r['chunks']}" if args.embeddings else ""
        first = next(iter(r["backends"].values()))
        print(
            f"  n={n:>6}{chunk_note}  graph={_fmt_cell(first['lanes'].get('graph'))}  "
            f"total={_fmt_cell(first['total'])}  ({time.perf_counter() - t0:.1f}s)",
            file=sys.stderr,
        )

    print()
    print(render_markdown(results, embeddings_on=args.embeddings, rerank=args.rerank or args.embeddings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
