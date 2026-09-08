from __future__ import annotations

import json
import math
import stat
from pathlib import Path

import httpx
import pytest

MODEL = "gpt-4o-2024-08-06"
KEY = "test-key-that-must-not-reach-disk"
OPENROUTER_KEY = "openrouter-key-that-must-not-reach-disk"
OPENROUTER_MODEL = "openai/gpt-4o-2024-08-06"


class FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


def _success(
    *,
    prompt_tokens: object = 100,
    completion_tokens: object = 10,
    cached_tokens: object = 20,
    finish_reason: object = "stop",
    model: object = MODEL,
    content: object = "answer",
) -> dict[str, object]:
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }


def _backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cap: float = 1.0):
    from lme.metered import MeteredOpenAIBackend

    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    return MeteredOpenAIBackend(
        tmp_path / "metered",
        cap_usd=cap,
        approval_token="founder-approved-seven-question-replay",
    )


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _openrouter_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cap: float = 1.0
):
    from lme.metered import MeteredOpenAIBackend

    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
    return MeteredOpenAIBackend(
        tmp_path / "metered",
        cap_usd=cap,
        approval_token="approved-openrouter-pilot",
        transport="openrouter",
    )


def _openrouter_success(
    *,
    cost: object = 0.0017,
    provider: object = "OpenAI",
    model: object = OPENROUTER_MODEL,
    content: object = "answer",
    is_byok: object | None = None,
    upstream_inference_cost: object | None = None,
) -> dict[str, object]:
    payload = _success(model=model, content=content)
    payload["id"] = "gen-openrouter-123"
    payload["provider"] = provider
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage["cost"] = cost
    if is_byok is not None:
        usage["is_byok"] = is_byok
    if upstream_inference_cost is not None:
        usage["cost_details"] = {
            "upstream_inference_cost": upstream_inference_cost
        }
    return payload


def test_complete_reserves_before_call_then_commits_measured_usage_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered

    backend = _backend(tmp_path, monkeypatch)
    observed: dict[str, object] = {}

    def post(url, *, json, headers, timeout):
        observed.update(url=url, body=json, headers=headers, timeout=timeout)
        ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
        assert ledger[-1]["kind"] == "reserve"
        assert ledger[-1]["units"] == pytest.approx(0.32512)
        return FakeResponse(_success())

    monkeypatch.setattr(metered.httpx, "post", post)
    result = backend.complete("Use the supplied evidence.", max_tokens=512)

    assert result.response == "answer"
    assert result.input_tokens == 100
    assert result.output_tokens == 10
    assert result.cost_usd == pytest.approx(0.000325)
    assert result.model_id == MODEL
    assert observed == {
        "url": "https://api.openai.com/v1/chat/completions",
        "body": {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Use the supplied evidence."}],
            "temperature": 0,
            "n": 1,
            "max_tokens": 512,
        },
        "headers": {"Authorization": f"Bearer {KEY}"},
        "timeout": 60.0,
    }
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [entry["kind"] for entry in ledger] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]
    assert [entry["seq"] for entry in ledger] == sorted(entry["seq"] for entry in ledger)
    assert ledger[-1]["running_total"] == pytest.approx(result.cost_usd)
    result_row = _jsonl(tmp_path / "metered" / "results.jsonl")[0]
    assert result_row["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 20},
    }
    assert result_row["cost_breakdown"] == {
        "cached_input_cost_usd": pytest.approx(0.000025),
        "cached_input_tokens": 20,
        "cached_rate_applied": True,
        "input_cost_usd": pytest.approx(0.000225),
        "output_cost_usd": pytest.approx(0.0001),
        "total_cost_usd": pytest.approx(0.000325),
        "uncached_input_cost_usd": pytest.approx(0.0002),
        "uncached_input_tokens": 80,
    }
    assert result_row["finish_reason"] == "stop"
    assert result_row["actual_model"] == MODEL


def test_cap_refusal_happens_before_any_outbound_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from protocol.budget import BudgetExceeded

    backend = _backend(tmp_path, monkeypatch, cap=0.32)
    outbound = 0

    def post(*args, **kwargs):
        nonlocal outbound
        outbound += 1
        raise AssertionError("cap refusal must precede transport")

    monkeypatch.setattr(metered.httpx, "post", post)
    with pytest.raises(BudgetExceeded, match="cap"):
        backend.complete("question")
    assert outbound == 0
    assert (tmp_path / "metered" / "STOP").is_file()
    assert stat.S_IMODE((tmp_path / "metered" / "STOP").stat().st_mode) == 0o600
    assert not (tmp_path / "metered" / "requests.jsonl").exists()


@pytest.mark.parametrize(
    "payload",
    [
        _success(prompt_tokens=None),
        _success(completion_tokens="10"),
        _success(prompt_tokens=-1),
        _success(prompt_tokens=True),
    ],
)
def test_unknown_or_malformed_usage_retains_reservation_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(metered.httpx, "post", lambda *args, **kwargs: FakeResponse(payload))

    with pytest.raises(MeteredCallError, match="usage"):
        backend.complete("question")
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [entry["kind"] for entry in ledger] == ["approval", "reserve"]
    assert ledger[-1]["running_total"] == pytest.approx(0.32512)
    assert (tmp_path / "metered" / "STOP").read_text(encoding="utf-8")
    assert _jsonl(tmp_path / "metered" / "failures.jsonl")[0]["charged"] == "reservation"


def test_timeout_retains_reservation_records_failure_stops_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    calls = 0

    def timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        raise httpx.ReadTimeout("ambiguous after send", request=request)

    monkeypatch.setattr(metered.httpx, "post", timeout)
    with pytest.raises(MeteredCallError, match="transport"):
        backend.complete("question")
    assert calls == 1
    assert [entry["kind"] for entry in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == [
        "approval",
        "reserve",
    ]
    assert (tmp_path / "metered" / "STOP").is_file()


def test_keyboard_interrupt_retains_reservation_and_writes_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered

    backend = _backend(tmp_path, monkeypatch)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(metered.httpx, "post", interrupt)
    with pytest.raises(KeyboardInterrupt):
        backend.complete("question")
    assert [entry["kind"] for entry in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == [
        "approval",
        "reserve",
    ]
    assert (tmp_path / "metered" / "STOP").is_file()
    assert _jsonl(tmp_path / "metered" / "failures.jsonl")[0]["charged"] == "reservation"


def test_absent_cached_usage_uses_and_labels_conservative_uncached_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered

    backend = _backend(tmp_path, monkeypatch)
    payload = _success()
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage.pop("prompt_tokens_details")
    monkeypatch.setattr(metered.httpx, "post", lambda *args, **kwargs: FakeResponse(payload))

    result = backend.complete("question")

    assert result.cost_usd == pytest.approx(0.00035)
    breakdown = _jsonl(tmp_path / "metered" / "results.jsonl")[0]["cost_breakdown"]
    assert breakdown["cached_input_tokens"] == 0
    assert breakdown["cached_rate_applied"] is False
    assert breakdown["uncached_input_tokens"] == 100


def test_truncated_output_is_billed_recorded_and_not_returned_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(_success(finish_reason="length")),
    )
    with pytest.raises(MeteredCallError, match="truncated"):
        backend.complete("question", max_tokens=10)
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [entry["kind"] for entry in ledger] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]
    assert _jsonl(tmp_path / "metered" / "failures.jsonl")[0]["finish_reason"] == "length"


def test_run_phase_exposes_reader_metrics_and_persists_phase_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from membench.judge.handshake import RequestItem

    backend = _backend(tmp_path, monkeypatch)
    replies = iter([_success(content="first"), _success(content="second")])
    monkeypatch.setattr(
        metered.httpx, "post", lambda *args, **kwargs: FakeResponse(next(replies))
    )
    phase_dir = tmp_path / "metered" / "reader" / "phase"
    items = [
        RequestItem("q1", "system-A", {"task": "answer", "prompt": "one"}),
        RequestItem("q2", "system-A", {"task": "answer", "prompt": "two"}),
    ]

    outcome = backend.run_phase(phase_dir, "answer", items)

    assert outcome.status == "executed"
    assert [result.status for result in outcome.results] == ["ok", "ok"]
    assert [result.response for result in outcome.results] == ["first", "second"]
    assert [result.input_tokens for result in outcome.results] == [100, 100]
    assert [result.output_tokens for result in outcome.results] == [10, 10]
    assert [result.cost_usd for result in outcome.results] == pytest.approx(
        [0.000325, 0.000325]
    )
    assert outcome.requests_path == phase_dir / "requests.jsonl"
    assert outcome.responses_path == phase_dir / "results.jsonl"
    assert len(_jsonl(outcome.requests_path)) == 2
    assert len(_jsonl(outcome.responses_path)) == 2


def test_backend_runs_through_api_reader_precreated_phase_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.dataset import load_dataset
    from lme.reader import ApiReader

    root = tmp_path / "metered"
    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(_success(content="reader answer")),
    )
    question = load_dataset(Path("benchmarks/lme/fixtures/mini.json")).questions[0]
    reader = ApiReader(backend=backend, approval_token="approved", run_dir=root)

    assert reader.answer(question, ["supporting context"]) == "reader answer"
    assert reader.last_call_metrics.input_tokens == 100
    assert reader.last_call_metrics.output_tokens == 10
    assert reader.last_call_metrics.cost_usd == pytest.approx(0.000325)


def test_missing_credentials_refuses_without_reservation_or_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredConfigurationError, MeteredOpenAIBackend

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = MeteredOpenAIBackend(
        tmp_path / "metered", cap_usd=1, approval_token="approved"
    )
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("missing credential must prevent transport"),
    )
    with pytest.raises(MeteredConfigurationError, match="OPENAI_API_KEY"):
        backend.complete("question")
    assert [entry["kind"] for entry in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == [
        "approval"
    ]
    assert not (tmp_path / "metered" / "requests.jsonl").exists()


@pytest.mark.parametrize("cap", [0, -1, math.nan, math.inf, -math.inf])
def test_cap_must_be_finite_and_positive(tmp_path: Path, cap: float) -> None:
    from lme.metered import MeteredConfigurationError, MeteredOpenAIBackend

    with pytest.raises(MeteredConfigurationError, match="finite and positive"):
        MeteredOpenAIBackend(tmp_path / "metered", cap_usd=cap, approval_token="approved")
    assert not (tmp_path / "metered").exists()


def test_cap_is_immutable_and_approval_must_be_nonblank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.metered import MeteredConfigurationError, MeteredOpenAIBackend
    from protocol.budget import CapImmutableError

    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    root = tmp_path / "metered"
    MeteredOpenAIBackend(root, cap_usd=1, approval_token="approved")
    with pytest.raises(CapImmutableError, match="immutable"):
        MeteredOpenAIBackend(root, cap_usd=2, approval_token="approved")
    with pytest.raises(MeteredConfigurationError, match="approval"):
        MeteredOpenAIBackend(tmp_path / "other", cap_usd=1, approval_token="  ")
    approval = _jsonl(root / "ledger.jsonl")[0]
    assert approval["kind"] == "approval"
    assert approval["op"]


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4o-latest", ""])
def test_only_the_verified_snapshot_model_is_accepted(tmp_path: Path, model: str) -> None:
    from lme.metered import MeteredConfigurationError, MeteredOpenAIBackend

    with pytest.raises(MeteredConfigurationError, match="verified model"):
        MeteredOpenAIBackend(
            tmp_path / "metered", cap_usd=1, approval_token="approved", model=model
        )


@pytest.mark.parametrize("max_tokens", [0, -1, 513, True, 1.5, "10"])
def test_completion_rejects_output_settings_outside_the_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_tokens: object,
) -> None:
    from lme.metered import MeteredConfigurationError

    backend = _backend(tmp_path, monkeypatch)
    with pytest.raises(MeteredConfigurationError, match="max_tokens"):
        backend.complete("question", max_tokens=max_tokens)  # type: ignore[arg-type]
    assert not (tmp_path / "metered" / "requests.jsonl").exists()


def test_api_response_must_match_the_single_verified_model_and_choice_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(_success(model="gpt-4o")),
    )
    with pytest.raises(MeteredCallError, match="model"):
        backend.complete("question")
    assert [entry["kind"] for entry in _jsonl(tmp_path / "metered" / "ledger.jsonl")] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]


def test_secret_bearing_response_is_billed_but_never_returned_or_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            _success(content=f"answer {KEY}", model=KEY, finish_reason=KEY)
        ),
    )
    with pytest.raises(MeteredCallError, match="credential") as caught:
        backend.complete("question")
    assert KEY not in str(vars(caught.value))

    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [entry["kind"] for entry in ledger] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]

    root = tmp_path / "metered"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.rglob("*"):
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected, path
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8")
    result_row = _jsonl(root / "results.jsonl")[0]
    assert result_row["status"] == "error"
    assert result_row["response"] == "answer [REDACTED]"
    assert (root / "STOP").is_file()


def test_run_phase_does_not_expose_a_secret_bearing_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from membench.judge.handshake import RequestItem

    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(_success(content=f"answer {KEY}")),
    )

    outcome = backend.run_phase(
        tmp_path / "metered" / "reader" / "phase",
        "answer",
        [RequestItem("q1", "system-A", {"task": "answer", "prompt": "question"})],
    )

    assert len(outcome.results) == 1
    assert outcome.results[0].status == "error"
    assert outcome.results[0].response is None
    assert KEY not in repr(outcome.results[0])


def test_malformed_response_metadata_is_constrained_and_recursively_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _backend(tmp_path, monkeypatch)
    payload = _success(finish_reason={"malformed": KEY})
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage[KEY] = {KEY: KEY}
    monkeypatch.setattr(
        metered.httpx, "post", lambda *args, **kwargs: FakeResponse(payload)
    )

    with pytest.raises(MeteredCallError, match="finish_reason"):
        backend.complete("question")

    root = tmp_path / "metered"
    failure = _jsonl(root / "failures.jsonl")[0]
    assert failure["finish_reason"] is None
    for path in root.rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8")


def test_openrouter_uses_the_fixed_wire_route_and_account_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered

    backend = _openrouter_backend(tmp_path, monkeypatch)
    observed: dict[str, object] = {}

    def post(url, *, json, headers, timeout):
        observed.update(url=url, body=json, headers=headers, timeout=timeout)
        return FakeResponse(_openrouter_success(cost=0.0017))

    monkeypatch.setattr(metered.httpx, "post", post)
    result = backend.complete("question", max_tokens=10)

    assert result.model_id == MODEL
    assert result.cost_usd == pytest.approx(0.0017)
    assert observed == {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "body": {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": "question"}],
            "temperature": 0,
            "n": 1,
            "max_tokens": 10,
            "provider": {
                "only": ["openai"],
                "order": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {"prompt": 2.5, "completion": 10, "request": 0},
            },
            "transforms": [],
        },
        "headers": {"Authorization": f"Bearer {OPENROUTER_KEY}"},
        "timeout": 60.0,
    }
    config = json.loads((tmp_path / "metered" / "config.json").read_text())
    assert config["transport"] == "openrouter"
    assert config["endpoint"] == "https://openrouter.ai/api/v1/chat/completions"
    assert config["wire_model"] == OPENROUTER_MODEL
    assert config["provider"] == "OpenAI"
    result_row = _jsonl(tmp_path / "metered" / "results.jsonl")[0]
    assert result_row["cost_usd"] == pytest.approx(0.0017)
    assert result_row["token_derived_cost_usd"] == pytest.approx(0.000325)
    assert result_row["generation_id"] == "gen-openrouter-123"
    assert result_row["provider"] == "OpenAI"
    assert result_row["actual_model"] == OPENROUTER_MODEL
    assert _jsonl(tmp_path / "metered" / "ledger.jsonl")[-1][
        "running_total"
    ] == pytest.approx(0.0017)


def test_openrouter_defaults_to_its_credential_and_allows_an_explicit_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.metered import MeteredOpenAIBackend

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    default = MeteredOpenAIBackend(
        tmp_path / "default",
        cap_usd=1,
        approval_token="approved",
        transport="openrouter",
    )
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        default.complete("question")

    monkeypatch.setenv("PILOT_ROUTER_KEY", OPENROUTER_KEY)
    explicit = MeteredOpenAIBackend(
        tmp_path / "explicit",
        cap_usd=1,
        approval_token="approved",
        transport="openrouter",
        api_key_env="PILOT_ROUTER_KEY",
    )
    assert explicit.api_key_env == "PILOT_ROUTER_KEY"


@pytest.mark.parametrize("transport", ["compat", "", "OPENROUTER", None])
def test_transport_is_allowlisted(tmp_path: Path, transport: object) -> None:
    from lme.metered import MeteredConfigurationError, MeteredOpenAIBackend

    with pytest.raises(MeteredConfigurationError, match="transport"):
        MeteredOpenAIBackend(
            tmp_path / "metered",
            cap_usd=1,
            approval_token="approved",
            transport=transport,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "cost", [None, "0.1", -1, math.nan, math.inf, 0.4, 10**400]
)
def test_openrouter_invalid_or_overbound_account_cost_retains_reservation_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost: object
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _openrouter_backend(tmp_path, monkeypatch)
    payload = _openrouter_success(cost=cost)
    monkeypatch.setattr(
        metered.httpx, "post", lambda *args, **kwargs: FakeResponse(payload)
    )

    with pytest.raises(MeteredCallError, match="cost"):
        backend.complete("question", max_tokens=10)
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [row["kind"] for row in ledger] == ["approval", "reserve"]
    assert ledger[-1]["running_total"] == pytest.approx(0.3201)
    assert (tmp_path / "metered" / "STOP").is_file()


@pytest.mark.parametrize(
    ("model", "provider", "match"),
    [
        (MODEL, "OpenAI", "model"),
        (OPENROUTER_MODEL, "Other", "provider"),
        (OPENROUTER_MODEL, None, "provider"),
    ],
)
def test_openrouter_wrong_wire_identity_is_billed_then_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: object,
    provider: object,
    match: str,
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _openrouter_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            _openrouter_success(cost=0.0017, model=model, provider=provider)
        ),
    )

    with pytest.raises(MeteredCallError, match=match):
        backend.complete("question", max_tokens=10)
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [row["kind"] for row in ledger] == [
        "approval",
        "reserve",
        "commit",
        "release",
    ]
    assert ledger[-1]["running_total"] == pytest.approx(0.0017)
    assert (tmp_path / "metered" / "STOP").is_file()


def test_openrouter_credential_reflection_is_billed_and_never_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _openrouter_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            _openrouter_success(cost=0.0017, content=f"answer {OPENROUTER_KEY}")
        ),
    )

    with pytest.raises(MeteredCallError, match="credential"):
        backend.complete("question", max_tokens=10)
    assert _jsonl(tmp_path / "metered" / "ledger.jsonl")[-1][
        "running_total"
    ] == pytest.approx(0.0017)
    for path in (tmp_path / "metered").rglob("*"):
        if path.is_file():
            assert OPENROUTER_KEY not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("is_byok", "upstream_cost"),
    [(True, None), (True, 0.002)],
)
def test_openrouter_byok_or_external_upstream_cost_is_recorded_then_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_byok: bool,
    upstream_cost: float | None,
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _openrouter_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            _openrouter_success(
                cost=0.0017,
                is_byok=is_byok,
                upstream_inference_cost=upstream_cost,
            )
        ),
    )

    with pytest.raises(MeteredCallError, match="BYOK|upstream"):
        backend.complete("question", max_tokens=10)
    row = _jsonl(tmp_path / "metered" / "results.jsonl")[0]
    assert row["account_charge_cost_usd"] == pytest.approx(0.0017)
    assert row["is_byok"] is is_byok
    assert row["external_upstream_cost_usd"] == upstream_cost
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    if upstream_cost is None:
        assert row["total_liability_cost_usd"] is None
        assert [entry["kind"] for entry in ledger] == [
            "approval",
            "reserve",
            "commit",
        ]
        assert ledger[-1]["running_total"] == pytest.approx(0.3201)
    else:
        assert row["total_liability_cost_usd"] == pytest.approx(0.0037)
        assert [entry["kind"] for entry in ledger] == [
            "approval",
            "reserve",
            "commit",
            "commit",
            "release",
        ]
        assert ledger[-1]["running_total"] == pytest.approx(0.0037)
    assert (tmp_path / "metered" / "STOP").is_file()


def test_openrouter_non_byok_upstream_cost_is_informative_not_additive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered

    backend = _openrouter_backend(tmp_path, monkeypatch)
    payload = _openrouter_success(
        cost=0.069185,
        is_byok=False,
        upstream_inference_cost=0.069185,
    )
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage["prompt_tokens"] = 27_658
    usage["completion_tokens"] = 4
    usage["prompt_tokens_details"] = {"cached_tokens": 0}
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(payload),
    )

    result = backend.complete("question", max_tokens=10)

    assert result.response == "answer"
    assert result.cost_usd == pytest.approx(0.069185)
    row = _jsonl(tmp_path / "metered" / "results.jsonl")[0]
    assert row["account_charge_cost_usd"] == pytest.approx(0.069185)
    assert row["reported_upstream_inference_cost_usd"] == pytest.approx(0.069185)
    assert row["external_upstream_cost_usd"] == 0
    assert row["total_liability_cost_usd"] == pytest.approx(0.069185)
    assert _jsonl(tmp_path / "metered" / "ledger.jsonl")[-1][
        "running_total"
    ] == pytest.approx(0.069185)
    assert not (tmp_path / "metered" / "STOP").exists()


def test_openrouter_positive_upstream_with_missing_byok_is_ambiguous_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _openrouter_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            _openrouter_success(cost=0.0017, upstream_inference_cost=0.002)
        ),
    )

    with pytest.raises(MeteredCallError, match="ambiguous"):
        backend.complete("question", max_tokens=10)
    row = _jsonl(tmp_path / "metered" / "results.jsonl")[0]
    assert row["total_liability_cost_usd"] is None
    assert row["reported_upstream_inference_cost_usd"] == pytest.approx(0.002)
    assert row["external_upstream_cost_usd"] is None
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [entry["kind"] for entry in ledger] == ["approval", "reserve", "commit"]
    assert ledger[-1]["running_total"] == pytest.approx(0.3201)
    assert (tmp_path / "metered" / "STOP").is_file()


def test_openrouter_malformed_upstream_cost_retains_reservation_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _openrouter_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            _openrouter_success(cost=0.0017, upstream_inference_cost=10**400)
        ),
    )

    with pytest.raises(MeteredCallError, match="upstream"):
        backend.complete("question", max_tokens=10)
    ledger = _jsonl(tmp_path / "metered" / "ledger.jsonl")
    assert [entry["kind"] for entry in ledger] == ["approval", "reserve"]
    assert ledger[-1]["running_total"] == pytest.approx(0.3201)
    assert (tmp_path / "metered" / "STOP").is_file()


def test_openrouter_non_200_keeps_only_bounded_error_code_and_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme import metered
    from lme.metered import MeteredCallError

    backend = _openrouter_backend(tmp_path, monkeypatch)
    payload = {
        "error": {
            "code": "rate_limit_exceeded",
            "type": "insufficient_quota",
            "message": f"raw secret {OPENROUTER_KEY}",
            "metadata": {"debug": OPENROUTER_KEY},
        }
    }
    monkeypatch.setattr(
        metered.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(payload, status_code=429),
    )

    with pytest.raises(MeteredCallError, match="unknown usage"):
        backend.complete("question", max_tokens=10)
    failure = _jsonl(tmp_path / "metered" / "failures.jsonl")[0]
    assert failure["api_error"] == {
        "code": "rate_limit_exceeded",
        "type": "insufficient_quota",
    }
    serialized = json.dumps(failure)
    assert "message" not in serialized and "metadata" not in serialized
    assert OPENROUTER_KEY not in serialized
