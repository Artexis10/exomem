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

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import recall_latency_gate as gate  # noqa: E402, I001


def _canary_case(**expected):
    return {
        "id": "case-01",
        "tool": "ask_memory",
        "arguments": {"query": "private-question"},
        "expected": {
            "state_path": ["ok"],
            "state": True,
            "outcome": "ok",
            "references": [{"path": ["hits", 0, "ref"], "value": "private-reference"}],
            "material_paths": [["hits", 0, "text"]],
            **expected,
        },
    }


def _run_canary(tmp_path, server, cases, **kwargs):
    from fastmcp import Client

    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    options = {
        "samples": 30, "warm_up": True, "contention": "uncontended",
        "deployment_id": "verified-deployment", "deployment_verified": True,
        "package_mode": "source", **kwargs,
    }
    return asyncio.run(
        gate.run_mcp_canary(
            client=Client(server), cases_file=path, **options,
        )
    )


def test_mcp_canary_proves_material_over_parsed_public_calls(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["verdict"] == "passed"
    assert report["availability"] == "passed"
    case = report["cases"][0]
    assert case["measured_attempts"] == 30
    assert case["warmth"] == "successful-case-proof"
    assert case["latency"][0]["boundary"] == "mcp-roundtrip-v1"
    assert case["latency"][0]["samples"] == 30
    assert case["latency"][0]["outcome_inclusion"] == ["ok", "error", "refused", "abstained"]
    assert report["initialization_ms"] >= 0
    assert all(item["elapsed_ms"] >= 0 for item in case["attempts"])
    assert report["observer"]["role"] == "observer-only"
    rendered = json.dumps(report)
    for secret in ("private-question", "private-reference", "verified-deployment"):
        assert secret not in rendered


def test_mcp_fast_typed_refusal_fails_and_stops_the_series(tmp_path, caplog):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": False, "error": {"code": "RETRIEVAL_INDEX_WARMING"}}

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["verdict"] == "failed"
    assert report["availability"] == "failed"
    assert report["coverage"] == "incomplete"
    attempts = report["cases"][0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "refused"
    assert attempts[0]["elapsed_ms"] >= 0
    assert any(
        record.event == "recall_canary_failed"
        for record in caplog.records
        if hasattr(record, "event")
    )


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"ok": True, "hits": [{"ref": "wrong", "text": "material"}]}, "missing-reference"),
        ({"ok": True, "hits": [{"ref": "private-reference", "text": "  "}]}, "missing-material"),
        ({"ok": True}, "missing-reference"),
        ({"ok": False}, "unexpected-outcome"),
    ],
)
def test_mcp_positive_cases_require_source_truth_and_material(tmp_path, response, reason):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return response

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["availability"] == "failed"
    assert report["cases"][0]["attempts"][0]["failure"] == reason


def test_mcp_declared_abstention_must_match_its_state(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def activate_context(query: str) -> dict:
        return {"abstained": True, "abstention": {"reason": "unresolved"}}

    case = _canary_case(
        state_path=["abstention", "reason"],
        state="unresolved",
        outcome="abstained",
        references=[],
        material_paths=[],
    )
    case["tool"] = "activate_context"
    report = _run_canary(tmp_path, server, [case])
    assert report["availability"] == "incomplete"
    assert report["cases"][0]["attempts"][0]["outcome"] == "abstained"
    assert report["verdict"] == "incomplete"
    assert report["positive_coverage"]["activate_context"]["successful_cases"] == 0
    case["expected"]["state"] = "ambiguous"
    assert _run_canary(tmp_path, server, [case])["availability"] == "failed"


def test_mcp_expected_abstention_needs_positive_coverage_for_its_tool(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def activate_context(query: str) -> dict:
        if query == "negative":
            return {"abstained": True, "abstention": {"reason": "unresolved"}}
        return {"abstained": False, "hits": [{"ref": "private-reference", "text": query}]}

    positive = _canary_case(state_path=["abstained"], state=False)
    positive["tool"] = "activate_context"
    negative = _canary_case(
        state_path=["abstention", "reason"], state="unresolved", outcome="abstained",
        references=[], material_paths=[],
    )
    negative.update(id="case-02", tool="activate_context", arguments={"query": "negative"})
    report = _run_canary(tmp_path, server, [positive, negative])
    assert report["verdict"] == "passed"
    assert report["positive_coverage"]["activate_context"]["successful_cases"] == 1
    assert report["cases"][1]["availability"] == "passed"
    assert report["cases"][1]["latency"][0]["abstentions"] == 30


@pytest.mark.parametrize("damage", ["write-tool", "empty-material", "bad-path", "duplicate"])
def test_mcp_invalid_cases_do_not_call_tools(tmp_path, damage):
    from fastmcp import FastMCP

    server = FastMCP("test")
    calls = []

    @server.tool
    def ask_memory(query: str) -> dict:
        calls.append(query)
        return {"ok": True}

    case = _canary_case()
    if damage == "write-tool":
        case["tool"] = "remember"
    elif damage == "empty-material":
        case["expected"]["material_paths"] = []
    elif damage == "bad-path":
        case["expected"]["state_path"] = [True]
    cases = [case, case] if damage == "duplicate" else [case]
    report = _run_canary(tmp_path, server, cases)
    assert report["verdict"] == "failed"
    assert report["failure"] == "invalid-cases"
    assert calls == []


@pytest.mark.parametrize("missing", [
    {"contention": "unknown"}, {"warm_up": False},
    {"deployment_verified": False}, {"samples": 1},
])
def test_mcp_missing_reference_conditions_prevent_acceptance(tmp_path, missing):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(tmp_path, server, [_canary_case()], **missing)
    assert report["availability"] == "passed"
    assert report["verdict"] == "incomplete"


def test_mcp_source_observer_cannot_claim_an_installed_package(tmp_path):
    from fastmcp import FastMCP

    report = _run_canary(tmp_path, FastMCP("test"), [_canary_case()], package_mode="installed")
    assert report["failure"] == "invalid-observer-or-contract"
    assert report["initialization_ms"] is None


def test_mcp_cli_rejects_invalid_target_without_leaking_inputs(tmp_path, capsys):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case()]))
    status = gate.main(
        ["--mcp-url", "file:///private-target", "--cases", str(path), "--package-mode", "source"]
    )
    output = capsys.readouterr()
    assert status != 0
    assert json.loads(output.out)["failure"] == "invalid-target"
    assert "private-target" not in output.out + output.err


def test_mcp_measured_failure_retains_all_outcomes_and_unattempted_cases(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")
    completed = 0

    @server.tool
    def ask_memory(query: str) -> dict:
        nonlocal completed
        completed += 1
        if completed == 3:
            return {"ok": False, "error": {"code": "RETRIEVAL_INDEX_WARMING"}}
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    later = _canary_case()
    later["id"] = "case-02"
    report = _run_canary(tmp_path, server, [_canary_case(), later])
    first, second = report["cases"]
    assert report["verdict"] == "failed"
    assert first["measured_attempts"] == 2
    assert first["latency"][0]["observed"] == 2
    assert first["latency"][0]["samples"] == 2
    assert first["latency"][0]["refusals"] == 1
    assert second["availability"] == "unattempted"
    assert second["attempts"] == []


@pytest.mark.parametrize("terminal", ["malformed", "tool-error", "timeout"])
def test_mcp_terminal_failures_have_elapsed_time_without_response_content(tmp_path, terminal):
    from fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent

    server = FastMCP("test")

    @server.tool
    async def ask_memory(query: str):
        if terminal == "timeout":
            await asyncio.sleep(1)
        return CallToolResult(
            isError=terminal == "tool-error",
            content=[TextContent(type="text", text="private-response")],
        )

    report = _run_canary(tmp_path, server, [_canary_case()], timeout_seconds=0.01)
    assert report["verdict"] == "failed"
    attempt = report["cases"][0]["attempts"][0]
    assert attempt["outcome"] == "error"
    assert attempt["elapsed_ms"] >= 0
    assert len(report["cases"][0]["attempts"]) == 1
    assert "private-response" not in json.dumps(report)


def test_mcp_bad_json_and_missing_case_files_fail_before_connection(tmp_path):
    from fastmcp import Client, FastMCP

    path = tmp_path / "cases.json"
    for content in (None, "{not json}"):
        if content:
            path.write_text(content)
        report = asyncio.run(
            gate.run_mcp_canary(
                client=Client(FastMCP("test")),
                cases_file=path,
                package_mode="source",
            )
        )
        assert report["failure"] == "invalid-cases"
        assert report["initialization_ms"] is None


def test_mcp_controller_contract_uses_its_own_revision_and_sample_count(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    contract = gate.CONTRACT.model_dump(mode="json")
    contract["revision"] = "controller-test"
    bar = next(bar for bar in contract["bars"] if bar["id"] == "programme-public-warm")
    bar["min_samples"] = 31
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    report = _run_canary(tmp_path, server, [_canary_case()], contract_file=path)
    assert report["verdict"] == "incomplete"
    assert report["contract"]["revision"] == "controller-test"
    assert report["contract"]["sha256"] == gate._digest(path.read_bytes())
    latency = report["cases"][0]["latency"][0]
    assert latency["contract_revision"] == "controller-test"
    assert latency["min_samples"] == 31


def test_mcp_cli_emits_only_content_free_json_and_structured_warning(tmp_path, monkeypatch, capsys):
    import fastmcp

    server = fastmcp.FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": ""}]}

    real_client = fastmcp.Client
    monkeypatch.setattr(fastmcp, "Client", lambda transport, **kwargs: real_client(server))
    monkeypatch.setenv("TEST_CANARY_TOKEN", "private-token")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case()]))
    status = gate.main(
        [
            "--mcp-url",
            "https://private-target.invalid/mcp",
            "--cases",
            str(path),
            "--token-env",
            "TEST_CANARY_TOKEN",
            "--package-mode",
            "source",
            "--warm-up",
        ]
    )
    output = capsys.readouterr()
    assert status == 1
    assert json.loads(output.out)["availability"] == "failed"
    assert json.loads(output.err)["event"] == "recall_canary_failed"
    for secret in ("private-token", "private-target", "private-reference", "private-question"):
        assert secret not in output.out + output.err


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
