"""The recall latency gate's own contract.

The gate is the instrument that decides whether the read path meets
`recall-latency-contract`. An instrument that can be talked out of its reading
is worse than no instrument, so these tests pin the three things that make the
reading trustworthy: the ceilings are the spec's numbers and not a calibration,
a contended box is refused rather than reported, and a series cannot be served
from the result cache.

Every node here drives the gate through injected transports and an injected
load source. That is deliberate: the gate's *judgement* must be testable
without an 8k-page warm cell, because the judgement is what regresses. The
numbers themselves are produced on the operator's box, by
`scripts/recall_latency_gate.py --served-url ...`, and never in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import recall_latency_gate as gate  # noqa: E402, I001


# --- The ceilings are the contract -----------------------------------------


def test_the_ceilings_are_the_spec_values_and_not_a_calibration() -> None:
    """Pinned as literals, against the spec text, so a tuning edit is a diff.

    `recall-latency-contract` says the ceilings "are the capability's contract,
    not calibrated from any runner, and a gate MUST NOT loosen them". The only
    way that sentence can be enforced is a pin that restates the numbers from
    the spec rather than importing whatever the script currently believes. If
    this test and the script are edited together the reviewer sees both halves
    in one diff, which is the point.
    """
    assert gate.HYBRID_P50_MS == 300.0
    assert gate.HYBRID_P95_MS == 600.0
    assert gate.KEYWORD_P50_MS == 120.0
    assert gate.FILTERED_HYBRID_P50_MS == 400.0
    assert gate.FILTERED_ELIGIBILITY_MS == 20.0
    assert gate.MAX_LOAD_AVERAGE == 2.0
    assert gate.SAMPLES_PER_SERIES == 30
    assert gate.MAX_WARMING_SAMPLES == 1
    assert gate.MIN_CORPUS_PAGES == 8000


def test_a_breach_is_reported_with_the_honest_number() -> None:
    """The gate names what it measured, not a rounded reassurance."""
    report = _report(hybrid_p50=421.5)

    with pytest.raises(SystemExit) as raised:
        gate.check(report)

    message = str(raised.value)
    assert "hybrid" in message
    assert "421.5" in message
    assert "300.0" in message


def test_a_series_inside_every_ceiling_passes() -> None:
    """The counter-case, so the gate is a measurement and not a constant."""
    gate.check(_report())


# --- A contended measurement is refused, not reported ----------------------


def test_the_gate_refuses_a_verdict_above_the_load_ceiling() -> None:
    """Above 2.0 the gate exits naming the load and emits no comparison.

    The spec's scenario is explicit that it "never emits ceiling comparisons
    from samples taken under that load", so the refusal has to happen before
    any sample is taken, not after.
    """
    loads = _StubLoad([6.4, 6.1, 5.9])
    transport = _StubTransport()

    with pytest.raises(SystemExit) as raised:
        gate.run(
            transport=transport,
            load_source=loads,
            quiescence_bound_seconds=1.0,
            sleep=lambda _seconds: None,
        )

    message = str(raised.value)
    assert "6.4" in message or "5.9" in message, message
    assert "load" in message.lower()
    # No verdict of any kind may leak from a contended run.
    for ceiling in ("300", "600", "120", "400"):
        assert ceiling not in message, f"a refusal leaked a ceiling comparison: {message}"
    assert transport.calls == [], "the gate sampled a contended cell"


def test_the_quiescence_wait_is_bounded_and_then_gives_up() -> None:
    """It waits, but it does not wait forever, and it says how long it waited."""
    loads = _StubLoad([9.0] * 50)
    slept: list[float] = []

    with pytest.raises(SystemExit):
        gate.run(
            transport=_StubTransport(),
            load_source=loads,
            quiescence_bound_seconds=3.0,
            sleep=slept.append,
        )

    assert sum(slept) <= 3.0 + gate.QUIESCENCE_POLL_SECONDS, (
        f"the wait ran past its bound: slept {sum(slept)}s"
    )


def test_a_cell_that_settles_within_the_bound_is_measured() -> None:
    """The refusal is a gate on the box, not a refusal to ever run."""
    loads = _StubLoad([7.0, 4.0, 1.2] + [1.2] * 500)

    report = gate.run(
        transport=_StubTransport(),
        load_source=loads,
        quiescence_bound_seconds=30.0,
        sleep=lambda _seconds: None,
    )

    assert report["refused"] is False
    assert report["series"]["hybrid"]["samples"] == gate.SAMPLES_PER_SERIES


def test_a_load_spike_between_samples_refuses_the_series() -> None:
    """Quiescence is checked between samples, not only at the start."""
    loads = _StubLoad([1.0, 1.0, 1.0, 9.9] + [9.9] * 500)

    with pytest.raises(SystemExit) as raised:
        gate.run(
            transport=_StubTransport(),
            load_source=loads,
            quiescence_bound_seconds=1.0,
            sleep=lambda _seconds: None,
        )

    assert "9.9" in str(raised.value)


# --- The series shape defeats the result cache -----------------------------


def test_a_series_asks_for_thirty_samples_per_shape() -> None:
    transport = _StubTransport()

    gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    for shape in ("hybrid", "keyword", "filtered_hybrid"):
        asked = [call for call in transport.calls if call["shape"] == shape]
        assert len(asked) == gate.SAMPLES_PER_SERIES, f"{shape} took {len(asked)} samples"


def test_every_query_in_a_run_is_novel_so_the_cache_cannot_serve_it() -> None:
    """A reused query measures the result cache, which is not the read path.

    This is the node that dies when the nonce is dropped, and it checks the
    whole run rather than one series, because a nonce that is per-series would
    still let the second series hit the first one's entries.
    """
    transport = _StubTransport()

    gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    queries = [call["query"] for call in transport.calls]
    assert len(queries) == len(set(queries)), (
        f"{len(queries) - len(set(queries))} query/queries were reused within one run"
    )


def test_two_runs_do_not_repeat_each_other_s_queries() -> None:
    """The cache outlives the process, so the nonce has to as well."""
    first, second = _StubTransport(), _StubTransport()
    for transport in (first, second):
        gate.run(
            transport=transport,
            load_source=_StubLoad([1.0] * 500),
            quiescence_bound_seconds=1.0,
            sleep=lambda _seconds: None,
        )

    overlap = {call["query"] for call in first.calls} & {call["query"] for call in second.calls}
    assert not overlap, f"{len(overlap)} query/queries survived into a second run"


def test_the_filtered_series_carries_exactly_one_structured_filter() -> None:
    """The contract's third series is 'one supported structured filter'."""
    transport = _StubTransport()

    gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    filtered = [call for call in transport.calls if call["shape"] == "filtered_hybrid"]
    assert filtered
    for call in filtered:
        assert call["mode"] == "hybrid"
        assert len(call["projects"]) == 1
    for call in transport.calls:
        if call["shape"] == "hybrid":
            assert not call["projects"], "the unfiltered series carried a filter"
        if call["shape"] == "keyword":
            assert call["mode"] == "keyword"


# --- Warming is excluded, counted, and bounded -----------------------------


def test_a_warming_outcome_is_excluded_from_the_percentiles_and_counted() -> None:
    """A warming request is a truthful refusal, not a fast recall.

    Folding it into the percentiles would let a cell that answered nothing
    report the best p50 it has ever produced.
    """
    transport = _StubTransport(warming_at={("hybrid", 4)}, elapsed_ms=200.0, warming_ms=1.0)

    report = gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    hybrid = report["series"]["hybrid"]
    assert hybrid["warming"] == 1
    assert hybrid["samples"] == gate.SAMPLES_PER_SERIES - 1
    assert hybrid["p50_ms"] == pytest.approx(200.0), (
        "a 1 ms warming refusal was averaged into the percentiles"
    )


def test_more_than_one_warming_outcome_fails_the_gate() -> None:
    report = _report(warming=2)

    with pytest.raises(SystemExit) as raised:
        gate.check(report)

    assert "warming" in str(raised.value)


def test_exactly_one_warming_outcome_is_tolerated() -> None:
    """The spec permits one; the counter-case keeps the rule a rule."""
    gate.check(_report(warming=1))


# --- The walk sentinel, read through the stage sources ----------------------


def test_a_walker_stage_that_reports_computed_fails_the_gate() -> None:
    """`computed` on an index-backed stage is the walk reappearing.

    The spec names the four stages that must consume maintained indexes:
    eligibility, widening, hydration and hit construction. Those are the
    stages whose source is decided at runtime, and the only ones where
    `computed` means the corpus was walked.

    The stage here is `outside_kb`, NOT `filter_eligibility`, and that is the
    whole point of the node. An earlier version used `filter_eligibility` and
    survived the mutant that deletes the walker loop outright, because the
    separate eligibility-source check below caught the same report and the node
    could not tell the two guards apart. A node that passes for the wrong
    reason is a guard that is not there. `outside_kb` is reachable only through
    the walker loop, so this now fails when that loop is removed.
    """
    report = _report(stage_sources={"outside_kb": "computed"})

    with pytest.raises(SystemExit) as raised:
        gate.check(report)

    message = str(raised.value)
    assert "outside_kb" in message
    assert "computed" in message


def test_the_filtered_eligibility_source_is_checked_on_its_own() -> None:
    """The second, narrower guard: the filtered series names its own stage.

    Separated from the node above so each guard has a node that dies with it
    rather than two nodes that both survive because the other one fired.
    """
    report = _report(stage_sources={"filter_eligibility": "declined"})

    with pytest.raises(SystemExit) as raised:
        gate.check(report)

    assert "filter_eligibility" in str(raised.value)


def test_each_walker_stage_is_watched_not_just_the_first() -> None:
    for stage in gate.WALKER_STAGES:
        with pytest.raises(SystemExit, match=stage):
            gate.check(_report(stage_sources={stage: "computed"}))


def test_a_qualified_walker_stage_name_is_still_watched() -> None:
    """Lane 1 moved the projection under qualified names; the guard follows.

    `recall_projection` now reports as `freshness.recall_projection` and
    friends. A guard that matched the bare name only would have stopped
    watching the stage the day it was renamed, and said nothing.
    """
    with pytest.raises(SystemExit, match="freshness.recall_projection"):
        gate.check(_report(stage_sources={"freshness.recall_projection": "computed"}))


def test_a_computing_stage_that_is_not_a_walker_is_not_a_walk() -> None:
    """The counter-case that stops the guard being a blanket failure.

    `rerank` and `fusion` genuinely compute — they read no index and the
    product does not mark them. A gate that called those walks would fail every
    run for a reason unrelated to the corpus, and would be switched off within
    a week.
    """
    gate.check(_report(stage_sources={"rerank": "computed", "fusion": "computed"}))


def test_a_walker_stage_may_decline_without_failing_the_walk_check() -> None:
    """Declining is the contract's prescribed answer, not a breach."""
    gate.check(_report(stage_sources={"outside_kb": "declined"}))


def test_a_cell_that_reports_no_stage_sources_is_not_a_pass() -> None:
    """Silence is not proof, and this is not hypothetical.

    Probed against the live 0.69.0 cell on 2026-09-03: `/api/ask_memory` with
    `include_timings` returned 24 stages and ZERO of them carried a `source`,
    because the source vocabulary ships with this change. A gate that reads
    that as "no walker stage reported computed, therefore no walk" would
    certify the walk sentinel against a cell that cannot answer it — and would
    do so most convincingly on exactly the cell where the check matters least.
    """
    report = _report()
    for series in report["series"].values():
        series["stage_sources"] = {}

    with pytest.raises(SystemExit) as raised:
        gate.check(report)

    assert "no stage sources" in str(raised.value)


def test_the_filtered_eligibility_stage_is_held_to_its_index_outcome() -> None:
    """The spec's second scenario: an index outcome under 20 ms."""
    with pytest.raises(SystemExit, match="filter_eligibility"):
        gate.check(_report(filtered_eligibility_ms=44.0))


# --- The report is content-free and carries its load ------------------------


def test_the_report_records_the_load_beside_every_percentile() -> None:
    report = gate.run(
        transport=_StubTransport(),
        load_source=_StubLoad([1.0, 1.4, 1.1] + [1.25] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    for shape, series in report["series"].items():
        assert "p50_ms" in series, shape
        assert "load_max" in series, f"{shape} reported a percentile with no load beside it"
        assert "load_mean" in series, shape
        assert series["load_max"] >= series["load_mean"] > 0.0, shape


def test_the_report_is_content_free() -> None:
    """Closed codes, counts, percentiles and load — never a query or a path."""
    transport = _StubTransport()
    report = gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    serialized = json.dumps(report)
    for call in transport.calls:
        assert call["query"] not in serialized, "the report leaked a query"
    for leak in ("Knowledge Base/", ".md", "excerpt", "title"):
        assert leak not in serialized, f"the report leaked {leak!r}"


def test_the_report_names_the_transport_and_the_corpus_size() -> None:
    """A number with no provenance cannot be compared against a baseline."""
    report = gate.run(
        transport=_StubTransport(),
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    assert report["transport"] == "stub"
    assert "pages" in report


def test_a_corpus_below_the_reference_size_is_not_a_verdict() -> None:
    """8,000 pages is part of the contract's premise, not a detail."""
    with pytest.raises(SystemExit, match="pages"):
        gate.check(_report(pages=120))


# --- Stubs -----------------------------------------------------------------


class _StubLoad:
    """A scripted one-minute load average."""

    def __init__(self, readings: list[float]) -> None:
        self._readings = list(readings)
        self._last = readings[-1] if readings else 0.0

    def __call__(self) -> float:
        if self._readings:
            self._last = self._readings.pop(0)
        return self._last


class _StubTransport:
    """Records what the gate asked for and answers in the envelope's shape."""

    name = "stub"
    pages = 8_192

    def __init__(
        self,
        *,
        warming_at: set[tuple[str, int]] | None = None,
        elapsed_ms: float = 100.0,
        warming_ms: float = 1.0,
    ) -> None:
        self.calls: list[dict] = []
        self._warming_at = warming_at or set()
        self._elapsed_ms = elapsed_ms
        self._warming_ms = warming_ms

    def ask(self, *, shape: str, query: str, mode: str, projects: tuple[str, ...], limit: int):
        index = len([call for call in self.calls if call["shape"] == shape])
        self.calls.append(
            {
                "shape": shape,
                "query": query,
                "mode": mode,
                "projects": projects,
                "limit": limit,
            }
        )
        if (shape, index) in self._warming_at:
            return gate.Sample(
                elapsed_ms=self._warming_ms,
                warming=True,
                stage_sources={},
                stage_ms={},
            )
        return gate.Sample(
            elapsed_ms=self._elapsed_ms,
            warming=False,
            stage_sources={"filter_eligibility": "index", "rerank": "computed"},
            stage_ms={"filter_eligibility": 3.0},
        )


def _report(
    *,
    hybrid_p50: float = 100.0,
    warming: int = 0,
    stage_sources: dict[str, str] | None = None,
    filtered_eligibility_ms: float = 3.0,
    pages: int = 8_192,
) -> dict:
    """A passing report, minimally perturbed by each node above."""
    sources = {"filter_eligibility": "index"}
    sources.update(stage_sources or {})
    return {
        "refused": False,
        "transport": "stub",
        "pages": pages,
        "series": {
            "hybrid": {
                "samples": 30,
                "warming": warming,
                "p50_ms": hybrid_p50,
                "p95_ms": 120.0,
                "load_mean": 1.0,
                "load_max": 1.2,
                "stage_sources": sources,
            },
            "keyword": {
                "samples": 30,
                "warming": 0,
                "p50_ms": 40.0,
                "p95_ms": 60.0,
                "load_mean": 1.0,
                "load_max": 1.2,
                "stage_sources": sources,
            },
            "filtered_hybrid": {
                "samples": 30,
                "warming": 0,
                "p50_ms": 110.0,
                "p95_ms": 130.0,
                "load_mean": 1.0,
                "load_max": 1.2,
                "stage_sources": sources,
                "eligibility_ms": filtered_eligibility_ms,
            },
        },
    }
