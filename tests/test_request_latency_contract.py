"""Contract matching and incomplete evidence must not certify a request bar."""

from __future__ import annotations

import json
from dataclasses import replace
from importlib.resources import files

import pytest

from exomem import latency_watch

NOW = 1_800_000_601.0


def test_slow_errors_remain_in_the_all_request_tail() -> None:
    watch = latency_watch.Watch(started_at=NOW - 601, clock=lambda: NOW)
    for outcome, count, milliseconds in (("ok", 90, 100), ("error", 10, 1500)):
        for _ in range(count):
            watch.observe(
                tool="activate_context",
                client="client-a",
                deep=False,
                total_ms=milliseconds,
                outcome=outcome,
                bar_id="activation-live-warm",
            )
    [row] = watch.verdicts()
    assert row["samples"] == 100
    assert row["p95_ms"] == 1500
    assert row["verdict"] == "failed"
    assert row["errors"] == 10
    assert row["outcome_inclusion"] == ["ok", "error", "refused", "abstained"]


def test_success_only_population_cannot_certify_an_all_request_bar() -> None:
    sample = latency_watch.make_sample(
        at=NOW,
        tool="activate_context",
        client="client-a",
        deep=False,
        total_ms=100,
        spans=None,
        bar_id="activation-live-warm",
        population="success-only",
    )
    [row] = latency_watch.summarize([sample] * 30, now=NOW)
    assert row["verdict"] == "unavailable"
    assert row["breach"] is False


@pytest.mark.parametrize("milliseconds", [None, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_or_unknown_timing_never_becomes_zero_or_pass(milliseconds) -> None:
    sample = latency_watch.make_sample(
        at=NOW,
        tool="activate_context",
        client=None,
        deep=False,
        total_ms=milliseconds,
        spans=None,
        bar_id="activation-live-warm",
    )
    [row] = latency_watch.summarize([sample] * 30, now=NOW)
    assert row["samples"] == 0
    assert row["p95_ms"] is None
    assert row["verdict"] == "unavailable"
    assert row["unknown_timings"] + row["invalid_timings"] == 30


def test_a_pending_bar_reports_measurements_without_a_verdict() -> None:
    sample = latency_watch.make_sample(
        at=NOW,
        tool="activate_context",
        client=None,
        deep=False,
        total_ms=200,
        spans=None,
        bar_id="activation-observed",
    )
    [row] = latency_watch.summarize([sample] * 30, now=NOW)
    assert row["p95_ms"] == 200
    assert row["ceiling_ms"] is None
    assert row["verdict"] == "pending"


def test_retention_reports_lost_traffic_instead_of_a_day_census() -> None:
    watch = latency_watch.Watch(started_at=NOW - 601, clock=lambda: NOW)
    for _ in range(latency_watch.RING_SIZE + 5):
        watch.observe(tool="ask_memory", client="client-a", deep=False, total_ms=100)
    [row] = watch.verdicts()
    assert row["samples"] == latency_watch.RING_SIZE
    assert row["coverage"]["population"] == "retained"
    assert row["coverage"]["evictions"] == 5
    assert row["coverage"]["oldest_retained_at"] == NOW
    assert row["verdict"] == "partial"


def test_boundary_and_profile_mismatch_cannot_pass() -> None:
    sample = latency_watch.make_sample(
        at=NOW,
        tool="activate_context",
        client=None,
        deep=False,
        total_ms=100,
        spans=None,
        bar_id="activation-live-warm",
    )
    for wrong in (
        replace(sample, boundary="mcp-dispatch-v1"),
        replace(sample, profile="model-free-observation"),
    ):
        [row] = latency_watch.summarize([wrong] * 30, now=NOW)
        assert row["verdict"] == "unavailable"


def test_the_packaged_contract_keeps_owners_and_strict_comparisons() -> None:
    from exomem.request_latency_contract import load_contract

    contract = load_contract()
    raw = json.loads(files("exomem").joinpath("request_latency_contract.json").read_text())
    assert contract.revision == raw["revision"]
    assert contract.revision == "request-latency-v4"
    assert contract.bar("activation-live-warm").comparison == "<"
    assert contract.bar("governed-hybrid").threshold_ms == 200
    assert contract.bar("governed-keyword").threshold_ms == 100
    assert contract.references[contract.bar("governed-hybrid").reference]["pages_min"] == 6500
    # These are the protocol's five in-process whole-call bars.
    for bar_id in ("governed-hybrid", "governed-hybrid-p95-target", "governed-keyword",
                   "governed-keyword-p95-target", "governed-p50-target"):
        assert contract.bar(bar_id).boundary.id == "recall-bound-command-v1"
    assert all(bar.requirement and bar.provenance for bar in contract.bars)


def test_a_selected_observation_bar_always_covers_the_sample_it_labels() -> None:
    from exomem.request_latency_contract import load_contract

    contract = load_contract()
    for surface in ("mcp", "rest", "activation"):
        for tool in ("ask_memory", "find", "read_memory", "activate_context"):
            for deep in (False, True):
                bar = contract.observation_bar(surface=surface, tool=tool, deep=deep)
                assert bar is None or bar.covers(tool, deep)
    # A deep call to a tool with no deep bar has no bar, not a fallback it cannot match.
    assert contract.observation_bar(surface="mcp", tool="read_memory", deep=True) is None
    assert contract.observation_bar(surface="mcp", tool="ask_memory", deep=True).id == "watch-deep"


def test_unknown_operation_names_do_not_become_evidence_content() -> None:
    sample = latency_watch.make_sample(
        at=NOW,
        tool="private request title",
        client=None,
        deep=False,
        total_ms=100,
        spans=None,
        surface="rest",
    )
    assert "private request title" not in repr(sample)
    [row] = latency_watch.summarize([sample], now=NOW)
    assert row["tool"] == "unknown-operation"
    assert row["verdict"] == "pending"


def test_a_malformed_contract_disables_observation_without_a_default(monkeypatch) -> None:
    def unavailable():
        raise ValueError("invalid contract")

    monkeypatch.setattr(latency_watch, "load_contract", unavailable)
    watch = latency_watch.Watch(started_at=NOW - 601, clock=lambda: NOW)
    watch.observe(tool="ask_memory", client=None, deep=False, total_ms=100, surface="mcp")
    [row] = watch.verdicts()
    assert row["verdict"] == "unavailable"
    assert row["ceiling_ms"] is None and row["contract_revision"] is None


def test_live_warm_bar_is_strict_at_one_second() -> None:
    sample = latency_watch.make_sample(
        at=NOW,
        tool="activate_context",
        client=None,
        deep=False,
        total_ms=1000,
        spans=None,
        bar_id="activation-live-warm",
    )
    [row] = latency_watch.summarize([sample] * 30, now=NOW)
    assert row["verdict"] == "failed"
    assert row["breach"] is True
