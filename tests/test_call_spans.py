"""Phase timing for one MCP call.

The defect these exist to prevent is not a wrong number, it is a *quiet* one:
instrumentation that silently records nothing looks identical to a call that
spent no time anywhere. Several of these assert on absence for that reason.
"""

from __future__ import annotations

import time

import pytest

from exomem import call_ledger, call_spans, command_surface


@pytest.fixture(autouse=True)
def _isolate_spans():
    call_spans.reset()
    yield
    call_spans.reset()


@pytest.fixture
def token():
    handle = call_spans.MCP_CALL_TOKEN.set("test-token")
    try:
        yield "test-token"
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)


def test_the_failure_breadcrumb_and_the_timers_share_one_token(token) -> None:
    """Two definitions that must agree are one definition that cannot disagree."""
    assert command_surface._MCP_CALL_TOKEN is call_spans.MCP_CALL_TOKEN


def test_repeated_phases_aggregate_into_one_row_with_a_count(token) -> None:
    """A phase entered per changed path must not put a row per path in the ledger."""
    for _ in range(3):
        call_spans.record_span("graph.refresh_paths", 10.0)

    spans = call_spans.pop_call_spans(token)

    assert spans == [{"name": "graph.refresh_paths", "count": 3, "ms": 30.0}]


def test_spans_come_back_slowest_first(token) -> None:
    """Truncation drops the least interesting phase, so order is load-bearing."""
    call_spans.record_span("fast", 1.0)
    call_spans.record_span("slow", 100.0)
    call_spans.record_span("middling", 10.0)

    assert [s["name"] for s in call_spans.pop_call_spans(token)] == [
        "slow",
        "middling",
        "fast",
    ]


def test_popping_twice_yields_nothing_the_second_time(token) -> None:
    """One call, one ledger row: a second pop must not re-attribute the same work."""
    call_spans.record_span("phase", 5.0)

    assert call_spans.pop_call_spans(token)
    assert call_spans.pop_call_spans(token) == []


def test_recording_outside_an_mcp_call_is_a_no_op() -> None:
    """The same instrumentation runs on CLI, watcher and test paths.

    Those never mint a token. Recording anyway would accumulate under a `None`
    key and leak for the life of the process.
    """
    with call_spans.span("phase.outside"):
        pass

    assert call_spans.pop_call_spans(None) == []
    assert not call_spans._SPANS


def test_a_phase_that_raised_is_still_recorded(token) -> None:
    """The phase that blew up after eighteen seconds is the one worth seeing."""
    with pytest.raises(ValueError):
        with call_spans.span("phase.explodes"):
            raise ValueError("boom")

    assert [s["name"] for s in call_spans.pop_call_spans(token)] == ["phase.explodes"]


def test_span_measures_real_elapsed_time(token) -> None:
    with call_spans.span("phase.sleeps"):
        time.sleep(0.02)

    (recorded,) = call_spans.pop_call_spans(token)
    assert recorded["ms"] >= 15.0, recorded


def test_a_call_generating_names_is_truncated_not_unbounded(token) -> None:
    """Exceeding the name budget is a caller bug; it must not grow the row."""
    for index in range(call_spans.MAX_NAMES_PER_CALL + 50):
        call_spans.record_span(f"generated.{index}", 1.0)

    assert len(call_spans.pop_call_spans(token)) == call_spans.MAX_NAMES_PER_CALL


def test_a_missed_pop_cannot_leak_indefinitely() -> None:
    """The middleware pops unconditionally; this is the guard for paths that don't."""
    for index in range(call_spans.MAX_CALLS + 20):
        handle = call_spans.MCP_CALL_TOKEN.set(f"token-{index}")
        call_spans.record_span("phase", 1.0)
        call_spans.MCP_CALL_TOKEN.reset(handle)

    assert len(call_spans._SPANS) <= call_spans.MAX_CALLS


def test_instrumentation_never_raises_into_the_call(token) -> None:
    """A timer that can fail the call it measures is worse than no timer."""
    call_spans.record_span("phase", float("nan"))
    call_spans.record_span(None, 1.0)  # type: ignore[arg-type]

    call_spans.pop_call_spans(token)  # must not raise


def test_the_decorator_preserves_the_function_and_its_result(token) -> None:
    @call_spans.timed("phase.decorated")
    def add(left: int, right: int) -> int:
        """Docstring survives."""
        return left + right

    assert add(2, 3) == 5
    assert add.__name__ == "add"
    assert add.__doc__ == "Docstring survives."
    assert [s["name"] for s in call_spans.pop_call_spans(token)] == ["phase.decorated"]


# ------------------------------------------------------------------ ledger row


def test_spans_reach_the_ledger_row_and_the_chain_still_verifies(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path))
    call_ledger.reset_chain_cache()

    row = call_ledger.record_call(
        request_id="r1",
        tool="edit_memory",
        outcome="ok",
        duration_ms=80.0,
        total_ms=81.0,
        arguments={"path": "a.md"},
        spans=[{"name": "corpus_context.build", "count": 1, "ms": 50.0}],
    )

    assert row is not None
    assert row["spans"] == [{"name": "corpus_context.build", "count": 1, "ms": 50.0}]
    assert call_ledger.verify() == []


def test_an_uninstrumented_call_records_an_empty_span_list(tmp_path, monkeypatch) -> None:
    """Absence must read as "nothing reported", never as a missing field."""
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path))
    call_ledger.reset_chain_cache()

    row = call_ledger.record_call(
        request_id="r1", tool="read_memory", outcome="ok", duration_ms=5.0
    )

    assert row is not None
    assert row["spans"] == []


def test_the_row_bounds_spans_independently_of_the_producer() -> None:
    """The row is hash-chained, so it cannot trust a caller to have bounded itself."""
    shaped = call_ledger._clip_spans(
        [{"name": f"phase.{i}", "count": 1, "ms": float(i)} for i in range(200)]
    )

    assert len(shaped) == call_ledger._MAX_SPANS
    assert shaped[0]["ms"] > shaped[-1]["ms"], "kept the slowest, not the first"


def test_the_row_rebuilds_span_fields_rather_than_passing_them_through() -> None:
    """An unexpected key would change a hashed row's identity meaninglessly."""
    shaped = call_ledger._clip_spans(
        [{"name": "phase", "count": 2, "ms": 3.0, "surprise": "payload"}]
    )

    assert shaped == [{"name": "phase", "count": 2, "ms": 3.0}]


def test_malformed_span_entries_are_dropped_not_fatal() -> None:
    shaped = call_ledger._clip_spans(
        [
            "not a dict",  # type: ignore[list-item]
            {"count": 1, "ms": 1.0},
            {"name": "", "ms": 1.0},
            {"name": "ok", "ms": "not a number"},
            {"name": "kept", "count": 1, "ms": 4.0},
        ]
    )

    assert shaped == [{"name": "kept", "count": 1, "ms": 4.0}]
