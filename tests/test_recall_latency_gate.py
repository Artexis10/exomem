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
from http.client import IncompleteRead
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import recall_latency_gate as gate  # noqa: E402, I001


# --- The ceilings are the contract -----------------------------------------


def test_the_current_ceiling_and_reference_bars_resolve_from_the_contract() -> None:
    assert gate.HYBRID_P95_MS == 200.0
    assert gate.KEYWORD_P95_MS == 100.0
    assert gate.HYBRID_P50_MS == 50.0
    assert gate.FILTERED_ELIGIBILITY_MS == 6.0
    assert gate.MIN_CORPUS_PAGES == 6500
    assert gate.SAMPLES_PER_SERIES == 30


def test_a_breach_names_the_p95_measurement() -> None:
    report = _report()
    report["series"]["hybrid"]["p95_ms"] = 421.5
    with pytest.raises(SystemExit) as raised:
        gate.check(report)
    assert "hybrid p95=421.5ms > 200.0ms" in str(raised.value)


def test_missed_targets_do_not_fail_a_ceiling() -> None:
    report = _report(hybrid_p50=62.0)
    report["series"]["hybrid"]["p95_ms"] = 160.0
    gate.check(report)
    assert report["series"]["hybrid"]["p50_target_met"] is False
    assert report["series"]["hybrid"]["p95_target_met"] is False


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


def test_later_load_is_reported_without_rejecting_a_started_series() -> None:
    report = gate.run(
        transport=_StubTransport(),
        load_source=_StubLoad([1.0, 9.9] + [1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda seconds: None,
    )
    assert report["series"]["hybrid"]["load_max"] == 9.9
    assert report["acceptance"] == "not measured"
    assert report["series"]["hybrid"]["contention"] == "unknown"


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

    The spec names four concepts — eligibility, widening, hydration and hit
    construction — and the list now actually covers them. It did not before:
    hydration is `keyword` and hit construction is `filter_hits`, both of which
    carried the static `computed` default and were therefore excluded from this
    very check, while `recall_projection` and `pending_visibility` report
    `index` unconditionally. So the walk check could only ever fire on two
    stages, and a real whole-scope walk in `keyword` went unreported by every
    instrument here. All six now decide their source at runtime.

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


def test_a_walker_stage_reporting_an_unknown_source_is_not_a_pass() -> None:
    """The spec names what a stage may report; anything else is not one of them.

    "Every stage reports `index`, `cache` or `declined` as its source" is an
    allowlist, and the gate reads this off a remote cell's JSON. Testing for
    the single word `computed` meant a walk that arrived spelled `walked`,
    `scan`, or `null` passed the sentinel without a word — on the one surface
    where the value is not produced by code this repository controls.
    """
    with pytest.raises(SystemExit, match="outside_kb reported source walked"):
        gate.check(_report(stage_sources={"outside_kb": "walked"}))


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


def test_a_missed_stage_budget_diagnoses_without_failing_the_ceiling() -> None:
    report = _report(filtered_eligibility_ms=44.0)
    gate.check(report)
    assert report["series"]["filtered_hybrid"]["eligibility_budget_ms"] == 6
    assert report["series"]["filtered_hybrid"]["eligibility_over_budget"] is True


@pytest.mark.parametrize("check,elapsed_ms", [(False, 40.0), (True, 40.0), (True, 421.5)])
def test_saved_gate_output_keeps_stage_diagnosis_before_the_ceiling_verdict(
    check: bool, elapsed_ms: float, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = _StubTransport(elapsed_ms=elapsed_ms, stage_ms={"filter_eligibility": 44.0})
    monkeypatch.setattr(gate, "VaultTransport", lambda _vault: transport)
    monkeypatch.setattr(transport, "warm", lambda: None, raising=False)
    measured_run = gate.run

    def run_at_known_load(**kwargs):
        return measured_run(**kwargs, load_source=_StubLoad([1.0] * 500))

    monkeypatch.setattr(gate, "run", run_at_known_load)
    arguments = ["--vault", "synthetic"] + (["--check"] if check else [])
    if check and elapsed_ms > gate.HYBRID_P95_MS:
        with pytest.raises(SystemExit, match="p95=421.5ms"):
            gate.main(arguments)
    else:
        assert gate.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    filtered = report["series"]["filtered_hybrid"]
    assert filtered["eligibility_ms"] == 44.0
    assert filtered["eligibility_budget_ms"] == 6.0
    assert filtered["eligibility_over_budget"] is True


def test_a_filtered_series_with_no_eligibility_stage_at_all_is_not_a_pass() -> None:
    """The premise cannot be certified from a stage that never ran.

    Reproduced end to end with `FILTER_PROJECTS` emptied as the single
    mutation: `eligibility_ms=0.0`, no `filter_eligibility` in `stage_sources`,
    verdict PASSED. An unfiltered recall opens no `filter_eligibility` span at
    all — `find.py:1670` opens it only when `filter_plan.root is not None` — so
    the source check was skipped by `if source is not None` and the duration
    check read the `0.0` default as a measurement comfortably under 20 ms. The
    hits guard cannot catch it either: an unfiltered recall returns plenty.
    """
    report = _report()
    report["series"]["filtered_hybrid"]["stage_sources"] = {"rerank": "computed"}

    with pytest.raises(SystemExit, match="no filter_eligibility stage"):
        gate.check(report)


def test_a_filtered_series_with_no_eligibility_duration_is_not_a_pass() -> None:
    """An absent duration is not a stage that ran in no time.

    The same zero-default shape, two lines below the one above: `0.0` passes
    `> 20.0` and reads in the artifact as the fastest eligibility lookup the
    cell has ever performed.
    """
    with pytest.raises(SystemExit, match="no filter_eligibility duration"):
        gate.check(_report(filtered_eligibility_ms=None))


def test_the_series_emits_no_eligibility_duration_when_the_stage_never_reported() -> None:
    """What `run_series` emits, driven through the real series rather than a stub report.

    The node above perturbs a hand-built report and so pins `check`'s reading
    of None, not the production of it. `run_series` used to round an empty
    collection to `0.0`, which is the same zero-default one layer up: a stage
    that never ran, reported as the fastest eligibility lookup the cell has
    ever performed. A mutant restoring that survived every other node here.
    """
    report = gate.run(
        transport=_StubTransport(stage_ms={}),
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    assert report["series"]["filtered_hybrid"]["eligibility_ms"] is None
    with pytest.raises(SystemExit, match="no filter_eligibility duration"):
        gate.check(report)


def test_a_qualified_eligibility_stage_name_is_still_held_to_its_source() -> None:
    """The rename hazard, guarded here the way `_is_walker` guards it elsewhere.

    `filters.filter_eligibility: declined` PASSED the bare-key lookup while
    plain `filter_eligibility: declined` correctly failed — the one place in
    the file undefended against a hazard the same file guards twice.
    """
    report = _report()
    report["series"]["filtered_hybrid"]["stage_sources"] = {
        "filters.filter_eligibility": "declined"
    }

    with pytest.raises(SystemExit, match="filters.filter_eligibility"):
        gate.check(report)


# --- The browse series the contract states no ceiling for -------------------


def test_the_contract_states_no_ceiling_for_the_browse_shape() -> None:
    """Browse remains measured under a pending threshold."""
    assert "browse" in gate.SERIES_SHAPES
    assert "browse" not in gate._P50_TARGETS
    assert gate.UNCEILINGED_SHAPES == frozenset({"browse"})
    assert set(gate._P50_TARGETS) | gate.UNCEILINGED_SHAPES == set(gate.SERIES_SHAPES)


def test_a_slow_browse_does_not_fail_a_ceiling_it_was_never_given() -> None:
    """The counter-case: no invented ceiling means no invented breach."""
    gate.check(_report(browse_p50=5_000.0))


def test_the_browse_series_is_still_held_to_every_structural_rule() -> None:
    """No ceiling is not no check. It is the shape the walk fix was about."""
    for perturbation, expected in (
        ({"shape_hits_p50": {"browse": 0}}, "browse series' median sample"),
        ({"stage_sources": {"keyword": "computed"}}, "stage keyword reported source computed"),
    ):
        with pytest.raises(SystemExit, match=expected):
            gate.check(_report(**perturbation))

    report = _report()
    report["series"]["browse"]["warming"] = 2
    with pytest.raises(SystemExit, match="browse returned 2 warming"):
        gate.check(report)


def test_the_browse_series_asks_the_empty_query_shape() -> None:
    """`find.py` routes on `query.strip()`, so the shape IS an empty strip.

    Every other series passes a non-empty nonce, which means `_find_keyword`
    always took its `if query_norm:` arm and the branch that can report
    `computed` was unreachable from any series the gate ran — while this change
    ships a fix for a whole-scope walk on exactly that branch.
    """
    transport = _StubTransport()

    gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    browse = [call for call in transport.calls if call["shape"] == "browse"]
    assert len(browse) == gate.SAMPLES_PER_SERIES
    for call in browse:
        assert call["query"].strip() == "", repr(call["query"])
        assert call["mode"] == "hybrid", "the browse shape is the schema default, not keyword"
        assert not call["projects"]


def test_the_keyword_series_asks_something_the_corpus_can_answer() -> None:
    """A keyword query built only from a nonce matches nothing, on any cell.

    Keyword recall is pure lexical matching, so the novelty that defeats the
    result cache cannot live in a word. It lives in trailing whitespace, which
    `find.py`'s `request_key` distinguishes and `query.lower().strip()` does
    not — so every sample misses the cache and every sample asks the same thing
    of the corpus.
    """
    transport = _StubTransport()

    gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    keyword = [call for call in transport.calls if call["shape"] == "keyword"]
    assert len(keyword) == gate.SAMPLES_PER_SERIES
    assert len({call["query"] for call in keyword}) == gate.SAMPLES_PER_SERIES
    for call in keyword:
        assert call["query"].strip() == gate.KEYWORD_TERM, repr(call["query"])


def test_a_verdict_taken_at_another_limit_is_refused() -> None:
    """The limit is part of the ceiling's definition, not a knob beside it.

    `rerank` scales with the candidate count, so a smaller limit buys a smaller
    p50 directly. A stub cell costing 1200 ms per request passes every ceiling
    at `--limit 1` while failing four of them at the default; the flag stays
    for exploratory runs, and `check` refuses to certify anything but the
    limit the ceilings are defined at.
    """
    with pytest.raises(SystemExit, match="limit"):
        gate.check(_report(limit=1))


@pytest.mark.parametrize("shape", sorted(gate.SERIES_SHAPES))
def test_a_series_that_matched_nothing_is_not_a_pass(shape: str) -> None:
    """An empty result set is fast for reasons that have nothing to do with the read path.

    Parametrized over EVERY series, because the guard used to sit inside `if
    shape == "filtered_hybrid":` while `run_series` emitted `hits` for all of
    them. The shape most likely to return nothing was the one outside the
    branch: `keyword` is pure lexical matching and carries the tightest ceiling
    the contract states, and it measured 0 hits in 47.3 ms against the fixture
    vault — certified at 2.5x inside 120 ms.
    """
    with pytest.raises(SystemExit, match=f"{shape} series' median sample returned no hits"):
        gate.check(_report(shape_hits_p50={shape: 0}))


def test_a_series_whose_median_sample_is_empty_fails_even_with_hits_in_it() -> None:
    """The guard is on the percentile the gate certifies, not on a sum.

    `hits` summed across thirty samples lets one hit clear a check whose whole
    purpose is to prove the p50 measured a real retrieval — weaker than the
    guard's own description of itself. Twenty-nine empty samples and one with
    four hits is exactly the shape that reads as measured and is not.
    """
    with pytest.raises(SystemExit, match="median sample returned no hits"):
        gate.check(_report(shape_hits_p50={"keyword": 0}, filtered_hits=4))


def test_a_series_with_a_non_empty_median_passes() -> None:
    """The counter-case, so the guard above is a measurement and not a constant."""
    gate.check(_report(shape_hits_p50={shape: 1 for shape in gate.SERIES_SHAPES}))


def test_the_series_reports_the_median_sample_and_the_total_separately() -> None:
    """What `run_series` collects, driven through the real series rather than a stub report.

    The nodes above perturb a hand-built report, so they pin `check`'s reading
    and say nothing about what produced the number. This drives the series: one
    sample in thirty carries forty hits and the other twenty-nine carry none,
    which is exactly the shape a sum reads as measured and a median reads as
    empty. A mutant that collected the total under the median's name survived
    every other node in this file.
    """
    transport = _StubTransport(hits=0, hits_at={("keyword", 7): 40})

    report = gate.run(
        transport=transport,
        load_source=_StubLoad([1.0] * 500),
        quiescence_bound_seconds=1.0,
        sleep=lambda _seconds: None,
    )

    keyword = report["series"]["keyword"]
    assert keyword["hits"] == 40, "the run total is what the diagnostic column is for"
    assert keyword["hits_p50"] == 0, (
        "the guarded quantity is the median sample, and this run's median was empty"
    )
    with pytest.raises(SystemExit, match="keyword series' median sample returned no hits"):
        gate.check(report)


@pytest.mark.parametrize("total", [None, -1, float("inf"), float("nan")])
def test_unknown_timing_is_distinct_from_typed_warming(total) -> None:
    sample = gate._sample_from_envelope({"hits": [], "timings": {"total_ms": total, "stages": {}}})
    assert sample.elapsed_ms is None and sample.warming is False
    warming = gate._sample_from_envelope({"warming": {"status": "warming"}})
    assert warming.elapsed_ms is None and warming.warming is True


def test_a_real_total_is_not_read_as_warming() -> None:
    """The counter-case, so the guard above is a test and not a constant."""
    sample = gate._sample_from_envelope(
        {"hits": [1, 2, 3], "timings": {"total_ms": 42.5, "stages": {}}}
    )

    assert sample.warming is False
    assert sample.elapsed_ms == 42.5
    assert sample.hits == 3


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


def test_a_corpus_size_that_could_not_be_derived_is_refused_by_name() -> None:
    """ "Unverified" and "too small" are different answers and must read differently.

    `ServedTransport` used to count `.md` files under the gate process's own
    `EXOMEM_VAULT_PATH` while the series ran against a cell over HTTP, so the
    8,000-page premise could be certified from vault A while every latency came
    from cell B. It now asks the measured cell, and says so when it cannot.
    """
    with pytest.raises(SystemExit, match="could not be derived from the cell"):
        gate.check(_report(pages=None))


def test_the_served_transport_derives_its_corpus_size_from_the_cell() -> None:
    """The premise and the percentiles must come from the same cell.

    Driven through the transport's own HTTP seam rather than asserted about it:
    the recorded calls are what prove the count came from the cell that will
    answer the series, and not from this process's environment.
    """
    transport = gate.ServedTransport("http://cell.invalid", "key")
    posted: list[tuple[str, dict]] = []

    def _fake_post(payload, *, command="ask_memory"):
        posted.append((command, payload))
        if payload.get("path") == "":
            return {"kb": {"present": True, "path": "Knowledge Base"}}
        return {"totals": {"markdown": 8_421}}

    transport._post = _fake_post  # type: ignore[method-assign]

    assert transport.measure_corpus() == 8_421
    assert [command for command, _ in posted] == ["browse_memory", "browse_memory"]
    assert posted[1][1]["path"] == "Knowledge Base"


def test_a_cell_that_will_not_report_its_size_yields_no_number() -> None:
    """A refusal to answer must not become a zero that reads like a small cell."""
    transport = gate.ServedTransport("http://cell.invalid", "key")

    def _refusing_post(payload, *, command="ask_memory"):
        raise SystemExit("recall latency gate failed: the cell refused the read")

    transport._post = _refusing_post  # type: ignore[method-assign]

    assert transport.measure_corpus() is None


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
        hits: int = 4,
        hits_at: dict[tuple[str, int], int] | None = None,
        stage_ms: dict[str, float] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._warming_at = warming_at or set()
        self._elapsed_ms = elapsed_ms
        self._warming_ms = warming_ms
        self._hits = hits
        self._hits_at = hits_at or {}
        self._stage_ms = {"filter_eligibility": 3.0} if stage_ms is None else stage_ms

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
            stage_ms=dict(self._stage_ms),
            hits=self._hits_at.get((shape, index), self._hits),
        )


def _series(
    *,
    p50: float,
    p95: float,
    sources: dict[str, str],
    warming: int = 0,
    hits_p50: int = 4,
    hits: int = 120,
) -> dict:
    return {
        "samples": 30,
        "warming": warming,
        "p50_ms": p50,
        "p95_ms": p95,
        "load_mean": 1.0,
        "load_max": 1.2,
        "stage_sources": sources,
        "hits": hits,
        "hits_p50": hits_p50,
    }


def _report(
    *,
    hybrid_p50: float = 100.0,
    warming: int = 0,
    stage_sources: dict[str, str] | None = None,
    filtered_eligibility_ms: float | None = 3.0,
    pages: int | None = 8_192,
    limit: int = gate.DEFAULT_LIMIT,
    filtered_hits: int = 120,
    filtered_hits_p50: int = 4,
    shape_hits_p50: dict[str, int] | None = None,
    browse_p50: float = 90.0,
) -> dict:
    """A passing report, minimally perturbed by each node above."""
    sources = {"filter_eligibility": "index"}
    sources.update(stage_sources or {})
    medians = {shape: 4 for shape in gate.SERIES_SHAPES}
    medians.update(shape_hits_p50 or {})
    filtered = _series(
        p50=110.0,
        p95=130.0,
        sources=sources,
        hits=filtered_hits,
        hits_p50=medians.get("filtered_hybrid", filtered_hits_p50),
    )
    filtered["eligibility_ms"] = filtered_eligibility_ms
    return {
        "refused": False,
        "transport": "stub",
        "limit": limit,
        "pages": pages,
        "series": {
            "hybrid": _series(
                p50=hybrid_p50,
                p95=120.0,
                sources=sources,
                warming=warming,
                hits_p50=medians["hybrid"],
            ),
            "keyword": _series(p50=40.0, p95=60.0, sources=sources, hits_p50=medians["keyword"]),
            "filtered_hybrid": filtered,
            "browse": _series(
                p50=browse_p50, p95=140.0, sources=sources, hits_p50=medians["browse"]
            ),
        },
    }


# The paired instrument uses independent cases, never repeat-level pseudo-cases.
def _paired_samples(scale: float = 1.0, *, series: str = "hybrid") -> list[dict]:
    return [
        {"case": f"case-{case:02d}", "series": series, "principal": "owner",
         "arm": arm, "repeat": repeat, "state": "ok",
         "elapsed_ms": (100 + case) * (scale if arm == "head" else 1),
         "stages": {"governed-stage-vector.embed": {"ms": 30 * (scale if arm == "head" else 1)}}}
        for case in range(40) for repeat in range(3) for arm in ("base", "head")
    ]


def _accept_record(series: str = "hybrid", maximum: float = 1.35) -> dict:
    return {"tag": "v1.0.0", "series": [{"name": series, "maximum_ratio": maximum}],
            "full_ci_run": "https://github.com/example/project/actions/runs/123",
            "reason": "A reviewed correctness repair adds work.",
            "pull_request": "https://github.com/example/project/pull/456"}


def test_self_pairs_pass_and_repeats_do_not_inflate_the_case_count() -> None:
    pytest.importorskip("scipy")
    report, = gate.compare_pairs(_paired_samples(), tag="v1.0.0")
    assert report["state"] == "passed"
    assert report["cases"] == 40
    assert report["ratio"] == report["lower"] == report["upper"] == 1.0


@pytest.mark.parametrize("allowance,expected", [
    (None, "failed"), (_accept_record(), "accepted"),
    (_accept_record("keyword"), "failed"), (_accept_record(maximum=1.20), "failed"),
    ({**_accept_record(), "tag": "v0.9.0"}, "failed"),
])
def test_a_thirty_percent_slowdown_needs_the_current_series_allowance(allowance, expected) -> None:
    pytest.importorskip("scipy")
    report, = gate.compare_pairs(_paired_samples(1.3), tag="v1.0.0", accept=allowance)
    assert report["state"] == expected
    assert report["ratio"] == pytest.approx(1.3)


@pytest.mark.parametrize("damage", ["missing-repeat", "missing-arm", "nan", "unknown"])
def test_a_broken_sample_cannot_be_averaged_into_a_passing_pair(damage: str) -> None:
    pytest.importorskip("scipy")
    samples = _paired_samples()
    if damage == "missing-repeat":
        samples.pop()
    elif damage == "missing-arm":
        samples = [row for row in samples if not (row["case"] == "case-00" and row["arm"] == "base")]
    elif damage == "nan":
        samples[0]["elapsed_ms"] = float("nan")
    else:
        samples[0]["state"] = "unknown"
    report, = gate.compare_pairs(samples, tag="v1.0.0")
    assert report["state"] == "failed"


def test_a_missing_tag_stage_is_not_comparable_and_stages_never_gate() -> None:
    pytest.importorskip("scipy")
    samples = _paired_samples()
    for row in samples:
        row["stages"] = ({"governed-stage-parent_hints": {"ms": 200.0}}
                         if row["arm"] == "head" else {})
    report, = gate.compare_pairs(samples, tag="v1.0.0")
    assert report["state"] == "passed"
    assert report["stages"]["governed-stage-parent_hints"]["state"] == "not comparable"


def test_three_repeats_of_nineteen_cases_stay_insufficient() -> None:
    samples = [row for row in _paired_samples() if int(row["case"].split("-")[1]) < 19]
    report, = gate.compare_pairs(samples, tag="v1.0.0")
    assert report["state"] == "insufficient samples"
    assert report["cases"] == 19


@pytest.mark.parametrize("warming,expected", [(1, "passed"), (2, "failed")])
def test_paired_warming_is_excluded_and_keeps_the_contract_bound(warming, expected) -> None:
    pytest.importorskip("scipy")
    samples = _paired_samples()
    for row in samples[:warming]:
        row["state"] = "warming"
    report, = gate.compare_pairs(samples, tag="v1.0.0")
    assert report["state"] == expected
    assert report["warming"] == warming
    assert report["unknown_samples"] == 0
    assert report["cases"] == 39


def test_expired_accept_record_is_visible_and_has_no_allowance(tmp_path: Path) -> None:
    path = tmp_path / "accept.json"
    path.write_text(json.dumps(_accept_record()))
    allowance, state = gate.read_accept("v1.0.1", path)
    assert allowance is None and state == "expired"


def test_accept_record_shape_and_rounding_cannot_silently_widen_it(tmp_path: Path) -> None:
    from jsonschema import ValidationError

    path = tmp_path / "accept.json"
    path.write_text(json.dumps({**_accept_record(), "dispatch_waiver": True}))
    with pytest.raises(ValidationError):
        gate.read_accept("v1.0.0", path)
    path.write_text(json.dumps(_accept_record(maximum=1.31)))
    with pytest.raises(ValueError, match="rounding"):
        gate.read_accept("v1.0.0", path)


def test_accept_margin_resolves_from_the_canonical_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gate, "PAIRING", {**gate.PAIRING, "margin": 1.5})
    path = tmp_path / "accept.json"
    path.write_text(json.dumps(_accept_record(maximum=1.35)))
    with pytest.raises(gate.MeasurementRefused):
        gate.read_accept("v1.0.0", path)


@pytest.mark.parametrize("maximum", [float("inf"), float("nan"), 1e308])
def test_accept_ratios_must_support_finite_contract_rounding(tmp_path: Path, maximum) -> None:
    path = tmp_path / "accept.json"
    path.write_text(json.dumps(_accept_record(maximum=maximum)))
    with pytest.raises(gate.MeasurementRefused):
        gate.read_accept("v1.0.0", path)


def _absolute_samples(*, delay: float | None = 0.0) -> list[dict]:
    return [{"principal": "owner", "series": "hybrid", "state": "ok", "elapsed_ms": 90.0,
             "runqueue_ms": delay, "stages": {}, "critical_path": [], "load": 1.0}
            for _ in range(40)]


def test_sqlite_outside_the_reference_cannot_receive_a_ceiling_verdict() -> None:
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    state, summaries = gate.summarize_absolute(_absolute_samples(),
        environment={"sqlite_version": "3.53.0"}, profile=profile)
    assert state == "not measured"
    assert summaries[0]["ceiling_met"] is None


def test_reference_run_keeps_its_own_pool_contention_and_live_run_discloses_exclusions() -> None:
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    samples = _absolute_samples()
    samples[0].update(elapsed_ms=800.0, runqueue_ms=100.0)
    _, reference = gate.summarize_absolute(samples, environment={"sqlite_version": "3.45.1"}, profile=profile)
    _, live = gate.summarize_absolute(samples, environment={"sqlite_version": "3.45.1"}, profile=profile, live=True)
    assert reference[0]["contended"] == 0
    assert reference[0]["samples"] == 40
    assert live[0]["contended"] == 1
    assert live[0]["samples"] == 39
    assert live[0]["p95_including_contention_ms"] >= live[0]["p95_ms"]


def test_more_than_ten_percent_live_contention_refuses_ceiling_comparisons() -> None:
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    samples = _absolute_samples()
    for sample in samples[:5]:
        sample["runqueue_ms"] = 20.0
    state, summaries = gate.summarize_absolute(samples, environment={"sqlite_version": "3.45.1"}, profile=profile, live=True)
    assert state == "refused"
    assert summaries[0]["p95_ms"] is None
    assert summaries[0]["ceiling_met"] is None


def test_unknown_live_runqueue_is_visible_and_cannot_claim_uncontended() -> None:
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    state, summaries = gate.summarize_absolute(_absolute_samples(delay=None),
        environment={"sqlite_version": "3.45.1"}, profile=profile, live=True)
    assert state == "refused"
    assert summaries[0]["unknown_contention"] == 40
    assert summaries[0]["ceiling_met"] is summaries[0]["p95_ms"] is None


def test_hydration_tail_and_unbudgeted_work_reach_the_stage_report() -> None:
    from exomem.recall_timing import Interval, Trace

    trace = Trace(known_serial=True)
    trace.started, trace.ended = 0, 44_000_000
    trace.edges = [(n * 1_000_000, n * 1_000_000) for n in (0, 1, 21, 41, 44)]
    trace.intervals = [Interval(name, 1, None, start * 1_000_000, end * 1_000_000)
                       for name, start, end in (("filter_hits", 0, 1), ("release_gate", 1, 21),
                                                ("serialize", 21, 41), ("matched_units", 41, 44))]
    accounting = trace.result(gate.REFERENCE["stage_rows"])
    samples = [{**sample, "stages": accounting["stages"]} for sample in _absolute_samples()]
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    state, summaries = gate.summarize_absolute(samples, environment={"sqlite_version": "3.45.1"}, profile=profile)
    assert state == "passed"
    hydration = summaries[0]["stages"]["governed-stage-hydration"]
    assert hydration["p95_ms"] == 41.0 and hydration["state"] == "failed"
    unbudgeted = summaries[0]["stages"]["unbudgeted:matched_units"]
    assert unbudgeted["state"] == "unbudgeted"
    assert unbudgeted["p50_ms"] == unbudgeted["p95_ms"] == 3.0
    assert unbudgeted["budget_ms"] is None


def test_encode_floor_is_measured_without_applying_the_twice_budget_rule() -> None:
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    samples = _absolute_samples()
    for sample in samples:
        sample["stages"] = {name: {"ms": duration, "cpu_ms": duration} for name, duration in
                            (("governed-stage-vector.embed", 100.0),
                             ("governed-stage-bm25", 20.0), ("governed-stage-keyword", 20.0))}
    state, summaries = gate.summarize_absolute(samples,
        environment={"sqlite_version": "3.45.1", "cpus": [0, 1]}, profile=profile)
    assert state == "passed"
    for row in samples[0]["stages"]:
        assert summaries[0]["stages"][row]["state"] == "passed"
        assert summaries[0]["stages"][row]["target_met"] is False
        assert summaries[0]["stages"][row]["twice_budget_applied"] is False
    assert summaries[0]["encode_floor"] == {
        "state": "measured", "p95_ms": 100.0, "budget_ms": 40.0,
        "samples": 40, "source": "retained-reference-series"}
    _, live = gate.summarize_absolute(samples,
        environment={"sqlite_version": "3.45.1", "cpus": [0, 1]}, profile=profile, live=True)
    assert live[0]["encode_floor"]["state"] == "not measured"
    assert live[0]["encode_floor"]["p95_ms"] is None


def test_unknown_paths_do_not_dilute_the_known_path_share() -> None:
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    samples = _absolute_samples()
    for index, sample in enumerate(samples):
        sample["stages"] = {"governed-stage-graph": {"ms": 2.0, "cpu_ms": 1.0}}
        sample["critical_path"] = ["governed-stage-graph"] if index == 0 else None
    _, summaries = gate.summarize_absolute(samples, environment={"sqlite_version": "3.45.1"}, profile=profile)
    graph = summaries[0]["stages"]["governed-stage-graph"]
    assert graph["critical_path_share"] == 1.0 and graph["critical_path_samples"] == 1
    samples[0]["critical_path"] = None
    _, summaries = gate.summarize_absolute(samples, environment={"sqlite_version": "3.45.1"}, profile=profile)
    assert summaries[0]["stages"]["governed-stage-graph"]["critical_path_share"] is None


def test_paired_path_shares_also_exclude_unknown_paths() -> None:
    samples = [row for row in _paired_samples() if row["case"] == "case-00"]
    for sample in samples:
        sample["critical_path"] = (["governed-stage-vector.embed"]
                                   if sample["arm"] == "head" and sample["repeat"] == 0 else None)
    report, = gate.compare_pairs(samples, tag="v1.0.0")
    encode = report["stages"]["governed-stage-vector.embed"]
    assert encode["head_critical_path_samples"] == 1 and encode["head_critical_path_share"] == 1.0
    assert encode["base_critical_path_samples"] == 0 and encode["base_critical_path_share"] is None


def test_repeat_validation_uses_the_canonical_contract(monkeypatch) -> None:
    monkeypatch.setattr(gate, "PAIRING", {**gate.PAIRING, "repeats": 5})
    sample = {"case": "case-00", "series": "hybrid", "principal": "owner", "arm": "head",
              "repeat": 4, "load": 1.0, "elapsed_ms": None, "state": "unknown", "cpu_ms": None,
              "cpu_source": "unknown", "runqueue_ms": None, "unattributed_ms": None,
              "overlap_cpu_ms": None, "unattributed_cpu_ms": None, "unbudgeted": [],
              "critical_path": None, "stages": {}}
    report = {"schema_version": 1, "mode": "paired", "state": "failed",
              "contract_revision": gate.CONTRACT.revision, "series": [], "samples": [sample]}
    gate.validate_document(report, "summary.schema.json")
    sample["repeat"] = 5
    with pytest.raises(gate.MeasurementRefused, match="repeat-outside-contract"):
        gate.validate_document(report, "summary.schema.json")


def _live_inputs(tmp_path: Path) -> tuple[Path, Path]:
    cases = [{"series": series, "query": "neutral query"}
             for series in {row["series"] for row in json.loads((gate.BENCHMARK / "cases.json").read_text())}
             for _ in range(gate.REFERENCE["queries_min"])]
    cases_path, corpus_path = tmp_path / "cases.json", tmp_path / "corpus.json"
    cases_path.write_text(json.dumps(cases))
    corpus_path.write_text(json.dumps({"source": "served-state-read-only-sql", "bucket_width": 500,
        "sqlite_version": "3.45.1", "pages": gate.REFERENCE["pages_min"],
        "chunks": gate.REFERENCE["chunks_min"], "units": gate.REFERENCE["units_min"]}))
    return cases_path, corpus_path


@pytest.mark.parametrize("damage", [None, "degraded", "stage-error"])
def test_live_inner_timings_preserve_failures_without_certifying_the_bound_call(
    tmp_path: Path, monkeypatch, damage,
) -> None:
    cases, corpus = _live_inputs(tmp_path)
    monkeypatch.setattr(gate, "one_minute_load", lambda: 1.0)
    timing = {"total_ms": 90.0, "runqueue_ms": 0.0, "sqlite_version": "3.45.1",
              "stage_rows": {"governed-stage-keyword": {"ms": 5.0, "cpu_ms": None}},
              "stages": {"vector": {"error": "ImportError"}} if damage == "stage-error" else {}}
    envelope = {"hits": [{"title": "private result"}], "timings": timing}
    if damage == "degraded":
        envelope["degraded"] = ["vector"]
    transport = gate.ServedTransport("https://example.invalid", "private credential")
    monkeypatch.setattr(transport, "_post", lambda arguments: envelope)
    output = tmp_path / "summary.json"
    assert gate.live_cli(transport, cases_path=cases, corpus_path=corpus, output=output, quiescence_bound=0) == 0
    report = json.loads(output.read_text())
    assert report["state"] == ("failed" if damage else "not measured")
    assert report["boundary"] == "response-inner-timings-v1"
    assert all(row["ceiling_met"] is None and row["p95_target_met"] is None
               and row["p50_target_met"] is None for row in report["series"])
    assert all(row["unknown_samples"] == (gate.REFERENCE["queries_min"] if damage else 0)
               for row in report["series"])
    assert all(sample["state"] == ("unknown" if damage else "ok") for sample in report["samples"])
    assert "private" not in output.read_text() and "ImportError" not in output.read_text()


@pytest.mark.parametrize("failure, expected, reason", [
    (None, "refused", "served-read-refused"),
    (TimeoutError("private URL and credential"), "failed", "served-timeout"),
    (IncompleteRead(b"private response body", 100), "failed", "served-network-error"),
    (ConnectionResetError("private URL and credential"), "failed", "served-network-error"),
])
def test_live_transport_failures_write_valid_content_free_attempts(
    tmp_path: Path, monkeypatch, failure, expected, reason,
) -> None:
    import io
    import urllib.request

    cases, corpus = _live_inputs(tmp_path)
    monkeypatch.setattr(gate, "one_minute_load", lambda: 1.0)
    class Response(io.BytesIO):
        def read(self, *args, **kwargs):
            if failure is not None:
                raise failure
            return super().read(*args, **kwargs)

    def response(*args, **kwargs):
        return Response(json.dumps({"success": False, "error": "private refusal"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", response)
    output = tmp_path / "summary.json"
    transport = gate.ServedTransport("https://example.invalid", "private credential")
    assert gate.live_cli(transport, cases_path=cases, corpus_path=corpus, output=output, quiescence_bound=0) == 0
    report = json.loads(output.read_text())
    gate.validate_document(report, "summary.schema.json")
    assert report["state"] == expected
    assert len(report["samples"]) == 1
    sample, = report["samples"]
    assert sample["state"] == "unknown" and sample["elapsed_ms"] is None
    assert sample["read_error"] == reason
    assert "private" not in output.read_text() and "https://" not in output.read_text()


def test_content_free_summary_schema_rejects_raw_queries_and_paths() -> None:
    from jsonschema import ValidationError

    report = {"schema_version": 1, "mode": "live", "state": "not measured",
              "contract_revision": gate.CONTRACT.revision, "series": [], "samples": []}
    gate.validate_document(report, "summary.schema.json")
    for key in ("query", "path", "title", "excerpt", "credential"):
        with pytest.raises(ValidationError):
            gate.validate_document({**report, key: "private text"}, "summary.schema.json")


def test_missing_prose_refuses_before_a_worker_or_encoder_starts(tmp_path: Path, monkeypatch) -> None:
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    profile["source"]["sha256"] = None
    (tmp_path / "profile.json").write_text(json.dumps(profile))
    with pytest.raises(gate.MeasurementRefused, match="reference-prose"):
        gate.reference_cases(profile, tmp_path)

    reference_cases = gate.reference_cases
    monkeypatch.setattr(gate, "reference_cases", lambda _profile: reference_cases(profile, tmp_path))
    output = tmp_path / "absolute.json"
    assert gate.absolute_cli(output, quiescence_bound=0) == 0
    report = json.loads(output.read_text())
    assert report["state"] == "refused"
    assert report["reason"] == "reference-prose-unavailable"


def test_worker_sample_includes_bound_command_logging(tmp_path: Path, monkeypatch, warm_managed_cell) -> None:
    from types import SimpleNamespace

    from synth_vault import gen_dense_vault

    from exomem import command_surface, commands

    gen_dense_vault(tmp_path, 3)
    warm_managed_cell(tmp_path)
    command = command_surface.resolve_command("ask_memory", commands.PRODUCT_COMMANDS)
    ask_memory = command_surface.bind_vault(command.leaf, tmp_path, command=command)
    clock = [0]

    def log_success(**_kwargs):
        clock[0] += 25_000_000

    monkeypatch.setattr(command_surface, "_log_tool_success", log_success)
    monkeypatch.setattr(gate, "time", SimpleNamespace(perf_counter_ns=lambda: clock[0]))
    sample = gate._worker_sample(ask_memory, {
        "principal": "owner", "query": "topic", "arguments": {"mode": "keyword"},
        "rows": {"keyword": ["keyword"]},
    })
    assert sample["state"] == "ok"
    assert sample["elapsed_ms"] == 25.0


def test_reference_admitted_principal_receives_the_owners_released_hits(
    tmp_path: Path, warm_managed_cell,
) -> None:
    """A withheld-only admitted series times notices and empty unit results."""
    from synth_vault import gen_reference_vault, reference_source

    from exomem import command_surface, commands
    from exomem.governance import principal

    profile = {**json.loads((gate.BENCHMARK / "profile.json").read_text()), "pages": 40}
    words, _digest = reference_source(profile, gate.BENCHMARK)
    with principal.library_scope():
        gen_reference_vault(tmp_path, profile, words)
    warm_managed_cell(tmp_path)
    command = command_surface.resolve_command("ask_memory", commands.PRODUCT_COMMANDS)
    ask_memory = command_surface.bind_vault(command.leaf, tmp_path, command=command)
    unit_page = next(tmp_path.glob("Knowledge Base/**/sample-00000.md")).read_text()
    unit_text = next(line for line in unit_page.splitlines() if line.startswith("- ["))
    query = max(unit_text.split("] ", 1)[1].split()[:6], key=len).strip(".,;:!?()")
    callers = {"owner": principal.owner_principal(surface="library"),
               "admitted": principal.RequestPrincipal(audience_id="benchmark-admitted", surface="mcp")}
    for level in ("unit", "mixed"):
        hits = {}
        for name, caller in callers.items():
            with principal.request_scope(caller), principal.invocation_scope():
                hits[name] = ask_memory(query=query, include_timings=False, limit=gate.DEFAULT_LIMIT,
                                        rerank=False, mode="keyword", result_level=level)
        assert any(hit.get("result_type") == "semantic_unit" for hit in hits["owner"])
        assert hits["admitted"] == hits["owner"]


@pytest.mark.skipif(not hasattr(gate.os, "sched_getaffinity"), reason="The worker uses the Linux reference profile")
@pytest.mark.parametrize("persisted", [False, True])
def test_worker_prepares_persisted_advisory_before_managed_admission(
    tmp_path: Path, monkeypatch, persisted: bool,
) -> None:
    from types import SimpleNamespace

    import managed_recall
    import numpy as np
    import synth_vault

    from exomem import due_state, embedding_backend, embeddings, recall_space

    (tmp_path / "Knowledge Base").mkdir()
    identity = recall_space.SpaceIdentity(recall_space.LEGACY_MODEL, "reference-space", 2)
    monkeypatch.setattr(recall_space, "current_identity", lambda _dim: identity)
    rel = "Knowledge Base/reference.md"
    (tmp_path / rel).write_text("---\ntitle: Reference\n---\nReference passage.\n")
    vector = np.asarray([[1.0, 0.0]], dtype=np.float32)
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    for name in embedding_backend._ARTIFACT_FILES:
        (artifact_root / name).write_bytes(b"small test artifact")
    artifact = SimpleNamespace(
        artifact_digest=embedding_backend._artifact_digest(artifact_root),
        fingerprint=lambda: identity.fingerprint,
    )
    index = embeddings.EmbeddingIndex(tmp_path)
    index.upsert_file(rel, ["Reference passage."], vector, 1.0)
    monkeypatch.setattr(index, "rebuild_all", lambda: None)
    monkeypatch.setattr(embeddings, "get_model", lambda: SimpleNamespace(profile=artifact))
    monkeypatch.setattr(embeddings, "embed_texts", lambda *_args, **_kwargs: vector)
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: index)
    monkeypatch.setattr(synth_vault, "reference_source", lambda *_args: (["reference"], "source-digest"))
    monkeypatch.setattr(synth_vault, "gen_reference_vault", lambda *_args: [rel])
    # This preparation fixture has one real page and one stored vector.
    census = {
        "pages": 1, "chunks": 1, "units": 0, "unit_pages": 0,
        "vector_units": 0, "median_page_bytes": (tmp_path / rel).stat().st_size, "spot_checks": 1,
        "vocabulary_terms": 1, "wikilinks_min": 0, "wikilinks_max": 0,
        "transcript_pages": 0, "content_digest": "0" * 64,
    }
    monkeypatch.setattr(gate, "_stored_corpus", lambda *_args: census)
    if not persisted:
        monkeypatch.setattr(due_state, "save", lambda *_args: None)
    admitted = []

    def enter_managed(root, *, full_startup):
        assert due_state.load(root) is not None
        admitted.append(full_startup)

    monkeypatch.setattr(managed_recall, "enter_managed_recall", enter_managed)
    if persisted:
        environment = gate._prepare_worker(tmp_path, {"profile": {}})
        assert admitted == [True]
        try:
            # The first managed selection must use the prepared dense lane.
            selected = index.select_chunks(
                vector[0], 1, allowed_paths={rel}, encoded_for=identity, defer_cold_load=True,
            )
        finally:
            loading = index._matrix_loading()
            if loading is not None:
                loading.join(timeout=30)
        assert [row[0] for row in selected] == [rel]
        startup = {
            "runtime_prefix": sys.prefix,
            "runtime_lock_digest": gate._runtime_lock_digest(ROOT),
            "runtime_policy": "parent-benchmark",
        }
        environment.update(gate._runtime_provenance(ROOT, startup))
        report = {
            "schema_version": 1, "mode": "absolute", "state": "not measured",
            "contract_revision": gate.CONTRACT.revision, "series": [], "samples": [],
            "environment": {"head": environment},
        }
        gate.validate_document(json.loads(json.dumps(report, allow_nan=False)), "summary.schema.json")
    else:
        with pytest.raises(gate.MeasurementRefused, match="advisory-projection-unavailable"):
            gate._prepare_worker(tmp_path, {"profile": {}})
        assert admitted == []


@pytest.mark.skipif(not hasattr(gate.os, "sched_getaffinity"), reason="The worker uses the Linux reference profile")
@pytest.mark.parametrize("invalid_cpus", [False, True])
def test_actual_worker_bootstrap_reports_failures_before_model_preparation(tmp_path: Path, invalid_cpus) -> None:
    import subprocess

    environment = {key: gate.os.environ[key] for key in ("PATH", "HOME") if key in gate.os.environ}
    environment.update(EXOMEM_STATE_ROOT=str(tmp_path / "state"), EXOMEM_VAULT_PATH=str(tmp_path / "vault"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "recall_latency_gate.py"), "--worker-source", str(ROOT),
         "--worker-cpus", "{" if invalid_cpus else json.dumps(
             sorted(gate.os.sched_getaffinity(0))[:gate.REFERENCE["reference_cpus"]]),
         "--worker-vault", str(tmp_path / "vault")],
        input="{", env=environment, cwd=tmp_path, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response["state"] == "worker-error"
    assert response["error_class"] == "JSONDecodeError"
    assert response["phase"] == ("worker-bootstrap" if invalid_cpus else "worker-startup")
    if invalid_cpus:
        assert response["reason"] == "benchmark-worker-bootstrap-failed"


@pytest.mark.skipif(not hasattr(gate.os, "sched_getaffinity"), reason="The worker uses the Linux reference profile")
def test_worker_uses_the_selected_runtime_and_preserves_closed_failures(tmp_path: Path, monkeypatch) -> None:
    import subprocess
    import types
    import venv

    runtime = tmp_path / "tag-runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    python = runtime / "bin" / "python"
    site = Path(subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True,
    ).strip())
    (site / "benchmark_dependency.py").write_text("VERSION = 'tag'\n")
    parent_dependency = types.ModuleType("benchmark_dependency")
    parent_dependency.VERSION = "parent"
    monkeypatch.setitem(sys.modules, "benchmark_dependency", parent_dependency)
    source = tmp_path / "tag"
    source.mkdir()
    (source / "uv.lock").write_text("tag dependency snapshot\n")
    script = tmp_path / "worker.py"
    script.write_text(
        "import json, sys\n"
        "from benchmark_dependency import VERSION\n"
        "json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'state': 'ready', 'runtime_prefix': sys.prefix, "
        "'environment': {'dependency': VERSION}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'state': 'worker-error', 'error_class': 'MeasurementRefused', "
        "'reason': 'stored-vector-invalid', 'phase': 'worker-request'}), flush=True)\n"
    )
    monkeypatch.setattr(gate, "__file__", str(script))
    profile = {"worker_environment": ["PATH"]}
    worker = gate._Worker(source, tmp_path / "vault", tmp_path / "state", [], profile,
                          python=python, runtime_prefix=runtime, runtime_policy="frozen-tag-runtime")
    try:
        assert parent_dependency.VERSION == "parent"
        assert worker.environment["dependency"] == "tag"
        assert worker.environment["runtime_policy"] == "frozen-tag-runtime"
        assert worker.environment["runtime_lock_digest"] == gate._runtime_lock_digest(source)
        with pytest.raises(gate.MeasurementRefused, match="stored-vector-invalid") as refused:
            worker.exchange({"measure": True})
        assert refused.value.phase == "worker-request"
    finally:
        worker.close()
    with pytest.raises(gate.MeasurementRefused, match="runtime-prefix-mismatch"):
        gate._Worker(source, tmp_path / "vault", tmp_path / "other-state", [], profile,
                     python=python, runtime_prefix=tmp_path / "incorrect-runtime",
                     runtime_policy="frozen-tag-runtime")


@pytest.mark.skipif(gate.os.name != "posix", reason="The paired runtime uses the Linux reference profile")
def test_a_paused_arm_cannot_hold_the_other_arms_governance_guard(tmp_path: Path, monkeypatch) -> None:
    """A background read frozen in one arm must not refuse the other arm's next recall."""
    (tmp_path / "home").mkdir()
    (tmp_path / "vault").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "0.2")
    script = tmp_path / "worker.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from exomem.writer_lease import LeaseConfig, LeaseManager, OpError\n"
        "json.loads(sys.stdin.readline())\n"
        "root = Path(sys.argv[sys.argv.index('--worker-vault') + 1])\n"
        "try:\n"
        "    manager = LeaseManager(LeaseConfig.from_env())\n"
        "    with manager.reserved_identity_guard(root, domains=('governance-store',), "
        "exclusive=False, advance_generation=False):\n"
        "        print(json.dumps({'state': 'ready', 'runtime_prefix': sys.prefix, "
        "'environment': {}}), flush=True)\n"
        "        for line in sys.stdin:\n"
        "            print(json.dumps({'state': 'held'}), flush=True)\n"
        "except OpError as error:\n"
        "    print(json.dumps({'state': 'worker-error', 'error_class': type(error).__name__, "
        "'reason': error.code}), flush=True)\n"
    )
    monkeypatch.setattr(gate, "__file__", str(script))
    profile = {"worker_environment": ["PATH", "HOME", "EXOMEM_MUTATION_TIMEOUT"]}
    workers = []
    try:
        for arm in ("base", "head"):
            workers.append(gate._Worker(ROOT, tmp_path / "vault", tmp_path / arm, [], profile))
        # Each worker can run while its paused peer retains its own policy guard.
        for worker in workers:
            try:
                assert worker.exchange({})["state"] == "held"
            finally:
                worker.pause()
    finally:
        for worker in workers:
            worker.close()


@pytest.mark.skipif(gate.os.name != "posix", reason="The paired runtime uses the Linux reference profile")
def test_tag_runtime_ignores_the_callers_project_and_operation_settings(tmp_path: Path, monkeypatch) -> None:
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("The runtime installation boundary requires uv")
    base = tmp_path / "tag"
    base.mkdir()
    (base / "pyproject.toml").write_text(
        '[project]\nname = "reference-runtime"\nversion = "0.0.0"\n'
        'requires-python = ">=3.11"\ndependencies = []\n'
        '[project.optional-dependencies]\nembeddings-onnx = []\n'
    )
    cache = tmp_path / "cache"
    subprocess.run(
        [uv, "--no-config", "lock", "--project", str(base), "--offline",
         "--no-python-downloads", "--python", sys.executable],
        env={"PATH": gate.os.environ["PATH"], "UV_CACHE_DIR": str(cache)},
        check=True, capture_output=True, text=True, timeout=20,
    )
    before = (base / "uv.lock").read_bytes()
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "pyproject.toml").write_text((base / "pyproject.toml").read_text())
    # The caller has no lock and must not replace the archived tag's project.
    monkeypatch.setenv("UV_PROJECT", str(caller))
    monkeypatch.setenv("UV_CONFIG_FILE", str(tmp_path / "caller-config.toml"))
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))

    python = gate._prepare_tag_runtime(base, tmp_path / "measurement")

    assert python.is_file()
    assert (base / "uv.lock").read_bytes() == before
    assert not (caller / "uv.lock").exists()


def test_a_paired_series_the_case_file_lacks_refuses_before_any_runtime(tmp_path: Path, monkeypatch) -> None:
    """A renamed or mistyped CI series must not measure nothing and report green."""
    monkeypatch.setattr(gate, "_prepare_tag_runtime", lambda *args: pytest.fail("prepared a runtime"))
    with pytest.raises(gate.MeasurementRefused, match="paired-series-unknown"):
        gate.paired_run(base=tmp_path, tag="v1.0.0", scratch=tmp_path, profile={},
                        cases=[{"id": "hybrid-00", "series": "hybrid"}], series="hybird")


@pytest.fixture
def paired_fragments(tmp_path, monkeypatch):
    """Tiny workers exercise the real sampling loop without constructing the reference vault."""
    import hashlib

    pytest.importorskip("scipy")  # Full discovery installs this optional statistics dependency.
    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    cases, digest = gate.reference_cases(profile)
    selected = [case for case in cases if case["series"] == "hybrid"]
    # Odd slice starts expose accidental re-enumeration; production keeps its original 40 cases.
    selected += [{**case, "id": f"hybrid-extra-{index}"} for index, case in enumerate(selected[:3])]
    base = tmp_path / "base"
    base.mkdir()
    lock = b"original base lock\n"
    (base / "uv.lock").write_bytes(lock)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(gate, "_git_bytes", lambda *args: lock if args[0] == "show" else
                        b"b" * 40 if args[-1].startswith("refs/tags/") else b"a" * 40)
    monkeypatch.setattr(gate, "reference_cases", lambda _profile: (selected, digest))
    monkeypatch.setattr(gate, "_prepare_tag_runtime", lambda *_args: tmp_path / "runtime/bin/python")
    monkeypatch.setattr(gate.os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    monkeypatch.setattr(gate, "one_minute_load", lambda: 0.25)
    calls = []
    workers = []

    class Worker:
        def __init__(self, source, *_args, **_kwargs):
            self.arm = "base" if source == base else "head"
            self.calls = []
            self.environment = {
                "corpus": {"pages": 6500, "chunks": 45000, "units": 9500, "unit_pages": 3250,
                           "vector_units": 9500, "spot_checks": 6, "median_page_bytes": 2500,
                           "vocabulary_terms": 6000, "wikilinks_min": 5, "wikilinks_max": 25,
                           "transcript_pages": 325, "content_digest": "c" * 64},
                "encoder_digest": "d" * 16, "encoder_identity": "reference-encoder",
                "sqlite_version": profile["sqlite_versions"][0], "python_version": "3.12.3",
                "cpus": [len(workers) // 2 * 2, len(workers) // 2 * 2 + 1],
                "runtime_lock_digest": (hashlib.sha256(lock).hexdigest() if self.arm == "base"
                                        else gate._runtime_lock_digest(gate.ROOT)),
                "runtime_policy": "frozen-tag-runtime" if self.arm == "base" else "parent-benchmark",
            }
            workers.append(self)

        def exchange(self, request):
            self.calls.append(request["id"])
            calls.append((self.arm, request["id"], request["principal"]))
            return {"state": "ok", "elapsed_ms": 100.0, "cpu_ms": 90.0,
                    "cpu_source": "process_time_ns", "runqueue_ms": 0.0,
                    "unattributed_ms": 0.0, "overlap_cpu_ms": 0.0, "unattributed_cpu_ms": 0.0,
                    "unbudgeted": [], "critical_path": None, "stages": {}}

        def pause(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(gate, "_Worker", Worker)
    return profile, selected, base, calls, workers


@pytest.mark.parametrize("sample_state", ["ok", "warming", "unknown"])
def test_raw_case_shards_preserve_unsharded_order_warmup_and_one_comparison(
    tmp_path, monkeypatch, paired_fragments, sample_state
):
    profile, cases, base, calls, workers = paired_fragments
    exchange = gate._Worker.exchange
    def observe(self, request):
        result = exchange(self, request)
        if self.arm == "head" and request["id"] == cases[5]["id"] and self.calls.count(request["id"]) == 1:
            result.update(state=sample_state, elapsed_ms=100.0 if sample_state == "ok" else None)
        return result
    monkeypatch.setattr(gate._Worker, "exchange", observe)
    unsharded = gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path,
                               profile=profile, cases=cases, series="hybrid")
    original_compare = gate.compare_pairs
    original_accept = gate.read_accept
    monkeypatch.setattr(gate, "compare_pairs", lambda *_args, **_kwargs: pytest.fail("shard made a verdict"))
    monkeypatch.setattr(gate, "read_accept", lambda *_args, **_kwargs: pytest.fail("shard applied acceptance"))
    fragments = []
    for shard in range(1, 5):
        calls.clear()
        fragment = gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path,
                                  profile=profile, cases=cases, series="hybrid", shard=shard,
                                  shards=4, base_commit="b" * 40)
        assert fragment["state"] == "collected"
        assert [worker.calls[0] for worker in workers[-2:]] == [cases[0]["id"]] * 2
        start = fragment["case_indices"][0]
        assert calls[2][0] == ("base" if start % 2 == 0 else "head")
        fragments.append(fragment)
    fragments[0]["run_attempt"] = "1"  # A successful earlier shard can survive a same-run retry.
    comparisons = []
    def compare(samples, **kwargs):
        comparisons.append(samples)
        return original_compare(samples, **kwargs)
    monkeypatch.setattr(gate, "compare_pairs", compare)
    monkeypatch.setattr(gate, "read_accept", original_accept)
    aggregate = gate.aggregate_paired(fragments, series="hybrid", tag="v1.0.0", base_commit="b" * 40, producer_outcome="success")
    assert len(comparisons) == 1
    assert aggregate["samples"] == unsharded["samples"]
    assert aggregate["series"] == unsharded["series"]
    assert aggregate["state"] == unsharded["state"]
    assert aggregate["raw_fragments"] == fragments
    gate.validate_document(aggregate, "summary.schema.json")
    from jsonschema import Draft202012Validator

    schema = json.loads((gate.BENCHMARK / "summary.schema.json").read_text())
    Draft202012Validator(schema).validate(aggregate)
    Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/paired_fragment"}).validate(fragments[0])


@pytest.mark.parametrize("corrupt", [False, True])
def test_aggregate_cli_keeps_missing_or_corrupt_fragments_a_visible_refusal(tmp_path, corrupt):
    if corrupt:
        (tmp_path / "recall-latency-fragment-hybrid-1.json").write_text("{")
    output = tmp_path / "final.json"
    assert gate.aggregate_paired_cli(tmp_path, output, series="hybrid", tag="v1.0.0", base_commit="b" * 40, producer_outcome="success") == 1
    report = json.loads(output.read_text())
    assert report["state"] == "refused" and report["samples"] == []
    assert report["phase"] == "paired-aggregation"
    gate.validate_document(report, "summary.schema.json")


@pytest.mark.parametrize("failed", [False, True])
def test_pinned_shard_cli_archives_the_discovered_commit_without_looking_up_latest(
    tmp_path, monkeypatch, paired_fragments, failed
):
    import subprocess

    profile, cases, base, _calls, _workers = paired_fragments
    fragment = gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path, profile=profile,
                               cases=cases, series="hybrid", shard=1, shards=4, base_commit="b" * 40)
    commands = []
    monkeypatch.setattr(gate, "_git_bytes", lambda *args: b"b" * 40)
    def sample(**kwargs):
        if failed:
            raise gate.MeasurementRefused("tag-runtime-install-failed", phase="tag-runtime-preparation")
        return fragment
    monkeypatch.setattr(gate, "paired_run", sample)
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: pytest.fail("looked up latest again"))
    def archive(arguments, **kwargs):
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0)
    monkeypatch.setattr(subprocess, "run", archive)
    output = tmp_path / "fragment.json"
    assert gate.paired_cli(output, series="hybrid", base_tag="v1.0.0", base_commit="b" * 40,
                           shard=1, shards=4) == int(failed)
    assert commands[0] == ["git", "archive", "b" * 40]
    report = json.loads(output.read_text())
    assert report["state"] == ("refused" if failed else "collected")
    assert (report["run_id"], report["run_attempt"], report["shard"], report["shards"]) == ("123", "2", 1, 4)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "head", "base", "case_digest", "corpus", "lock", "sample", "foreign", "assignment", "refused"])
def test_aggregation_refuses_incomplete_or_mixed_raw_evidence(tmp_path, paired_fragments, damage):
    profile, cases, base, _calls, _workers = paired_fragments
    fragments = [gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path,
                                profile=profile, cases=cases, series="hybrid", shard=shard,
                                shards=4, base_commit="b" * 40) for shard in range(1, 5)]
    if damage == "missing":
        fragments.pop()
    elif damage == "duplicate":
        fragments.append(dict(fragments[0]))
    elif damage in ("head", "base", "case_digest"):
        fragments[0][{"head": "head_commit", "base": "base_commit", "case_digest": "cases_digest"}[damage]] = "e" * (64 if damage == "case_digest" else 40)
    elif damage == "corpus":
        fragments[0]["environment"]["head"]["corpus"]["content_digest"] = "e" * 64
    elif damage == "lock":
        fragments[0]["environment"]["base"]["runtime_lock_digest"] = "e" * 64
    elif damage == "sample":
        fragments[0]["samples"].append(dict(fragments[0]["samples"][0]))
    elif damage == "foreign":
        fragments[0]["samples"][0]["case"] = "foreign-case"
    elif damage == "assignment":
        fragments[0]["case_indices"] = fragments[1]["case_indices"]
    elif damage == "refused":
        fragments[0]["state"] = "refused"
    with pytest.raises(gate.MeasurementRefused):
        gate.aggregate_paired(fragments, series="hybrid", tag="v1.0.0", base_commit="b" * 40, producer_outcome="success")


@pytest.mark.parametrize("retry", ["replacement", "latest_refused", "same_attempt_duplicate", "future", "foreign_run"])
def test_failed_shard_replacement_keeps_inherited_success_and_latest_producer(
    tmp_path, monkeypatch, paired_fragments, retry
):
    profile, cases, base, _calls, _workers = paired_fragments
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    fragments = [gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path, profile=profile,
                                cases=cases, series="hybrid", shard=shard, shards=4,
                                base_commit="b" * 40) for shard in range(1, 5)]
    refused = {"schema_version": 1, "mode": "paired_fragment", "state": "refused",
               "contract_revision": gate.CONTRACT.revision, "series": "hybrid", "shard": 1,
               "shards": 4, "run_id": "123", "run_attempt": "1", "samples": []}
    replacement = {**fragments[0], "run_attempt": "2"}
    inputs = [refused, replacement, *fragments[1:]]
    if retry == "latest_refused":
        replacement.update(state="refused", samples=[])
    elif retry == "same_attempt_duplicate":
        inputs.append(dict(replacement))
    elif retry == "future":
        replacement["run_attempt"] = "3"
    elif retry == "foreign_run":
        replacement["run_id"] = "456"
    for index, fragment in enumerate(inputs):
        directory = tmp_path / f"artifact-{index}"
        directory.mkdir()
        (directory / "recall-latency-fragment-hybrid.json").write_text(json.dumps(fragment))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    output = tmp_path / "aggregate.json"
    assert gate.aggregate_paired_cli(tmp_path, output, series="hybrid", tag="v1.0.0", base_commit="b" * 40, producer_outcome="success") == (0 if retry == "replacement" else 1)
    report = json.loads(output.read_text())
    if retry == "replacement":
        assert {row["shard"]: row["run_attempt"] for row in report["raw_fragments"]} == {1: "2", 2: "1", 3: "1", 4: "1"}
    else:
        assert report["state"] == "refused" and report["samples"] == []
    assert len(list(tmp_path.rglob("recall-latency-fragment-*.json"))) == len(inputs)


def test_pinned_label_must_match_its_commit_before_sampling_or_comparison(
    tmp_path, monkeypatch, paired_fragments
):
    profile, cases, base, _calls, _workers = paired_fragments
    fragments = [gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path, profile=profile,
                                cases=cases, series="hybrid", shard=shard, shards=4,
                                base_commit="b" * 40) for shard in range(1, 5)]
    monkeypatch.setattr(gate, "_git_bytes", lambda *args: b"c" * 40)
    monkeypatch.setattr(gate, "paired_run", lambda **kwargs: pytest.fail("sampled an unverified label"))
    output = tmp_path / "refused.json"
    assert gate.paired_cli(output, series="hybrid", base_tag="v1.0.0", base_commit="b" * 40,
                           shard=1, shards=4) == 1
    assert json.loads(output.read_text())["reason"] == "pinned-release-tag-mismatch"
    with pytest.raises(gate.MeasurementRefused, match="pinned-release-tag-mismatch"):
        gate.aggregate_paired(fragments, series="hybrid", tag="v1.0.0", base_commit="b" * 40, producer_outcome="success")


@pytest.mark.parametrize("producer_outcome", ["failure", None])
def test_elapsed_only_retry_cannot_pass_from_inherited_old_fragments(
    tmp_path, monkeypatch, paired_fragments, producer_outcome
):
    profile, cases, base, _calls, _workers = paired_fragments
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    for shard in range(1, 5):
        fragment = gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path, profile=profile,
                                   cases=cases, series="hybrid", shard=shard, shards=4,
                                   base_commit="b" * 40)
        directory = tmp_path / f"original-{shard}"
        directory.mkdir()
        (directory / "recall-latency-fragment-hybrid.json").write_text(json.dumps(fragment))
    retry = tmp_path / "latest-attempt-2"
    retry.mkdir()
    (retry / "recall-latency-job-elapsed.json").write_text('{"elapsed_seconds":2400}')
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(gate, "compare_pairs", lambda *_args, **_kwargs: pytest.fail("compared a failed producer matrix"))
    output = tmp_path / "aggregate.json"
    assert gate.aggregate_paired_cli(tmp_path, output, series="hybrid", tag="v1.0.0", base_commit="b" * 40,
                                    producer_outcome=producer_outcome) == 1
    report = json.loads(output.read_text())
    assert report["state"] == "refused" and report["samples"] == []
    assert report["reason"] == ("paired-producer-result-unavailable" if producer_outcome is None else "paired-producer-result-not-success")
    assert len(list(tmp_path.rglob("recall-latency-fragment-*.json"))) == 4


def test_invalid_shard_cli_retains_a_schema_valid_refusal(tmp_path, monkeypatch, paired_fragments):
    import subprocess

    _profile, _cases, _base, _calls, workers = paired_fragments
    monkeypatch.setattr(subprocess, "run", lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0))
    output = tmp_path / "refused.json"
    assert gate.main(["--paired", "--series", "hybrid", "--base-tag", "v1.0.0", "--base-commit", "b" * 40,
                      "--shard", "-1", "--shards", "4", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["state"] == "refused" and report["shard"] == -1
    assert report["reason"] == "paired-shard-outside-case-range"
    assert workers == []
    gate.validate_document(report, "summary.schema.json", definition="paired_fragment")


@pytest.mark.parametrize("changed_lock, reason", [
    (False, "tag-runtime-install-failed"), (True, "tag-runtime-lock-changed"),
])
def test_tag_runtime_failure_writes_a_refusal_without_head_fallback(tmp_path: Path, monkeypatch,
                                                                  changed_lock, reason) -> None:
    import subprocess

    base = tmp_path / "base"
    base.mkdir()
    (base / "uv.lock").write_text("tag dependency snapshot\n")
    def install(*args, **kwargs):
        if changed_lock:
            (base / "uv.lock").write_text("changed snapshot\n")
        return subprocess.CompletedProcess([], 0 if changed_lock else 1)
    monkeypatch.setattr(subprocess, "run", install)
    monkeypatch.setattr(gate.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    monkeypatch.setattr(gate, "_Worker", lambda *args, **kwargs: pytest.fail("A failed tag runtime started a worker"))
    with pytest.raises(gate.MeasurementRefused, match=reason) as refused:
        gate.paired_run(base=base, tag="v1.0.0", scratch=tmp_path, profile={}, cases=[])
    assert refused.value.phase == "tag-runtime-preparation"

    def incomplete_run(**kwargs):
        raise refused.value
    monkeypatch.setattr(gate, "paired_run", incomplete_run)
    monkeypatch.setattr(gate, "reference_cases", lambda profile: ([], "0" * 64))
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs:
                        "v1.0.0" if kwargs.get("text") else b"b" * 40)
    output = tmp_path / "summary.json"
    assert gate.paired_cli(output) == 1
    report = json.loads(output.read_text())
    assert report["state"] == "refused" and report["samples"] == []
    assert report["reason"] == reason and report["phase"] == "tag-runtime-preparation"
    assert "snapshot" not in output.read_text()
