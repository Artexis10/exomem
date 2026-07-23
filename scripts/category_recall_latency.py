"""Reproducible, privacy-safe exact-category recall latency gate.

Implements the workstation release-evidence harness from OpenSpec change
`restore-indexed-category-recall`, spec `find-recall-efficiency` (Requirement:
"Reproducible Structured Category Latency Gate") and design.md decision 7
("Performance evidence is reproducible and private"). This is a FOCUSED
category-recall harness — it does not overload `scripts/latency_curve.py`
(the broad per-lane corpus-size curve), which measures something different
(hybrid ranking lanes vs. corpus size, not indexed exact-category eligibility).

What it measures, in a live service process against an already-warm semantic
catalog and OS file cache:

  - Exactly four lanes: page cold, page hot, unit cold, unit hot.
  - 30 samples per lane (nearest-rank percentiles; dependency-free).
  - A fixed request shape per sample: empty query, `scope="kb-only"`,
    `mode="keyword"`, one exact `categories=[...]` filter, graph/rerank/pack
    disabled, and `result_level` "page" or "unit".
  - A cold sample resets ONLY the in-process parsed-page cache and hot
    find/result cache before the call (see `reset_cold_sample_caches` below);
    a hot sample repeats the unchanged request with that cache left live.
  - Connector RTT and startup/catalog construction are excluded: the harness
    runs in-process against `exomem.commands.op_find`, and a bounded
    catalog-warm-up retry (see `wait_for_catalog_ready`) happens BEFORE any
    lane is timed.

Gates (workstation release evidence — NOT a shared-runner CI gate; see
spec's "Structural Scaling Is The CI Gate" requirement, which keeps CI on
operation-count tests instead):
  - cold `filter_eligibility` p95 < 100 ms
  - cold total p95 < 250 ms
  - hot total p95 < 10 ms

Output is deliberately narrow — anonymous run ID, corpus-size bucket
(rounded to 500), candidate-count bucket, sample count, cache policy,
percentile method, latency distributions, and pass/fail — per the spec's
"Real-Vault Reports Are Aggregate And Anonymized" requirement. The category
value, query text, vault path, page paths, project names, hit content, and
exact corpus/candidate counts are read only in-memory to run the request and
are NEVER written to the report.

The harness requires `find.reset_page_and_result_caches()` plus page/unit
`filter_eligibility` timing and result caching. These are part of the retrieval
contract: a missing timing stage is a harness error, never tolerated as
"not_measured" release evidence.

Usage:
    uv run python scripts/category_recall_latency.py --vault /path/to/vault \\
        --category some-real-category-name
    uv run python scripts/category_recall_latency.py --vault /path/to/vault \\
        --category some-real-category-name --out report.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exomem import cli_ops  # noqa: E402
from exomem import commands as commands_module  # noqa: E402
from exomem import find as find_module  # noqa: E402
from exomem import freshness as freshness_module  # noqa: E402
from exomem.vault import walk_vault_md  # noqa: E402

SAMPLE_COUNT = 30
CORPUS_BUCKET_SIZE = 500
CANDIDATE_BUCKET_SIZE = 5
REQUIRED_CANDIDATE_COUNT = 2
PREFLIGHT_LIMIT = 10

GATE_COLD_FILTER_ELIGIBILITY_MS = 100.0
GATE_COLD_TOTAL_MS = 250.0
GATE_HOT_TOTAL_MS = 10.0

RESULT_LEVELS = ("page", "unit")


# --------------------------------------------------------------------------
# Pure math helpers — no I/O, directly unit-testable.
# --------------------------------------------------------------------------


def nearest_rank_percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (dependency-free)."""
    if not sorted_values:
        return 0.0
    rank = math.ceil(pct / 100.0 * len(sorted_values))
    idx = min(max(rank, 1), len(sorted_values)) - 1
    return sorted_values[idx]


def distribution(values: list[float]) -> dict[str, Any] | None:
    """A `{n, min, p50, p90, p95, max}` latency distribution, or None if empty."""
    if not values:
        return None
    sv = sorted(values)
    return {
        "n": len(sv),
        "min": sv[0],
        "p50": nearest_rank_percentile(sv, 50),
        "p90": nearest_rank_percentile(sv, 90),
        "p95": nearest_rank_percentile(sv, 95),
        "max": sv[-1],
    }


def round_to_bucket(value: int, size: int) -> int:
    """Round `value` to the nearest multiple of `size` (banker's-free, half-up)."""
    if size <= 0:
        return value
    return int(round(value / size)) * size


def bucket_candidate_count(count: int, size: int = CANDIDATE_BUCKET_SIZE) -> int:
    """Bucket a small candidate count into a ceiling band (e.g. 2 -> 5), so the
    exact count never appears in output even though preflight requires it be 2."""
    if count <= 0:
        return size
    return ((count - 1) // size + 1) * size


# --------------------------------------------------------------------------
# The narrow find.py cache-reset dependency.
# --------------------------------------------------------------------------


def reset_cold_sample_caches(module: Any = find_module) -> dict[str, int]:
    """Clear ONLY the in-process parsed-page cache and hot find/result cache
    before a cold sample, preserving the resolver cache, freshness registry,
    semantic catalog, and OS file cache.

    Uses `find.reset_page_and_result_caches()`. It must NOT be `unload_ram_caches()`
    (also evicts the resolver cache) or `clear_cache()` (also clears freshness) —
    both are broader than the design's cold-reset contract and would make a
    "cold" sample pay for work the real production request never repeats.

    Raises RuntimeError naming the exact missing seam until it lands.
    """
    reset = getattr(module, "reset_page_and_result_caches", None)
    if reset is None:
        raise RuntimeError(
            "find.reset_page_and_result_caches() is missing. The category recall "
            "latency harness needs a narrow reset seam in src/exomem/find.py that "
            "clears ONLY the parsed-page cache (_CACHE) and the hot find/result "
            "cache (_FIND_CACHE), preserving the resolver cache (_RESOLVER_CACHE), "
            "the freshness registry, the semantic catalog, and the OS file cache. "
            "See scripts/category_recall_latency.py:reset_cold_sample_caches."
        )
    return reset()


# --------------------------------------------------------------------------
# Request shape.
# --------------------------------------------------------------------------


def build_request(category: str, result_level: str, *, limit: int, include_timings: bool) -> dict[str, Any]:
    """The one fixed request shape every sample/preflight uses, per spec:
    empty query, kb-only scope, keyword mode, one exact category, graph/
    rerank/pack disabled."""
    if result_level not in RESULT_LEVELS:
        raise ValueError(f"result_level must be one of {RESULT_LEVELS}, got {result_level!r}")
    return {
        "query": "",
        "categories": [category],
        "result_level": result_level,
        "scope": "kb-only",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "pack": False,
        "graph_enrich": False,
        "limit": limit,
        "include_timings": include_timings,
    }


# --------------------------------------------------------------------------
# Preflight: current semantic catalog, exactly two indexed candidates.
# --------------------------------------------------------------------------


def preflight_once(
    op_find: Callable[..., Any],
    vault_root: Path,
    category: str,
    *,
    limit: int = PREFLIGHT_LIMIT,
) -> dict[str, Any]:
    """One (untimed) page-level lookup: candidate PAGE eligibility is shared
    between the page and unit lanes (both consume the same structured-filter
    candidate set), so a single page-level check preflights both.

    Returns `{"catalog_status": "ready"|<OpError code>, "candidate_count": int|None}`.
    Never raises — a not-ready catalog is a reportable status, not a crash.
    """
    request = build_request(category, "page", limit=limit, include_timings=False)
    try:
        hits = op_find(vault_root, **request)
    except cli_ops.OpError as exc:
        return {"catalog_status": exc.code, "candidate_count": None}
    return {"catalog_status": "ready", "candidate_count": len(hits)}


def wait_for_catalog_ready(
    op_find: Callable[..., Any],
    vault_root: Path,
    category: str,
    *,
    max_wait_s: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    limit: int = PREFLIGHT_LIMIT,
) -> dict[str, Any]:
    """Bounded retry so a warming catalog doesn't fail the gate outright — this
    setup cost is explicitly excluded from every timed lane. Returns the last
    `preflight_once` result (ready or not) once `max_wait_s` elapses."""
    deadline = time.monotonic() + max_wait_s
    result = preflight_once(op_find, vault_root, category, limit=limit)
    while result["catalog_status"] != "ready" and time.monotonic() < deadline:
        sleep(0.25)
        result = preflight_once(op_find, vault_root, category, limit=limit)
    return result


def check_cardinality(candidate_count: int | None, *, expected: int = REQUIRED_CANDIDATE_COUNT) -> bool:
    """`True` only when the catalog reports exactly `expected` candidates."""
    return candidate_count == expected


# --------------------------------------------------------------------------
# Lane sampling.
# --------------------------------------------------------------------------


def run_lane(
    op_find: Callable[..., Any],
    vault_root: Path,
    category: str,
    result_level: str,
    *,
    cold: bool,
    samples: int = SAMPLE_COUNT,
    limit: int = PREFLIGHT_LIMIT,
    reset_cache: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run `samples` requests of one fixed shape. Cold samples reset the
    page/result caches immediately before each call; hot samples never reset
    and repeat the identical request against a live cache."""
    if cold and reset_cache is None:
        reset_cache = reset_cold_sample_caches
    request = build_request(category, result_level, limit=limit, include_timings=True)

    total_ms: list[float] = []
    filter_eligibility_ms: list[float] = []
    for _ in range(samples):
        if cold:
            reset_cache()
        envelope = op_find(vault_root, **request)
        timings = envelope["timings"]
        total_ms.append(timings["total_ms"])
        stage = timings.get("stages", {}).get("filter_eligibility")
        if not stage or "ms" not in stage:
            lane = "cold" if cold else "hot"
            raise RuntimeError(
                f"{result_level}_{lane} did not report filter_eligibility.ms"
            )
        filter_eligibility_ms.append(stage["ms"])

    return {
        "cache_reset": "before_each_sample" if cold else "none",
        "sample_count": len(total_ms),
        "total_ms": distribution(total_ms),
        "filter_eligibility_ms": distribution(filter_eligibility_ms),
    }


# --------------------------------------------------------------------------
# Gates.
# --------------------------------------------------------------------------


def evaluate_gates(lanes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Apply the three release-evidence gates to each result level's cold/hot
    lanes. Missing distributions fail closed; `run_lane` normally rejects the
    missing timing stage before reports reach this function."""
    gates: dict[str, Any] = {}
    booleans: list[bool] = []
    for level in RESULT_LEVELS:
        cold = lanes[f"{level}_cold"]
        hot = lanes[f"{level}_hot"]

        elig = cold["filter_eligibility_ms"]
        key = f"{level}_cold_filter_eligibility_p95_lt_{int(GATE_COLD_FILTER_ELIGIBILITY_MS)}ms"
        if elig is None:
            gates[key] = False
            booleans.append(False)
        else:
            gates[key] = elig["p95"] < GATE_COLD_FILTER_ELIGIBILITY_MS
            booleans.append(gates[key])

        cold_total = cold["total_ms"]
        key = f"{level}_cold_total_p95_lt_{int(GATE_COLD_TOTAL_MS)}ms"
        if cold_total is None:
            gates[key] = False
            booleans.append(False)
        else:
            gates[key] = cold_total["p95"] < GATE_COLD_TOTAL_MS
            booleans.append(gates[key])

        hot_total = hot["total_ms"]
        key = f"{level}_hot_total_p95_lt_{int(GATE_HOT_TOTAL_MS)}ms"
        if hot_total is None:
            gates[key] = False
            booleans.append(False)
        else:
            gates[key] = hot_total["p95"] < GATE_HOT_TOTAL_MS
            booleans.append(gates[key])

    gates["pass"] = bool(booleans) and all(booleans)
    return gates


# --------------------------------------------------------------------------
# Corpus-size bucket (metadata only — never the exact count).
# --------------------------------------------------------------------------


def count_corpus_pages(vault_root: Path, *, walk: Callable[[Path], Any] = walk_vault_md) -> int:
    """Count Markdown pages in the walked scope (kb-only -> `Knowledge Base/`
    when present, else the whole vault). Excluded from every timed lane —
    used only to derive the anonymized corpus-size bucket."""
    kb = vault_root / "Knowledge Base"
    root = kb if kb.is_dir() else vault_root
    return sum(1 for _ in walk(root))


# --------------------------------------------------------------------------
# Orchestration.
# --------------------------------------------------------------------------


def run_harness(
    vault_root: Path,
    category: str,
    *,
    op_find: Callable[..., Any] = commands_module.op_find,
    reset_cache: Callable[[], Any] = reset_cold_sample_caches,
    samples: int = SAMPLE_COUNT,
    limit: int = PREFLIGHT_LIMIT,
    warmup_timeout_s: float = 30.0,
    count_pages: Callable[[Path], int] = count_corpus_pages,
    prepare_freshness: Callable[[Path], Any] = freshness_module.rebaseline,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the full four-lane harness and return the anonymized report.

    `category`, `query`, `vault_root`, page paths, and hit content are used
    only in-memory to build requests; none of them appear in the return value.
    """
    report: dict[str, Any] = {
        "run_id": run_id or uuid.uuid4().hex,
        "corpus_size_bucket": round_to_bucket(count_pages(vault_root), CORPUS_BUCKET_SIZE),
        "sample_count": samples,
        "percentile_method": "nearest_rank",
        "cache_policy": {
            "cold": "reset_page_and_result_caches before each sample "
                    "(preserves resolver/freshness/catalog/OS cache)",
            "hot": "unchanged request repeated against a live result cache",
        },
    }

    # A live service seeds this registry when its watcher starts. The harness
    # runs in-process to exclude connector RTT, so reproduce that one-time setup
    # explicitly before any timed call; otherwise every cache-key calculation
    # walks the corpus and the supposed hot lane measures watcher absence.
    prepare_freshness(vault_root)

    preflight = wait_for_catalog_ready(
        op_find, vault_root, category, max_wait_s=warmup_timeout_s, limit=limit
    )
    candidate_count = preflight["candidate_count"]
    cardinality_ok = check_cardinality(candidate_count)
    report["preflight"] = {
        "catalog_status": preflight["catalog_status"],
        "candidate_count_bucket": (
            bucket_candidate_count(candidate_count) if candidate_count is not None else None
        ),
        "cardinality_ok": cardinality_ok,
    }

    if preflight["catalog_status"] != "ready" or not cardinality_ok:
        report["lanes"] = {}
        report["gates"] = {"pass": False, "reason": "preflight_failed"}
        return report

    lanes: dict[str, dict[str, Any]] = {}
    for level in RESULT_LEVELS:
        lanes[f"{level}_cold"] = run_lane(
            op_find, vault_root, category, level,
            cold=True, samples=samples, limit=limit, reset_cache=reset_cache,
        )
        lanes[f"{level}_hot"] = run_lane(
            op_find, vault_root, category, level,
            cold=False, samples=samples, limit=limit,
        )
    report["lanes"] = lanes
    report["gates"] = evaluate_gates(lanes)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", type=Path, required=True, help="real vault root (never written to the report)")
    ap.add_argument("--category", type=str, required=True, help="exact category to filter on (never written to the report)")
    ap.add_argument("--samples", type=int, default=SAMPLE_COUNT, help=f"samples per lane (default {SAMPLE_COUNT})")
    ap.add_argument("--limit", type=int, default=PREFLIGHT_LIMIT, help=f"result limit per request (default {PREFLIGHT_LIMIT})")
    ap.add_argument("--warmup-timeout", type=float, default=30.0, help="max seconds to wait for a warming catalog before the gate fails (default 30)")
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here instead of stdout")
    args = ap.parse_args(argv)

    report = run_harness(
        args.vault,
        args.category,
        samples=args.samples,
        limit=args.limit,
        warmup_timeout_s=args.warmup_timeout,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if report["gates"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
