"""The request-scoped deadline every MCP tool call carries.

The defect this closes is work that finishes after the caller stopped
listening. A budget that quietly fails to exist looks exactly like a call that
had all the time in the world, so several of these assert on presence and on
absence rather than on a number alone.
"""

from __future__ import annotations

import logging
import time

import pytest

from exomem import command_surface, request_budget


@pytest.fixture(autouse=True)
def _clear_resolved_budget():
    request_budget.reset_resolution_cache()
    yield
    request_budget.reset_resolution_cache()


def test_no_budget_outside_an_mcp_call() -> None:
    """CLI, watcher and in-process callers behave exactly as before."""
    assert request_budget.current() is None


def test_an_mcp_call_carries_the_default_fifty_second_deadline() -> None:
    entry = time.monotonic()
    with command_surface.mcp_request_context("req-1", tool="ask_memory"):
        budget = request_budget.current()
        assert budget is not None
        assert budget.seconds == request_budget.MCP_REQUEST_BUDGET_SECONDS == 50.0
        # Entry plus the budget, measured on the same clock the writer lease
        # already uses for its acknowledgement deadlines.
        assert entry + 49.0 <= budget.deadline <= time.monotonic() + 50.0
    assert request_budget.current() is None


def test_the_operator_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "12.5")
    with command_surface.mcp_request_context("req-2", tool="ask_memory"):
        budget = request_budget.current()
        assert budget is not None
        assert budget.seconds == 12.5


def test_a_malformed_override_falls_back_to_the_default_with_one_warning(
    monkeypatch, caplog
) -> None:
    """A typo in an operator variable must not remove the bound entirely."""
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "soon-ish")
    with caplog.at_level(logging.WARNING, logger="exomem.request_budget"):
        for index in range(3):
            with command_surface.mcp_request_context(f"req-{index}", tool="ask_memory"):
                budget = request_budget.current()
                assert budget is not None
                assert budget.seconds == request_budget.MCP_REQUEST_BUDGET_SECONDS

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name == "exomem.request_budget"
    ]
    assert len(warnings) == 1
    assert request_budget.BUDGET_ENV_VAR in warnings[0].getMessage()


@pytest.mark.parametrize("value", ["0", "-3", "nan", ""])
def test_a_non_positive_override_is_malformed(monkeypatch, value) -> None:
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, value)
    with command_surface.mcp_request_context("req-3", tool="ask_memory"):
        budget = request_budget.current()
        assert budget is not None
        assert budget.seconds == request_budget.MCP_REQUEST_BUDGET_SECONDS


def test_reconcile_class_calls_are_exempt() -> None:
    """Their terminal is the derived state; the graph join is unbounded by design."""
    with command_surface.mcp_request_context("req-4", tool="reconcile"):
        assert request_budget.current() is None
    with command_surface.mcp_request_context(
        "req-5", tool="maintain_memory", arguments={"mode": "reconcile"}
    ):
        assert request_budget.current() is None


def test_maintain_memory_outside_reconcile_mode_is_budgeted() -> None:
    with command_surface.mcp_request_context(
        "req-6", tool="maintain_memory", arguments={"mode": "audit"}
    ):
        assert request_budget.current() is not None


def test_a_call_with_no_tool_name_still_gets_a_budget() -> None:
    """The dispatch site always knows the tool; a defensive miss must not unbound."""
    with command_surface.mcp_request_context("req-7"):
        assert request_budget.current() is not None


def test_can_afford_compares_the_reserve_against_what_is_left() -> None:
    budget = request_budget.RequestBudget(seconds=10.0)
    assert budget.can_afford(1.0) is True
    assert budget.can_afford(10_000.0) is False
    assert 9.0 < budget.remaining() <= 10.0


def test_the_response_block_is_absent_until_something_is_left_out() -> None:
    budget = request_budget.RequestBudget(seconds=10.0)
    assert budget.as_response_block() is None
    # The row is not a message to a client: it records the budget either way,
    # so an operator can see a reserve about to start costing calls.
    assert budget.as_ledger_block()["skipped"] == []

    budget.note_skipped("rerank")
    budget.note_skipped("rerank")
    budget.note_truncated("pack")

    block = budget.as_response_block()
    assert block is not None
    assert block["applied"] is True
    assert block["seconds"] == 10.0
    assert block["skipped"] == ["rerank"]
    assert block["truncated"] == ["pack"]
    assert isinstance(block["remaining_ms_at_return"], int)

    ledger = budget.as_ledger_block()
    assert ledger["seconds"] == 10.0
    assert ledger["skipped"] == ["rerank"]
    assert isinstance(ledger["remaining_ms"], int)


def test_no_tool_exposes_a_budget_argument() -> None:
    """A client cannot loosen the bound: there is no knob on the surface.

    The budget exists to protect the caller from work it will never receive,
    and the caller that most needs it is the one that cannot be trusted to set
    it. A per-call argument would also move the tool-surface fingerprint.
    """
    import inspect

    from exomem import commands

    offenders = [
        (command.name, name)
        for command in commands.PRODUCT_COMMANDS
        for name in inspect.signature(command.leaf).parameters
        if "budget" in name or "deadline" in name or name == "timeout"
    ]
    assert offenders == []


def test_an_expired_budget_reports_a_non_negative_remaining() -> None:
    budget = request_budget.RequestBudget(seconds=10.0, entry=time.monotonic() - 60.0)
    assert budget.remaining() == 0.0
    assert budget.can_afford(0.1) is False
    budget.note_skipped("pack")
    block = budget.as_response_block()
    assert block is not None
    assert block["remaining_ms_at_return"] == 0
