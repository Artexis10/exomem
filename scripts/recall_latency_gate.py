"""Recall latency gate for `recall-latency-contract`.

Runs the contract's three series against a cell, reads the per-stage timing
diagnostics, and compares the result with the fixed ceilings.

Three properties make the reading worth having, and each one is pinned by a
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
  answered from a previous one — within the run or across runs.

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
#: All six now decide their source at runtime, and that is what makes this list
#: worth anything. An earlier version named only the first four and claimed to
#: cover the spec's four concepts; it did not. Hydration is `keyword` and hit
#: construction is `filter_hits`, and both carried the static `computed`
#: default, so both were excluded from the check — which is exactly how a real
#: whole-scope walk in `keyword` on the empty-query browse went unreported by
#: every instrument this change ships. `recall_projection` and
#: `pending_visibility` report `index` unconditionally on every shape measured,
#: so before that fix the check could only ever fire on two stages.
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

#: Severity order, matching `find_types._SOURCE_WIDTH`: when a stage reports
#: different sources across a series, the report keeps the worst one, because a
#: single walk in thirty is the finding.
_SOURCE_SEVERITY = {"cache": 0, "index": 1, "declined": 2, "computed": 3}

#: The filter used by the third series. One supported structured filter, on the
#: field Lane 2 moved onto the page catalogue.
FILTER_PROJECTS: tuple[str, ...] = ("exomem",)

SERIES_SHAPES: tuple[str, ...] = ("hybrid", "keyword", "filtered_hybrid")


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
    """What the gate needs from a cell: a name, a size, and a timed ask."""

    name: str
    pages: int

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


def novel_query(shape: str, index: int, nonce: str) -> str:
    """A query no cell has been asked before.

    The nonce is what defeats the result cache. It is per run AND per sample,
    because a nonce that were only per series would still let the second series
    hit the first one's entries, and one that were only per run would let a
    re-run of the same series be answered from memory.

    The prose around the nonce is fixed and generic on purpose: it makes the
    three series comparable, and it is not vault content, so it can appear in a
    log without disclosing anything.
    """
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
    hits = 0

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
            hits += int(sample.hits)
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

    series = {
        "samples": len(measured),
        "warming": warming,
        "p50_ms": round(_percentile(measured, 0.50), 1),
        "p95_ms": round(_percentile(measured, 0.95), 1),
        "load_mean": round(statistics.fmean(loads), 2) if loads else 0.0,
        "load_max": round(max(loads), 2) if loads else 0.0,
        "stage_sources": sources,
        "hits": hits,
    }
    if shape == "filtered_hybrid":
        series["eligibility_ms"] = round(max(eligibility), 1) if eligibility else 0.0
    return series


def run(
    *,
    transport,
    load_source=one_minute_load,
    quiescence_bound_seconds: float = DEFAULT_QUIESCENCE_BOUND_SECONDS,
    sleep=time.sleep,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Take all three series, or refuse before taking any sample at all."""
    first, final, waited = await_quiescence(
        load_source, bound_seconds=quiescence_bound_seconds, sleep=sleep
    )
    if final > MAX_LOAD_AVERAGE:
        raise _refuse(first, final, waited)

    nonce = run_nonce()
    return {
        "refused": False,
        "transport": transport.name,
        "pages": int(transport.pages),
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

    pages = int(report.get("pages", 0))
    if pages < MIN_CORPUS_PAGES:
        failures.append(
            f"the cell holds {pages} governed pages, below the {MIN_CORPUS_PAGES} "
            "pages the contract measures on"
        )

    for shape in SERIES_SHAPES:
        series = report["series"][shape]
        p50 = float(series["p50_ms"])
        ceiling = _P50_CEILINGS[shape]
        if p50 > ceiling:
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
            if source == "computed" and _is_walker(stage):
                failures.append(f"{shape} stage {stage} reported source computed: a walk")

        if shape == "filtered_hybrid":
            if int(series.get("hits", 0)) < 1:
                failures.append(
                    "the filtered series returned no hits across the whole run: the "
                    "filter matched nothing on this cell, so its percentiles measure "
                    "empty-result recalls and not a filtered read path"
                )
            eligibility = float(series.get("eligibility_ms", 0.0))
            if eligibility > FILTERED_ELIGIBILITY_MS:
                failures.append(
                    f"{shape} stage filter_eligibility took {eligibility:.1f}ms > "
                    f"{FILTERED_ELIGIBILITY_MS:.1f}ms"
                )
            source = series.get("stage_sources", {}).get("filter_eligibility")
            if source is not None and source not in ("index", "cache"):
                failures.append(
                    f"{shape} stage filter_eligibility answered from {source}, not an index"
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
        self._url = base_url.rstrip("/") + "/api/ask_memory"
        self._key = api_key
        self._timeout = timeout
        self.pages = 0

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        request = Request(
            self._url,
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

    def measure_corpus(self) -> int:
        """Ask the cell how many governed pages it holds, for the premise check."""
        from exomem import vault as vault_module

        root = os.environ.get("EXOMEM_VAULT_PATH", "").strip()
        if not root:
            return 0
        try:
            return sum(1 for _ in vault_module.walk_vault_md(Path(root)))
        except Exception:  # noqa: BLE001 - an uncountable corpus is not a verdict
            return 0

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
        self.pages = sum(1 for _ in vault_module.walk_vault_md(vault_root))

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
