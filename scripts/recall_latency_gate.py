"""Recall latency gate for `recall-latency-contract`.

Runs the contract's three series against a cell — plus a fourth the contract
states no ceiling for — reads the per-stage timing diagnostics, and compares
the result with the fixed ceilings.

The fourth is `browse`, the `query=""` shape the tool schema recommends. It is
measured and reported and every structural rule applies to it, but no latency
comparison is made, because the spec states no number for it. See
`UNCEILINGED_SHAPES`.

Four properties make the reading worth having, and each one is pinned by a
node in `tests/test_recall_latency_gate.py`:

* **The ceilings are the contract.** They are literals here because the spec
  says they are "the capability's contract, not calibrated from any runner",
  and a gate MUST NOT loosen them. If this cell cannot meet them, the honest
  output is a failure with the measured number beside the ceiling — not a
  wider ceiling. A ceiling edited until the gate passes measures nothing.
* **A contended box is refused, not reported.** Above a one-minute load
  average of 2.0 the gate waits a bounded time and then exits *without a
  verdict*, naming the load. It re-checks between samples, because a box that
  was quiet at the start is not necessarily quiet at sample twenty. Every
  percentile carries the load it was produced under.
* **The result cache cannot serve the series.** Every query in a run carries a
  nonce that is unique to the run and to the sample, so no request can be
  answered from a previous one — within the run or across runs. Two shapes
  cannot spell that nonce in words — the browse is DEFINED by an empty query,
  and a keyword series has to match the corpus — so they carry it in
  whitespace, which the result cache distinguishes and the read path strips.
* **Nothing measured is nothing certified.** Every series must return hits at
  its median sample, the premise about the corpus must come from the cell the
  series ran against, and a stage the check depends on that did not report at
  all fails closed rather than defaulting into a pass.

Output is content-free by construction: closed codes, counts, percentiles and
load. No query text, no paths, no excerpts. A latency artifact gets stored
under an OpenSpec change and read by people who are not entitled to the vault's
contents.

Two transports:

    --served-url http://127.0.0.1:8765
        The live cell, over the REST facade at `/api/ask_memory`, authenticated
        with `Authorization: Bearer $EXOMEM_REST_API_KEY`. This is the surface
        that answers a key; the streamable-HTTP MCP endpoint is behind the
        GitHub OAuth proxy and correctly rejects that key with a 401.
        The key is read from the environment and never echoed.

    --vault /path/to/vault
        The same series in-process, for a CI-shaped run on a small corpus.
        It will not meet the ceilings on a fixture vault and is not meant to;
        `--check` against a corpus below the contract's 8,000 pages fails on
        the premise rather than pretending the numbers mean anything.

The measured quantity is the server-side `timings.total_ms`, the same quantity
the 0.69.0 baseline recorded, so the before/after comparison is like for like
and does not fold this box's loopback and JSON costs into the read path.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# --- The contract ----------------------------------------------------------
#
# From `openspec/changes/accelerate-governed-recall/specs/recall-latency-contract
# /spec.md`, requirement "Governed Recall Meets Fixed Latency Ceilings On A
# Quiescent Cell". Restated as literals so that loosening one is a visible diff
# against a test that restates the same sentence.

#: Hybrid recall without structured filters.
HYBRID_P50_MS = 300.0
HYBRID_P95_MS = 600.0
#: Keyword recall.
KEYWORD_P50_MS = 120.0
#: Hybrid recall carrying one supported structured filter.
FILTERED_HYBRID_P50_MS = 400.0
#: "the eligibility stage reports an index outcome with a duration under 20 ms"
FILTERED_ELIGIBILITY_MS = 20.0

#: "with the load average at or below 2.0"
MAX_LOAD_AVERAGE = 2.0
#: How often the bounded quiescence wait re-reads the load.
QUIESCENCE_POLL_SECONDS = 1.0
#: How long it waits before giving up, unless `--quiescence-bound` says otherwise.
DEFAULT_QUIESCENCE_BOUND_SECONDS = 120.0

#: "thirty novel hybrid recalls run back to back"
SAMPLES_PER_SERIES = 30
#: "more than one warming outcome in a measured series SHALL fail the gate"
MAX_WARMING_SAMPLES = 1
#: "a quiescent cell of at least 8,000 governed pages"
MIN_CORPUS_PAGES = 8_000

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

    elapsed_ms: float
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


def await_quiescence(
    load_source, *, bound_seconds: float, sleep
) -> tuple[float, float, float]:
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


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; no interpolation, so 30 samples stay legible."""
    if not values:
        return 0.0
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
        else:
            measured.append(float(sample.elapsed_ms))
            hit_counts.append(int(sample.hits))
        for stage, source in sample.stage_sources.items():
            sources[stage] = _widest(sources.get(stage), source)
        for stage, milliseconds in sample.stage_ms.items():
            if _is_walker(stage) and stage.rsplit(".", 1)[-1] == "filter_eligibility":
                eligibility.append(float(milliseconds))

        # Between samples, not only at the start: a box that was quiet at
        # sample one is not necessarily quiet at sample twenty, and a series
        # that straddles a spike is not a measurement of this cell.
        load = float(load_source())
        if load > MAX_LOAD_AVERAGE:
            first, final, waited = await_quiescence(
                load_source, bound_seconds=quiescence_bound_seconds, sleep=sleep
            )
            if final > MAX_LOAD_AVERAGE:
                raise _refuse(load, final, waited)
            loads.append(first)
        loads.append(load)

    series: dict[str, Any] = {
        "samples": len(measured),
        "warming": warming,
        "p50_ms": round(_percentile(measured, 0.50), 1),
        "p95_ms": round(_percentile(measured, 0.95), 1),
        "load_mean": round(statistics.fmean(loads), 2) if loads else 0.0,
        "load_max": round(max(loads), 2) if loads else 0.0,
        "stage_sources": sources,
        # The total is kept because it is diagnostic — "120 hits across thirty
        # samples but a median of nothing" says something a single number does
        # not — but the guard is on the median, because the p50 is what the
        # gate certifies. A sum lets one hit in thirty clear a check that is
        # supposed to prove the p50 measured a real retrieval.
        "hits": sum(hit_counts),
        "hits_p50": int(_percentile([float(count) for count in hit_counts], 0.50)),
    }
    if shape == "filtered_hybrid":
        # None, not 0.0, when the stage never reported: a stage that did not run
        # is not a stage that ran in no time, and 0.0 sails under every ceiling.
        # `check` fails closed on the None.
        series["eligibility_ms"] = round(max(eligibility), 1) if eligibility else None
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

_P50_CEILINGS = {
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
        p50 = float(series["p50_ms"])
        ceiling = _P50_CEILINGS.get(shape)
        if ceiling is not None and p50 > ceiling:
            failures.append(f"{shape} p50={p50:.1f}ms > {ceiling:.1f}ms")
        if shape == "hybrid":
            p95 = float(series["p95_ms"])
            if p95 > HYBRID_P95_MS:
                failures.append(f"{shape} p95={p95:.1f}ms > {HYBRID_P95_MS:.1f}ms")

        if int(series["samples"]) < 1:
            failures.append(f"{shape} produced no measured sample")

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
        if int(series.get("hits_p50", 0)) < 1:
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
                    "20 ms index-outcome premise cannot be read on this cell, and an "
                    "absent stage is not a stage that ran in no time"
                )
            elif float(eligibility) > FILTERED_ELIGIBILITY_MS:
                failures.append(
                    f"{shape} stage filter_eligibility took {float(eligibility):.1f}ms > "
                    f"{FILTERED_ELIGIBILITY_MS:.1f}ms"
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
                    failures.append(
                        f"{shape} stage {stage} answered from {source}, not an index"
                    )

    if failures:
        raise SystemExit("recall latency gate failed: " + "; ".join(failures))


# --- Transports -------------------------------------------------------------


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
        except (HTTPError, URLError, TimeoutError) as error:
            # The class only. A transport error's message can carry the URL,
            # and the URL carries the key in no case here, but a header echo in
            # a future urllib would.
            raise SystemExit(
                f"recall latency gate failed: served call failed ({type(error).__name__})"
            ) from error
        if not body.get("success"):
            raise SystemExit("recall latency gate failed: the cell refused the read")
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
            scoped = self._post(
                {"path": scope_path, "mode": "overview"}, command="browse_memory"
            )
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
        return Sample(elapsed_ms=0.0, warming=True)
    if "warming" in data:
        return Sample(elapsed_ms=0.0, warming=True)
    timings = data.get("timings")
    if not isinstance(timings, dict):
        # No diagnostics means no verdict is possible for this sample; treat it
        # the way a warming outcome is treated rather than inventing a number.
        return Sample(elapsed_ms=0.0, warming=True)
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
    if not isinstance(total, (int, float)) or float(total) <= 0.0:
        # Thirty of these would be thirty 0.0 ms samples and a green verdict.
        # An absent `timings` dict is already treated as warming three lines
        # up; a timings dict with no usable total says exactly as little.
        return Sample(elapsed_ms=0.0, warming=True)
    hit_list = data.get("hits")
    return Sample(
        elapsed_ms=float(total),
        warming=False,
        stage_sources=sources,
        stage_ms=milliseconds,
        hits=len(hit_list) if isinstance(hit_list, list) else 0,
    )


# --- Entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure governed recall against the fixed latency ceilings."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--served-url", help="base URL of a served cell, e.g. http://127.0.0.1:8765")
    source.add_argument("--vault", type=Path, help="vault root, for an in-process run")
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

    if args.served_url:
        key = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
        if not key:
            raise SystemExit(
                "recall latency gate failed: EXOMEM_REST_API_KEY is required for "
                "--served-url; export it rather than passing it on the command line"
            )
        transport: Any = ServedTransport(args.served_url, key)
        transport.pages = transport.measure_corpus()
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
