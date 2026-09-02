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
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratch_root  # noqa: E402
from synth_vault import gen_dense_vault  # noqa: E402

from exomem import (  # noqa: E402
    call_spans,
    derived_drain,
    derived_receipts,
    find,
    freshness,
    graph_sync,
    lexstore,
    memory_refs,
    pending_recall,
    readiness,
    semantic_contract,
    semantic_writes,
    vault,
    writer_lease,
)
from exomem.vault import walk_vault_md  # noqa: E402

DEFAULT_SIZES = (2_000, 8_000)
#: Fixed so a run is reproducible and the stable-ref row measures the same
#: resolution every time.
_TARGET_IDENTITY = "11111111-2222-4333-8444-555555555555"
# FTS5 visibility is report-only AND opt-in (--fts5-visibility).  Measured
# 2026-08-20 on the same contended Windows host as the Python baseline: the
# targeted retry itself landed in 32ms @ 2k and 250ms @ 8k, but the subsequent
# verified keyword read took 35.6s @ 2k and 164.4s @ 8k (~4.6x at 4x pages).  A
# ceiling with honest runner headroom over that maximum would be too broad to
# express a visibility SLO and would gate corpus-sync noise instead.
#
# So it cannot fail the build -- which is exactly why it does not run by
# default.  Producing it costs the cold catalog build plus the visibility wait
# at both sizes, about seven minutes on a required gate that currently takes
# four.  Correctness on this path is enforced every run by
# tests/test_read_after_write_visibility.py, which is deterministic and cheap;
# this flag is for measuring the window when someone is working on it.
# Environment: the fast-acknowledgement rows need managed retrieval admission,
# and the gate asserts it rather than measuring the offline walk by accident.
# Two env levers are worth stating explicitly because they look like they
# should matter and only one of them does.
#
#   EXOMEM_DISABLE_FILE_WATCHER=1 -- inert here, and safe to keep set. It is
#   read only by `graph_drain`, `server_runtime` and `hosted_runtime`; nothing
#   in `freshness`, `readiness` or `lexstore` consults it, and this script is
#   not a server, so it starts no watcher either way. Admission is unaffected,
#   and the gate does NOT need to be run without it.
#
#   EXOMEM_DISABLE_EVENT_INDEXES -- this is the one `freshness.
#   event_indexes_enabled()` actually reads, and it changes how admission is
#   proved, not whether it can be. Set, `recall_is_live` is always False and
#   the catalogue proof falls back to published rather than live checkpoints
#   (the documented polling rollback); unset, `warmup.warm_retrieval_catalog`
#   rebaselines the projections once and proves them live. Both admit. Leave
#   it unset for a gate run, so the rows measure the maintained path the
#   product ships.
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

# --- Scaling-bound floors for the three O(N) rows.
#
# SCALING_BOUND_CEILING_FRACTION above floors validate and commit and is
# explicitly withheld here: "only the two operations expected to stay FLAT with
# corpus size get this; the read/cold rows are deliberately O(N) and carry their
# own ratios."  That reasoning covers ratio MAGNITUDE.  It does not cover
# baseline NOISE, and noise is what fails the build -- the bound is anchored to
# the small sample either way, so a lucky-fast or a genuinely-improved 2k
# measurement drags it down exactly as it did for commit.
#
# MEASURED BASIS (nine consecutive Linux CI runs on main, 2026-08-21 -- the
# authoritative platform this lane actually gates on, not the contended Windows
# box the ceilings above were calibrated from):
#   read_after_write_median_ms  2k  671.8-2736.0 (4.1x spread)  8k  6578.7-19305.3
#   cold_read_after_write_ms    2k 1633.8-3398.2               8k  5427.9-11614.9
#   cold_preflight_ms           2k 2669.3-7540.7               8k  8630.8-22738.5
# The read row's 2k baseline alone moves its bound by 16.5s across those runs,
# while the 8k value it bounds spans 12.7s: the ruler is noisier than the thing
# it measures, which is the commit-row finding restated one row down.
#
# The ratio itself is the runner-invariant statistic and stayed 6.6-8.3x across
# seven of the nine -- a slow runner inflates both ends together.  It then read
# 12.3-12.5x on the two most recent, because #715 and #718 made the SMALL corpus
# disproportionately faster (#718's post-RRF early exit fires hard at 2k and
# barely at 8k, since candidate_k grows with the eligible set).  Both 8k numbers
# were at or below the running median.  Nothing regressed; the denominator
# shrank, and a ratio rule cannot tell those apart.
#
# So floor each bound just clear of the worst UNREGRESSED large-corpus figure
# observed on this runner, and keep every floor strictly below its own absolute
# ceiling so the scaling rule still fails a real super-linear regression before
# the ceiling does.  Runner spread is ~3x in absolute terms, so a floor that
# clears the slow-runner normal cannot also be tight -- that is a real limit of
# same-run anchoring, not a slack choice.  Re-measure rather than hand-tune.
READ_AFTER_WRITE_SCALING_BOUND_FLOOR_MS = 22_000.0
COLD_READ_AFTER_WRITE_SCALING_BOUND_FLOOR_MS = 14_000.0
COLD_PREFLIGHT_SCALING_BOUND_FLOOR_MS = 26_000.0

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


# --- Fast durable acknowledgement (design "Fast Acknowledgement Is Proven End
# To End"). Unlike every ceiling above, these four are NOT calibrated from this
# runner and are not this gate's to tune: they are the capability's stated
# contract. The rows above bound the mutation boundary and the read that
# follows it, and by construction they cannot see the acknowledgement the
# caller actually waits for -- which is exactly the quantity this change moves.
# A boundary-only measurement would report work relocated onto the public leaf
# as a pure improvement, which is the invisibility class the design names.
PUBLIC_WRITE_P95_MS = 4_000.0
IMMEDIATE_READ_P95_MS = 1_500.0
PAIRED_WRITE_READ_P95_MS = 5_000.0
# A bound on EVERY sample, not a percentile: the post-canonical budget is one
# shared hard deadline, so a single sample outside it is a breach of the
# contract rather than a tail.
POST_CANONICAL_BOUND_MS = 2_000.0


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


#: How long a measured phase may wait for the lexical catalogue to go quiet.
#:
#: This is a *setup* budget, not a gate threshold: no measured row is compared
#: against it and no ceiling depends on it. What it bounds is warm-up, and
#: exceeding it fails the run as a setup failure rather than letting a
#: half-built catalogue be timed as though it were the product.
#:
#: It replaces an unnamed 60-second value described as "only a deadlock valve".
#: That framing was wrong in a way that cost a run: on a box at load 7 the
#: catalogue legitimately needed longer than 60 s, the valve tripped in the
#: FTS5 report phase, and the failure named neither a budget nor a cause. A
#: wait that can fail a run is a budget whatever it is called, so it is named,
#: stated, and sized for a loaded box here.
LEXICAL_WARMUP_BUDGET_SECONDS: Final = 300.0


def _wait_for_lexical_repair_idle(
    vault_root: Path, timeout: float = LEXICAL_WARMUP_BUDGET_SECONDS
) -> None:
    """Wait, within a stated budget, for the lexical repair flight to finish.

    Failing here is deliberate and must stay failing: proceeding with repairs
    still in flight would measure a catalogue that is still being built, and
    the reads this gate bounds would be timed against something no served
    reader ever sees.
    """
    key = vault_root.resolve()
    started = time.monotonic()
    deadline = started + timeout
    wake = threading.Event()
    while True:
        with lexstore._REPAIRS_LOCK:
            if key not in lexstore._REPAIRS_IN_FLIGHT:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "lexical repair worker did not become idle within the "
                f"{timeout:.0f}s warm-up budget "
                f"(waited {time.monotonic() - started:.1f}s); this is a setup "
                "failure, not a measured row"
            )
        wake.wait(min(0.01, remaining))


@contextmanager
def _production_read_backend(vault_root: Path):
    """Scope a phase to the backend a served reader actually gets.

    `auto` is the product's own selection -- FTS5 where the SQLite build has
    it, the Python rung where it does not -- so a row measured in here is a row
    about the shipped read path. The catalogue is built and allowed to go quiet
    first, because an unbuilt or still-repairing catalogue turns the first
    sample into a build cost and the rest into something else entirely.
    """
    with _lexical_backend("auto"):
        lexstore.ensure_fresh(vault_root)
        _wait_for_lexical_repair_idle(vault_root)
        yield


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


def _enter_managed_recall(vault_root: Path) -> None:
    """Stand up the admission a served process has, for this phase only.

    The warm window is not the admission. `begin_warm`/`finish_warm` only open
    and close the window; what actually admits retrieval is the *catalogue
    proof* published inside it -- `readiness.admit_retrieval_proof`, which is
    the sole writer of the `retrieval_catalog` event that
    `readiness.retrieval_admission` reads. An earlier version of this function
    opened and closed the window without ever publishing that proof, so it left
    `_warm_finished` set with the event unset, which is precisely the
    `unavailable` state, and the gate died at the assertion below within a
    minute of starting.

    So this delegates to `warmup.warm_retrieval_catalog`, the function the
    served process itself calls, rather than restating an abbreviation of it.
    That keeps the rebaseline, the live-projection requirement, the maintained
    versus reference index distinction and the proof CAS in one place, and it
    means this warm-up cannot drift away from the product's.

    `EXOMEM_EAGER_BOOT` is set for the call because a benchmark needs the
    synchronous contract: without it, an incomplete catalogue is delegated to
    the background repair worker and the function returns False, leaving the
    sampling loop to race a rebuild. With it, the repair is awaited and proven,
    and a failure to converge raises here instead of quietly measuring the
    wrong thing.
    """
    from exomem import warmup

    lexstore.ensure_fresh(vault_root)
    readiness.manage_runtime()
    previous_eager = os.environ.get("EXOMEM_EAGER_BOOT")
    os.environ["EXOMEM_EAGER_BOOT"] = "1"
    readiness.begin_warm()
    try:
        warmup.warm_retrieval_catalog(vault_root)
    finally:
        readiness.finish_warm()
        if previous_eager is None:
            os.environ.pop("EXOMEM_EAGER_BOOT", None)
        else:
            os.environ["EXOMEM_EAGER_BOOT"] = previous_eager

    admission = readiness.retrieval_admission(vault_root)
    if not admission.get("admitted"):
        # A gate that silently measured the offline walk instead of managed
        # recall would report the wrong capability entirely.
        raise RuntimeError(
            f"managed recall admission was not granted: {admission}"
        )


def _drain_derived_custody(vault_root: Path, *, passes: int = 64) -> None:
    """Run the production scheduler pass until exact custody settles.

    Time is advanced explicitly rather than slept: the store's backoff is a
    wall-clock `next_attempt_at`, and sleeping it out would measure the backoff
    constant instead of convergence.
    """
    dispatch = derived_drain.component_dispatcher()
    observe = derived_drain.canonical_generation_observer()
    now = time.time()
    for _attempt in range(passes):
        derived_drain.drain_once(
            vault_root,
            dispatch=dispatch,
            observe_current_generation=observe,
            visibility_publisher=pending_recall.publish,
            limit=32,
            now=now,
        )
        now += derived_drain.MAX_RETRY_SECONDS + 1.0
        if not derived_receipts.due_component_count(
            vault_root, now=now
        ) and not derived_receipts.recoverable_batch_count(vault_root):
            return


def _public_transition(
    vault_root: Path,
    rel_path: str,
    version: int,
    manager: Any,
) -> tuple[float, float]:
    """Time one complete public governed write, entry to acknowledgement.

    The boundary rows below measure preflight and commit directly. This drives
    the same transition through the real lease manager, which is where the fast
    acknowledgement session lives -- so what is timed is what the caller waits
    for, guard, terminal persistence and post-canonical observation included.

    The second value is the interval the shared 2.0-second budget governs,
    taken from the writer's own phase rather than inferred by subtracting other
    spans: the one measurement in this gate that must not be an approximation.
    """
    path = vault_root / rel_path
    before = path.read_text(encoding="utf-8")
    after = _next_source(before, version)

    def leaf(root: Path):
        preflight = semantic_writes.preflight_existing(
            root, path=rel_path, after_source=after, operation="observe"
        )
        if preflight.contract_result.should_block:
            codes = [item.code for item in preflight.contract_result.blocking_findings]
            raise RuntimeError(f"synthetic transition was blocked: {codes}")
        semantic_writes.commit_existing(root, preflight=preflight)
        return {"path": rel_path, "warnings": []}

    command = SimpleNamespace(name="observe_memory", leaf=leaf, read_only=False)
    token_value = f"semantic-write-latency-{version}"
    call_spans.reset()
    token = call_spans.MCP_CALL_TOKEN.set(token_value)
    started = time.perf_counter()
    try:
        manager.invoke(
            command,
            (vault_root,),
            {"response_detail": "compact"},
            mutation_request_id=str(uuid.uuid4()),
        )
        public_ms = (time.perf_counter() - started) * 1_000.0
        spans = {
            span["name"]: span for span in call_spans.pop_call_spans(token_value)
        }
    finally:
        call_spans.MCP_CALL_TOKEN.reset(token)
    post_canonical_ms = float(spans.get("derived.post_canonical", {}).get("ms", 0.0))
    return public_ms, post_canonical_ms


def _immediate_reads(
    vault_root: Path, rel_path: str, version: int
) -> tuple[float, float, float | None]:
    """Keyword, hybrid and stable-reference reads of a just-committed version.

    Each asserts the new generation is actually visible before returning its
    cost. A fast-but-stale read has to fail this gate, not pass it -- that is
    the whole difference between deferring derived work and losing it.
    """
    marker = f"Synthetic write latency version {version} "
    keyword_started = time.perf_counter()
    keyword_hits = find.find(
        vault_root, query=marker.strip(), scope="kb", mode="keyword", limit=5
    )
    keyword_ms = (time.perf_counter() - keyword_started) * 1_000.0
    if not any(marker in (hit.excerpt or "") for hit in keyword_hits):
        raise RuntimeError(
            f"immediate keyword read did not see version {version} for {rel_path}"
        )

    hybrid_started = time.perf_counter()
    hybrid_hits = find.find(
        vault_root, query=marker.strip(), scope="kb", mode="hybrid", limit=5
    )
    hybrid_ms = (time.perf_counter() - hybrid_started) * 1_000.0
    if not any(hit.path == rel_path for hit in hybrid_hits):
        raise RuntimeError(
            f"immediate hybrid read did not see version {version} for {rel_path}"
        )

    identity = memory_refs.normalize_id(
        vault.parse_frontmatter((vault_root / rel_path).read_text(encoding="utf-8"))[
            0
        ].get(memory_refs.ID_FIELD)
    )
    if identity is None:
        # Recorded as absent rather than as zero: the row is report-only and a
        # missing stable identity is a fact about the corpus, not a fast read.
        return keyword_ms, hybrid_ms, None
    # The stable reference, not the bare identity: this is the read-only route
    # a caller verifying by reference actually takes, and it is the one that
    # consults the pending identity projection before the sidecar.
    reference = memory_refs.memory_ref(identity)
    ref_started = time.perf_counter()
    resolved = memory_refs.resolve_identifier_read_only(vault_root, reference)
    ref_ms = (time.perf_counter() - ref_started) * 1_000.0
    if resolved != rel_path:
        raise RuntimeError(
            f"immediate stable-ref read resolved {resolved!r}, not {rel_path!r}"
        )
    return keyword_ms, hybrid_ms, ref_ms


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


def _warm_activation_boundary(vault_root: Path, rel_path: str) -> None:
    """Finish the initial whole-vault graph build before timed write samples.

    The activation transition starts derived graph work asynchronously.  Letting
    the timed burst begin immediately makes each sample invalidate one full
    stabilization pass; on a slower runner the final sample can exhaust the
    bounded rebuild just as the corpus stops moving.  That measures cold graph
    construction and retry churn, not steady-state semantic write latency.
    """
    _transition(vault_root, rel_path, 1)
    if not graph_sync.drain_active_rebuilds():
        raise RuntimeError(
            "initial graph rebuild did not finish before semantic write samples"
        )


def measure(
    vault_root: Path,
    size: int,
    samples: int,
    *,
    fts5_visibility: bool = False,
) -> dict[str, float | int]:
    # Keep the historical deterministic baseline pinned to the Python rung:
    # `validate`, `commit`, `read_after_write` and the cold rows are a stable
    # cross-release series and their comparability depends on the instrument
    # not changing under them.
    #
    # Two phases override this scope for their own reads, and both are about
    # the shipped read path rather than the baseline: the focused FTS5
    # visibility measurement, and the fast-acknowledgement rows, which run
    # under the production `auto` selection (ruling R4).
    with _lexical_backend("python"):
        return _measure(vault_root, size, samples, fts5_visibility=fts5_visibility)


def _measure(
    vault_root: Path,
    size: int,
    samples: int,
    *,
    fts5_visibility: bool,
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
        # A real compiled page carries a stable identity, and the immediate
        # stable-reference read below is only a measurement if there is one to
        # resolve.
        f"{memory_refs.ID_FIELD}: {_TARGET_IDENTITY}\n"
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
    _warm_activation_boundary(vault_root, target_rel)
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
    # --- Fast durable acknowledgement: the public leaf, and the read that
    # immediately follows it. Driven through the real lease manager with the
    # capability enabled, because the acknowledgement session only exists
    # there -- measuring `commit_existing` directly would time the boundary
    # again under a new name.
    public_writes: list[float] = []
    post_canonical: list[float] = []
    keyword_reads: list[float] = []
    hybrid_reads: list[float] = []
    stable_ref_reads: list[float] = []
    paired: list[float] = []
    previous_flag = os.environ.get("EXOMEM_FAST_DURABLE_ACK")
    os.environ["EXOMEM_FAST_DURABLE_ACK"] = "1"
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault_root.parent / "lease-state")
    )
    # The pending overlay is deliberately scoped to managed recall: an offline
    # caller keeps its exact source-walk fallback. This gate is about what a
    # served request sees, so it stands up the same admission a served process
    # has -- managed runtime, a completed warm, and an admitted catalogue proof
    # -- for exactly this phase, and hands admission back afterwards. Without
    # the warm and the proof, managed recall answers `warming` by contract and
    # the rows below would measure a refusal.
    # Ruling R4: these rows run on the *production* backend selection, not the
    # Python rung the historical boundary rows are pinned to.
    #
    # The rung above is deliberately O(N) even when warm, which is exactly what
    # makes it a stable baseline for the boundary rows -- and exactly what makes
    # it the wrong instrument here. The design's 1.5 s immediate-read and 5.0 s
    # paired contracts are promises about what a served reader waits for, and a
    # served reader gets `auto`. Measured on the slow rung these rows reported
    # 6.2 s keyword and 11.6 s hybrid at 8k pages while the write side passed
    # with two orders of magnitude of headroom, and the immediate keyword read
    # came out equal to `read_after_write` at both sizes -- the overlay adding
    # nothing measurable, the rung supplying all of it. That is a measurement of
    # the instrument, not of the product.
    #
    # No ceiling changes. The rows are simply pointed at the path they are
    # about.
    with _production_read_backend(vault_root):
        _enter_managed_recall(vault_root)
        try:
            first_public = samples + 2
            for version in range(first_public, first_public + samples):
                public_ms, post_ms = _public_transition(
                    vault_root, target_rel, version, manager
                )
                keyword_ms, hybrid_ms, ref_ms = _immediate_reads(
                    vault_root, target_rel, version
                )
                public_writes.append(public_ms)
                post_canonical.append(post_ms)
                keyword_reads.append(keyword_ms)
                hybrid_reads.append(hybrid_ms)
                if ref_ms is not None:
                    stable_ref_reads.append(ref_ms)
                # The pair is one caller's complete experience: acknowledge,
                # then immediately verify. Measured together because a change
                # that shortens one by lengthening the other is exactly what
                # the paired bound exists to catch.
                paired.append(public_ms + max(keyword_ms, hybrid_ms))
        finally:
            readiness.unmanage_runtime()
            # Converge the custody this phase created before anything else is
            # measured. The cold rows below read through the persistent
            # catalogues, and leaving a burst of exact receipts outstanding
            # would charge their convergence to a row that is not about it.
            _drain_derived_custody(vault_root)
            pending_recall.reset(vault_root)
            find.clear_cache()
            if previous_flag is None:
                os.environ.pop("EXOMEM_FAST_DURABLE_ACK", None)
            else:
                os.environ["EXOMEM_FAST_DURABLE_ACK"] = previous_flag

    cold_write_version = samples + 2 + samples
    cold_probe_version = cold_write_version + 1
    _transition(vault_root, target_rel, cold_write_version)  # ordinary warm write
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    cold_read_after_write_ms = _read_after_write(vault_root, target_rel, cold_write_version)
    cold_preflight_ms, _, _ = _transition(vault_root, target_rel, cold_probe_version)

    measured: dict[str, float | int] = {
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
        "public_write_median_ms": round(statistics.median(public_writes), 1),
        "public_write_p95_ms": round(_percentile(public_writes, 0.95), 1),
        "immediate_keyword_read_median_ms": round(
            statistics.median(keyword_reads), 1
        ),
        "immediate_keyword_read_p95_ms": round(_percentile(keyword_reads, 0.95), 1),
        "immediate_hybrid_read_median_ms": round(statistics.median(hybrid_reads), 1),
        "immediate_hybrid_read_p95_ms": round(_percentile(hybrid_reads, 0.95), 1),
        "immediate_stable_ref_read_median_ms": (
            round(statistics.median(stable_ref_reads), 1) if stable_ref_reads else None
        ),
        "immediate_stable_ref_read_p95_ms": (
            round(_percentile(stable_ref_reads, 0.95), 1) if stable_ref_reads else None
        ),
        "paired_write_read_p95_ms": round(_percentile(paired, 0.95), 1),
        "post_canonical_max_ms": round(max(post_canonical), 1),
    }
    if fts5_visibility:
        measured["fts5_visibility_latency_ms"] = round(
            _measure_fts5_visibility(
                vault_root, target_rel, current_version=cold_probe_version
            ),
            1,
        )
    return measured


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
            ("public_write_p95_ms", PUBLIC_WRITE_P95_MS),
            ("immediate_keyword_read_p95_ms", IMMEDIATE_READ_P95_MS),
            ("immediate_hybrid_read_p95_ms", IMMEDIATE_READ_P95_MS),
            ("paired_write_read_p95_ms", PAIRED_WRITE_READ_P95_MS),
            ("post_canonical_max_ms", POST_CANONICAL_BOUND_MS),
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
        read_bound = max(
            float(small[read_key]) * READ_AFTER_WRITE_SCALING_RATIO
            + READ_AFTER_WRITE_SCALING_SLACK_MS,
            READ_AFTER_WRITE_SCALING_BOUND_FLOOR_MS,
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
        cold_bound = max(
            float(small[cold_key]) * COLD_READ_AFTER_WRITE_SCALING_RATIO
            + COLD_READ_AFTER_WRITE_SCALING_SLACK_MS,
            COLD_READ_AFTER_WRITE_SCALING_BOUND_FLOOR_MS,
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
        cold_preflight_bound = max(
            float(small[cold_preflight_key]) * COLD_PREFLIGHT_SCALING_RATIO
            + COLD_PREFLIGHT_SCALING_SLACK_MS,
            COLD_PREFLIGHT_SCALING_BOUND_FLOOR_MS,
        )
        if float(large[cold_preflight_key]) >= cold_preflight_bound:
            failures.append(
                f"cold_preflight scaling: {large[cold_preflight_key]}ms >= "
                f"{cold_preflight_bound:.1f}ms ({small['pages']} -> {large['pages']} pages)"
            )
    if failures:
        raise SystemExit("semantic write latency gate failed: " + "; ".join(failures))


def measure_all(
    sizes: list[int],
    samples: int,
    root: Path | None,
    *,
    fts5_visibility: bool = False,
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
                results.append(
                    measure(vault_root, size, samples, fts5_visibility=fts5_visibility)
                )
                # Only the FTS5 phase starts a repair worker, and one still
                # running would charge its tail to the next corpus.
                if fts5_visibility and index < len(roots) - 1:
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
                results.append(
                    measure(vault_root, size, samples, fts5_visibility=fts5_visibility)
                )
                if fts5_visibility and index < len(sizes) - 1:
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
    parser.add_argument(
        "--fts5-visibility",
        action="store_true",
        help=(
            "also measure committed-to-queryable latency on the production FTS5 "
            "backend (report-only, and slow: see the note above DEFAULT_SIZES)"
        ),
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

    results = measure_all(
        args.sizes, args.samples, args.root, fts5_visibility=args.fts5_visibility
    )
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
