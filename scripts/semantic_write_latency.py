"""Semantic write and read-after-write visibility gate at realistic scale."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratch_root  # noqa: E402
from synth_vault import gen_dense_vault  # noqa: E402

from exomem import (  # noqa: E402
    find,
    freshness,
    graph_sync,
    lexstore,
    semantic_contract,
    semantic_writes,
)
from exomem.vault import walk_vault_md  # noqa: E402

DEFAULT_SIZES = (2_000, 8_000)
# FTS5 stays report-only.  Measured 2026-08-20 on the same contended Windows
# host as the Python baseline: the targeted retry itself landed in 32ms @ 2k
# and 250ms @ 8k, but the subsequent verified keyword read took 35.6s @ 2k and
# 164.4s @ 8k (~4.6x at 4x pages).  A ceiling with honest runner headroom over
# that maximum would be too broad to be useful and would gate corpus-sync noise,
# not a stable visibility SLO.  CI still prints this metric on every run; the
# deterministic regression test separately enforces correctness on every read.
VALIDATE_MEDIAN_MS = 500.0
VALIDATE_P95_MS = 1_000.0
COMMIT_MEDIAN_MS = 750.0
COMMIT_P95_MS = 1_500.0
SCALING_RATIO = 2.0
SCALING_SLACK_MS = 200.0
# The scaling bound is derived from the SMALL corpus measurement, and on this
# shared runner that measurement is the noisiest number the gate produces.
# Across twelve consecutive CI runs the 2k commit median ranged 110.1-519.0ms
# (median 129.8) while the 8k median it is supposed to bound stayed inside
# 265.7-454.7ms (median 331.1). So the thing being measured is roughly stable
# and the ruler is not: `small * 2 + 200` put four of those twelve runs within
# 100ms of failing and one at -3.9ms, which is how a green tree gets a red
# required gate. Sampling more is not available here -- a `--root` run gets one
# attempt per size.
#
# Floor the bound at a fraction of the operation's own absolute ceiling so a
# lucky-fast small sample cannot make it tighter than an unregressed large
# sample can satisfy. At 0.8 the commit floor is 600ms (32% clear of the worst
# 8k median observed) and validate's is 400ms (58% clear of its worst, 253.2ms)
# -- while both stay strictly below their ceilings, so the scaling check still
# fails a real super-linear regression before the absolute ceiling does. Only
# the two operations expected to stay FLAT with corpus size get this; the
# read/cold rows are deliberately O(N) and carry their own ratios.
SCALING_BOUND_CEILING_FRACTION = 0.8

# --- Read-after-write budget (warm path). A governed write normally PATCHES
# the corpus-context cache and the live freshness registry, so the very next
# caller's read stays cheap. These ceilings bound THAT read, not just the
# preflight+commit that preceded it -- the gap this gate used to leave open
# (a change moving cost from inside commit onto the next caller's read read
# as a pure improvement).
#
# MEASURED BASIS (contended Windows dev box, EXOMEM_LEXICAL_BACKEND=python so
# the read runs the reference O(N) scan every time, not just cold):
#   read_after_write_median_ms   2k ~6193ms   8k ~30601ms   (~4.9x @ 4x pages)
# Linux CI has neither the per-file Windows short-name resolve() this scan
# pays (`recall_policy._safe_regular_file`) nor this box's contention, so the
# real gate floor is expected to sit well under these numbers -- headroom is
# wide on purpose. Re-measure (don't hand-tune) if that basis changes; retune
# alongside the cold rows below once #510 lands and the relocated cost has a
# real shape to calibrate against.
READ_AFTER_WRITE_MEDIAN_MS = 60_000.0
READ_AFTER_WRITE_P95_MS = 75_000.0
# Separate (not shared with SCALING_RATIO/SCALING_SLACK_MS): this read is
# deliberately O(N) even when warm (see EXOMEM_LEXICAL_BACKEND above), so its
# expected growth over a 4x corpus (the DEFAULT_SIZES jump) is ~4x, not flat
# like validate/commit's cached steady state. Ratio/slack sit above the
# measured ~4.9x with real headroom so ordinary linear scaling never trips
# this; a super-linear regression still would.
READ_AFTER_WRITE_SCALING_RATIO = 8.0
READ_AFTER_WRITE_SCALING_SLACK_MS = 2_000.0

# --- Cold/evicted scenario (the row the upcoming #510 fix must move a cost
# onto). `reset_corpus_context_cache()` drops the semantic-contract
# corpus-context cache; `freshness.clear()` drops freshness liveness (only
# the file watcher re-seeds it in production -- see FreshnessSnapshot's O(N)
# stat-walk fallback in find.py). Eviction runs BETWEEN an ordinary warm
# write and the two probes that follow it, not before the write: preflight
# calls build_corpus_context() itself, so evicting before the write just lets
# that SAME write's own preflight quietly repopulate the cache before
# anything is timed -- the relocated cost would never land on any gated row.
# One eviction, two rows:
#   cold_read_after_write_ms -- the very next read's honest reader-side cold
#     cost (freshness-liveness fallback etc.), visibility-asserted the same
#     as the warm row.
#   cold_preflight_ms -- the very next transition's validate_ms. Preflight is
#     where a cold corpus-context rebuild lands BY CONSTRUCTION
#     (build_corpus_context has no cache entry to patch), so this is the
#     honest incident-class net: the ~70s relocated walk from the incident
#     would breach THIS row, not the read.
#
# MEASURED BASIS (contended Windows dev box, same run as the warm figures,
# post-reorder so eviction genuinely lands after the write and before both
# probes -- see the restructured `measure()` cold block):
#   cold_read_after_write_ms   2k ~9861ms   8k ~36734ms   (~3.7x @ 4x pages)
#   cold_preflight_ms          2k ~6462ms   8k ~23749ms   (~3.7x @ 4x pages)
# PROVISIONAL for both: calibrated generously from this lane's own measured
# 2k/8k numbers so they are a regression net against materially WORSE
# behavior, not a tight bound on today's known-bad cost -- re-measure after
# #510 lands and tighten (Linux CI, not this contended Windows box, is the
# authoritative floor). Both rows are n=1 per size: a `--root` run refuses a
# pre-existing directory so it gets exactly one attempt with no re-measure,
# so a contended box can make either row noisy in isolation -- expected, and
# Linux CI adjudicates a reproducible failure.
COLD_READ_AFTER_WRITE_MS = 100_000.0
COLD_READ_AFTER_WRITE_SCALING_RATIO = 8.0
COLD_READ_AFTER_WRITE_SCALING_SLACK_MS = 3_000.0
COLD_PREFLIGHT_MS = 80_000.0
COLD_PREFLIGHT_SCALING_RATIO = 8.0
COLD_PREFLIGHT_SCALING_SLACK_MS = 5_000.0


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999) - 1))
    return ordered[index]


def _seed_freshness(vault_root: Path) -> None:
    freshness.seed(
        vault_root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(vault_root)),
    )
    kb = vault_root / "Knowledge Base"
    freshness.seed(
        vault_root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find._walk_md(kb)),
    )


def _next_source(source: str, version: int) -> str:
    marker = "Synthetic write latency version "
    start = source.index(marker) + len(marker)
    end = source.index(" ", start)
    return source[:start] + str(version) + source[end:]


def _read_after_write(vault_root: Path, rel_path: str, version: int) -> float:
    """Timed, visibility-asserted keyword read for a version already committed.

    What the NEXT caller pays to see a write, not just what the writer itself
    paid. A fast-but-stale read must fail the gate, not pass it, so this
    asserts the new content is actually visible before returning its cost.
    """
    marker = f"Synthetic write latency version {version} "
    started = time.perf_counter()
    hits = find.find(vault_root, query=marker.strip(), scope="kb", mode="keyword", limit=5)
    read_ms = (time.perf_counter() - started) * 1_000.0
    if not any(marker in (hit.excerpt or "") for hit in hits):
        excerpts = [hit.excerpt for hit in hits]
        raise RuntimeError(
            f"read-after-write visibility failed: version {version} marker not found "
            f"in {len(hits)} hit(s) for {rel_path}: {excerpts!r}"
        )
    return read_ms


@contextmanager
def _lexical_backend(name: str):
    """Scope a benchmark phase to one backend without leaking cache entries."""
    previous = os.environ.get("EXOMEM_LEXICAL_BACKEND")
    os.environ["EXOMEM_LEXICAL_BACKEND"] = name
    find.clear_cache()
    try:
        yield
    finally:
        find.clear_cache()
        if previous is None:
            os.environ.pop("EXOMEM_LEXICAL_BACKEND", None)
        else:
            os.environ["EXOMEM_LEXICAL_BACKEND"] = previous


def _wait_for_lexical_repair_idle(vault_root: Path, timeout: float = 60.0) -> None:
    """Wait for the repair-flight condition; timeout is only a deadlock valve."""
    key = vault_root.resolve()
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while True:
        with lexstore._REPAIRS_LOCK:
            if key not in lexstore._REPAIRS_IN_FLIGHT:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("lexical repair worker did not become idle")
        wake.wait(min(0.01, remaining))


def _measure_fts5_visibility(
    vault_root: Path,
    rel_path: str,
    *,
    current_version: int,
) -> float:
    """Measure committed-to-queryable visibility on the production backend."""
    if not lexstore.fts5_available():
        raise RuntimeError("fts5 visibility measurement requires SQLite FTS5 support")
    with _lexical_backend("fts5"):
        # Build and warm the real catalog before samples.  Every measured write
        # then takes the production deferred-upsert path: the governed writer
        # holds the vault lock and the inline upsert declines.  The deterministic
        # test covers an immediate reader racing that worker; this metric measures
        # the visibility window itself by waiting on the real repair-flight
        # condition and then verifying the FTS5 keyword query.
        lexstore.ensure_fresh(vault_root)
        _read_after_write(vault_root, rel_path, current_version)
        version = current_version + 1
        _commit_transition(vault_root, rel_path, version)
        visible_started = time.perf_counter()
        _wait_for_lexical_repair_idle(vault_root)
        _read_after_write(vault_root, rel_path, version)
        return (time.perf_counter() - visible_started) * 1_000.0


def _commit_transition(vault_root: Path, rel_path: str, version: int) -> tuple[float, float]:
    path = vault_root / rel_path
    before = path.read_text(encoding="utf-8")
    after = _next_source(before, version)
    started = time.perf_counter()
    preflight = semantic_writes.preflight_existing(
        vault_root,
        path=rel_path,
        after_source=after,
        operation="observe",
    )
    validate_ms = (time.perf_counter() - started) * 1_000.0
    if preflight.contract_result.should_block:
        codes = [item.code for item in preflight.contract_result.blocking_findings]
        raise RuntimeError(f"synthetic transition was blocked: {codes}")
    started = time.perf_counter()
    semantic_writes.commit_existing(vault_root, preflight=preflight)
    commit_ms = (time.perf_counter() - started) * 1_000.0
    return validate_ms, commit_ms


def _transition(vault_root: Path, rel_path: str, version: int) -> tuple[float, float, float]:
    validate_ms, commit_ms = _commit_transition(vault_root, rel_path, version)
    read_after_write_ms = _read_after_write(vault_root, rel_path, version)
    return validate_ms, commit_ms, read_after_write_ms


def measure(vault_root: Path, size: int, samples: int) -> dict[str, float | int]:
    # Keep the historical deterministic baseline pinned to the Python rung.
    # The focused FTS5 phase below overrides this scope only for its own reads.
    with _lexical_backend("python"):
        return _measure(vault_root, size, samples)


def _measure(
    vault_root: Path, size: int, samples: int
) -> dict[str, float | int]:
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    gen_dense_vault(vault_root, size, links_per_note=3)
    target_rel = "Knowledge Base/Entities/Concepts/write-latency-target.md"
    target = vault_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        "title: Write latency target\n"
        "type: entity\n"
        "status: active\n"
        "updated: 2026-07-22\n"
        "---\n\n"
        "# Write latency target\n\n"
        "## Observations\n\n"
        "- [config] Synthetic write latency version 0 #latency (benchmark) ^latency-gate\n",
        encoding="utf-8",
    )

    cold_started = time.perf_counter()
    semantic_contract.build_corpus_context(vault_root)
    cold_ms = (time.perf_counter() - cold_started) * 1_000.0
    _seed_freshness(vault_root)
    semantic_contract.build_corpus_context(vault_root)

    # Install the activation boundary and warm derived sidecars outside samples.
    _transition(vault_root, target_rel, 1)
    validates: list[float] = []
    commits: list[float] = []
    reads: list[float] = []
    for version in range(2, samples + 2):
        validate_ms, commit_ms, read_ms = _transition(vault_root, target_rel, version)
        validates.append(validate_ms)
        commits.append(commit_ms)
        reads.append(read_ms)

    # Cold/evicted scenario (see the module-level constant comments for the
    # full mechanism). Eviction must land BETWEEN a write and the probes that
    # follow it, not before the write: preflight_existing() itself calls
    # build_corpus_context(), so evicting first just lets that SAME write's
    # own preflight quietly repopulate the cache before anything is timed --
    # the relocated cost would never land on any gated row. One eviction, two
    # samples, neither folded into the warm `reads`/`validates`/`commits`
    # lists above so they cannot skew ceilings scoped to steady-state
    # (cache-warm) operation:
    cold_write_version = samples + 2
    cold_probe_version = samples + 3
    _transition(vault_root, target_rel, cold_write_version)  # ordinary warm write
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    cold_read_after_write_ms = _read_after_write(vault_root, target_rel, cold_write_version)
    cold_preflight_ms, _, _ = _transition(vault_root, target_rel, cold_probe_version)
    fts5_visibility_ms = _measure_fts5_visibility(
        vault_root,
        target_rel,
        current_version=cold_probe_version,
    )

    return {
        "pages": size,
        "samples": samples,
        "cold_ms": round(cold_ms, 1),
        "validate_median_ms": round(statistics.median(validates), 1),
        "validate_p95_ms": round(_percentile(validates, 0.95), 1),
        "commit_median_ms": round(statistics.median(commits), 1),
        "commit_p95_ms": round(_percentile(commits, 0.95), 1),
        "read_after_write_median_ms": round(statistics.median(reads), 1),
        "read_after_write_p95_ms": round(_percentile(reads, 0.95), 1),
        "cold_read_after_write_ms": round(cold_read_after_write_ms, 1),
        "cold_preflight_ms": round(cold_preflight_ms, 1),
        "fts5_visibility_latency_ms": round(fts5_visibility_ms, 1),
    }


def check(results: list[dict[str, float | int]]) -> None:
    failures: list[str] = []
    for result in results:
        pages = int(result["pages"])
        for key, ceiling in (
            ("validate_median_ms", VALIDATE_MEDIAN_MS),
            ("validate_p95_ms", VALIDATE_P95_MS),
            ("commit_median_ms", COMMIT_MEDIAN_MS),
            ("commit_p95_ms", COMMIT_P95_MS),
            ("read_after_write_median_ms", READ_AFTER_WRITE_MEDIAN_MS),
            ("read_after_write_p95_ms", READ_AFTER_WRITE_P95_MS),
            ("cold_read_after_write_ms", COLD_READ_AFTER_WRITE_MS),
            ("cold_preflight_ms", COLD_PREFLIGHT_MS),
        ):
            value = float(result[key])
            if value >= ceiling:
                failures.append(f"{pages} pages: {key}={value:.1f}ms >= {ceiling:.1f}ms")
    if len(results) >= 2:
        ordered = sorted(results, key=lambda item: int(item["pages"]))
        small, large = ordered[0], ordered[-1]
        for operation, operation_ceiling in (
            ("validate", VALIDATE_MEDIAN_MS),
            ("commit", COMMIT_MEDIAN_MS),
        ):
            key = f"{operation}_median_ms"
            bound = max(
                float(small[key]) * SCALING_RATIO + SCALING_SLACK_MS,
                operation_ceiling * SCALING_BOUND_CEILING_FRACTION,
            )
            if float(large[key]) >= bound:
                failures.append(
                    f"{operation} scaling: {large[key]}ms >= {bound:.1f}ms "
                    f"({small['pages']} -> {large['pages']} pages)"
                )
        read_key = "read_after_write_median_ms"
        read_bound = (
            float(small[read_key]) * READ_AFTER_WRITE_SCALING_RATIO
            + READ_AFTER_WRITE_SCALING_SLACK_MS
        )
        if float(large[read_key]) >= read_bound:
            failures.append(
                f"read_after_write scaling: {large[read_key]}ms >= {read_bound:.1f}ms "
                f"({small['pages']} -> {large['pages']} pages)"
            )
        # The relocated-cost trap #510 must close: the cold row growing
        # faster than headroom allows means the "next caller" bill is
        # scaling with corpus size, not staying flat.
        cold_key = "cold_read_after_write_ms"
        cold_bound = (
            float(small[cold_key]) * COLD_READ_AFTER_WRITE_SCALING_RATIO
            + COLD_READ_AFTER_WRITE_SCALING_SLACK_MS
        )
        if float(large[cold_key]) >= cold_bound:
            failures.append(
                f"cold_read_after_write scaling: {large[cold_key]}ms >= {cold_bound:.1f}ms "
                f"({small['pages']} -> {large['pages']} pages)"
            )
        # The true incident-class net: a cold preflight IS where the
        # relocated corpus rebuild lands, so this is the row that would
        # actually catch the ~70s walk moving back in.
        cold_preflight_key = "cold_preflight_ms"
        cold_preflight_bound = (
            float(small[cold_preflight_key]) * COLD_PREFLIGHT_SCALING_RATIO
            + COLD_PREFLIGHT_SCALING_SLACK_MS
        )
        if float(large[cold_preflight_key]) >= cold_preflight_bound:
            failures.append(
                f"cold_preflight scaling: {large[cold_preflight_key]}ms >= "
                f"{cold_preflight_bound:.1f}ms ({small['pages']} -> {large['pages']} pages)"
            )
    if failures:
        raise SystemExit("semantic write latency gate failed: " + "; ".join(failures))


def measure_all(
    sizes: list[int], samples: int, root: Path | None
) -> list[dict[str, float | int]]:
    if root is not None:
        root.mkdir(parents=True, exist_ok=False)
        runtime_temp = root / "runtime-temp"
        runtime_temp.mkdir()
        tempfile.tempdir = str(runtime_temp)
        roots = [root / f"vault-{size}" for size in sizes]
        for vault_root in roots:
            vault_root.mkdir(parents=True)
        try:
            results = []
            for index, (vault_root, size) in enumerate(zip(roots, sizes, strict=True)):
                results.append(measure(vault_root, size, samples))
                if index < len(roots) - 1:
                    _wait_for_lexical_repair_idle(vault_root, timeout=300.0)
            return results
        finally:
            graph_sync.drain_active_rebuilds()
    with scratch_root.scratch_root("exomem-write-latency-") as base:
        results: list[dict[str, float | int]] = []
        try:
            for index, size in enumerate(sizes):
                vault_root = base / f"vault-{size}"
                vault_root.mkdir()
                results.append(measure(vault_root, size, samples))
                if index < len(sizes) - 1:
                    _wait_for_lexical_repair_idle(vault_root, timeout=300.0)
        finally:
            # A write stopped joining its own graph rebuild (#576), so a rebuild
            # is routinely still running here -- writing into a tree
            # `TemporaryDirectory` is about to remove. On POSIX that removal
            # races the rebuild and fails with "Directory not empty", which is
            # how this gate reported a *cleanup* crash as a latency failure and
            # sent a real Class C livelock in the same run past unread. Same
            # boundary rule the CLI and the suite already apply: nothing
            # outlives the vault it was building against.
            if not graph_sync.drain_active_rebuilds():
                print(
                    "semantic-write-latency: a graph rebuild did not finish "
                    "before teardown; measurements above stand, cleanup may not",
                    file=sys.stderr,
                )
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=list(DEFAULT_SIZES))
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--attempts",
        type=int,
        default=2,
        help="measurement attempts before --check fails the build (default 2)",
    )
    args = parser.parse_args(argv)
    for name in (
        "EXOMEM_DISABLE_EMBEDDINGS",
        "EXOMEM_DISABLE_CLIP",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION",
        "EXOMEM_DISABLE_RANKING",
    ):
        os.environ[name] = "1"
    # Preserve the historical Python baseline as the process default. `measure`
    # scopes that baseline explicitly, then `_measure_fts5_visibility` switches
    # only its focused phase to the production backend and reports the distinct
    # `fts5_visibility_latency_*` metrics alongside it.
    os.environ["EXOMEM_LEXICAL_BACKEND"] = "python"

    results = measure_all(args.sizes, args.samples, args.root)
    print(json.dumps({"results": results}, sort_keys=True))
    if not args.check:
        return 0

    # One sample set on a shared runner is not evidence. The scaling bound is
    # anchored to the same run's small-corpus median, so a contended runner can
    # breach it from both ends at once: the baseline comes in unusually fast,
    # which *tightens* the bound, while the large measurement inflates. That is
    # a measured failure, not a flaky assertion, so it cannot be papered over by
    # loosening the threshold -- doing that would blunt the regression signal
    # the gate exists for. Re-measure and require the failure to reproduce.
    #
    # `--root` keeps its artifacts for inspection and refuses a pre-existing
    # directory, so a retry there would fail on mkdir; those runs get one shot.
    for attempt in range(2, args.attempts + 1):
        try:
            check(results)
        except SystemExit as failure:
            if args.root is not None:
                raise
            print(f"attempt {attempt - 1}: {failure}; re-measuring", file=sys.stderr)
            results = measure_all(args.sizes, args.samples, None)
            print(json.dumps({"attempt": attempt, "results": results}, sort_keys=True))
        else:
            return 0
    check(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
