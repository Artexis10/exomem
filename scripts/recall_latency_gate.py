"""Whole-call governed recall evidence and a same-runner release comparison.

The packaged contract supplies bars and interval ownership. Reference inputs
and evidence schemas live beside the accept record. Legacy observation flags
remain available and continue to identify their narrower boundary.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# Legacy diagnostics retain their source default. MCP evidence defaults to the
# installed observer; adding source requires an explicit opt-in before import.
_bootstrap = argparse.ArgumentParser(add_help=False)
_bootstrap.add_argument("--package-mode", choices=("installed", "source"))
_bootstrap.add_argument("--mcp-url")
_bootstrap.add_argument("--worker-source", type=Path)
_bootstrap.add_argument("--worker-cpus")
_boot_args = _bootstrap.parse_known_args()[0]
try:
    if _boot_args.worker_source:
        # Pin before importing application modules or any native encoder dependency.
        os.sched_setaffinity(0, set(json.loads(_boot_args.worker_cpus)))
        sys.path.insert(0, str(_boot_args.worker_source / "src"))
    elif (_boot_args.package_mode or ("installed" if _boot_args.mcp_url else "source")) == "source" and str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    from exomem.request_latency_contract import load_contract

    CONTRACT = load_contract()
    if _boot_args.worker_source:
        # Both arms read head's canonical bars.
        CONTRACT = type(CONTRACT).model_validate_json(
            (SRC / "exomem" / "request_latency_contract.json").read_text()
        )
except Exception as error:  # noqa: BLE001 - bootstrap errors can contain local paths.
    if not _boot_args.worker_source:
        raise
    print(json.dumps({"state": "worker-error", "error_class": type(error).__name__,
                      "reason": "benchmark-worker-bootstrap-failed", "phase": "worker-bootstrap"}), flush=True)
    raise SystemExit(1) from None
HYBRID_BAR = CONTRACT.bar("governed-hybrid")
KEYWORD_BAR = CONTRACT.bar("governed-keyword")
HYBRID_P95_MS = HYBRID_BAR.threshold_ms
KEYWORD_P95_MS = KEYWORD_BAR.threshold_ms
HYBRID_P50_MS = CONTRACT.bar("governed-p50-target").threshold_ms
KEYWORD_P50_MS = HYBRID_P50_MS
FILTERED_HYBRID_P50_MS = HYBRID_P50_MS
FILTERED_ELIGIBILITY_MS = CONTRACT.bar("governed-stage-filter_eligibility").threshold_ms
RESPONSE_INNER_BOUNDARY = "response-inner-timings-v1"

#: "with the load average at or below 2.0"
MAX_LOAD_AVERAGE = 2.0
#: How often the bounded quiescence wait re-reads the load.
QUIESCENCE_POLL_SECONDS = 1.0
#: How long it waits before giving up, unless `--quiescence-bound` says otherwise.
DEFAULT_QUIESCENCE_BOUND_SECONDS = 120.0

#: "thirty novel hybrid recalls run back to back"
SAMPLES_PER_SERIES = HYBRID_BAR.min_samples
#: "more than one warming outcome in a measured series SHALL fail the gate"
MAX_WARMING_SAMPLES = int(CONTRACT.references[HYBRID_BAR.reference]["max_warming"])
#: "a quiescent cell of at least 6,500 governed pages"
MIN_CORPUS_PAGES = int(CONTRACT.references[HYBRID_BAR.reference]["pages_min"])

#: The request limit the verdict is defined at. `rerank` scales with the
#: candidate count — about 103-120 ms at 15 candidates and 217 ms at 30 — so
#: the limit moves the p50 directly, and a verdict taken at a different limit
#: is not a verdict against these ceilings. `--limit` stays for exploratory
#: runs, and `check` refuses to certify anything but this value: a stub cell
#: costing 1200 ms per request passes every ceiling at `--limit 1`.
DEFAULT_LIMIT = 15

#: The stages the contract requires to consume maintained indexes: "Eligibility,
#: widening, hydration and hit construction SHALL consume maintained indexes and
#: exact receipts". `computed` on one of these is the corpus walk reappearing.
#:
#: Only two of the six decide their source at runtime, and saying otherwise was
#: the last version's mistake. What each one actually does, because a watch list
#: whose entries cannot report a walk is a longer list and not a stronger check:
#:
#: * `filter_eligibility` — RUNTIME. Marks `index`, `cache` or `declined` from
#:   which arm answered (`find.py` 3150-3305), and is the one stage the spec
#:   gives a second, narrower scenario of its own.
#: * `outside_kb` — RUNTIME. `declined` on every refusal, `index` when the
#:   reserve was answered from the catalogue (`find.py` 4393-4516).
#: * `keyword` — RUNTIME in `find._find_keyword`, which is the branch that CAN
#:   enumerate and marks `computed` when it does. STATIC `index` in
#:   `find_candidates.py:472`, whose only source of paths is the injected
#:   provider.
#: * `recall_projection`, `pending_visibility` — STATIC `index`, written as the
#:   span's opening default (`find.py:1164,1178,1192`).
#: * `filter_hits` — STATIC `index` (`find.py:3967`).
#:
#: So four of the six report a constant. They stay on the list because the list
#: is what the check reads, and a stage that later learns to walk must already
#: be watched when it does — but the gate's walk sentinel can only ever fire on
#: `filter_eligibility`, `outside_kb` and `find.py`'s `keyword`. That is a real
#: bound on what a live run proves, and the in-suite `ScopeWalkSentinel` and
#: `_PageReadSentinel` are what cover the rest.
#:
#: Deliberately still NOT every stage. `rerank`, `fusion` and `vector.search`
#: really do compute — they read no index, the product marks none of them, and
#: a gate that called those walks would fail every run for a reason unrelated
#: to the corpus. `test_a_computing_stage_that_is_not_a_walker_is_not_a_walk`
#: is the counter-case that keeps this list honest.
WALKER_STAGES: tuple[str, ...] = (
    "filter_eligibility",
    "outside_kb",
    "recall_projection",
    "pending_visibility",
    "keyword",
    "filter_hits",
)

#: The sources the spec permits a stage to report: "every stage reports `index`,
#: `cache` or `declined` as its source". Checked as an allowlist rather than by
#: testing for the single word `computed`, because the gate reads this off a
#: remote cell's JSON: a walk that arrived spelled anything else at all would
#: have passed a denylist without a word.
PERMITTED_SOURCES: frozenset[str] = frozenset({"index", "cache", "declined"})

#: Severity order, matching `find_types._SOURCE_WIDTH`: when a stage reports
#: different sources across a series, the report keeps the worst one, because a
#: single walk in thirty is the finding.
_SOURCE_SEVERITY = {"cache": 0, "index": 1, "declined": 2, "computed": 3}

#: The filter used by the third series. One supported structured filter, on the
#: field Lane 2 moved onto the page catalogue.
FILTER_PROJECTS: tuple[str, ...] = ("exomem",)

#: The term the keyword series searches for. A keyword recall is pure lexical
#: matching, so a query built only from a nonce matches nothing on any corpus,
#: and thirty empty recalls are fast for reasons that have nothing to do with
#: the read path — measured at 0 hits in 47.3 ms before this existed, under the
#: tightest ceiling the contract states, measuring nothing.
#:
#: Like `FILTER_PROJECTS`, this is a property of the CELL BEING CERTIFIED and
#: not of the contract, and the gate cannot verify it from here: on the fixture
#: vault `exomem` matches 0 pages, as does `projects=["exomem"]`, because that
#: fixture is not the cell either constant describes. What makes a wrong choice
#: safe is the hits guard, which turns "this series matched nothing" into a
#: failure instead of the fastest percentile the gate can emit. If a run fails
#: on the keyword series' median hits, the term is wrong for that cell — change
#: it here, where a reviewer sees it, and never the ceiling.
KEYWORD_TERM = "exomem"

#: `hybrid`, `keyword` and `filtered_hybrid` are the contract's three series.
#: `browse` is the fourth and carries no ceiling of its own — see
#: `_P50_CEILINGS`. It is here because this change ships a fix for a whole-scope
#: walk on `ask_memory(query="")`, the shape the tool schema recommends for
#: browsing, and the artifact that certifies the live cell did not exercise it:
#: every other series passes a non-empty query, so `find.py` always took the
#: `if query_norm:` arm and the branch that can report `computed` was
#: unreachable from the gate.
SERIES_SHAPES: tuple[str, ...] = ("hybrid", "keyword", "filtered_hybrid", "browse")


@dataclass(frozen=True)
class Sample:
    """One request's closed outcome. Carries no content, by construction.

    `hits` is a COUNT, never the hits. Without it the filtered series can
    measure empty-result recalls — no rerank, no fusion — sail under 400 ms and
    report `filter_eligibility: index`, which is a green verdict for a filter
    that matched nothing on this cell.
    """

    elapsed_ms: float | None
    warming: bool
    stage_sources: dict[str, str] = field(default_factory=dict)
    stage_ms: dict[str, float] = field(default_factory=dict)
    hits: int = 0


class Transport(Protocol):
    """What the gate needs from a cell: a name, a size, and a timed ask.

    `pages` is None when the transport could not derive the size of the cell it
    measures, which is a different answer from zero and is refused by name.
    """

    name: str
    pages: int | None

    def ask(
        self, *, shape: str, query: str, mode: str, projects: tuple[str, ...], limit: int
    ) -> Sample: ...


# --- Quiescence -------------------------------------------------------------


def one_minute_load() -> float:
    """The one-minute load average the contract names."""
    try:
        return float(os.getloadavg()[0])
    except (OSError, AttributeError):
        # A platform with no load average cannot prove quiescence, and the
        # contract's refusal is about proof. Report an impossible load so the
        # gate refuses rather than measuring a box it cannot vouch for.
        return float("inf")


def await_quiescence(load_source, *, bound_seconds: float, sleep) -> tuple[float, float, float]:
    """Wait a bounded time for the load to fall to the ceiling.

    Returns `(first_load, final_load, waited_seconds)`. The caller decides what
    to do about a `final_load` still above the ceiling; this function never
    exits on its own, so the refusal message can name both ends of what it saw.

    Elapsed time is accumulated from the sleeps rather than read from the
    clock, so the bound is exact under an injected sleep and a test does not
    have to spend real seconds proving the wait terminates.
    """
    first = load = float(load_source())
    waited = 0.0
    while load > MAX_LOAD_AVERAGE and waited < bound_seconds:
        sleep(QUIESCENCE_POLL_SECONDS)
        waited += QUIESCENCE_POLL_SECONDS
        load = float(load_source())
    return first, load, waited


def _refuse(first_load: float, final_load: float, waited: float) -> SystemExit:
    """Exit without a verdict, naming the load.

    The message carries no ceiling comparison of any kind: the spec says the
    gate "never emits ceiling comparisons from samples taken under that load",
    and the cleanest way to honour that is to have taken no samples and to say
    nothing about latency at all.
    """
    return SystemExit(
        "recall latency gate refused: one-minute load average "
        f"{first_load:.2f} at start, {final_load:.2f} after waiting {waited:.0f}s; "
        f"the contract measures at or below {MAX_LOAD_AVERAGE:.1f}. No samples were taken."
    )


# --- Series -----------------------------------------------------------------


def run_nonce() -> str:
    """A per-run token, so a second run cannot be served the first one's cache."""
    return uuid.uuid4().hex[:12]


def _whitespace_nonce(nonce: str, index: int) -> str:
    """The run-and-sample nonce written in whitespace instead of words.

    Two series need novelty without a word in the query. The browse shape IS
    `query.strip() == ""` — that is the test `find.py` routes on — so any word
    at all makes it a different request through a different lane; and the
    keyword series has to match the corpus, which a nonce token cannot do.

    Both work because the two halves are keyed differently: the result cache is
    keyed on the RAW query (`find.py`'s `request_key`) and the read path
    branches and matches on the STRIPPED one. So 64 bits of the run token and
    the sample index, tab for one and space for zero, is a query no cell has
    been asked before that is nonetheless the same request the read path sees.
    """
    value = ((int(nonce, 16) << 8) | (index & 0xFF)) & ((1 << 64) - 1)
    return format(value, "064b").replace("0", " ").replace("1", "\t")


def novel_query(shape: str, index: int, nonce: str) -> str:
    """A query no cell has been asked before.

    The nonce is what defeats the result cache. It is per run AND per sample,
    because a nonce that were only per series would still let the second series
    hit the first one's entries, and one that were only per run would let a
    re-run of the same series be answered from memory.

    The prose around the nonce is fixed and generic on purpose: it makes the
    series comparable, and it is not vault content, so it can appear in a log
    without disclosing anything.
    """
    if shape == "browse":
        return _whitespace_nonce(nonce, index)
    if shape == "keyword":
        return KEYWORD_TERM + _whitespace_nonce(nonce, index)
    return f"governed recall latency probe {shape} {nonce} {index:02d}"


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile; no interpolation, so 30 samples stay legible."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(fraction * len(ordered) + 0.5))))
    return ordered[rank - 1]


def _widest(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    if _SOURCE_SEVERITY.get(candidate, 99) > _SOURCE_SEVERITY.get(current, 99):
        return candidate
    return current


def _is_walker(stage: str) -> bool:
    """Match qualified names too.

    Lane 1 moved the projection under qualified names (`freshness.
    recall_projection`, `graph.resolver.recall_projection`). A guard that
    matched the bare name only would have quietly stopped watching the stage on
    the day it was renamed.
    """
    if stage in WALKER_STAGES:
        return True
    return stage.rsplit(".", 1)[-1] in WALKER_STAGES


def _report_targets(series: dict[str, Any], shape: str) -> None:
    if shape == "filtered_hybrid":
        eligibility = series.get("eligibility_ms")
        # Stage budgets diagnose their row; they never fail the ceiling verdict.
        series["eligibility_budget_ms"] = FILTERED_ELIGIBILITY_MS
        series["eligibility_over_budget"] = (
            float(eligibility) > FILTERED_ELIGIBILITY_MS if eligibility is not None else None
        )
    p50 = series.get("p50_ms")
    p95 = series.get("p95_ms")
    target = _P50_TARGETS.get(shape)
    series["p50_target_ms"] = target
    series["p50_target_met"] = (
        float(p50) <= target if p50 is not None and target is not None else None
    )
    p95_target = (
        CONTRACT.bar(
            "governed-keyword-p95-target" if shape == "keyword" else "governed-hybrid-p95-target"
        )
        if shape != "browse"
        else None
    )
    series["p95_target_ms"] = p95_target.threshold_ms if p95_target else None
    series["p95_target_met"] = (
        float(p95) <= p95_target.threshold_ms if p95 is not None and p95_target else None
    )


def run_series(
    transport,
    shape: str,
    *,
    nonce: str,
    load_source,
    limit: int,
    quiescence_bound_seconds: float,
    sleep,
) -> dict[str, Any]:
    """Take one series, refusing rather than reporting if the box gets busy."""
    mode = "keyword" if shape == "keyword" else "hybrid"
    projects: tuple[str, ...] = FILTER_PROJECTS if shape == "filtered_hybrid" else ()

    measured: list[float] = []
    loads: list[float] = []
    warming = 0
    unknown = 0
    sources: dict[str, str] = {}
    eligibility: list[float] = []
    hit_counts: list[int] = []

    for index in range(SAMPLES_PER_SERIES):
        sample = transport.ask(
            shape=shape,
            query=novel_query(shape, index, nonce),
            mode=mode,
            projects=projects,
            limit=limit,
        )
        if sample.warming:
            # A truthful refusal is not a fast recall. Excluded from the
            # percentiles, counted, and bounded by `check`.
            warming += 1
        elif (
            sample.elapsed_ms is None
            or not math.isfinite(sample.elapsed_ms)
            or sample.elapsed_ms < 0
        ):
            unknown += 1
        else:
            measured.append(float(sample.elapsed_ms))
            hit_counts.append(int(sample.hits))
        for stage, source in sample.stage_sources.items():
            sources[stage] = _widest(sources.get(stage), source)
        for stage, milliseconds in sample.stage_ms.items():
            if _is_walker(stage) and stage.rsplit(".", 1)[-1] == "filter_eligibility":
                eligibility.append(float(milliseconds))

        # The owner checks quiescence once; later process load is diagnostic.
        load = float(load_source())
        loads.append(load)

    series: dict[str, Any] = {
        "samples": len(measured),
        "warming": warming,
        "unknown_timings": unknown,
        "contention": "unknown",
        "p50_ms": round(_percentile(measured, 0.50), 1) if measured else None,
        "p95_ms": round(_percentile(measured, 0.95), 1) if measured else None,
        "load_mean": round(statistics.fmean(loads), 2) if loads else 0.0,
        "load_max": round(max(loads), 2) if loads else 0.0,
        "stage_sources": sources,
        # The total is kept because it is diagnostic — "120 hits across thirty
        # samples but a median of nothing" says something a single number does
        # not — but the guard is on the median, because the p50 is what the
        # gate certifies. A sum lets one hit in thirty clear a check that is
        # supposed to prove the p50 measured a real retrieval.
        "hits": sum(hit_counts),
        "hits_p50": int(_percentile([float(count) for count in hit_counts], 0.50))
        if hit_counts
        else None,
    }
    if shape == "filtered_hybrid":
        # None, not 0.0, when the stage never reported: a stage that did not run
        # is not a stage that ran in no time, and 0.0 sails under every ceiling.
        # `check` fails closed on the None.
        series["eligibility_ms"] = round(max(eligibility), 1) if eligibility else None
    _report_targets(series, shape)
    return series


def run(
    *,
    transport,
    load_source=one_minute_load,
    quiescence_bound_seconds: float = DEFAULT_QUIESCENCE_BOUND_SECONDS,
    sleep=time.sleep,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Take every series, or refuse before taking any sample at all."""
    first, final, waited = await_quiescence(
        load_source, bound_seconds=quiescence_bound_seconds, sleep=sleep
    )
    if final > MAX_LOAD_AVERAGE:
        raise _refuse(first, final, waited)

    nonce = run_nonce()
    pages = transport.pages
    return {
        "refused": False,
        "contract_revision": CONTRACT.revision,
        "acceptance": "not measured",
        "boundary": RESPONSE_INNER_BOUNDARY,
        "profile": "legacy-read-path-observation",
        "unmeasured_dimensions": [
            "reference-query-mix",
            "owner-authority",
            "admitted-principal",
            "stage-interval-unions",
            "run-queue-contention",
            "served-encoder",
        ],
        "transport": transport.name,
        # None when the transport could not derive the size of the cell it
        # measured. Carried through as None rather than coerced to a number,
        # because a printed integer reads as verified.
        "pages": None if pages is None else int(pages),
        "limit": limit,
        "series": {
            shape: run_series(
                transport,
                shape,
                nonce=nonce,
                load_source=load_source,
                limit=limit,
                quiescence_bound_seconds=quiescence_bound_seconds,
                sleep=sleep,
            )
            for shape in SERIES_SHAPES
        },
    }


# --- The verdict ------------------------------------------------------------

_P50_TARGETS = {
    "hybrid": HYBRID_P50_MS,
    "keyword": KEYWORD_P50_MS,
    "filtered_hybrid": FILTERED_HYBRID_P50_MS,
}

#: Series the contract states no latency ceiling for. `browse` is measured and
#: reported — its percentiles go into the artifact beside the others, and every
#: structural rule applies to it: the walk sentinel, the warming bound, the hits
#: guard — but no p50 or p95 comparison is made, because there is no number in
#: the spec to compare against. Inventing one would put a figure into a stored
#: artifact that reads as the contract's and is not; a ceiling calibrated from
#: this box is exactly what the spec's "not calibrated from any runner" forbids,
#: and one calibrated from nothing is worse. If the browse should carry a
#: ceiling, that is a change to `recall-latency-contract`, not to this file.
UNCEILINGED_SHAPES: frozenset[str] = frozenset({"browse"})


def check(report: dict[str, Any]) -> None:
    """Fail on any breach, naming it with the number actually measured."""
    failures: list[str] = []

    limit = int(report.get("limit", -1))
    if limit != DEFAULT_LIMIT:
        failures.append(
            f"the series ran at limit={limit}, and the ceilings are defined at "
            f"{DEFAULT_LIMIT}; rerank scales with the candidate count, so this is "
            "not a verdict against them"
        )

    pages = report.get("pages")
    if pages is None:
        failures.append(
            "the corpus size could not be derived from the cell that was measured, so "
            f"the contract's premise of {MIN_CORPUS_PAGES} governed pages is unverified"
        )
    elif int(pages) < MIN_CORPUS_PAGES:
        failures.append(
            f"the cell holds {int(pages)} governed pages, below the {MIN_CORPUS_PAGES} "
            "pages the contract measures on"
        )

    for shape in SERIES_SHAPES:
        series = report["series"][shape]
        p50 = series.get("p50_ms")
        p95 = series.get("p95_ms")
        if p50 is None or p95 is None or int(series.get("unknown_timings", 0)):
            failures.append(f"{shape} has unknown timing observations")
        ceiling = (
            KEYWORD_P95_MS if shape == "keyword" else HYBRID_P95_MS if shape != "browse" else None
        )
        if ceiling is not None and p95 is not None and float(p95) > ceiling:
            failures.append(f"{shape} p95={float(p95):.1f}ms > {ceiling:.1f}ms")
        _report_targets(series, shape)

        if int(series["samples"]) < SAMPLES_PER_SERIES - MAX_WARMING_SAMPLES:
            failures.append(f"{shape} produced insufficient measured samples")

        warming = int(series["warming"])
        if warming > MAX_WARMING_SAMPLES:
            failures.append(
                f"{shape} returned {warming} warming outcomes, above the "
                f"{MAX_WARMING_SAMPLES} the contract allows"
            )

        # EVERY shape, not the filtered one. An empty result set is fast for
        # reasons that have nothing to do with the read path — no rerank, no
        # fusion — and the series most likely to return nothing is `keyword`,
        # which is pure lexical matching and carries the tightest ceiling the
        # contract states. Measured at 0 hits in 47.3 ms against the fixture
        # vault, which the gate certified as 2.5x inside 120 ms.
        #
        # The median, not the sum: the gate certifies a p50, so one hit across
        # thirty samples clearing the guard would leave the very percentile
        # being certified an empty recall.
        if int(series.get("hits_p50") or 0) < 1:
            failures.append(
                f"the {shape} series' median sample returned no hits ("
                f"{int(series.get('hits', 0))} across the whole run): its percentiles "
                "measure empty-result recalls and not a read path"
            )

        # Measured against the live 0.69.0 cell on 2026-09-03: 24 stages, and
        # NONE of them carried a `source`, because the stage-source vocabulary
        # ships with this change and that cell predates it. Read naively, the
        # walk check below then finds no walker stage, reports no walk, and
        # passes — silence dressed as proof. A gate that cannot read its own
        # sentinel has to say so, so an empty source map fails closed.
        stage_sources = series.get("stage_sources", {})
        if not stage_sources:
            failures.append(
                f"{shape} reported no stage sources at all: the walk sentinel cannot be "
                "read on this cell, so zero walks is silence and not proof"
            )
        for stage, source in sorted(stage_sources.items()):
            if _is_walker(stage) and source not in PERMITTED_SOURCES:
                failures.append(f"{shape} stage {stage} reported source {source}: a walk")

        if shape == "filtered_hybrid":
            eligibility = series.get("eligibility_ms")
            if eligibility is None:
                failures.append(
                    f"{shape} reported no filter_eligibility duration at all: the "
                    "stage-budget premise cannot be read on this cell, and an "
                    "absent stage is not a stage that ran in no time"
                )
            # Matched the way `_is_walker` and the `eligibility_ms` collector
            # already match, because Lane 1 moves stages under qualified names
            # and a bare-key lookup stops watching on the day one is renamed:
            # `filters.filter_eligibility: declined` passed a bare lookup while
            # `filter_eligibility: declined` correctly failed.
            #
            # Absent fails closed. `FILTER_PROJECTS` emptied as the single
            # mutation produced a recall with no `filter_eligibility` span at
            # all — `find.py:1670` opens it only when `filter_plan.root is not
            # None` — and the run PASSED: the hits guard cannot catch it either,
            # because an unfiltered recall returns plenty of hits. The precedent
            # is the empty-source-map rule above, for the same reason.
            eligibility_sources = {
                stage: source
                for stage, source in stage_sources.items()
                if stage.rsplit(".", 1)[-1] == "filter_eligibility"
            }
            if not eligibility_sources:
                failures.append(
                    f"{shape} reported no filter_eligibility stage: the series did not "
                    "carry a structured filter the index was asked to answer, so this "
                    "is not a measurement of a filtered read path"
                )
            for stage, source in sorted(eligibility_sources.items()):
                if source not in ("index", "cache"):
                    failures.append(f"{shape} stage {stage} answered from {source}, not an index")

    if failures:
        raise SystemExit("recall latency gate failed: " + "; ".join(failures))


# --- Transports -------------------------------------------------------------


class ServedReadFailure(SystemExit):
    """Keep legacy CLI exits while supplying content-free evidence codes."""

    def __init__(self, reason: str, message: str, *, refused: bool = False):
        super().__init__(message)
        self.reason, self.refused = reason, refused


class ServedTransport:
    """The live cell, over the REST facade.

    `/api/ask_memory` authenticates with `Authorization: Bearer
    $EXOMEM_REST_API_KEY` (`server_rest.py::_rest_principal`). The
    streamable-HTTP MCP endpoint on the same port is behind the GitHub OAuth
    proxy and rejects that key with a 401, which is correct and is not a bug to
    work around: this facade is the read surface.

    The key is read from the environment and never written to the report, the
    log, or an exception message.
    """

    name = "served"

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 120.0) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self.pages: int | None = None

    def _post(self, payload: dict[str, Any], *, command: str = "ask_memory") -> dict[str, Any]:
        from http.client import HTTPException
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        request = Request(
            f"{self._base}/api/{command}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self._key}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                body = json.loads(response.read())
        except (HTTPError, URLError, HTTPException, OSError) as error:
            # The class only. A transport error's message can carry the URL,
            # and the URL carries the key in no case here, but a header echo in
            # a future urllib would.
            reason = ("served-http-error" if isinstance(error, HTTPError) else
                      "served-timeout" if isinstance(error, TimeoutError) else "served-network-error")
            raise ServedReadFailure(
                reason, f"recall latency gate failed: served call failed ({type(error).__name__})"
            ) from error
        except ValueError as error:
            raise ServedReadFailure("served-invalid-response", "recall latency gate failed: invalid response") from error
        if not isinstance(body, dict):
            raise ServedReadFailure("served-invalid-response", "recall latency gate failed: invalid response")
        if not body.get("success"):
            raise ServedReadFailure("served-read-refused", "recall latency gate failed: the cell refused the read",
                                    refused=True)
        if body.get("data") is not None and not isinstance(body["data"], dict):
            raise ServedReadFailure("served-invalid-response", "recall latency gate failed: invalid response")
        return body.get("data") or {}

    def measure_corpus(self) -> int | None:
        """Ask THE MEASURED CELL how many governed pages it holds.

        This used to count `.md` files under the gate process's own
        `EXOMEM_VAULT_PATH` while the series were timed against a cell over
        HTTP. Nothing tied the two together, so the contract's 8,000-page
        premise could be certified from vault A while every latency came from
        cell B — and the artifact printed the number as if it had been
        verified.

        `browse_memory(mode="overview")` is read-only, on the REST surface, and
        reports the counts the cell itself sees. Two calls: the root overview
        names the cell's knowledge-base directory, and the second counts the
        markdown pages under it, which is the scope every series is measured
        at. The whole-vault markdown count would be a superset, and certifying
        a MINIMUM from a superset fails open.

        Returns None when the cell cannot be asked. `check` then refuses the
        premise by name rather than reporting a zero that reads like a small
        cell or an integer that reads like a measurement.
        """
        try:
            root_overview = self._post({"path": "", "mode": "overview"}, command="browse_memory")
            knowledge_base = root_overview.get("kb") or {}
            if not knowledge_base.get("present"):
                return None
            scope_path = str(knowledge_base.get("path") or "").strip()
            if not scope_path:
                return None
            scoped = self._post({"path": scope_path, "mode": "overview"}, command="browse_memory")
            markdown = (scoped.get("totals") or {}).get("markdown")
        except SystemExit:
            # A cell that will not answer the premise is not a cell that failed
            # it; `check` says which of the two happened.
            return None
        if not isinstance(markdown, int):
            return None
        return markdown

    def ask(
        self, *, shape: str, query: str, mode: str, projects: tuple[str, ...], limit: int
    ) -> Sample:
        arguments: dict[str, Any] = {
            "query": query,
            "mode": mode,
            "limit": limit,
            "scope": "kb",
            "detail": "compact",
            "include_timings": True,
        }
        if projects:
            arguments["projects"] = list(projects)
        data = self._post(arguments)
        return _sample_from_envelope(data)

    def __str__(self) -> str:  # pragma: no cover - defensive, keeps the key out
        return "<ServedTransport>"

    __repr__ = __str__


class VaultTransport:
    """The same series in-process, for a CI-shaped run on a small corpus."""

    name = "vault"

    def __init__(self, vault_root: Path) -> None:
        from exomem import vault as vault_module

        self._root = vault_root
        # Counted locally, and here that IS the cell being measured: this
        # transport runs the series in-process against this same vault. The
        # served transport cannot do this and must ask the cell instead.
        self.pages: int | None = sum(1 for _ in vault_module.walk_vault_md(vault_root))

    def warm(self) -> None:
        """Stand up the admission a served process has.

        Delegates to `warmup.warm_retrieval_catalog`, the function the served
        process itself calls, rather than restating an abbreviation of it —
        the reasoning is `scripts/semantic_write_latency.py::_enter_managed_recall`,
        whose warm-up defect (opening the warm window without publishing the
        catalogue proof) this deliberately does not repeat.
        """
        from exomem import lexstore, readiness, warmup

        lexstore.ensure_fresh(self._root)
        readiness.manage_runtime()
        previous = os.environ.get("EXOMEM_EAGER_BOOT")
        os.environ["EXOMEM_EAGER_BOOT"] = "1"
        readiness.begin_warm()
        try:
            warmup.warm_retrieval_catalog(self._root)
        finally:
            readiness.finish_warm()
            if previous is None:
                os.environ.pop("EXOMEM_EAGER_BOOT", None)
            else:
                os.environ["EXOMEM_EAGER_BOOT"] = previous

        admission = readiness.retrieval_admission(self._root)
        if not admission.get("admitted"):
            raise SystemExit(
                "recall latency gate failed: managed recall admission was not granted; "
                "an offline walk is not the capability under test"
            )

    def ask(
        self, *, shape: str, query: str, mode: str, projects: tuple[str, ...], limit: int
    ) -> Sample:
        from exomem import commands

        kwargs: dict[str, Any] = {
            "query": query,
            "mode": mode,
            "limit": limit,
            "scope": "kb",
            "detail": "compact",
            "include_timings": True,
        }
        if projects:
            kwargs["projects"] = list(projects)
        result = commands.op_ask_memory(self._root, **kwargs)
        return _sample_from_envelope(result if isinstance(result, dict) else {})


def _sample_from_envelope(data: Any) -> Sample:
    """Read one closed outcome out of the `include_timings` envelope."""
    if not isinstance(data, dict):
        return Sample(elapsed_ms=None, warming=False)
    if "warming" in data:
        return Sample(elapsed_ms=None, warming=True)
    timings = data.get("timings")
    if not isinstance(timings, dict):
        # Missing diagnostics are unknown, distinct from a typed warming outcome.
        return Sample(elapsed_ms=None, warming=False)
    stages = timings.get("stages") or {}
    sources: dict[str, str] = {}
    milliseconds: dict[str, float] = {}
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            continue
        source = stage.get("source")
        if isinstance(source, str):
            sources[name] = source
        value = stage.get("ms")
        if isinstance(value, (int, float)):
            milliseconds[name] = float(value)
    total = timings.get("total_ms")
    if (
        isinstance(total, bool)
        or not isinstance(total, (int, float))
        or not math.isfinite(total)
        or total < 0
    ):
        # Invalid totals remain unknown; they never enter the percentile as zero.
        return Sample(elapsed_ms=None, warming=False)
    hit_list = data.get("hits")
    return Sample(
        elapsed_ms=float(total),
        warming=False,
        stage_sources=sources,
        stage_ms=milliseconds,
        hits=len(hit_list) if isinstance(hit_list, list) else 0,
    )


# --- Read-only MCP canary ----------------------------------------------------


# --- Reference measurement and same-runner comparison ------------------------

BENCHMARK = ROOT / "benchmarks/recall-latency"
REFERENCE = CONTRACT.references[HYBRID_BAR.reference]
PAIRING = REFERENCE["paired"]
#: The principals whose paired series gate; the paired run samples only these.
PAIRED_PRINCIPALS = tuple(PAIRING["principals"])


class MeasurementRefused(ValueError):
    """A closed failure code; the summary schema owns phase and policy values."""

    def __init__(self, code: str, *, phase: str | None = None):
        super().__init__(code)
        self.code, self.phase = code, phase


def validate_document(document: dict, schema: str, *, directory: Path = BENCHMARK,
                      definition: str | None = None) -> None:
    """Validate stored evidence using the project's maintained JSON Schema engine."""
    from jsonschema import Draft202012Validator, FormatChecker

    shape = json.loads((directory / schema).read_text())
    if definition is not None:
        shape = {"$defs": shape["$defs"], "$ref": f"#/$defs/{definition}"}
    validator = Draft202012Validator(shape, format_checker=FormatChecker())
    validator.validate(document)
    if schema == "summary.schema.json" and definition in (None, "paired_fragment"):
        if any(sample["repeat"] >= PAIRING["repeats"] for sample in document["samples"]):
            raise MeasurementRefused("sample-repeat-outside-contract")


def paired_statistic(ratios: list[float]) -> dict[str, Any]:
    """Student-t interval over independent cases' log median ratios."""
    if any(isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in ratios):
        raise MeasurementRefused("invalid-paired-ratio")
    if len(ratios) < PAIRING["minimum_cases"]:
        return {"state": "insufficient samples", "cases": len(ratios)}
    from scipy.stats import t

    logs = [math.log(value) for value in ratios]
    mean = statistics.fmean(logs)
    half = float(t.ppf((1 + PAIRING["confidence"]) / 2, len(logs) - 1)) * (
        statistics.stdev(logs) / math.sqrt(len(logs))
    )
    return {"state": "measured", "cases": len(ratios), "ratio": math.exp(mean),
            "lower": math.exp(mean - half), "upper": math.exp(mean + half)}


def read_accept(tag: str, path: Path = BENCHMARK / "accept.json") -> tuple[dict | None, str]:
    if not path.exists():
        return None, "absent"
    document = json.loads(path.read_text())
    validate_document(document, "accept.schema.json")
    names = [row["name"] for row in document["series"]]
    if len(set(names)) != len(names) or any(
        row["maximum_ratio"] < PAIRING["margin"] or
        not math.isfinite(row["maximum_ratio"] / PAIRING["accept_rounding"]) or
        not math.isclose(row["maximum_ratio"] / PAIRING["accept_rounding"],
                         round(row["maximum_ratio"] / PAIRING["accept_rounding"]))
        for row in document["series"]
    ):
        raise MeasurementRefused("accept-record-series-or-rounding-invalid")
    if document["tag"] != tag:
        return None, "expired"
    return document, "current"


def compare_pairs(samples: list[dict], *, tag: str, accept: dict | None = None) -> list[dict]:
    """Compare closed samples; one broken repeat invalidates its whole series."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for sample in samples:
        grouped[sample["principal"], sample["series"]].append(sample)
    reports = []
    for (principal_name, series_name), series_samples in sorted(grouped.items()):
        cases: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        warming = sum(sample.get("state") == "warming" for sample in series_samples)
        broken = warming > MAX_WARMING_SAMPLES
        for sample in series_samples:
            cases[sample["case"]][sample["arm"]].append(sample)
            value = sample.get("elapsed_ms")
            if (sample.get("state") != "warming" and
                (sample.get("state") != "ok" or isinstance(value, bool)
                or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0)):
                broken = True
        ratios, stage_ratios = [], defaultdict(list)
        base_stages, head_stages = set(), set()
        for arms in cases.values():
            if set(arms) != {"base", "head"} or any(
                len(rows) != PAIRING["repeats"]
                or {row["repeat"] for row in rows} != set(range(PAIRING["repeats"]))
                for rows in arms.values()
            ):
                broken = True
                continue
            if any(row.get("state") != "ok" or not _positive(row.get("elapsed_ms"))
                   for rows in arms.values() for row in rows):
                continue
            ratios.append(statistics.median(row["elapsed_ms"] for row in arms["head"]) /
                          statistics.median(row["elapsed_ms"] for row in arms["base"]))
            base_stages.update(key for row in arms["base"] for key in row["stages"])
            head_stages.update(key for row in arms["head"] for key in row["stages"])
            for stage in base_stages | head_stages:
                stage_values = {
                    arm: [row["stages"].get(stage, {}).get("ms") for row in rows]
                    for arm, rows in arms.items()
                }
                if all(_positive(value) for values in stage_values.values() for value in values):
                    stage_ratios[stage].append(
                        statistics.median(stage_values["head"]) /
                        statistics.median(stage_values["base"])
                    )
        report = {"principal": principal_name, "series": series_name,
                  **paired_statistic(ratios), "gating": principal_name in PAIRED_PRINCIPALS}
        report["state"] = "failed" if broken else (
            "insufficient samples" if "lower" not in report else
            "failed" if report["lower"] > PAIRING["margin"] else "passed"
        )
        if report["state"] == "failed" and not broken and accept and accept["tag"] == tag:
            allowance = next((item for item in accept["series"] if item["name"] == series_name), None)
            if allowance and report["lower"] <= allowance["maximum_ratio"]:
                report.update(state="accepted", reason=accept["reason"])
        report["unknown_samples"] = sum(
            row.get("state") != "warming" and
            (row.get("state") != "ok" or not _positive(row.get("elapsed_ms")))
            for row in series_samples
        )
        report["warming"] = warming
        for arm in ("base", "head"):
            arm_samples = [row for row in series_samples if row["arm"] == arm]
            elapsed = [row["elapsed_ms"] for row in arm_samples
                       if row.get("state") == "ok" and _positive(row.get("elapsed_ms"))]
            loads = [row["load"] for row in arm_samples if isinstance(row.get("load"), (int, float))]
            report[arm + "_p50_ms"] = _percentile(elapsed, 0.5)
            report[arm + "_p95_ms"] = _percentile(elapsed, 0.95)
            report[arm + "_load_mean"] = statistics.fmean(loads) if loads else None
            report[arm + "_load_max"] = max(loads) if loads else None
        report["stages"] = {}
        for stage in sorted(set(REFERENCE["stage_rows"]) | base_stages | head_stages):
            comparison = ({"state": "not comparable", "cases": 0}
                          if stage not in base_stages or stage not in head_stages else
                          paired_statistic(stage_ratios[stage]))
            for arm in ("base", "head"):
                arm_samples = [row for row in series_samples if row["arm"] == arm]
                cpu = [row["stages"].get(stage, {}).get("cpu_ms") for row in arm_samples]
                cpu = [value for value in cpu if isinstance(value, (int, float)) and math.isfinite(value)]
                comparison[arm + "_cpu_p95_ms"] = _percentile(cpu, 0.95)
                paths = [row["critical_path"] for row in arm_samples if row.get("critical_path") is not None]
                comparison[arm + "_critical_path_samples"] = len(paths)
                comparison[arm + "_critical_path_share"] = (
                    sum(stage in path for path in paths) / len(paths) if paths else None)
            report["stages"][stage] = comparison
        reports.append(report)
    return reports


def _positive(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def summarize_absolute(samples: list[dict], *, environment: dict, profile: dict,
                       live: bool = False, boundary: str | None = None) -> tuple[str, list[dict]]:
    """Keep live contention separate from reference process costs."""
    summaries = []
    for principal_name, series_name in sorted({(s["principal"], s["series"]) for s in samples}):
        selected = [s for s in samples if (s["principal"], s["series"]) == (principal_name, series_name)]
        known = [s for s in selected if s["state"] == "ok" and _positive(s.get("elapsed_ms"))]
        contended = [s for s in known if live and s.get("runqueue_ms") is not None
                     and s["runqueue_ms"] > max(5.0, 0.1 * s["elapsed_ms"])]
        retained = [s for s in known if s not in contended]
        warming = sum(s["state"] == "warming" for s in selected)
        unknown = len(selected) - len(known) - warming
        unknown_contention = sum(s.get("runqueue_ms") is None for s in known) if live else 0
        refused = (bool(unknown_contention) or
                   len(contended) > len(selected) * REFERENCE["max_contended_fraction"])
        bar = KEYWORD_BAR if series_name == "keyword" else HYBRID_BAR
        comparable = boundary is None or boundary == bar.boundary.id
        target = CONTRACT.bar("governed-keyword-p95-target" if series_name == "keyword"
                              else "governed-hybrid-p95-target")
        values = [s["elapsed_ms"] for s in retained]
        p50, p95 = _percentile(values, 0.5), _percentile(values, 0.95)
        state = ("refused" if refused else "failed" if unknown
                 or warming > MAX_WARMING_SAMPLES or len(values) < bar.min_samples
                 else "not measured" if not comparable
                 else "passed" if p95 <= bar.threshold_ms else "failed")
        stages = {}
        observed_rows = {row for sample in retained for row in sample["stages"]}
        paths = [s["critical_path"] for s in retained if s.get("critical_path") is not None]
        policy = REFERENCE["stage_diagnostics"]
        for row in sorted(set(REFERENCE["stage_rows"]) | observed_rows):
            observations = [s["stages"][row] for s in retained if row in s["stages"]]
            wall = [value["ms"] for value in observations]
            cpu = [value["cpu_ms"] for value in observations if value["cpu_ms"] is not None]
            budget = CONTRACT.bar(row) if row in REFERENCE["stage_rows"] else None
            sufficient = len(wall) >= (budget.min_samples if budget else REFERENCE["stage_min"])
            stage_p95 = _percentile(wall, 0.95)
            # Target-only rows and the multiplier are fixed by the contract.
            calibrated = budget is not None and row not in policy["target_only"]
            stages[row] = {"state": "not run" if not wall else
                           "insufficient samples" if not sufficient else
                           "unbudgeted" if budget is None else
                           "failed" if calibrated and stage_p95 > policy["twice_budget_multiplier"] * budget.threshold_ms
                           else "passed",
                           "samples": len(wall), "p50_ms": _percentile(wall, 0.5), "p95_ms": stage_p95,
                           "cpu_p50_ms": _percentile(cpu, 0.5), "cpu_p95_ms": _percentile(cpu, 0.95),
                           "budget_ms": budget.threshold_ms if budget else None,
                           "target_met": stage_p95 <= budget.threshold_ms if budget and sufficient else None,
                           "twice_budget_applied": calibrated and sufficient,
                           "critical_path_samples": len(paths),
                           "critical_path_share": (sum(row in path for path in paths) / len(paths) if paths else None)}
        floor_bar = CONTRACT.bar(policy["floor_bar"])
        encode = stages[policy["floor_bar"]]
        eligible_floor = (not live and not refused and environment["sqlite_version"] in profile["sqlite_versions"]
                          and len(environment.get("cpus", [])) == REFERENCE["reference_cpus"]
                          and encode["samples"] >= floor_bar.min_samples
                          and encode["p95_ms"] > floor_bar.threshold_ms)
        floor = {"state": "measured" if eligible_floor else "not measured",
                 "p95_ms": encode["p95_ms"] if eligible_floor else None,
                 "budget_ms": floor_bar.threshold_ms, "samples": encode["samples"],
                 "source": "retained-reference-series" if eligible_floor else None}
        loads = [s["load"] for s in selected]
        summary = {"principal": principal_name, "series": series_name,
                   "state": state, "samples": len(retained), "warming": warming,
                   "unknown_samples": unknown, "unknown_contention": unknown_contention,
                   "contended": len(contended), "p50_ms": p50, "p95_ms": p95,
                   "p95_including_contention_ms": _percentile([s["elapsed_ms"] for s in known], 0.95),
                   "load_mean": statistics.fmean(loads), "load_max": max(loads),
                   "ceiling_bar": bar.id, "p95_target_bar": target.id,
                   "p50_target_bar": "governed-p50-target",
                   "p50_target_met": p50 <= HYBRID_P50_MS if comparable and p50 is not None and not refused else None,
                   "p95_target_met": p95 <= target.threshold_ms if comparable and p95 is not None and not refused else None,
                   "ceiling_met": p95 <= bar.threshold_ms if comparable and p95 is not None and not refused else None,
                   "encode_floor": floor, "stages": stages}
        if refused:
            summary.update(p50_ms=None, p95_ms=None)
        summaries.append(summary)
    owner_states = [row["state"] for row in summaries if row["principal"] == "owner"]
    if environment["sqlite_version"] not in profile["sqlite_versions"]:
        for row in summaries:
            row.update(state="not measured", ceiling_met=None)
        return "not measured", summaries
    return ("refused" if "refused" in owner_states else
            "failed" if "failed" in owner_states or not owner_states else
            "not measured" if "not measured" in owner_states else "passed"), summaries


def absolute_cli(output: Path, *, quiescence_bound: float) -> int:
    """Record absolute evidence; its state never controls the command's exit code."""
    import tempfile

    report = {"schema_version": 1, "mode": "absolute", "state": "not measured",
              "contract_revision": CONTRACT.revision, "series": [], "samples": []}
    worker = None
    try:
        profile = json.loads((BENCHMARK / "profile.json").read_text())
        cases, digest = reference_cases(profile)
        first, final, waited = await_quiescence(
            one_minute_load, bound_seconds=quiescence_bound, sleep=time.sleep,
        )
        report["load"] = {"first": first, "final": final, "waited_seconds": waited}
        if final > MAX_LOAD_AVERAGE:
            report.update(state="refused", reason="load-above-reference")
        else:
            with tempfile.TemporaryDirectory(prefix="exomem-recall-latency-") as temporary:
                scratch = Path(temporary)
                cpus = sorted(os.sched_getaffinity(0))[:REFERENCE["reference_cpus"]]
                if len(cpus) != REFERENCE["reference_cpus"]:
                    raise MeasurementRefused("two-cpu-reference-unavailable")
                worker = _Worker(ROOT, scratch / "vault", scratch / "state", cpus, profile)
                try:
                    for principal_name in ("owner", "admitted"):
                        # First calls are excluded as warm-up, with real query encodes.
                        for case in {c["series"]: c for c in reversed(cases)}.values():
                            worker.exchange({**case, "principal": principal_name,
                                             "rows": REFERENCE["stage_rows"]})
                        for case in cases:
                            sample = worker.exchange({**case, "principal": principal_name,
                                                      "rows": REFERENCE["stage_rows"]})
                            report["samples"].append({"case": case["id"], "series": case["series"],
                                "principal": principal_name, "arm": "head", "repeat": 0,
                                "load": one_minute_load(), **sample})
                    report["environment"] = {"head": worker.environment}
                    report["source_digest"] = digest
                    report["unmeasured"] = profile["unmeasured"]
                    report["state"], report["series"] = summarize_absolute(
                        report["samples"], environment=worker.environment, profile=profile,
                    )
                finally:
                    worker.close()
                    worker = None
    except (ValueError, OSError) as error:
        report.update(state="refused" if isinstance(error, MeasurementRefused) else "not measured",
                      reason=error.code if isinstance(error, MeasurementRefused) else type(error).__name__,
                      phase=(error.phase or "reference-preparation") if isinstance(error, MeasurementRefused)
                      else "reference-preparation")
    finally:
        if worker is not None:
            worker.close()
    validate_document(report, "summary.schema.json")
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"state": report["state"], "artifact": output.name}))
    return 0


def reference_cases(profile: dict, directory: Path = BENCHMARK) -> tuple[list[dict], str]:
    from synth_vault import reference_source

    try:
        _, digest = reference_source(profile, directory)
    except ValueError as error:
        raise MeasurementRefused("reference-prose-unavailable") from error
    specifications = json.loads((directory / profile["case_file"]).read_text())
    cases = []
    for specification in specifications:
        cases.append({"id": specification["id"], "series": specification["series"],
                      "query": specification["query"], "arguments": specification["arguments"]})
    for series in {case["series"] for case in cases}:
        selected = [case for case in cases if case["series"] == series]
        if len(selected) < REFERENCE["queries_min"]:
            raise MeasurementRefused("reference-case-count-below-profile")
        lengths = [len(case["query"].split()) for case in selected]
        # These word bands are the measurement contract's closed sampling bins.
        if ([sum(1 <= n <= 3 for n in lengths), sum(4 <= n <= 9 for n in lengths),
             sum(15 <= n <= 40 for n in lengths)] != [10, 20, 10]):
            raise MeasurementRefused("reference-query-bands-invalid")
    return cases, digest


def _stored_corpus(vault_root: Path, index: Any, rels: list[str], profile: dict) -> dict:
    """Count actual stored rows and check deterministic samples against source truth."""
    import hashlib
    import sqlite3
    import string

    import numpy as np
    from synth_vault import reference_source

    from exomem import find_results, lexstore, vault

    with sqlite3.connect(lexstore.lexical_path(vault_root)) as conn:
        pages = conn.execute("SELECT count(*) FROM pages WHERE in_kb=1 AND is_nav=0").fetchone()[0]
        units, unit_pages = conn.execute(
            "SELECT count(*), count(DISTINCT parent_path) FROM semantic_units WHERE in_kb=1"
        ).fetchone()
        for rel in (rels[0], rels[len(rels) // 2], rels[-1]):
            digest = conn.execute("SELECT content_hash FROM pages WHERE path=?", (rel,)).fetchone()
            if digest is None or digest[0] != hashlib.sha256((vault_root / rel).read_bytes()).hexdigest():
                raise MeasurementRefused("stored-page-source-mismatch")
    with sqlite3.connect(index.path) as conn:
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        vector_units = conn.execute("SELECT count(*) FROM semantic_unit_vectors").fetchone()[0]
        for blob, in conn.execute("SELECT vector FROM chunks ORDER BY file_path, chunk_idx LIMIT 3"):
            vector = np.frombuffer(blob, dtype=np.float32)
            if not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1.0):
                raise MeasurementRefused("stored-vector-invalid")
    source_words, _digest = reference_source(profile, BENCHMARK)
    source_terms = {word.strip(string.punctuation).casefold() for word in source_words}
    sizes, links, transcript_sizes = [], [], []
    terms: set[str] = set()
    corpus_digest = hashlib.sha256()
    for rel in sorted(rels):
        raw = (vault_root / rel).read_bytes()
        path_bytes = rel.encode("utf-8")
        corpus_digest.update(len(path_bytes).to_bytes(8, "big"))
        corpus_digest.update(path_bytes)
        corpus_digest.update(hashlib.sha256(raw).digest())
        sizes.append(len(raw))
        frontmatter, body, _ = vault.parse_frontmatter(raw.decode("utf-8"))
        links.append(len(find_results.wikilink_targets(body)))
        terms.update(word.strip(string.punctuation).casefold() for word in body.split())
        if frontmatter.get("media_type"):
            transcript_sizes.append(len(raw))
    vocabulary = len((terms & source_terms) - {""})
    median = statistics.median(sizes)
    if (pages < REFERENCE["pages_min"] or chunks < REFERENCE["chunks_min"]
        or units < REFERENCE["units_min"] or unit_pages < pages * profile["unit_page_fraction"]
        or vector_units < units or vocabulary < profile["vocabulary_min"]
        or min(links) < profile["links_min"] or max(links) > profile["links_max"]
        or len(transcript_sizes) < pages * profile["transcript_fraction"]
        or any(not profile["transcript_bytes"][0] <= size <= profile["transcript_bytes"][1]
               for size in transcript_sizes)
        or not profile["median_page_bytes"][0] <= median <= profile["median_page_bytes"][1]):
        raise MeasurementRefused("stored-corpus-below-reference-profile")
    return {"pages": pages, "chunks": chunks, "units": units, "unit_pages": unit_pages,
            "vector_units": vector_units, "median_page_bytes": median, "spot_checks": 6,
            "vocabulary_terms": vocabulary, "wikilinks_min": min(links), "wikilinks_max": max(links),
            "transcript_pages": len(transcript_sizes), "content_digest": corpus_digest.hexdigest()}


def _prepare_worker(vault_root: Path, startup: dict) -> dict:
    import hashlib
    import sqlite3

    import numpy as np
    from managed_recall import enter_managed_recall
    from synth_vault import gen_reference_vault, reference_source

    from exomem import due_state, embeddings, freshness, recall_space
    from exomem.governance import principal

    profile = startup["profile"]
    words, _digest = reference_source(profile, BENCHMARK)
    model = embeddings.get_model()
    artifact = getattr(model, "profile", None)
    if artifact is None or not artifact.artifact_digest:
        raise MeasurementRefused("served-encoder-artifact-unknown")
    probe = embeddings.embed_texts(["reference encoder warmup"], is_query=True)
    identity = recall_space.current_identity(int(probe.shape[1]))
    if identity.fingerprint != artifact.fingerprint():
        raise MeasurementRefused("resident-encoder-identity-mismatch")
    with principal.library_scope():
        manifest = vault_root / "reference-corpus.json"
        if manifest.exists():
            rels = json.loads(manifest.read_text())
        else:
            rels = gen_reference_vault(vault_root, profile, words)
            manifest.write_text(json.dumps(rels))
        original = embeddings.embed_texts

        def seeded(texts, *, is_query=False):
            if is_query:
                raise MeasurementRefused("query-encoder-substituted-during-build")
            vectors = []
            for text in texts:
                seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "little")
                vector = np.random.default_rng(seed).standard_normal(identity.dim).astype(np.float32)
                vectors.append(vector / np.linalg.norm(vector))
            return np.asarray(vectors, dtype=np.float32).reshape((-1, identity.dim))

        try:
            embeddings.embed_texts = seeded
            index = embeddings.get_embedding_index(vault_root)
            index.rebuild_all()
        finally:
            embeddings.embed_texts = original
        if index.identity != identity:
            raise MeasurementRefused("seeded-index-identity-mismatch")
        from exomem import lexstore, memory_refs

        lexstore.ensure_fresh(vault_root)
        # Managed serialization requires its maintained reference sidecar before sampling.
        memory_refs.ReferenceIndex(vault_root).rebuild_all()
        due_state.reconcile(vault_root)
        if due_state.load(vault_root) is None:
            # A failed best-effort save must not label a cold benchmark fixture warm.
            raise MeasurementRefused("advisory-projection-unavailable")
        corpus = _stored_corpus(vault_root, index, rels, profile)
        for scope in ("vault", "kb"):
            freshness.seed(vault_root, scope, (
                (str(vault_root / rel), freshness.stat_signature(vault_root / rel)) for rel in rels
            ))
        enter_managed_recall(vault_root, full_startup=True)
        # Normal startup may leave this disposable matrix cold; preparation owns the wait.
        index.all_vectors()
    return {"corpus": corpus, "encoder_digest": artifact.artifact_digest,
            "encoder_identity": artifact.fingerprint(), "sqlite_version": sqlite3.sqlite_version,
            "python_version": sys.version.split()[0], "cpus": sorted(os.sched_getaffinity(0))}


def _failed_stage(result: Any, collectors=()) -> bool:
    """Read the producers' explicit degraded and stage error fields."""
    stages = (result.get("timings") or {}).get("stages", {}) if isinstance(result, dict) else {}
    return bool(isinstance(result, dict) and result.get("degraded")) or any(
        stage.get("error") for stage in stages.values()
    ) or any(stage.get("error") for collector in collectors for stage in collector.stages.values())


def _worker_sample(ask_memory: Callable[..., Any], request: dict) -> dict:
    from recall_latency_adapter import clear_results, instrument

    from exomem.governance import principal

    clear_results()
    caller = (principal.owner_principal(surface="library") if request["principal"] == "owner"
              else principal.RequestPrincipal(audience_id="benchmark-admitted", surface="mcp"))
    with principal.request_scope(caller), principal.invocation_scope():
        with instrument() as (trace, collectors):
            started = time.perf_counter_ns()
            result = ask_memory(
                query=request["query"], include_timings=True,
                limit=DEFAULT_LIMIT, rerank=False, **request["arguments"],
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1e6
    accounting = trace.result(request["rows"])
    hits = result.get("hits", []) if isinstance(result, dict) else result
    failed_stage = _failed_stage(result, collectors)
    state = ("unknown" if failed_stage else
             "warming" if isinstance(result, dict) and "warming" in result else
             "unknown" if not collectors or not trace.intervals else
             "empty" if not isinstance(hits, list) or not hits else "ok")
    return {"elapsed_ms": elapsed_ms, "state": state, **{
        key: value for key, value in accounting.items() if key != "total_ms"
    }}


def worker_main(vault_root: Path) -> int:
    """Persistent JSON-lines worker; never emit product output or raw exceptions."""
    phase = "worker-startup"
    preparation_started = False
    try:
        startup = json.loads(sys.stdin.readline())
        runtime = _runtime_provenance(_boot_args.worker_source, startup)
        preparation_started = True
        with redirect_stdout(sys.stderr):
            environment = _prepare_worker(vault_root, startup)
            from exomem import command_surface, commands

            command = command_surface.resolve_command("ask_memory", commands.PRODUCT_COMMANDS)
            ask_memory = command_surface.bind_vault(command.leaf, vault_root, command=command)
        print(json.dumps({"state": "ready", "runtime_prefix": sys.prefix,
                          "environment": {**environment, **runtime}}), flush=True)
        phase = "worker-request"
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("stop"):
                break
            with redirect_stdout(sys.stderr):
                sample = _worker_sample(ask_memory, request)
            print(json.dumps(sample, allow_nan=False), flush=True)
    except Exception as error:  # noqa: BLE001 - raw errors can contain corpus content.
        response = {"state": "worker-error", "error_class": type(error).__name__, "phase": phase}
        if isinstance(error, MeasurementRefused):
            response.update(reason=error.code, phase=error.phase or phase)
        elif getattr(error, "code", None) == "RETRIEVAL_INDEX_WARMING":
            # The product validates this public site; raw exception messages stay private.
            response["reason"] = "benchmark-warming-site-" + error.site
        print(json.dumps(response), flush=True)
        return 1
    finally:
        if preparation_started:
            from exomem import readiness

            readiness.unmanage_runtime()
    return 0


def _runtime_lock_digest(source: Path, *, phase: str | None = None) -> str:
    import hashlib

    try:
        return hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest()
    except OSError as error:
        raise MeasurementRefused("runtime-lock-unavailable", phase=phase) from error


def _runtime_provenance(source: Path, startup: dict, *, prefix: str | None = None) -> dict:
    """Check the private interpreter prefix before accepting public provenance."""
    if Path(sys.prefix if prefix is None else prefix).resolve() != Path(startup["runtime_prefix"]).resolve():
        raise MeasurementRefused("runtime-prefix-mismatch", phase="worker-startup")
    digest = _runtime_lock_digest(source, phase="worker-startup")
    if digest != startup["runtime_lock_digest"]:
        raise MeasurementRefused("runtime-lock-changed", phase="worker-startup")
    return {"runtime_lock_digest": digest, "runtime_policy": startup["runtime_policy"]}


def _prepare_tag_runtime(base: Path, scratch: Path) -> Path:
    """Read the immutable tag lock into one owned environment, outside timers."""
    import subprocess

    phase = "tag-runtime-preparation"
    try:
        digest = _runtime_lock_digest(base)
        runtime = (scratch / "tag-runtime").absolute()
        # UV_ is uv's operation namespace; this fixed recipe retains only its cache location.
        environment = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
        if "UV_CACHE_DIR" in os.environ:
            environment["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
        environment.pop("VIRTUAL_ENV", None)
        environment["UV_PROJECT_ENVIRONMENT"] = str(runtime)
        # Frozen reads use the lock's sources; historical writer settings do not apply.
        result = subprocess.run(
            ["uv", "--no-config", "sync", "--project", str(base.absolute()),
             "--frozen", "--no-default-groups", "--extra", "embeddings-onnx",
             "--no-python-downloads", "--python", sys.executable], cwd=base, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if _runtime_lock_digest(base) != digest:
            raise MeasurementRefused("tag-runtime-lock-changed", phase=phase)
        if result.returncode:
            raise MeasurementRefused("tag-runtime-install-failed", phase=phase)
        python = runtime / "bin" / "python"
        if not python.is_file():
            raise MeasurementRefused("tag-runtime-python-unavailable", phase=phase)
        return python
    except MeasurementRefused as error:
        raise MeasurementRefused(error.code, phase=phase) from error
    except OSError as error:
        raise MeasurementRefused("tag-runtime-install-failed", phase=phase) from error


class _Worker:
    """One owned child; suspend its idle native pool outside the timed call."""

    def __init__(self, source: Path, vault: Path, state: Path, cpus: list[int], profile: dict,
                 *, python: Path | str | None = None, runtime_prefix: Path | None = None,
                 runtime_policy: str = "parent-benchmark"):
        import subprocess

        startup = {"profile": profile, "runtime_lock_digest": _runtime_lock_digest(source, phase="worker-startup"),
                   "runtime_prefix": str(runtime_prefix if runtime_prefix is not None else sys.prefix),
                   "runtime_policy": runtime_policy}
        # Only declared cache and resource settings cross into disposable workers.
        environment = {key: os.environ[key] for key in profile["worker_environment"] if key in os.environ}
        environment.update(EXOMEM_STATE_ROOT=str(state), EXOMEM_VAULT_PATH=str(vault),
                           # A paused peer must not retain this arm's policy or mutation locks.
                           EXOMEM_WRITER_LEASE_STATE_DIR=str(state / "writer-lease"),
                           EXOMEM_EMBED_BACKEND="onnx", EXOMEM_DISABLE_RANKING="1",
                           EXOMEM_DISABLE_CLIP="1", EXOMEM_DISABLE_MEDIA_EXTRACTION="1")
        environment.pop("EXOMEM_DISABLE_EMBEDDINGS", None)
        environment.pop("KB_MCP_DISABLE_EMBEDDINGS", None)
        try:
            self.process = subprocess.Popen(
                [str(python if python is not None else sys.executable), "-u", str(Path(__file__).resolve()), "--worker-source", str(source),
                 "--worker-cpus", json.dumps(cpus), "--worker-vault", str(vault)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=environment, text=True,
            )
        except OSError as error:
            raise MeasurementRefused("benchmark-worker-launch-failed", phase="worker-startup") from error
        self.paused = False
        try:
            response = self.exchange(startup, phase="worker-startup")
            if not isinstance(response.get("environment"), dict) or not isinstance(response.get("runtime_prefix"), str):
                raise MeasurementRefused("benchmark-worker-startup-invalid", phase="worker-startup")
            self.environment = {**response["environment"],
                                **_runtime_provenance(source, startup, prefix=response["runtime_prefix"])}
        except BaseException:
            self.close()
            raise
        self.pause()

    def exchange(self, request: dict, *, phase: str = "worker-request") -> dict:
        self.resume()
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        except OSError as error:
            raise MeasurementRefused("benchmark-worker-pipe-failed", phase=phase) from error
        if not line:
            raise MeasurementRefused("benchmark-worker-ended", phase=phase)
        try:
            response = json.loads(line)
        except ValueError as error:
            raise MeasurementRefused("benchmark-worker-invalid-response", phase=phase) from error
        if not isinstance(response, dict):
            raise MeasurementRefused("benchmark-worker-invalid-response", phase=phase)
        if response.get("state") == "worker-error":
            reason = response.get("reason") or "benchmark-worker-failed-" + response["error_class"]
            raise MeasurementRefused(reason, phase=response.get("phase", phase))
        return response

    def pause(self) -> None:
        import signal

        if not self.paused and self.process.poll() is None:
            self.process.send_signal(signal.SIGSTOP)
            # waitpid confirms the stop before the other arm starts its timer.
            os.waitpid(self.process.pid, os.WUNTRACED)
            self.paused = True

    def resume(self) -> None:
        import signal

        if self.paused:
            self.process.send_signal(signal.SIGCONT)
            self.paused = False

    def close(self) -> None:
        import subprocess

        self.resume()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def _git_bytes(*arguments: str) -> bytes:
    import subprocess

    return subprocess.check_output(["git", *arguments], cwd=ROOT, stderr=subprocess.DEVNULL)


def _document_digest(value: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _verify_pinned_base(tag: str, commit: str) -> None:
    if _git_bytes("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").decode().strip() != commit:
        raise MeasurementRefused("pinned-release-tag-mismatch")


def _paired_identity(profile: dict, cases: list[dict], *, tag: str, base_commit: str) -> dict:
    return {"tag": tag, "base_commit": base_commit,
            "head_commit": _git_bytes("rev-parse", "HEAD").decode().strip(),
            "contract_revision": CONTRACT.revision,
            "contract_digest": _document_digest(CONTRACT.model_dump(mode="json")),
            "profile_digest": _document_digest(profile), "cases_digest": _document_digest(cases),
            "source_digest": profile["source"]["sha256"]}


def _case_partition(cases: list[dict], shard: int, shards: int) -> list[tuple[int, dict]]:
    if not 1 <= shard <= shards <= len(cases):
        raise MeasurementRefused("paired-shard-outside-case-range")
    return list(enumerate(cases))[len(cases) * (shard - 1) // shards:len(cases) * shard // shards]


def _paired_report(samples: list[dict], *, tag: str, profile: dict, accept_path: Path) -> dict:
    accept, accept_state = read_accept(tag, accept_path)
    reports = compare_pairs(samples, tag=tag, accept=accept)
    return {"schema_version": 1, "mode": "paired", "tag": tag,
            "state": "failed" if not any(row["gating"] for row in reports) or any(
                row["gating"] and row["state"] not in ("passed", "accepted") for row in reports
            ) else "passed",
            "contract_revision": CONTRACT.revision, "accept_record": accept_state,
            "source_digest": profile["source"]["sha256"], "unmeasured": profile["unmeasured"],
            "series": reports, "samples": samples}


def aggregate_paired(fragments: list[dict], *, series: str, tag: str, base_commit: str,
                     producer_outcome: str | None,
                     accept_path: Path = BENCHMARK / "accept.json") -> dict:
    """Refuse incomplete or mixed experiments before computing one complete comparison.

    A false mismatch delays the benchmark owner; accepting mixed evidence would
    make the release's statistical verdict unsupported. No manual gate is added.
    """
    import hashlib

    # The Actions matrix result covers current attempts that emitted no raw report.
    if not producer_outcome:
        raise MeasurementRefused("paired-producer-result-unavailable")
    if producer_outcome != "success":
        raise MeasurementRefused("paired-producer-result-not-success")
    profile = json.loads((BENCHMARK / "profile.json").read_text())
    all_cases, _digest = reference_cases(profile)
    cases = [case for case in all_cases if case["series"] == series]
    if not cases:
        raise MeasurementRefused("paired-series-unknown")
    _verify_pinned_base(tag, base_commit)
    expected = _paired_identity(profile, cases, tag=tag, base_commit=base_commit)
    shards = profile["paired_shards"]
    latest, versions = {}, set()
    for fragment in fragments:
        validate_document(fragment, "summary.schema.json", definition="paired_fragment")
        attempt = fragment["run_attempt"]
        if (fragment["series"] != series or fragment["shards"] != shards
            or not 1 <= fragment["shard"] <= shards
            or fragment["run_id"] != os.environ.get("GITHUB_RUN_ID") or attempt is None
            or int(attempt) > int(os.environ["GITHUB_RUN_ATTEMPT"])):
            raise MeasurementRefused("paired-fragment-producer-mismatch")
        version = (fragment["shard"], int(attempt))
        if version in versions:
            raise MeasurementRefused("paired-fragment-version-ambiguous")
        versions.add(version)
        if fragment["shard"] not in latest or int(attempt) > int(latest[fragment["shard"]]["run_attempt"]):
            latest[fragment["shard"]] = fragment
    if set(latest) != set(range(1, shards + 1)):
        raise MeasurementRefused("paired-fragments-incomplete")
    # Select a whole latest attempt, including a refusal; never combine or fall back between versions.
    fragments = [latest[shard] for shard in sorted(latest)]
    locks = {"head": _runtime_lock_digest(ROOT),
             "base": hashlib.sha256(_git_bytes("show", f"{base_commit}:uv.lock")).hexdigest()}
    # These are the worker launch recipes implemented by this benchmark, not authored vocabulary.
    policies = {"head": "parent-benchmark", "base": "frozen-tag-runtime"}
    rows = []
    environments = None
    for fragment in fragments:
        if (fragment["state"] != "collected"
            or any(fragment.get(key) != value for key, value in expected.items())):
            raise MeasurementRefused("paired-fragment-identity-mismatch")
        assigned = _case_partition(cases, fragment["shard"], shards)
        if fragment["case_indices"] != [index for index, _case in assigned]:
            raise MeasurementRefused("paired-fragment-assignment-mismatch")
        expected_tuples = Counter(
            (series, case["id"], principal, repeat, arm)
            for _index, case in assigned for principal in PAIRED_PRINCIPALS
            for repeat in range(PAIRING["repeats"]) for arm in ("base", "head")
        )
        if Counter((row["series"], row["case"], row["principal"], row["repeat"], row["arm"])
                   for row in fragment["samples"]) != expected_tuples:
            raise MeasurementRefused("paired-fragment-samples-incomplete")
        stable = {}
        for arm, environment in fragment["environment"].items():
            if (environment["runtime_lock_digest"] != locks[arm]
                or environment["runtime_policy"] != policies[arm]
                or len(set(environment["cpus"])) != REFERENCE["reference_cpus"]
                or environment["sqlite_version"] not in profile["sqlite_versions"]):
                raise MeasurementRefused("paired-fragment-runtime-mismatch")
            stable[arm] = {key: value for key, value in environment.items() if key != "cpus"}
        # CPU IDs agree within a pair; the schema's other fields agree across runners.
        if (set(fragment["environment"]["head"]["cpus"]) != set(fragment["environment"]["base"]["cpus"])
            or any(stable["head"][key] != stable["base"][key]
                for key in ("corpus", "encoder_digest", "encoder_identity", "sqlite_version"))
            or (environments is not None and stable != environments)):
            raise MeasurementRefused("paired-fragment-environment-mismatch")
        environments = stable
        rows.extend(fragment["samples"])
    order = [(series, case["id"], principal, repeat, arm)
             for index, case in enumerate(cases) for principal in PAIRED_PRINCIPALS
             for repeat in range(PAIRING["repeats"])
             for arm in (("base", "head") if index % 2 == 0 else ("head", "base"))]
    if len(set(order)) != len(order):
        raise MeasurementRefused("paired-case-identities-duplicate")
    by_tuple = {(row["series"], row["case"], row["principal"], row["repeat"], row["arm"]): row
                for row in rows}
    report = _paired_report([by_tuple[key] for key in order], tag=tag, profile=profile,
                            accept_path=accept_path)
    # Raw fragments retain actual CPU IDs and the original producer attempt on every runner.
    report["raw_fragments"] = sorted(fragments, key=lambda fragment: fragment["shard"])
    return report


def paired_run(*, base: Path, tag: str, scratch: Path, profile: dict,
               cases: list[dict], accept_path: Path = BENCHMARK / "accept.json",
               series: str | None = None, shard: int | None = None,
               shards: int | None = None, base_commit: str | None = None) -> dict:
    if series is not None:
        # One CI job per series keeps each paired comparison inside its job budget.
        cases = [case for case in cases if case["series"] == series]
        if not cases:
            raise MeasurementRefused("paired-series-unknown")
    shard_count = profile["paired_shards"] if shard is not None and shards is None else shards
    assigned = (list(enumerate(cases)) if shard is None else
                _case_partition(cases, shard, shard_count))
    if shard is not None and (series is None or not base_commit):
        raise MeasurementRefused("paired-shard-requires-pinned-series")
    if shard is not None and shard_count != profile["paired_shards"]:
        raise MeasurementRefused("paired-shard-count-differs-profile")
    # Bind raw Actions evidence before preparing either expensive runtime.
    if shard is not None and not all(os.environ.get(key) for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")):
        raise MeasurementRefused("paired-producer-identity-unavailable")
    if not hasattr(os, "sched_getaffinity") or len(os.sched_getaffinity(0)) < REFERENCE["reference_cpus"]:
        raise MeasurementRefused("two-cpu-reference-unavailable")
    cpus = sorted(os.sched_getaffinity(0))[:REFERENCE["reference_cpus"]]
    workers: dict[str, _Worker] = {}
    samples = []
    try:
        python = _prepare_tag_runtime(base, scratch)
        # Head generates the corpus once from its registry; base reads those bytes.
        for arm, source in (("head", ROOT), ("base", base)):
            runtime = ({"python": python, "runtime_prefix": python.parent.parent,
                        "runtime_policy": "frozen-tag-runtime"} if arm == "base" else {})
            workers[arm] = _Worker(source, scratch / "vault", scratch / arm, cpus, profile, **runtime)
        environments = {arm: worker.environment for arm, worker in workers.items()}
        if environments["head"]["sqlite_version"] not in profile["sqlite_versions"]:
            raise MeasurementRefused("reference-sqlite-unsupported")
        if (environments["base"]["encoder_digest"] != environments["head"]["encoder_digest"]
            or environments["base"]["encoder_identity"] != environments["head"]["encoder_identity"]
            or environments["base"]["corpus"] != environments["head"]["corpus"]
            or environments["base"]["sqlite_version"] != environments["head"]["sqlite_version"]):
            raise MeasurementRefused("paired-reference-inputs-differ")
        # One warm-up per series and principal, with the real encoder restored.
        first_cases = {case["series"]: case for case in reversed(cases)}
        for worker in workers.values():
            try:
                for principal_name in PAIRED_PRINCIPALS:
                    for case in first_cases.values():
                        worker.exchange({**case, "principal": principal_name,
                                         "rows": REFERENCE["stage_rows"]})
            finally:
                worker.pause()
        for case_index, case in assigned:
            order = ("base", "head") if case_index % 2 == 0 else ("head", "base")
            for principal_name in PAIRED_PRINCIPALS:
                for repeat in range(PAIRING["repeats"]):
                    for arm in order:
                        worker = workers[arm]
                        try:
                            measured = worker.exchange({**case, "principal": principal_name,
                                                        "rows": REFERENCE["stage_rows"]})
                        finally:
                            worker.pause()
                        samples.append({"case": case["id"], "series": case["series"],
                                        "principal": principal_name, "arm": arm, "repeat": repeat,
                                        "load": one_minute_load(), **measured})
        if shard is not None:
            return {"schema_version": 1, "mode": "paired_fragment", "state": "collected",
                    "series": series, "shard": shard, "shards": shard_count,
                    "case_indices": [index for index, _case in assigned],
                    **_paired_identity(profile, cases, tag=tag, base_commit=base_commit),
                    "environment": environments, "samples": samples,
                    "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"]}
        return {**_paired_report(samples, tag=tag, profile=profile, accept_path=accept_path),
                "environment": environments}
    finally:
        for worker in workers.values():
            worker.close()


def corpus_snapshot(vault_root: Path) -> dict:
    """Read actual served-state row counts without loading an encoder or matrix."""
    import sqlite3

    from exomem import index_paths, lexstore

    with sqlite3.connect(lexstore.lexical_path(vault_root).as_uri() + "?mode=ro", uri=True) as conn:
        pages = conn.execute("SELECT count(*) FROM pages WHERE in_kb=1 AND is_nav=0").fetchone()[0]
        units = conn.execute("SELECT count(*) FROM semantic_units WHERE in_kb=1").fetchone()[0]
    with sqlite3.connect(index_paths.sidecar_path(vault_root).as_uri() + "?mode=ro", uri=True) as conn:
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    return {"source": "served-state-read-only-sql", "bucket_width": 500,
            "pages": pages // 500 * 500, "chunks": chunks // 500 * 500,
            "units": units // 500 * 500, "sqlite_version": sqlite3.sqlite_version}


def live_cli(transport: ServedTransport, *, cases_path: Path, corpus_path: Path | None,
             output: Path, quiescence_bound: float) -> int:
    """Measure private live queries; only generated case codes leave this process."""
    profile = json.loads((BENCHMARK / "profile.json").read_text())
    cases = json.loads(cases_path.read_text())
    expected = {row["series"] for row in json.loads((BENCHMARK / profile["case_file"]).read_text())}
    if {case["series"] for case in cases} != expected:
        raise MeasurementRefused("live-series-incomplete")
    if any(sum(case["series"] == series for case in cases) < REFERENCE["queries_min"]
           for series in expected):
        raise MeasurementRefused("live-cases-insufficient")
    report = {"schema_version": 1, "mode": "live", "state": "not measured",
              "contract_revision": CONTRACT.revision, "series": [], "samples": [],
              "nonce_strategy": "trailing-whitespace", "boundary": RESPONSE_INNER_BOUNDARY}
    first, final, waited = await_quiescence(
        one_minute_load, bound_seconds=quiescence_bound, sleep=time.sleep,
    )
    report["load"] = {"first": first, "final": final, "waited_seconds": waited}
    if final > MAX_LOAD_AVERAGE:
        report.update(state="refused", reason="load-above-reference")
    else:
        nonce = run_nonce()
        versions: set[str] = set()
        read_failure: ServedReadFailure | None = None
        for index, case in enumerate(cases):
            arguments = {**case.get("arguments", {}), "query": case["query"] + _whitespace_nonce(nonce, index),
                         "include_timings": True, "scope": "kb", "limit": DEFAULT_LIMIT}
            try:
                envelope = transport._post(arguments)
            except SystemExit as error:
                read_failure = (error if isinstance(error, ServedReadFailure) else
                                ServedReadFailure("served-call-aborted", "served call aborted"))
                envelope = {}
            timing = envelope.get("timings") or {}
            if timing.get("sqlite_version"):
                versions.add(timing["sqlite_version"])
            sample = {"case": f"case-{index:04d}", "series": case["series"], "principal": "owner",
                      "arm": "head", "repeat": 0, "load": one_minute_load(),
                      "elapsed_ms": timing.get("total_ms"),
                      "state": "unknown" if _failed_stage(envelope) or read_failure else
                      "warming" if "warming" in envelope else
                      "ok" if _positive(timing.get("total_ms")) and timing.get("stage_rows") else "unknown",
                      "cpu_ms": timing.get("cpu_ms"), "cpu_source": timing.get("cpu_source", "unknown"),
                      "runqueue_ms": timing.get("runqueue_ms"),
                      "unattributed_ms": timing.get("unattributed_ms"),
                      "overlap_cpu_ms": timing.get("overlap_cpu_ms"),
                      "unattributed_cpu_ms": timing.get("unattributed_cpu_ms"),
                      "unbudgeted": timing.get("unbudgeted", []),
                      "critical_path": timing.get("critical_path"), "stages": timing.get("stage_rows", {})}
            if read_failure:
                sample["read_error"] = read_failure.reason
            report["samples"].append(sample)
            if read_failure:
                break
        if corpus_path:
            corpus = json.loads(corpus_path.read_text())
            validate_document(corpus, "summary.schema.json", definition="live_corpus")
            report["corpus"] = corpus
            # The summary records unknown when served diagnostics cannot bind SQLite.
            environment = {"sqlite_version": next(iter(versions)) if len(versions) == 1 else "unknown"}
            report["state"], report["series"] = summarize_absolute(
                report["samples"], environment=environment, profile=profile, live=True,
                boundary=RESPONSE_INNER_BOUNDARY,
            )
            if (corpus["pages"] < REFERENCE["pages_min"] or corpus["chunks"] < REFERENCE["chunks_min"]
                or corpus["units"] < REFERENCE["units_min"]):
                report.update(state="not measured", reason="live-corpus-below-reference")
                for row in report["series"]:
                    row.update(state="not measured", ceiling_met=None)
        else:
            report["reason"] = "live-corpus-unverified"
        if read_failure:
            report.update(state="refused" if read_failure.refused else "failed", reason=read_failure.reason)
    validate_document(report, "summary.schema.json")
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"state": report["state"], "artifact": output.name}))
    return 0


def paired_cli(output: Path, *, series: str | None = None, base_tag: str | None = None,
               base_commit: str | None = None, shard: int | None = None,
               shards: int | None = None) -> int:
    """Measure a pinned release baseline, resolving it once for unsharded use."""
    import subprocess
    import tempfile

    from jsonschema.exceptions import ValidationError

    report: dict = {"schema_version": 1, "mode": "paired", "state": "refused",
                    "contract_revision": CONTRACT.revision, "series": [], "samples": []}
    if shard is not None:
        report.update(mode="paired_fragment", series=series, shard=shard, shards=shards,
                      run_id=os.environ.get("GITHUB_RUN_ID"), run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"))
    phase = "reference-preparation"
    try:
        profile = json.loads((BENCHMARK / "profile.json").read_text())
        cases, digest = reference_cases(profile)
        phase = "release-archive"
        if base_tag is None and base_commit is None:
            tag = subprocess.check_output(
                ["gh", "release", "view", "--json", "tagName", "--jq", ".tagName"], text=True,
                cwd=ROOT, stderr=subprocess.DEVNULL,
            ).strip()
            base_commit = _git_bytes("rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip()
        elif base_tag is not None and base_commit is not None:
            tag = base_tag
            _verify_pinned_base(tag, base_commit)
        else:
            raise MeasurementRefused("pinned-release-identity-incomplete")
        if not tag or tag.startswith("-"):
            raise MeasurementRefused("latest-release-tag-unavailable")
        report["tag"] = tag
        with tempfile.TemporaryDirectory(prefix="exomem-recall-latency-") as temporary:
            scratch = Path(temporary)
            base = scratch / "base"
            base.mkdir()
            archive = scratch / "release.tar"
            with archive.open("wb") as stream:
                subprocess.run(["git", "archive", base_commit], cwd=ROOT, stdout=stream, check=True)
            # Git owns this archive; the checkout contains no external tar input.
            subprocess.run(["tar", "-xf", str(archive), "-C", str(base)], check=True)
            phase = "paired-measurement"
            report = paired_run(base=base, tag=tag, scratch=scratch, profile=profile, cases=cases,
                                series=series, shard=shard, shards=shards, base_commit=base_commit)
            report["source_digest"] = digest
    except (ValueError, OSError, subprocess.SubprocessError, ValidationError) as error:
        # Closed local error codes; never print arbitrary subprocess output.
        report["reason"] = error.code if isinstance(error, MeasurementRefused) else type(error).__name__
        report["phase"] = (error.phase or phase) if isinstance(error, MeasurementRefused) else phase
    validate_document(report, "summary.schema.json",
                      definition="paired_fragment" if shard is not None else None)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"state": report["state"], "artifact": output.name}))
    return int(report["state"] != ("collected" if shard is not None else "passed"))


def aggregate_paired_cli(directory: Path, output: Path, *, series: str, tag: str, base_commit: str,
                         producer_outcome: str | None = None) -> int:
    import subprocess

    from jsonschema.exceptions import ValidationError

    report: dict = {"schema_version": 1, "mode": "paired", "state": "refused",
                    "contract_revision": CONTRACT.revision, "series": [], "samples": []}
    try:
        # This filename is the raw Actions artifact protocol; elapsed records are separate files.
        fragments = [json.loads(path.read_text()) for path in sorted(directory.rglob("recall-latency-fragment-*.json"))]
        report = aggregate_paired(fragments, series=series, tag=tag, base_commit=base_commit,
                                  producer_outcome=producer_outcome)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, ValidationError) as error:
        report["reason"] = error.code if isinstance(error, MeasurementRefused) else type(error).__name__
        report["phase"] = "paired-aggregation"
    validate_document(report, "summary.schema.json")
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"state": report["state"], "artifact": output.name}))
    return int(report["state"] != "passed")


# --- Entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure governed recall against the fixed latency ceilings."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reference", action="store_true", help="record the absolute reference series")
    source.add_argument("--encoder-cache-key", action="store_true", help=argparse.SUPPRESS)
    source.add_argument("--validate-summary", type=Path, help="validate a content-free stored summary")
    source.add_argument("--paired", action="store_true", help="compare with the latest release")
    source.add_argument("--aggregate-paired", type=Path, help="compare one complete series from raw shard artifacts")
    parser.add_argument("--producer-outcome", help="current Actions sampler matrix result, required for aggregation")
    parser.add_argument("--series", help="limit --paired to one series of the case file")
    parser.add_argument("--base-tag", help="release label resolved once before sampling")
    parser.add_argument("--base-commit", help="peeled release commit resolved before sampling")
    parser.add_argument("--shard", type=int, help="one contiguous case shard; emits raw samples only")
    parser.add_argument("--shards", type=int, help="number of case shards")
    source.add_argument("--worker-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-cpus", help=argparse.SUPPRESS)
    parser.add_argument("--worker-vault", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--live-cases", type=Path, help="private live case file; never copied to evidence")
    parser.add_argument("--live-corpus", type=Path, help="counts measured on the served host")
    parser.add_argument("--corpus-counts", action="store_true", help="read content-free corpus counts")
    parser.add_argument("--output", type=Path, default=Path("recall-latency-summary.json"))
    source.add_argument(
        "--served-url", help="base URL of a served cell, e.g. http://127.0.0.1:8765"
    )
    source.add_argument("--vault", type=Path, help="vault root, for an in-process run")
    from exomem.recall_canary import add_arguments, run_cli

    add_arguments(parser, source_group=source, selectable_check=False)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against the fixed ceilings and exit non-zero on a breach",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--quiescence-bound",
        type=float,
        default=DEFAULT_QUIESCENCE_BOUND_SECONDS,
        help="seconds to wait for the load average to fall before refusing",
    )
    args = parser.parse_args(argv)

    if args.worker_source:
        return worker_main(args.worker_vault)
    if args.corpus_counts:
        if not args.vault:
            parser.error("--corpus-counts requires --vault on the served host")
        args.output.write_text(json.dumps(corpus_snapshot(args.vault), indent=2) + "\n")
        return 0
    if args.validate_summary:
        validate_document(json.loads(args.validate_summary.read_text()), "summary.schema.json")
        return 0
    if args.encoder_cache_key:
        from exomem import embedding_backend, embeddings

        print("digest=" + embedding_backend.served_artifact(embeddings.MODEL_NAME).digest)
        return 0
    if args.paired:
        if args.shard is not None and not all((args.series, args.base_tag, args.base_commit, args.shards)):
            parser.error("case shards require --series, --base-tag, --base-commit and --shards")
        return paired_cli(args.output, series=args.series, base_tag=args.base_tag,
                          base_commit=args.base_commit, shard=args.shard, shards=args.shards)
    if args.aggregate_paired:
        if not all((args.series, args.base_tag, args.base_commit)):
            parser.error("aggregation requires --series, --base-tag and --base-commit")
        return aggregate_paired_cli(args.aggregate_paired, args.output, series=args.series,
                                    tag=args.base_tag, base_commit=args.base_commit,
                                    producer_outcome=args.producer_outcome)
    if args.reference:
        return absolute_cli(args.output, quiescence_bound=args.quiescence_bound)
    if args.mcp_url:
        return run_cli(args)

    if args.served_url:
        key = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
        if not key:
            raise SystemExit(
                "recall latency gate failed: EXOMEM_REST_API_KEY is required for "
                "--served-url; export it rather than passing it on the command line"
            )
        transport: Any = ServedTransport(args.served_url, key)
        transport.pages = transport.measure_corpus()
        if args.live_cases:
            return live_cli(transport, cases_path=args.live_cases, corpus_path=args.live_corpus,
                            output=args.output, quiescence_bound=args.quiescence_bound)
    else:
        transport = VaultTransport(args.vault)
        transport.warm()

    report = run(
        transport=transport,
        limit=args.limit,
        quiescence_bound_seconds=args.quiescence_bound,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.check:
        check(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
