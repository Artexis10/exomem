"""Acceptance invariants for the durable-closure benchmark harness."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "durable_closure_benchmark.py"
spec = importlib.util.spec_from_file_location("durable_closure_benchmark", MODULE_PATH)
benchmark = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


def test_ordinary_recall_refusal_fails_useful_closure_and_keeps_its_window() -> None:
    """A write ACK cannot mask an intervening public recall refusal."""
    report = benchmark.evaluate_useful_closure(
        calls=[
            {"tool": "remember", "outcome": "ok", "started_ms": 0, "ended_ms": 5},
            {
                "tool": "ask_memory",
                "outcome": "refused",
                "error_code": "RETRIEVAL_INDEX_WARMING",
                "started_ms": 6,
                "ended_ms": 8,
            },
            {"tool": "read_memory", "outcome": "ok", "started_ms": 9, "ended_ms": 12},
        ],
        final_read_your_write=True,
        graph_warming_components=(),
    )

    assert report["passed"] is False
    assert report["refusal_observations"] == [
        {
            "tool": "ask_memory",
            "code": "RETRIEVAL_INDEX_WARMING",
            "window_ms": [6.0, 8.0],
        }
    ]


def test_successful_recall_with_graph_warming_passes_and_keeps_lag_separate() -> None:
    report = benchmark.evaluate_useful_closure(
        calls=[
            {"tool": "remember", "outcome": "ok", "started_ms": 0, "ended_ms": 5},
            {"tool": "ask_memory", "outcome": "ok", "started_ms": 6, "ended_ms": 8},
        ],
        final_read_your_write=True,
        graph_warming_components=("graph",),
    )

    assert report == {
        "passed": True,
        "final_read_your_write": True,
        "refusal_observations": [],
        "warming_components": ["graph"],
    }


def test_overlapping_ledger_calls_keep_sum_and_union_distinct() -> None:
    timings = benchmark.summarize_ledger_calls(
        [
            {"tool": "remember", "duration_ms": 30, "total_ms": 40, "started_ms": 0, "ended_ms": 40},
            {"tool": "ask_memory", "duration_ms": 20, "total_ms": 30, "started_ms": 10, "ended_ms": 40},
        ]
    )

    assert timings["server_duration_sum_ms"] == 50.0
    assert timings["server_total_sum_ms"] == 70.0
    assert timings["ledger_observation_span_ms"] == 40.0
    assert timings["server_occupied_union_ms"] == 40.0
    assert timings["server_idle_within_observed_span_ms"] == 0.0


def test_ledger_durations_join_to_each_client_call_without_inventing_intervals() -> None:
    calls = [
        {"tool": "remember", "client_elapsed_ms": 8.0},
        {"tool": "ask_memory", "client_elapsed_ms": 3.0},
    ]
    joined = benchmark.attach_ledger_measurements(
        calls,
        [
            {"tool": "remember", "duration_ms": 5.0, "total_ms": 7.0},
            {"tool": "ask_memory", "duration_ms": 2.0, "total_ms": 2.5},
        ],
    )

    assert joined[0]["server_duration_ms"] == 5.0
    assert joined[1]["server_total_ms"] == 2.5
    assert joined[0]["server_interval"] is None


def test_missing_required_public_tool_is_a_refusal_not_a_reduced_workflow() -> None:
    missing = benchmark.required_tools_missing({"remember", "ask_memory"})

    assert missing == ["preserve_artifacts", "process_media", "read_memory"]


def test_stress_variant_places_a_public_probe_after_each_mutating_step() -> None:
    optimized = benchmark.workflow_plan("optimized")
    stress = benchmark.workflow_plan("stress")

    assert optimized != stress
    mutation_steps = [step for step in stress if step["mutates"]]
    for mutation in mutation_steps:
        position = stress.index(mutation)
        assert stress[position + 1] == {
            "name": f"probe-after-{mutation['name']}",
            "tool": "ask_memory",
            "mutates": False,
            "probe": True,
        }


def test_small_model_free_smoke_uses_one_registered_stdio_product_session(
    tmp_path: Path,
) -> None:
    """The smoke is an integration boundary: registered tools, not leaf fakes."""
    report = benchmark.asyncio.run(
        benchmark.run_public_workflow(
            python=Path(sys.executable),
            state=tmp_path / "state",
            vault=tmp_path / "vault",
            pages=4,
            profile=benchmark.MODEL_FREE_PROFILE,
            timeout=30.0,
        )
    )

    assert report["status"] == "pass"
    assert report["public_call_count"] >= 5
    assert {call["tool"] for call in report["calls"]} >= {
        "bootstrap",
        "remember",
        "ask_memory",
        "read_memory",
    }
    assert report["media"]["status"] == "blocked"
