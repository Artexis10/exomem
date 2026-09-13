"""Backward-compatible phase instrumentation: typed outcome, time and context.

Drives the real worker subprocess with a fake model; no network, no product.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from lme.native_agent import AgentLimits, run_agent_phase

from tests.test_lme_native_agent import Backend, broker, tool


def _run(b, tmp_path, name="run"):
    return asyncio.run(run_agent_phase(b, phase="answer", turn="Question", out=tmp_path / name))


def test_completed_phase_is_typed_and_times_are_split(tmp_path):
    b = broker(Backend([tool("ask_memory", {"query": "x"}), {"role": "assistant", "content": "Cedar."}]))
    result = _run(b, tmp_path)
    assert result["status"] == "completed"
    assert result["outcome_class"] == "completed"
    assert result["model_seconds"] is not None and result["tool_seconds"] is not None
    assert result["startup_seconds"] is not None
    assert result["model_seconds"] + result["tool_seconds"] <= result["elapsed_seconds"]
    assert result["peak_context_tokens"] > 0
    assert result["cumulative_context_tokens"] >= result["peak_context_tokens"]


def test_declared_exhaustion_is_classified_apart_from_infrastructure(tmp_path):
    limits = AgentLimits(phase_model_calls=1, phase_seconds=20, run_seconds=30)
    b = broker(Backend([tool("ask_memory", {"query": "x"}), {"role": "assistant", "content": "done"}]),
               limits=limits)
    result = _run(b, tmp_path)
    assert result["status"] == "incomplete"
    assert result["outcome_class"] == "declared_exhaustion"


def test_transport_failure_is_classified_as_infrastructure(tmp_path):
    class Broken:
        async def complete_messages(self, messages, *, tools, max_tokens):
            raise ConnectionError("transport failure with ambiguous spend")

    result = _run(broker(Broken()), tmp_path)
    assert result["outcome_class"] == "infrastructure"


def test_actor_output_failure_is_classified_from_its_typed_category(tmp_path):
    from lme.metered import MeteredActorOutputError

    class Truncating:
        async def complete_messages(self, messages, *, tools, max_tokens):
            raise MeteredActorOutputError("completion was truncated at max_tokens",
                                          input_tokens=120, output_tokens=2000, cost_usd=0.002)

    result = _run(broker(Truncating()), tmp_path)
    assert result["outcome_class"] == "actor_output"
    # The failed call's charge and usage survive the exception.
    assert result["input_tokens"] == 120
    assert result["output_tokens"] == 2000
    assert result["cost_usd"] == pytest.approx(0.002)
    assert result["failed_model_calls"] == 1


def test_unknown_usage_on_a_failed_call_is_not_invented_as_zero(tmp_path):
    from lme.metered import MeteredCallError

    class Uncertain:
        async def complete_messages(self, messages, *, tools, max_tokens):
            raise MeteredCallError("transport response had unknown usage; reservation retained")

    result = _run(broker(Uncertain()), tmp_path)
    assert result["failed_model_calls"] == 1
    assert result["unknown_usage_calls"] == 1
    assert result["outcome_class"] == "infrastructure"


def test_provider_cache_and_reasoning_usage_is_recorded_only_when_reported(tmp_path):
    class Reporting:
        def __init__(self):
            self.replies = iter([{"role": "assistant", "content": "done"}])

        async def complete_messages(self, messages, *, tools, max_tokens):
            return SimpleNamespace(message=next(self.replies), input_tokens=30, output_tokens=10,
                                   cost_usd=0.001, cache_read_tokens=12, reasoning_tokens=5)

    result = _run(broker(Reporting()), tmp_path)
    assert result["cache_read_tokens"] == 12
    assert result["reasoning_tokens"] == 5
    assert result["output_tokens"] == 10, "reasoning already inside output is never added twice"

    plain = _run(broker(Backend([{"role": "assistant", "content": "done"}])), tmp_path, name="plain")
    assert plain["reasoning_tokens"] is None
    assert plain["cache_read_tokens"] is None


def test_existing_lme_result_contract_is_unchanged(tmp_path):
    result = _run(broker(Backend([{"role": "assistant", "content": "done"}])), tmp_path)
    for field in ("phase", "status", "model_calls", "tool_calls", "input_tokens",
                  "output_tokens", "cost_usd", "answer", "elapsed_seconds"):
        assert field in result, field
    saved = json.loads((tmp_path / "run" / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
