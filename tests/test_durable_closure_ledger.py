"""Workflow attribution must not invent client/connector time from overlaps."""

import importlib.util
import json
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "durable_closure_ledger.py"
    assert path.is_file(), "the workflow ledger analyser is missing"
    spec = importlib.util.spec_from_file_location("closure_ledger", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(sequence, second, total, *, tool="ask_memory", outcome="ok", code=None):
    return {
        "sequence": sequence,
        "ts_utc": f"2026-09-05T00:00:{second:06.3f}+00:00",
        "client_name": "fixture-client",
        "tool": tool,
        "total_ms": total,
        "duration_ms": total - 10,
        "outcome": outcome,
        "error_code": code,
        "args": {"query": {"len": 6, "sha256": "a" * 64}},
        "target_paths": ["PRIVATE-PATH"],
    }


def test_overlapping_calls_separate_summed_work_from_occupancy():
    # [0, 4], [2, 6], [8, 9]: nine seconds elapsed, nine of work, seven busy.
    report = _module().summarize_rows([
        _row(1, 4, 4000), _row(2, 6, 4000), _row(3, 9, 1000),
    ])
    assert report["public_tool_calls"] == 3
    assert report["ledger_observation_span_ms"] == 9000
    assert report["workflow_wall_ms"] is None
    assert report["server_execution_sum_ms"] == 9000
    assert report["server_occupied_ms"] == 7000
    assert report["server_idle_within_observed_span_ms"] == 2000
    assert report["client_gap_ms"] is None
    assert report["connector_overhead_ms"] is None
    assert report["model_planning_ms"] is None
    assert report["clock_basis"] == "UTC completion timestamps plus monotonic server durations"
    assert report["clock_continuity_verified"] is False
    assert "PRIVATE-PATH" not in json.dumps(report)


def test_refusal_observations_are_not_claimed_as_continuous_outages():
    report = _module().summarize_rows([
        _row(1, 1, 100, outcome="refused", code="RETRIEVAL_INDEX_WARMING"),
        _row(2, 2, 100, tool="read_memory"),
        _row(3, 4, 100, outcome="refused", code="RETRIEVAL_INDEX_WARMING"),
        _row(4, 5, 100),
    ])
    assert report["warming_refusals"] == 2
    assert report["observed_refusal_spans"][0]["elapsed_ms"] == 4000
    assert report["observed_refusal_spans"][0]["continuous_outage_proven"] is False
    assert report["verification_only_calls"] is None


def test_malformed_rows_are_named_and_do_not_silently_abort_audit():
    module = _module()
    report = module.summarize_rows([_row(1, 1, 100), {"sequence": 2}, _row(3, 3, 100)])
    assert report["public_tool_calls"] == 2
    assert report["invalid_rows"] == [{"row": 2, "reason": "invalid_call_timing"}]
    assert report["complete"] is False


def test_same_millisecond_completions_follow_ledger_sequence():
    report = _module().summarize_rows([
        _row(1, 1, 10, outcome="refused", code="RETRIEVAL_INDEX_WARMING"),
        _row(2, 1, 100),
    ])
    assert report["observed_refusal_spans"] == [{
        "elapsed_ms": 0, "refusals": 1, "closed_by_success": True,
        "continuous_outage_proven": False,
    }]


def test_invalid_sequence_cannot_prove_refusal_order():
    row = _row("bad", 1, 100)
    report = _module().summarize_rows([row])
    assert report["complete"] is False


def test_legacy_leaf_only_rows_do_not_masquerade_as_total_wall_time():
    row = _row(1, 1, 100)
    del row["total_ms"]
    report = _module().summarize_rows([row])
    assert report["public_tool_calls"] == 0
    assert report["complete"] is False
    assert report["workflow_wall_ms"] is None


def test_span_totals_preserve_coverage_and_never_invent_component_percentiles():
    row = _row(1, 1, 100)
    row["spans"] = [
        {"name": "derived.canonical_commit", "count": 3, "ms": 6.5},
        {"name": "index.upsert_after_write", "count": 2, "ms": 80},
    ]
    report = _module().summarize_rows([row, _row(2, 2, 100)])
    assert report["measured_spans"]["derived.canonical_commit"] == {
        "calls_with_measurement": 1, "invocations": 3, "sum_ms": 6.5,
    }
    assert report["canonical_commit_ms"] == 6.5
    assert report["canonical_commit_coverage_calls"] == 1
    assert report["canonical_commit_p95_ms"] is None


def test_bad_span_is_named_and_does_not_hide_valid_sibling():
    row = _row(1, 1, 100)
    row["spans"] = [
        {"name": "private path/secret", "count": 1, "ms": 10},
        {"name": "derived.canonical_commit", "count": 1, "ms": 1},
    ]
    report = _module().summarize_rows([row])
    assert not report["complete"]
    assert report["canonical_commit_ms"] == 1
    assert "private path" not in json.dumps(report)
