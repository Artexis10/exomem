"""Filtered semantic-recall latency at vault scale: slice the matrix vs mask the scores.

Issue #951. `find_candidates` always passes `allowed_paths`, so every semantic
recall took `EmbeddingIndex.search`'s scan branch, which ran a Python membership
loop over every chunk row and then fancy-indexed `matrix[keep]` — a fresh copy of
most of a ~200 MB float32 matrix, per query. This harness times that pre-fix
implementation and the shipped one against the SAME synthetic sidecar, warm cache
in both cases, so the difference reported is the change and not a cold start.

Synthetic only: it builds its own sidecar under a scratch root and never reads a
real vault. Do not point this at a live cell — an out-of-process reader against a
running cell was measured to leave its catalog stale and grow its deferred queue.

    uv run python scripts/recall_mask_latency.py
    uv run python scripts/recall_mask_latency.py --chunks 67000 --files 6074 --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratch_root  # noqa: E402

from exomem import embeddings  # noqa: E402

# The measured cell in #951: a 6,074-file vault whose `.embeddings.sqlite` was
# 310 MB, which at 768-dim float32 is ~67,000 chunk rows and a ~200 MB matrix.
DEFAULT_CHUNKS = 67_000
DEFAULT_FILES = 6_074
# `find_candidates` asks for `candidate_k * 3`, and candidate_k is floored at the
# eligible-path count, so recall's k is large — not a top-10.
DEFAULT_K = 300
DEFAULT_QUERIES = 30
# Eligible fractions worth separating: recall normally admits nearly the whole
# vault, but a scoped or checkpoint-bound request can narrow it hard, and that is
# the regime where scoring the full matrix does the most extra arithmetic.
DEFAULT_FRACTIONS = (1.0, 0.85, 0.5, 0.05)


def _search_pre_951(index, query_vec, k, *, allowed_paths):
    """The pre-fix `EmbeddingIndex.search` scan, transcribed verbatim.

    Provenance: `git show 9bf3d804:src/exomem/embedding_index.py`. Text hydration
    is left out of BOTH lanes here — it is identical point-lookup SQL either side
    of the change, and including it would bury the difference under sqlite.
    """
    metadata, matrix = index.all_vectors()
    if not metadata:
        return []
    keep = [i for i, (path, _chunk) in enumerate(metadata) if path in allowed_paths]
    if not keep:
        return []
    metadata = [metadata[i] for i in keep]
    matrix = matrix[keep]
    scores = matrix @ query_vec.astype(np.float32, copy=False)
    k_eff = min(k, len(scores))
    if k_eff <= 0:
        return []
    top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
    top_idx = top_idx[np.argsort(-scores[top_idx])]
    return [(metadata[i][0], metadata[i][1], float(scores[i])) for i in top_idx]


def _search_masked(index, query_vec, k, *, allowed_paths):
    """The shipped scan: score the full matrix once, mask the scores."""
    metadata, matrix = index.all_vectors()
    if not metadata:
        return []
    mask, eligible = index._eligibility_mask(metadata, allowed_paths)
    if not eligible:
        return []
    k_eff = min(k, eligible)
    if k_eff <= 0:
        return []
    scores = matrix @ query_vec.astype(np.float32, copy=False)
    scores = np.where(mask, scores, -np.inf)
    top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
    top_idx = top_idx[np.argsort(-scores[top_idx])]
    return [(metadata[i][0], metadata[i][1], float(scores[i])) for i in top_idx]


def _breakdown(index, queries, allowed) -> dict:
    """Split the pre-fix scan into its parts, and price one cold matrix reload.

    #951 attributes ~776 ms of a semantic recall to the keep-loop and the
    `matrix[keep]` copy. This prices each of them directly so the attribution can
    be checked rather than inferred from the lane total, and prices the full
    reload beside them because that is the other corpus-linear cost on this path
    and the one a cold or churning cache pays instead.
    """
    metadata, matrix = index.all_vectors()
    loop_ms, copy_ms, matmul_ms, mask_cold_ms, mask_warm_ms = [], [], [], [], []
    for query in queries:
        started = time.perf_counter()
        keep = [i for i, (path, _c) in enumerate(metadata) if path in allowed]
        loop_ms.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        sliced = matrix[keep]
        copy_ms.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        _ = matrix @ query.astype(np.float32, copy=False)
        matmul_ms.append((time.perf_counter() - started) * 1000.0)

        index._mask_cache = None
        started = time.perf_counter()
        index._eligibility_mask(metadata, allowed)
        mask_cold_ms.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        index._eligibility_mask(metadata, allowed)
        mask_warm_ms.append((time.perf_counter() - started) * 1000.0)
        del sliced

    started = time.perf_counter()
    index._load_all_rows()
    full_reload_ms = (time.perf_counter() - started) * 1000.0
    return {
        "pre_951_keep_loop": _stats(loop_ms),
        "pre_951_matrix_copy": _stats(copy_ms),
        "full_matrix_matmul": _stats(matmul_ms),
        "mask_build_cold": _stats(mask_cold_ms),
        "mask_lookup_warm": _stats(mask_warm_ms),
        "cold_full_matrix_reload_ms": round(full_reload_ms, 1),
    }


def _build_index(vault: Path, chunks: int, files: int, seed: int):
    """A synthetic sidecar of `chunks` normalized rows spread over `files`."""
    rng = np.random.default_rng(seed)
    index = embeddings.EmbeddingIndex(vault)
    paths = [f"Knowledge Base/Notes/note-{n:05d}.md" for n in range(files)]
    per_file = [chunks // files] * files
    for n in range(chunks - sum(per_file)):
        per_file[n] += 1
    started = time.perf_counter()
    for path, count in zip(paths, per_file, strict=True):
        if not count:
            continue
        block = rng.standard_normal((count, embeddings.VECTOR_DIM), dtype=np.float32)
        block /= np.linalg.norm(block, axis=1, keepdims=True)
        index.upsert_file(path, [f"{path}#{c}" for c in range(count)], block, 1.0)
    build_seconds = time.perf_counter() - started
    return index, paths, build_seconds


def _time_lane(fn, index, queries, k, allowed) -> tuple[list[float], list]:
    samples: list[float] = []
    last = None
    for query in queries:
        started = time.perf_counter()
        last = fn(index, query, k, allowed_paths=allowed)
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples, last


def _stats(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered), 2),
        "min_ms": round(ordered[0], 2),
        "max_ms": round(ordered[-1], 2),
        "mean_ms": round(statistics.fmean(ordered), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=int, default=DEFAULT_CHUNKS)
    parser.add_argument("--files", type=int, default=DEFAULT_FILES)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--queries", type=int, default=DEFAULT_QUERIES)
    parser.add_argument("--seed", type=int, default=951)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--breakdown",
        action="store_true",
        help="also price the pre-fix scan's parts and one cold matrix reload",
    )
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed + 1)
    report: dict = {
        "chunks": args.chunks,
        "files": args.files,
        "k": args.k,
        "queries": args.queries,
        "lanes": [],
    }

    with scratch_root.scratch_root("exomem-recall-mask-") as base:
        vault = base / "vault"
        (vault / "Knowledge Base").mkdir(parents=True)
        index, paths, build_seconds = _build_index(vault, args.chunks, args.files, args.seed)
        report["build_seconds"] = round(build_seconds, 1)

        metadata, matrix = index.all_vectors()  # warm the cache for BOTH lanes
        report["rows"] = len(metadata)
        report["matrix_mb"] = round(matrix.nbytes / 1e6, 1)
        if not args.json:
            print(
                f"synthetic sidecar: {len(metadata)} rows x {embeddings.VECTOR_DIM} dims "
                f"= {matrix.nbytes / 1e6:.1f} MB matrix over {args.files} files "
                f"(built in {build_seconds:.1f}s)"
            )
            print(f"k={args.k}, {args.queries} queries per lane, warm matrix cache\n")
            print(f"{'eligible':>9}  {'pre-#951 p50':>13}  {'masked p50':>11}  {'speedup':>8}  agree")

        queries = []
        for _ in range(args.queries):
            q = rng.standard_normal(embeddings.VECTOR_DIM).astype(np.float32)
            queries.append(q / np.linalg.norm(q))

        for fraction in DEFAULT_FRACTIONS:
            take = max(1, int(len(paths) * fraction))
            allowed = set(paths[:take])
            index._mask_cache = None

            old_samples, old_hits = _time_lane(_search_pre_951, index, queries, args.k, allowed)
            new_samples, new_hits = _time_lane(_search_masked, index, queries, args.k, allowed)

            same_rows = [(p, c) for p, c, _s in old_hits] == [(p, c) for p, c, _s in new_hits]
            deltas = [
                abs(a - b) for (*_x, a), (*_y, b) in zip(old_hits, new_hits, strict=True)
            ]
            old_stats, new_stats = _stats(old_samples), _stats(new_samples)
            lane = {
                "eligible_fraction": fraction,
                "eligible_files": take,
                "pre_951": old_stats,
                "masked": new_stats,
                "speedup": round(old_stats["p50_ms"] / max(new_stats["p50_ms"], 1e-9), 2),
                "identical_rows": same_rows,
                "max_score_delta": max(deltas) if deltas else 0.0,
            }
            report["lanes"].append(lane)
            if not args.json:
                print(
                    f"{fraction * 100:>8.0f}%  {old_stats['p50_ms']:>10.2f} ms  "
                    f"{new_stats['p50_ms']:>8.2f} ms  {lane['speedup']:>7.2f}x  "
                    f"{'same rows' if same_rows else 'DIVERGED'} "
                    f"(max |dscore| {lane['max_score_delta']:.2e})"
                )

        if args.breakdown:
            index._mask_cache = None
            report["breakdown"] = _breakdown(index, queries[:10], set(paths))
            if not args.json:
                print("\nwhere the pre-#951 scan's time went (100% eligible):")
                for name, value in report["breakdown"].items():
                    if isinstance(value, dict):
                        print(f"  {name:<22} p50 {value['p50_ms']:>8.2f} ms")
                    else:
                        print(f"  {name:<22}     {value:>8.1f} ms")

    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
