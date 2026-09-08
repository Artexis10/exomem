from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

MODEL = "gpt-4o-2024-08-06"
OPENROUTER_MODEL = "openai/gpt-4o-2024-08-06"
KEY = "native-test-key-that-must-not-reach-disk"


class FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


class FakeAsyncClient:
    def __init__(self, replies: list[object]) -> None:
        self.replies = iter(replies)
        self.requests: list[dict[str, object]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        assert isinstance(reply, FakeResponse)
        return reply

    async def aclose(self) -> None:
        self.closed = True


def _payload(
    message: object,
    *,
    finish_reason: object,
    transport: str = "openai",
    prompt_tokens: object = 100,
    completion_tokens: object = 10,
    provider: object = "OpenAI",
    cost: object = 0.0017,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "generation-123",
        "model": OPENROUTER_MODEL if transport == "openrouter" else MODEL,
        "choices": [{"finish_reason": finish_reason, "message": message}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": 20},
        },
    }
    if transport == "openrouter":
        payload["provider"] = provider
        assert isinstance(payload["usage"], dict)
        payload["usage"]["cost"] = cost
    return payload


def _tool_message(
    *,
    call_id: str = "call_1",
    name: str = "recall",
    arguments: str = '{"query":"Ada"}',
) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Recall memory",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }
]


def _backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: str = "openai",
):
    from lme.metered import MeteredOpenAIBackend

    env = "OPENROUTER_API_KEY" if transport == "openrouter" else "OPENAI_API_KEY"
    monkeypatch.setenv(env, KEY)
    return MeteredOpenAIBackend(
        tmp_path / "metered",
        cap_usd=2,
        approval_token="approved-native-agent-test",
        transport=transport,
    )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("transport", ["openai", "openrouter"])
def test_native_tool_call_round_trip_preserves_messages_and_fixed_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    from lme import metered

    backend = _backend(tmp_path, monkeypatch, transport=transport)
    tool_message = _tool_message()
    final_message = {"role": "assistant", "content": "Ada was mentioned."}
    client = FakeAsyncClient(
        [
            FakeResponse(_payload(tool_message, finish_reason="tool_calls", transport=transport)),
            FakeResponse(_payload(final_message, finish_reason="stop", transport=transport)),
        ]
    )
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    messages: list[dict] = [{"role": "user", "content": "Who was mentioned?"}]
    called = asyncio.run(backend.complete_messages(messages, tools=TOOLS, max_tokens=64))
    assert called.message == tool_message
    messages.extend(
        [
            called.message,
            {"role": "tool", "tool_call_id": "call_1", "content": "Ada"},
        ]
    )
    final = asyncio.run(backend.complete_messages(messages, tools=TOOLS, max_tokens=64))

    assert final.message == final_message
    assert final.input_tokens == 100
    assert final.output_tokens == 10
    assert final.cost_usd == pytest.approx(0.0017 if transport == "openrouter" else 0.000325)
    assert final.model_id == MODEL
    assert client.closed is True
    assert len(client.requests) == 2
    for request in client.requests:
        body = request["json"]
        assert isinstance(body, dict)
        assert body["tools"] == TOOLS
        assert body["tool_choice"] == "auto"
        assert body["parallel_tool_calls"] is False
        assert body["max_tokens"] == 64
        assert body["temperature"] == 0
        assert body["n"] == 1
    assert client.requests[1]["json"]["messages"] == messages  # type: ignore[index]
    if transport == "openrouter":
        assert client.requests[0]["json"]["provider"] == {  # type: ignore[index]
            "only": ["openai"],
            "order": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 2.5, "completion": 10.0, "request": 0},
        }
        assert client.requests[0]["json"]["transforms"] == []  # type: ignore[index]

    requests = _jsonl(tmp_path / "metered" / "requests.jsonl")
    results = _jsonl(tmp_path / "metered" / "results.jsonl")
    assert requests[1]["request"]["messages"] == messages
    assert results[0]["message"] == tool_message
    assert results[1]["message"] == final_message


@pytest.mark.parametrize("transport", ["openai", "openrouter"])
def test_native_empty_tools_uses_the_exact_text_completion_request_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    from lme import metered

    backend = _backend(tmp_path, monkeypatch, transport=transport)
    message = {"role": "assistant", "content": "judge result"}
    payload = _payload(message, finish_reason="stop", transport=transport)
    sync_request: dict[str, object] = {}

    def post(url: str, **kwargs: object) -> FakeResponse:
        sync_request.update({"url": url, **kwargs})
        return FakeResponse(payload)

    monkeypatch.setattr(metered.httpx, "post", post)
    backend.complete("judge prompt", max_tokens=10)

    client = FakeAsyncClient([FakeResponse(payload)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)
    completion = asyncio.run(
        backend.complete_messages(
            [{"role": "user", "content": "judge prompt"}],
            tools=[],
            max_tokens=10,
        )
    )

    assert completion.message == message
    assert client.requests[0]["json"] == sync_request["json"]
    assert "tools" not in client.requests[0]["json"]  # type: ignore[operator]
    assert "tool_choice" not in client.requests[0]["json"]  # type: ignore[operator]
    assert "parallel_tool_calls" not in client.requests[0]["json"]  # type: ignore[operator]


def test_native_empty_tools_rejects_a_tool_call_response_after_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    client = FakeAsyncClient([FakeResponse(_payload(_tool_message(), finish_reason="tool_calls"))])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(MeteredCallError, match="name"):
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "judge prompt"}],
                tools=[],
                max_tokens=10,
            )
        )
    assert [row["kind"] for row in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]


def test_native_cancellation_closes_http_retains_reservation_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered

    backend = _backend(tmp_path, monkeypatch)
    started = asyncio.Event()

    class HangingClient:
        closed = False

        async def post(self, *args: object, **kwargs: object) -> FakeResponse:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            self.closed = True

    client = HangingClient()
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    async def cancel_call() -> None:
        task = asyncio.create_task(
            backend.complete_messages(
                [{"role": "user", "content": "question"}], tools=TOOLS, max_tokens=10
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_call())

    assert client.closed is True
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [row["kind"] for row in ledger] == ["approval", "reserve"]
    assert ledger[-1]["running_total"] == pytest.approx(0.3201)
    assert (tmp_path / "metered" / "STOP").is_file()
    assert _jsonl(tmp_path / "metered" / "failures.jsonl")[0]["charged"] == "reservation"


@pytest.mark.parametrize("max_tokens", [0, -1, 4097, True, 1.5, "64"])
def test_native_rejects_invalid_output_cap_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_tokens: object,
) -> None:
    from lme import metered
    from lme.metered import MeteredConfigurationError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("invalid settings must prevent HTTP"),
    )

    with pytest.raises(MeteredConfigurationError, match="max_tokens"):
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "question"}],
                tools=TOOLS,
                max_tokens=max_tokens,  # type: ignore[arg-type]
            )
        )
    assert [row["kind"] for row in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == ["approval"]


def test_native_refuses_oversized_serialized_context_before_reservation_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredConfigurationError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("oversized context must prevent HTTP"),
    )

    with pytest.raises(MeteredConfigurationError, match="context"):
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "word " * 150_000}],
                tools=TOOLS,
                max_tokens=4096,
            )
        )
    assert [row["kind"] for row in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == ["approval"]
    assert not (tmp_path / "metered" / "requests.jsonl").exists()


def test_native_rejects_an_empty_assistant_message_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredConfigurationError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("invalid messages must prevent HTTP"),
    )

    with pytest.raises(MeteredConfigurationError, match="assistant"):
        asyncio.run(
            backend.complete_messages([{"role": "assistant", "content": None}], tools=TOOLS)
        )
    assert [row["kind"] for row in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == ["approval"]


@pytest.mark.parametrize(
    ("message", "match"),
    [
        ({"role": "assistant", "content": None, "tool_calls": []}, "tool_calls"),
        (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "same",
                        "type": "function",
                        "function": {"name": "recall", "arguments": "{}"},
                    },
                    {
                        "id": "same",
                        "type": "function",
                        "function": {"name": "recall", "arguments": "{}"},
                    },
                ],
            },
            "duplicate",
        ),
        (_tool_message(name="not_declared"), "name"),
        (_tool_message(arguments="not-json"), "arguments"),
        (_tool_message(arguments="[]"), "arguments"),
        (_tool_message(arguments='{"query":NaN}'), "arguments"),
    ],
)
def test_native_rejects_malformed_or_duplicate_tool_calls_after_charge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, object],
    match: str,
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    client = FakeAsyncClient([FakeResponse(_payload(message, finish_reason="tool_calls"))])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(MeteredCallError, match=match):
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "question"}], tools=TOOLS, max_tokens=64
            )
        )
    assert [row["kind"] for row in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]
    assert (tmp_path / "metered" / "STOP").is_file()


def test_native_truncated_completion_is_charged_recorded_and_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    client = FakeAsyncClient(
        [
            FakeResponse(
                _payload({"role": "assistant", "content": "partial"}, finish_reason="length")
            )
        ]
    )
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(MeteredCallError, match="truncated"):
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "question"}], tools=TOOLS, max_tokens=64
            )
        )
    assert _jsonl(tmp_path / "metered" / "results.jsonl")[0]["status"] == "error"
    assert _jsonl(tmp_path / "metered" / "failures.jsonl")[0]["finish_reason"] == "length"


def test_native_nested_credential_reflection_is_redacted_stopped_and_never_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    message = _tool_message(arguments=json.dumps({"query": {"secret": KEY}}))
    client = FakeAsyncClient([FakeResponse(_payload(message, finish_reason="tool_calls"))])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(MeteredCallError, match="credential") as caught:
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "question"}], tools=TOOLS, max_tokens=64
            )
        )
    assert KEY not in repr(caught.value)
    assert (tmp_path / "metered" / "STOP").is_file()
    for path in (tmp_path / "metered").rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8")
    assert "[REDACTED]" in json.dumps(_jsonl(tmp_path / "metered" / "results.jsonl")[0]["message"])


@pytest.mark.parametrize("transport", ["openai", "openrouter"])
def test_native_json_escaped_credential_is_decoded_redacted_and_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch, transport=transport)
    escaped_key = KEY.replace("n", r"\u006e")
    arguments = '{"query":{"secret":"' + escaped_key + '"}}'
    message = _tool_message(arguments=arguments)
    client = FakeAsyncClient(
        [
            FakeResponse(
                _payload(message, finish_reason="tool_calls", transport=transport)
            )
        ]
    )
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(MeteredCallError, match="credential") as caught:
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "question"}], tools=TOOLS, max_tokens=64
            )
        )

    assert KEY not in repr(caught.value)
    assert (tmp_path / "metered" / "STOP").is_file()
    assert [row["kind"] for row in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]
    stored_message = _jsonl(tmp_path / "metered" / "results.jsonl")[0]["message"]
    stored_arguments = stored_message["tool_calls"][0]["function"]["arguments"]
    assert json.loads(stored_arguments) == {"query": {"secret": "[REDACTED]"}}


@pytest.mark.parametrize(
    ("transport", "payload", "match", "settled"),
    [
        (
            "openai",
            _payload(
                {"role": "assistant", "content": "answer"},
                finish_reason="stop",
                prompt_tokens=None,
            ),
            "usage",
            False,
        ),
        (
            "openrouter",
            _payload(
                {"role": "assistant", "content": "answer"},
                finish_reason="stop",
                transport="openrouter",
                provider="Other",
            ),
            "provider",
            True,
        ),
        (
            "openrouter",
            _payload(
                {"role": "assistant", "content": "answer"},
                finish_reason="stop",
                transport="openrouter",
                cost=None,
            ),
            "cost",
            False,
        ),
    ],
)
def test_native_reuses_usage_and_route_fail_closed_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    payload: dict[str, object],
    match: str,
    settled: bool,
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch, transport=transport)
    client = FakeAsyncClient([FakeResponse(payload)])
    monkeypatch.setattr(metered.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(MeteredCallError, match=match):
        asyncio.run(
            backend.complete_messages(
                [{"role": "user", "content": "question"}], tools=TOOLS, max_tokens=64
            )
        )
    kinds = [row["kind"] for row in _jsonl(tmp_path / "metered" / "ledger.jsonl")]
    assert kinds == (
        ["approval", "reserve", "commit", "release"] if settled else ["approval", "reserve"]
    )
    assert (tmp_path / "metered" / "STOP").is_file()


def test_existing_text_completion_rejects_tool_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(_payload(_tool_message(), finish_reason="tool_calls")),
    )

    with pytest.raises(MeteredCallError, match="completion"):
        backend.complete("question")
