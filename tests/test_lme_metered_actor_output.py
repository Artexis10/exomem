"""Typed actor-output failures: opt-in task failure without a financial STOP.

No network: these drive the response processor directly with authored replies.
"""
from __future__ import annotations

import json

import pytest


def backend(tmp_path, monkeypatch, **kwargs):
    from lme.metered import MeteredOpenAIBackend

    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-credential")
    return MeteredOpenAIBackend(tmp_path / "run", cap_usd=1, approval_token="instrument-test", **kwargs)


def prepared(b, max_tokens=64):
    return b._prepare_call({"model": b.wire_model}, max_tokens=max_tokens,
                           artifact_root=b.run_dir, request_id=None, kind="native_chat")


def reply(b, *, finish_reason="length", message=None, model=None, usage=None):
    return {
        "model": b.wire_model if model is None else model,
        "id": "gen-1",
        "choices": [{"finish_reason": finish_reason,
                     "message": message if message is not None else {"role": "assistant", "content": "partial"}}],
        "usage": usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 64},
    }


def process(b, data, **kwargs):
    return b._process_response(data, prepared(b), native=True, tool_names=frozenset({"ask_memory"}), **kwargs)


def failures(b):
    path = b.run_dir / "failures.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_default_backend_still_stops_on_truncated_output(tmp_path, monkeypatch):
    from lme.metered import MeteredCallError

    b = backend(tmp_path, monkeypatch)
    with pytest.raises(MeteredCallError):
        process(b, reply(b))
    assert b.ledger.stop_path.exists()


def test_opt_in_treats_settled_truncation_as_a_typed_actor_output_failure(tmp_path, monkeypatch):
    from lme.metered import MeteredActorOutputError

    b = backend(tmp_path, monkeypatch, stop_on_actor_failure=False)
    with pytest.raises(MeteredActorOutputError) as excinfo:
        process(b, reply(b))
    error = excinfo.value
    assert error.benchmark_failure_category == "actor_output"
    assert not b.ledger.stop_path.exists(), "a declared task-budget loss is not a financial stop"
    # The charge for the failed call is preserved, not erased.
    assert error.cost_usd is not None and error.cost_usd > 0
    assert error.input_tokens == 100 and error.output_tokens == 64
    committed = sum(e.units for e in b.ledger._entries() if e.kind == "commit")
    assert committed == pytest.approx(error.cost_usd)
    assert failures(b)[-1]["category"] == "actor_output"
    assert failures(b)[-1]["charged"] == "measured"


def test_opt_in_still_stops_on_reflected_credential(tmp_path, monkeypatch):
    from lme.metered import MeteredCallError

    b = backend(tmp_path, monkeypatch, stop_on_actor_failure=False)
    message = {"role": "assistant", "content": "key is test-only-not-a-credential"}
    with pytest.raises(MeteredCallError) as excinfo:
        process(b, reply(b, finish_reason="stop", message=message))
    assert getattr(excinfo.value, "benchmark_failure_category", None) != "actor_output"
    assert b.ledger.stop_path.exists()


def test_opt_in_still_stops_on_wrong_response_model(tmp_path, monkeypatch):
    from lme.metered import MeteredCallError

    b = backend(tmp_path, monkeypatch, stop_on_actor_failure=False)
    with pytest.raises(MeteredCallError):
        process(b, reply(b, finish_reason="stop", model="some-other-model",
                         message={"role": "assistant", "content": "hi"}))
    assert b.ledger.stop_path.exists()
    assert failures(b)[-1]["category"] == "integrity"


def test_opt_in_still_stops_on_malformed_usage(tmp_path, monkeypatch):
    from lme.metered import MeteredCallError

    b = backend(tmp_path, monkeypatch, stop_on_actor_failure=False)
    with pytest.raises(MeteredCallError) as excinfo:
        process(b, reply(b, usage={"prompt_tokens": "many", "completion_tokens": 1}))
    assert getattr(excinfo.value, "benchmark_failure_category", None) != "actor_output"
    assert b.ledger.stop_path.exists()


def test_opt_in_still_stops_on_byok_billing(tmp_path, monkeypatch):
    """BYOK billing is an accounting-integrity fault, never an actor failure."""
    from lme.metered import MeteredCallError

    b = backend(tmp_path, monkeypatch, stop_on_actor_failure=False)
    usage = {"prompt_tokens": 100, "completion_tokens": 64, "cost": 0.001, "is_byok": True}
    data = reply(b, finish_reason="stop", usage=usage,
                 message={"role": "assistant", "content": "done"})
    data["provider"] = "OpenAI"
    call = prepared(b)
    monkeypatch.setattr(b, "transport", "openrouter")
    with pytest.raises(MeteredCallError) as excinfo:
        b._process_response(data, call, native=True, tool_names=frozenset({"ask_memory"}))
    assert getattr(excinfo.value, "benchmark_failure_category", None) != "actor_output"
    assert b.ledger.stop_path.exists()
    assert failures(b)[-1]["category"] == "integrity"


def test_malformed_tool_call_output_is_an_actor_output_failure(tmp_path, monkeypatch):
    from lme.metered import MeteredActorOutputError

    b = backend(tmp_path, monkeypatch, stop_on_actor_failure=False)
    message = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "call-1", "type": "function",
                               "function": {"name": "ask_memory", "arguments": "not-json"}}]}
    with pytest.raises(MeteredActorOutputError):
        process(b, reply(b, finish_reason="tool_calls", message=message))
    assert not b.ledger.stop_path.exists()


def test_valid_native_completion_is_unaffected_by_the_option(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch, stop_on_actor_failure=False)
    processed = process(b, reply(b, finish_reason="stop",
                                 message={"role": "assistant", "content": "done"}))
    assert processed.message == {"role": "assistant", "content": "done"}
    assert not b.ledger.stop_path.exists()


def test_provider_reported_cache_and_reasoning_usage_is_passed_through(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    usage = {"prompt_tokens": 100, "completion_tokens": 64,
             "prompt_tokens_details": {"cached_tokens": 40},
             "completion_tokens_details": {"reasoning_tokens": 12}}
    processed = process(b, reply(b, finish_reason="stop", usage=usage,
                                 message={"role": "assistant", "content": "done"}))
    assert processed.cache_read_tokens == 40
    assert processed.reasoning_tokens == 12
    # Reasoning is reported beside output, never added to it a second time.
    assert processed.output_tokens == 64


def test_usage_without_reported_details_stays_unknown(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    processed = process(b, reply(b, finish_reason="stop",
                                 message={"role": "assistant", "content": "done"}))
    assert processed.reasoning_tokens is None
    assert processed.cache_write_tokens in (0, None)
