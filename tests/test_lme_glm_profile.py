"""The economical native agent retains the same accounting and isolation contract."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from lme import metered, metered_profiles
from test_lme_metered_chat import KEY, TOOLS, FakeAsyncClient, FakeResponse, _payload, _tool_message

GLM = "z-ai/glm-5.3-flash"
GLM_WIRE = "z-ai/glm-5.3-flash-20260826"


def metadata(provider="Z.AI"):
    return {"endpoints": {"available": [{"provider": provider, "model": GLM, "selected": True}]}}


@pytest.fixture
def tokenizer_file(tmp_path, monkeypatch):
    raw = json.dumps({"version": "1.0", "truncation": None, "padding": None,
        "added_tokens": [], "normalizer": None, "pre_tokenizer": {"type": "Whitespace"},
        "post_processor": None, "decoder": None,
        "model": {"type": "WordLevel", "vocab": {"[UNK]": 0, "hello": 1}, "unk_token": "[UNK]"}}).encode()
    path = tmp_path / "tokenizer.json"
    path.write_bytes(raw)
    monkeypatch.setattr(metered_profiles, "GLM_TOKENIZER_SHA256", hashlib.sha256(raw).hexdigest(), raising=False)
    return path


def backend(tmp_path, tokenizer_file, monkeypatch, cap=1):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    return metered.MeteredOpenAIBackend(tmp_path / "run", cap_usd=cap,
        approval_token="offline test", model=GLM, transport="openrouter",
        tokenizer_path=tokenizer_file)


def test_glm_profile_uses_regular_prices_and_its_own_provider():
    profile = metered_profiles.model_profile(GLM, "openrouter")
    assert profile.wire_model("openrouter") == GLM_WIRE
    assert profile.provider_name == "Z.AI"
    assert profile.provider_slug == "z-ai/fp8"
    assert (profile.input_rate, profile.cached_rate, profile.output_rate) == (.15, .03, .5)
    assert profile.tokenizer != "o200k_base"
    with pytest.raises(ValueError):
        metered_profiles.model_profile(GLM, "openai")


@pytest.mark.parametrize("endpoint_model", [GLM, GLM_WIRE])
@pytest.mark.parametrize("response_model", [GLM, GLM_WIRE])
def test_glm_and_openai_judge_share_one_ledger_with_distinct_routes(tmp_path, tokenizer_file, monkeypatch, endpoint_model, response_model):
    instance = backend(tmp_path, tokenizer_file, monkeypatch)
    message = _tool_message()
    message["reasoning_details"] = [{"type": "reasoning.text", "text": "Inspect memory."}]
    reply = _payload(message, finish_reason="tool_calls", transport="openrouter", provider="Z.AI", cost=.001)
    reply["model"] = response_model
    reply["openrouter_metadata"] = metadata()
    reply["openrouter_metadata"]["endpoints"]["available"][0]["model"] = endpoint_model
    del reply["provider"]  # Modern documented metadata does not require this legacy field.
    judge = _payload({"role": "assistant", "content": "yes"}, finish_reason="stop", transport="openrouter", cost=.002)
    client = FakeAsyncClient([FakeResponse(reply), FakeResponse(judge)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)
    result = asyncio.run(instance.complete_messages([{"role": "user", "content": "hello"}], tools=TOOLS))
    assert result.message == message
    body = client.requests[0]["json"]
    assert body["model"] == GLM_WIRE
    assert client.requests[0]["headers"]["X-OpenRouter-Metadata"] == "enabled"
    assert body["provider"]["only"] == ["z-ai/fp8"]
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["max_price"] == {"prompt": .15, "completion": .5, "request": 0}
    assert not {"n", "parallel_tool_calls"} & body.keys()
    asyncio.run(instance.complete_messages([{"role": "user", "content": "Judge"}], tools=[], model=metered_profiles.JUDGE_MODEL, max_tokens=10))
    assert client.requests[1]["json"]["provider"]["only"] == ["openai"]
    ledger = [json.loads(line) for line in (tmp_path / "run/ledger.jsonl").read_text().splitlines()]
    assert sum(row["units"] for row in ledger if row["kind"] == "commit") == pytest.approx(.003)
    assert KEY not in (tmp_path / "run/requests.jsonl").read_text()


def test_glm_tokenizer_drift_refuses_before_creating_ledger(tmp_path, tokenizer_file, monkeypatch):
    tokenizer_file.write_text("{}")
    with pytest.raises(metered.MeteredConfigurationError, match="tokenizer"):
        backend(tmp_path, tokenizer_file, monkeypatch)
    assert not (tmp_path / "run/ledger.jsonl").exists()


def test_glm_never_uses_openai_tokenizer_for_agent_context(tmp_path, tokenizer_file, monkeypatch):
    import tiktoken
    instance = backend(tmp_path, tokenizer_file, monkeypatch)
    monkeypatch.setattr(tiktoken, "get_encoding", lambda *a, **k: pytest.fail("GLM used OpenAI tokenizer"))
    assert instance.count_chat_tokens([{"role": "user", "content": "hello"}], TOOLS) > 0


def test_glm_wrong_provider_is_charged_then_stops_judge(tmp_path, tokenizer_file, monkeypatch):
    instance = backend(tmp_path, tokenizer_file, monkeypatch)
    reply = _payload({"role": "assistant", "content": "done"}, finish_reason="stop", transport="openrouter", provider="Other", cost=.001)
    reply["model"] = GLM_WIRE
    reply["openrouter_metadata"] = metadata("Other")
    client = FakeAsyncClient([FakeResponse(reply)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(metered.MeteredCallError, match="provider"):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "hello"}], tools=[]))
    with pytest.raises(metered.BudgetExceeded):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "Judge"}], tools=[], model=metered_profiles.JUDGE_MODEL, max_tokens=10))
    ledger = [json.loads(line) for line in (tmp_path / "run/ledger.jsonl").read_text().splitlines()]
    assert sum(row["units"] for row in ledger if row["kind"] == "commit") == pytest.approx(.001)
    assert len(client.requests) == 1


@pytest.mark.parametrize("bad_metadata", [None, {}, {"endpoints": {"available": []}},
    {"endpoints": {"available": metadata()["endpoints"]["available"] * 2}}])
def test_glm_requires_one_selected_provider_receipt(tmp_path, tokenizer_file, monkeypatch, bad_metadata):
    instance = backend(tmp_path, tokenizer_file, monkeypatch)
    reply = _payload({"role": "assistant", "content": "done"}, finish_reason="stop", transport="openrouter", provider="Z.AI", cost=.001)
    reply["model"] = GLM_WIRE
    reply["openrouter_metadata"] = bad_metadata
    client = FakeAsyncClient([FakeResponse(reply)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(metered.MeteredCallError, match="provider"):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "hello"}], tools=[]))
    assert (tmp_path / "run/STOP").exists()


def test_glm_unknown_charge_holds_its_reservation_and_blocks_judge(tmp_path, tokenizer_file, monkeypatch):
    instance = backend(tmp_path, tokenizer_file, monkeypatch)
    client = FakeAsyncClient([FakeResponse({}, status_code=503)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(metered.MeteredCallError):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "hello"}], tools=[]))
    ledger = [json.loads(line) for line in (tmp_path / "run/ledger.jsonl").read_text().splitlines()]
    assert ledger[-1]["kind"] == "reserve"
    assert ledger[-1]["units"] == pytest.approx(128000*.15/1e6+4096*.5/1e6)
    with pytest.raises(metered.BudgetExceeded):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "Judge"}], tools=[], model=metered_profiles.JUDGE_MODEL))
    assert len(client.requests) == 1


def test_glm_preparation_freezes_tokenizer_and_refuses_rebound_digest(tmp_path, tokenizer_file, monkeypatch):
    from lme import native_pilot as pilot

    # Reuse the same minimal source/product fixture as the preparation suite.
    from test_lme_native_pilot import inputs
    dataset, product = inputs.__wrapped__(tmp_path, monkeypatch)
    out = tmp_path / "prepared"
    result = pilot.prepare_native(dataset, tmp_path / "judge", out, product_root=product,
        profile="fixture", size=1, budget_cap_usd=2, transport="openrouter", agent_model=GLM,
        agent_tokenizer=tokenizer_file)
    assert (out / "agent-tokenizer.json").read_bytes() == tokenizer_file.read_bytes()
    tokenizer_file.write_text("changed source does not change snapshot")
    pilot.validate_native_run(out, expected_plan_sha256=result["plan_sha256"])
    frozen = out / "agent-tokenizer.json"
    frozen.write_text("{}")
    plan = json.loads((out / "native-plan.json").read_text())
    plan["artifacts"]["agent-tokenizer.json"] = hashlib.sha256(frozen.read_bytes()).hexdigest()
    raw = pilot._json(plan)
    (out / "native-plan.json").write_bytes(raw)
    with pytest.raises(ValueError, match="tokenizer"):
        pilot.validate_native_run(out, expected_plan_sha256=hashlib.sha256(raw).hexdigest())
    assert not (out / "execution").exists()


def test_native_broker_uses_selected_model_counter_before_spend(tmp_path, monkeypatch):
    import tiktoken
    from lme.native_agent import run_agent_phase
    from test_lme_native_agent import Backend, broker
    instance = Backend([])
    instance.count_chat_tokens = lambda messages, tools: 120000
    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda *a: pytest.fail("broker selected an OpenAI tokenizer"))
    result = asyncio.run(run_agent_phase(broker(instance), phase="writer", turn="hello", out=tmp_path / "writer"))
    assert result["status"] == "incomplete"
    assert result["model_calls"] == 0
    assert instance.requests == []


@pytest.mark.parametrize("reported_model", [None, "attacker/other-model", {"invalid": "shape"}])
def test_glm_rejects_inconsistent_selected_endpoint_model(tmp_path, tokenizer_file, monkeypatch, reported_model):
    instance = backend(tmp_path, tokenizer_file, monkeypatch)
    reply = _payload({"role": "assistant", "content": "done"}, finish_reason="stop", transport="openrouter", provider="Z.AI", cost=.001)
    reply["model"] = GLM_WIRE
    reply["openrouter_metadata"] = metadata()
    reply["openrouter_metadata"]["endpoints"]["available"][0]["model"] = reported_model
    client = FakeAsyncClient([FakeResponse(reply)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(metered.MeteredCallError, match="provider"):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "hello"}], tools=[]))
    assert (tmp_path / "run/STOP").exists()
    ledger = [json.loads(line) for line in (tmp_path / "run/ledger.jsonl").read_text().splitlines()]
    assert sum(row["units"] for row in ledger if row["kind"] == "commit") == pytest.approx(.001)
    with pytest.raises(metered.BudgetExceeded):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "Judge"}], tools=[], model=metered_profiles.JUDGE_MODEL))
    assert len(client.requests) == 1


@pytest.mark.parametrize("reported_model", [None, "attacker/other-model", {"invalid": "shape"}])
def test_glm_rejects_unrecognized_response_model(tmp_path, tokenizer_file, monkeypatch, reported_model):
    instance = backend(tmp_path, tokenizer_file, monkeypatch)
    reply = _payload({"role": "assistant", "content": "done"}, finish_reason="stop", transport="openrouter", provider="Z.AI", cost=.001)
    reply["model"] = GLM_WIRE
    reply["openrouter_metadata"] = metadata()
    reply["model"] = reported_model
    client = FakeAsyncClient([FakeResponse(reply)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(metered.MeteredCallError, match="model"):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "hello"}], tools=[]))
    assert (tmp_path / "run/STOP").exists()
    ledger = [json.loads(line) for line in (tmp_path / "run/ledger.jsonl").read_text().splitlines()]
    assert sum(row["units"] for row in ledger if row["kind"] == "commit") == pytest.approx(.001)
    with pytest.raises(metered.BudgetExceeded):
        asyncio.run(instance.complete_messages([{"role": "user", "content": "Judge"}], tools=[], model=metered_profiles.JUDGE_MODEL))
    assert len(client.requests) == 1
