"""Budget-bounded OpenAI transport for the scored diagnostic replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard, cast

import httpx
from membench.judge.backends import PhaseOutcome
from membench.judge.handshake import RequestItem
from protocol.budget import BudgetExceeded, BudgetLedger

VERIFIED_MODEL = "gpt-4o-2024-08-06"
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "openai/gpt-4o-2024-08-06"
OPENAI_PROVIDER = "OpenAI"
CONTEXT_TOKENS = 128_000
INPUT_PER_MILLION_USD = 2.50
CACHED_INPUT_PER_MILLION_USD = 1.25
OUTPUT_PER_MILLION_USD = 10.00
TIMEOUT_SECONDS = 60.0
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_DIAGNOSTIC = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TOOL_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CHAT_FRAMING_BASE_TOKENS = 64
_CHAT_FRAMING_ITEM_TOKENS = 16


class MeteredConfigurationError(ValueError):
    """The caller requested a setting outside the approved replay contract."""


class MeteredCallError(RuntimeError):
    """One completion failed after its budget state was made durable."""

    def __init__(
        self,
        message: str,
        *,
        model_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        super().__init__(message)
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd


@dataclass(frozen=True)
class MeteredCompletion:
    response: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model_id: str


@dataclass(frozen=True)
class MeteredChatCompletion:
    message: dict
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model_id: str


@dataclass(frozen=True)
class _PreparedCall:
    api_key: str
    artifact_root: Path
    kind: str
    max_tokens: int
    operation_id: str
    operation_seq: int
    request_id: str | None
    reservation: float


@dataclass(frozen=True)
class _ProcessedCompletion:
    message: dict[str, object]
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class MeteredBackendResult:
    request_id: str
    sample_index: int
    status: str
    model_id: str | None = None
    response: str | None = None
    command: str | None = None
    detail: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class MeteredOpenAIBackend:
    """One fixed-model backend with durable, conservative pre-call reservations."""

    name = "metered-openai"

    def __init__(
        self,
        run_dir: Path,
        *,
        cap_usd: float,
        approval_token: str,
        api_key_env: str | None = None,
        model: str = VERIFIED_MODEL,
        transport: str = "openai",
    ) -> None:
        if transport not in {"openai", "openrouter"}:
            raise MeteredConfigurationError("transport must be 'openai' or 'openrouter'")
        if model != VERIFIED_MODEL:
            raise MeteredConfigurationError(
                f"verified model must be exactly {VERIFIED_MODEL!r}"
            )
        if isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float)):
            raise MeteredConfigurationError("cap_usd must be finite and positive")
        cap = float(cap_usd)
        if not math.isfinite(cap) or cap <= 0:
            raise MeteredConfigurationError("cap_usd must be finite and positive")
        if not isinstance(approval_token, str) or not approval_token.strip():
            raise MeteredConfigurationError("approval_token must be nonblank")
        if api_key_env is None:
            api_key_env = (
                "OPENROUTER_API_KEY" if transport == "openrouter" else "OPENAI_API_KEY"
            )
        if not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env):
            raise MeteredConfigurationError("api_key_env must be a valid environment name")

        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.run_dir.is_dir():
            raise MeteredConfigurationError("run_dir must identify a directory")
        os.chmod(self.run_dir, 0o700)
        self.model = model
        self.transport = transport
        self.endpoint = (
            OPENROUTER_ENDPOINT if transport == "openrouter" else OPENAI_ENDPOINT
        )
        self.wire_model = OPENROUTER_MODEL if transport == "openrouter" else model
        self.provider = OPENAI_PROVIDER
        self.api_key_env = api_key_env
        self.cap_usd = cap
        self._lock = threading.Lock()

        pricing_path = self.run_dir / "pricing.yaml"
        self._write_once(
            pricing_path,
            (
                "models:\n"
                f"  {VERIFIED_MODEL}:\n"
                f"    input_per_million_usd: {INPUT_PER_MILLION_USD:.2f}\n"
                f"    cached_input_per_million_usd: {CACHED_INPUT_PER_MILLION_USD:.2f}\n"
                f"    output_per_million_usd: {OUTPUT_PER_MILLION_USD:.2f}\n"
            ),
        )
        self.ledger = BudgetLedger(
            self.run_dir, caps={"usd": cap}, pricing_path=pricing_path
        )
        self._privatize(self.run_dir)

        config = {
            "api_key_env": api_key_env,
            "cached_input_per_million_usd": CACHED_INPUT_PER_MILLION_USD,
            "cap_usd": cap,
            "endpoint": self.endpoint,
            "input_per_million_usd": INPUT_PER_MILLION_USD,
            "model": model,
            "output_per_million_usd": OUTPUT_PER_MILLION_USD,
            "provider": self.provider,
            "pricing_source": "https://developers.openai.com/api/docs/models/gpt-4o",
            "pricing_verified_on": "2026-09-08",
            "reservation_input_tokens": CONTEXT_TOKENS,
            "transport": transport,
            "wire_model": self.wire_model,
        }
        self._write_once(
            self.run_dir / "config.json",
            json.dumps(config, ensure_ascii=False, sort_keys=True) + "\n",
        )
        approval_proof = hashlib.sha256(approval_token.strip().encode()).hexdigest()[:16]
        self._ledger_approve(f"approval-{approval_proof}")

    def complete(self, prompt: str, *, max_tokens: int = 512) -> MeteredCompletion:
        """Complete one prompt under the fixed request and accounting contract."""

        return self._complete(
            prompt,
            max_tokens=max_tokens,
            artifact_root=self.run_dir,
            request_id=None,
            kind="complete",
        )

    async def complete_messages(
        self,
        messages: list[dict],
        *,
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> MeteredChatCompletion:
        """Complete one bounded native-agent turn with standard function tools."""

        self._validate_max_tokens(max_tokens, maximum=4096)
        tool_definitions, tool_names = self._validate_tools(tools)
        message_history = self._validate_messages(messages, tool_names=tool_names)
        input_tokens = _serialized_chat_tokens(message_history, tool_definitions)
        if input_tokens + max_tokens > CONTEXT_TOKENS:
            raise MeteredConfigurationError(
                "serialized messages and tools exceed the verified model context window"
            )
        body = self._request_body(
            message_history,
            max_tokens=max_tokens,
            tools=tool_definitions or None,
        )
        prepared = self._prepare_call(
            body,
            max_tokens=max_tokens,
            artifact_root=self.run_dir,
            request_id=None,
            kind="native_chat",
        )

        try:
            client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
            try:
                reply = await client.post(
                    self.endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {prepared.api_key}"},
                )
            finally:
                await asyncio.shield(client.aclose())
        except asyncio.CancelledError:
            self._uncertain_failure(
                prepared.artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=prepared.kind,
                reason="transport cancelled with ambiguous spend; no retry attempted",
            )
            raise
        except KeyboardInterrupt:
            self._uncertain_failure(
                prepared.artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=prepared.kind,
                reason="transport interrupted with ambiguous spend; no retry attempted",
            )
            raise
        except (httpx.HTTPError, OSError):
            self._uncertain_failure(
                prepared.artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=prepared.kind,
                reason="transport failure with ambiguous spend; no retry attempted",
            )
            raise MeteredCallError("OpenAI transport failed; reservation retained") from None

        data = self._decode_reply(reply, prepared)
        processed = self._process_response(
            data,
            prepared,
            native=True,
            tool_names=tool_names,
        )
        return MeteredChatCompletion(
            message=processed.message,
            input_tokens=processed.input_tokens,
            output_tokens=processed.output_tokens,
            cost_usd=processed.cost_usd,
            model_id=self.model,
        )

    def run_phase(
        self,
        run_dir: Path,
        kind: str,
        items: list[RequestItem] | tuple[RequestItem, ...],
        *,
        samples: int = 1,
        seed: str = "membench",
    ) -> PhaseOutcome:
        """Adapt metered completions to the backend shape consumed by ``ApiReader``."""

        del seed
        if kind not in {"answer", "judge"}:
            raise MeteredConfigurationError("kind must be 'answer' or 'judge'")
        if samples != 1:
            raise MeteredConfigurationError("metered backend fixes n and samples at 1")
        phase_root = self._owned_root(run_dir)
        results: list[MeteredBackendResult] = []
        stopped = False
        for item in items:
            if stopped:
                results.append(
                    MeteredBackendResult(
                        request_id=item.item_id,
                        sample_index=0,
                        status="skip",
                        detail="not attempted after metered STOP",
                    )
                )
                continue
            prompt = item.payload.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                results.append(
                    MeteredBackendResult(
                        request_id=item.item_id,
                        sample_index=0,
                        status="error",
                        detail="request payload has no nonblank string 'prompt' field",
                    )
                )
                continue
            try:
                completion = self._complete(
                    prompt,
                    max_tokens=512,
                    artifact_root=phase_root,
                    request_id=item.item_id,
                    kind=kind,
                )
            except (BudgetExceeded, MeteredConfigurationError, MeteredCallError) as exc:
                results.append(
                    MeteredBackendResult(
                        request_id=item.item_id,
                        sample_index=0,
                        status="error",
                        model_id=getattr(exc, "model_id", None),
                        detail=str(exc),
                        input_tokens=getattr(exc, "input_tokens", None),
                        output_tokens=getattr(exc, "output_tokens", None),
                        cost_usd=getattr(exc, "cost_usd", None),
                    )
                )
                stopped = self.ledger.stop_path.exists()
                continue
            results.append(
                MeteredBackendResult(
                    request_id=item.item_id,
                    sample_index=0,
                    status="ok",
                    model_id=completion.model_id,
                    response=completion.response,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    cost_usd=completion.cost_usd,
                )
            )
        requests_path = phase_root / "requests.jsonl"
        responses_path = phase_root / "results.jsonl"
        result_tuple = tuple(results)
        ok_count = sum(result.status == "ok" for result in result_tuple)
        error_count = sum(result.status == "error" for result in result_tuple)
        skip_count = sum(result.status == "skip" for result in result_tuple)
        return PhaseOutcome(
            kind=kind,
            backend=self.name,
            status="executed" if result_tuple else "not_run",
            note=(
                f"{kind}: {ok_count} ok, {skip_count} skipped, "
                f"{error_count} error(s) of {len(result_tuple)} request(s)"
            ),
            results=result_tuple,  # type: ignore[arg-type]
            requests_path=requests_path if requests_path.is_file() else None,
            responses_path=responses_path if responses_path.is_file() else None,
        )

    def _complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        artifact_root: Path,
        request_id: str | None,
        kind: str,
    ) -> MeteredCompletion:
        if not isinstance(prompt, str) or not prompt:
            raise MeteredConfigurationError("prompt must be a nonblank string")
        self._validate_max_tokens(max_tokens, maximum=512)
        body = self._request_body(
            [{"role": "user", "content": prompt}], max_tokens=max_tokens
        )
        prepared = self._prepare_call(
            body,
            max_tokens=max_tokens,
            artifact_root=artifact_root,
            request_id=request_id,
            kind=kind,
        )

        try:
            reply = httpx.post(
                self.endpoint,
                json=body,
                headers={"Authorization": f"Bearer {prepared.api_key}"},
                timeout=TIMEOUT_SECONDS,
            )
        except KeyboardInterrupt:
            self._uncertain_failure(
                artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=kind,
                reason="transport interrupted with ambiguous spend; no retry attempted",
            )
            raise
        except (httpx.HTTPError, OSError):
            self._uncertain_failure(
                artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=kind,
                reason="transport failure with ambiguous spend; no retry attempted",
            )
            raise MeteredCallError("OpenAI transport failed; reservation retained") from None

        data = self._decode_reply(reply, prepared)
        processed = self._process_response(
            data,
            prepared,
            native=False,
            tool_names=frozenset(),
        )
        content = processed.message["content"]
        assert isinstance(content, str)
        return MeteredCompletion(
            response=content,
            input_tokens=processed.input_tokens,
            output_tokens=processed.output_tokens,
            cost_usd=processed.cost_usd,
            model_id=self.model,
        )

    @staticmethod
    def _validate_max_tokens(max_tokens: object, *, maximum: int) -> None:
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= maximum
        ):
            raise MeteredConfigurationError(
                f"max_tokens must be an integer from 1 to {maximum}"
            )

    def _request_body(
        self,
        messages: list[dict[str, object]],
        *,
        max_tokens: int,
        tools: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self.wire_model,
            "messages": messages,
            "temperature": 0,
            "n": 1,
            "max_tokens": max_tokens,
        }
        if tools is not None:
            body.update(
                {
                    "tools": tools,
                    "tool_choice": "auto",
                }
            )
            if self.transport == "openai":
                body["parallel_tool_calls"] = False
            # OpenRouter does not advertise this optional control for the
            # pinned endpoint; require_parameters would exclude our only
            # provider. The native broker executes each returned tool in order.
        if self.transport == "openrouter":
            body.update(
                {
                    "provider": {
                        "only": ["openai"],
                        "order": ["openai"],
                        "allow_fallbacks": False,
                        "require_parameters": True,
                        "max_price": {
                            "prompt": INPUT_PER_MILLION_USD,
                            "completion": OUTPUT_PER_MILLION_USD,
                            "request": 0,
                        },
                    },
                    "transforms": [],
                }
            )
        return body

    def _prepare_call(
        self,
        body: dict[str, object],
        *,
        max_tokens: int,
        artifact_root: Path,
        request_id: str | None,
        kind: str,
    ) -> _PreparedCall:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise MeteredConfigurationError(
                f"credential environment variable {self.api_key_env} is unset"
            )
        artifact_request_id = _redact(request_id, api_key) if request_id is not None else None

        operation_id = secrets.token_hex(16)
        reservation = (
            CONTEXT_TOKENS * INPUT_PER_MILLION_USD / 1_000_000
            + max_tokens * OUTPUT_PER_MILLION_USD / 1_000_000
        )
        with self._lock:
            operation_seq = self._next_seq()
            try:
                self.ledger.reserve(
                    ts=_now(),
                    seq=operation_seq,
                    actor=self.name,
                    op=operation_id,
                    units=reservation,
                    model_id=self.model,
                )
            finally:
                self._privatize(self.run_dir)
        request_record = {
            "kind": kind,
            "max_reserved_cost_usd": reservation,
            "operation_id": operation_id,
            "request": _redact_tree(body, api_key),
            "request_id": artifact_request_id,
            "seq": operation_seq,
            "ts": _now(),
        }
        self._append_jsonl(artifact_root / "requests.jsonl", request_record)
        return _PreparedCall(
            api_key=api_key,
            artifact_root=artifact_root,
            kind=kind,
            max_tokens=max_tokens,
            operation_id=operation_id,
            operation_seq=operation_seq,
            request_id=artifact_request_id,
            reservation=reservation,
        )

    def _decode_reply(self, reply: object, prepared: _PreparedCall) -> object:
        status_code = getattr(reply, "status_code", None)
        if status_code != 200:
            api_error = self._api_error(reply, prepared.api_key)
            self._uncertain_failure(
                prepared.artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=prepared.kind,
                reason=f"HTTP {status_code} with unknown usage; no retry attempted",
                api_error=api_error,
            )
            raise MeteredCallError("transport response had unknown usage; reservation retained")
        try:
            return reply.json()  # type: ignore[attr-defined]
        except (ValueError, TypeError):
            self._uncertain_failure(
                prepared.artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=prepared.kind,
                reason="response was not JSON and usage is unknown",
            )
            raise MeteredCallError(
                "OpenAI response usage was unavailable; reservation retained"
            ) from None

    def _process_response(
        self,
        data: object,
        prepared: _PreparedCall,
        *,
        native: bool,
        tool_names: frozenset[str],
    ) -> _ProcessedCompletion:
        try:
            usage, input_tokens, output_tokens, cached_tokens, cached_rate_applied = (
                self._usage(data, max_tokens=prepared.max_tokens)
            )
        except MeteredCallError as exc:
            self._uncertain_failure(
                prepared.artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=prepared.kind,
                reason=str(exc),
            )
            raise
        uncached_tokens = input_tokens - cached_tokens
        uncached_input_cost = uncached_tokens * INPUT_PER_MILLION_USD / 1_000_000
        cached_input_cost = cached_tokens * CACHED_INPUT_PER_MILLION_USD / 1_000_000
        input_cost = uncached_input_cost + cached_input_cost
        output_cost = output_tokens * OUTPUT_PER_MILLION_USD / 1_000_000
        token_derived_cost = input_cost + output_cost
        account_charge_cost = token_derived_cost
        is_byok: bool | None = None
        upstream_cost: float | None = None
        if self.transport == "openrouter":
            try:
                account_charge_cost, is_byok, upstream_cost = (
                    self._openrouter_accounting(usage, reservation=prepared.reservation)
                )
            except MeteredCallError as exc:
                self._uncertain_failure(
                    prepared.artifact_root,
                    operation_id=prepared.operation_id,
                    operation_seq=prepared.operation_seq,
                    request_id=prepared.request_id,
                    kind=prepared.kind,
                    reason=str(exc),
                )
                raise
        unknown_external_liability = (
            is_byok is True and upstream_cost is None
        ) or (
            is_byok is None and upstream_cost is not None and upstream_cost > 0
        )
        known_external_cost = (
            upstream_cost
            if is_byok is True and upstream_cost is not None
            else 0.0 if is_byok is False else None
        )
        total_liability_cost: float | None = account_charge_cost
        if unknown_external_liability:
            total_liability_cost = None
            self._commit_known(prepared.operation_id, actual=account_charge_cost)
        elif known_external_cost is not None and known_external_cost > 0:
            total_liability_cost = account_charge_cost + known_external_cost
            self._settle_split(
                prepared.operation_id,
                reservation=prepared.reservation,
                account_charge=account_charge_cost,
                upstream_charge=known_external_cost,
            )
        else:
            self._settle(
                prepared.operation_id,
                reservation=prepared.reservation,
                actual=account_charge_cost,
            )

        actual_model = data.get("model") if isinstance(data, dict) else None
        actual_provider = data.get("provider") if isinstance(data, dict) else None
        generation_id = data.get("id") if isinstance(data, dict) else None
        choice: object = None
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and len(choices) == 1:
            choice = choices[0]
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        failure: str | None = None
        accepted_message: dict[str, object] | None = None
        credential_reflected = any(
            isinstance(value, str) and prepared.api_key in value
            for value in (
                actual_model,
                actual_provider,
                generation_id,
                finish_reason,
                content,
            )
        ) or (
            native
            and (
                _tree_contains_secret(message, prepared.api_key)
                or _decoded_tool_arguments_contain_secret(message, prepared.api_key)
            )
        )
        if credential_reflected:
            failure = "completion contained the configured credential"
        elif actual_model != self.wire_model:
            failure = "response model differs from the verified model"
        elif self.transport == "openrouter" and actual_provider != self.provider:
            failure = "response provider differs from the required provider"
        elif native:
            if is_byok is True:
                failure = "OpenRouter returned BYOK billing for the diagnostic"
            elif unknown_external_liability:
                failure = "OpenRouter upstream inference cost has ambiguous BYOK semantics"
            else:
                try:
                    accepted_message = self._native_message(
                        message,
                        finish_reason=finish_reason,
                        tool_names=tool_names,
                    )
                except MeteredCallError as exc:
                    failure = str(exc)
        elif not isinstance(content, str):
            failure = "response must contain exactly one string completion"
        elif finish_reason == "length":
            failure = "completion was truncated at max_tokens"
        elif finish_reason != "stop":
            failure = "completion finish_reason is outside the approved response contract"
        elif is_byok is True:
            failure = "OpenRouter returned BYOK billing for the diagnostic"
        elif unknown_external_liability:
            failure = "OpenRouter upstream inference cost has ambiguous BYOK semantics"
        else:
            accepted_message = {"role": "assistant", "content": content}

        result_record = {
            "account_charge_cost_usd": account_charge_cost,
            "actual_model": actual_model,
            "cost_breakdown": {
                "cached_input_cost_usd": cached_input_cost,
                "cached_input_tokens": cached_tokens,
                "cached_rate_applied": cached_rate_applied,
                "input_cost_usd": input_cost,
                "output_cost_usd": output_cost,
                "total_cost_usd": token_derived_cost,
                "uncached_input_cost_usd": uncached_input_cost,
                "uncached_input_tokens": uncached_tokens,
            },
            "cost_usd": account_charge_cost,
            "external_upstream_cost_usd": known_external_cost,
            "finish_reason": finish_reason,
            "generation_id": generation_id,
            "is_byok": is_byok,
            "kind": prepared.kind,
            "operation_id": prepared.operation_id,
            "provider": actual_provider,
            "reported_upstream_inference_cost_usd": upstream_cost,
            "request_id": prepared.request_id,
            "response": (
                _redact(content, prepared.api_key) if isinstance(content, str) else None
            ),
            "seq": prepared.operation_seq,
            "status": "error" if failure else "ok",
            "token_derived_cost_usd": token_derived_cost,
            "total_liability_cost_usd": total_liability_cost,
            "ts": _now(),
            "usage": usage,
        }
        if native:
            result_record["message"] = _redact_native_message(
                message, prepared.api_key
            )
        redacted_record = _redact_tree(result_record, prepared.api_key)
        assert isinstance(redacted_record, dict)
        self._append_jsonl(
            prepared.artifact_root / "results.jsonl",
            redacted_record,
        )
        if failure:
            self._record_failure(
                prepared.artifact_root,
                operation_id=prepared.operation_id,
                operation_seq=prepared.operation_seq,
                request_id=prepared.request_id,
                kind=prepared.kind,
                reason=failure,
                charged="measured",
                finish_reason=(
                    _redact(finish_reason, prepared.api_key)
                    if isinstance(finish_reason, str)
                    else None
                ),
            )
            self._write_stop(failure)
            raise MeteredCallError(
                failure,
                model_id=(
                    _redact(actual_model, prepared.api_key)
                    if isinstance(actual_model, str)
                    else None
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=(
                    total_liability_cost
                    if total_liability_cost is not None
                    else prepared.reservation
                ),
            )
        assert accepted_message is not None
        return _ProcessedCompletion(
            message=accepted_message,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=account_charge_cost,
        )

    @staticmethod
    def _validate_tools(
        tools: object,
    ) -> tuple[list[dict[str, object]], frozenset[str]]:
        copied = _json_copy(tools, label="tools")
        if not isinstance(copied, list):
            raise MeteredConfigurationError("tools must be a list of function definitions")
        names: set[str] = set()
        for tool in copied:
            function = tool.get("function") if isinstance(tool, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if (
                not isinstance(tool, dict)
                or tool.get("type") != "function"
                or not isinstance(function, dict)
                or not isinstance(name, str)
                or not _FUNCTION_NAME.fullmatch(name)
            ):
                raise MeteredConfigurationError(
                    "tools must contain valid OpenAI function definitions"
                )
            if name in names:
                raise MeteredConfigurationError("tool function names must be unique")
            names.add(name)
        return copied, frozenset(names)

    @classmethod
    def _validate_messages(
        cls,
        messages: object,
        *,
        tool_names: frozenset[str],
    ) -> list[dict[str, object]]:
        copied = _json_copy(messages, label="messages")
        if not isinstance(copied, list) or not copied:
            raise MeteredConfigurationError("messages must be a nonempty list")
        for message in copied:
            if not isinstance(message, dict):
                raise MeteredConfigurationError("each message must be an object")
            role = message.get("role")
            content = message.get("content")
            if role in {"system", "user"}:
                if not isinstance(content, str):
                    raise MeteredConfigurationError(
                        f"{role} message content must be a string"
                    )
            elif role == "assistant":
                if content is not None and not isinstance(content, str):
                    raise MeteredConfigurationError(
                        "assistant message content must be a string or null"
                    )
                tool_calls = message.get("tool_calls")
                if tool_calls is not None:
                    try:
                        cls._tool_calls(tool_calls, tool_names=tool_names)
                    except MeteredCallError as exc:
                        raise MeteredConfigurationError(str(exc)) from None
                elif content is None:
                    raise MeteredConfigurationError(
                        "assistant messages require content or tool_calls"
                    )
            elif role == "tool":
                call_id = message.get("tool_call_id")
                if (
                    not isinstance(content, str)
                    or not isinstance(call_id, str)
                    or not _TOOL_CALL_ID.fullmatch(call_id)
                ):
                    raise MeteredConfigurationError(
                        "tool messages require string content and a valid tool_call_id"
                    )
            else:
                raise MeteredConfigurationError("message role is outside the chat contract")
        return copied

    @classmethod
    def _native_message(
        cls,
        message: object,
        *,
        finish_reason: object,
        tool_names: frozenset[str],
    ) -> dict[str, object]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise MeteredCallError("response must contain one assistant message")
        content = message.get("content")
        if finish_reason == "length":
            raise MeteredCallError("completion was truncated at max_tokens")
        if finish_reason == "stop":
            if not isinstance(content, str):
                raise MeteredCallError("final assistant content must be a string")
            if message.get("tool_calls") not in (None, []):
                raise MeteredCallError("final assistant response cannot contain tool_calls")
            return {"role": "assistant", "content": content}
        if finish_reason == "tool_calls":
            if content is not None and not isinstance(content, str):
                raise MeteredCallError("tool-call assistant content must be a string or null")
            calls = cls._tool_calls(message.get("tool_calls"), tool_names=tool_names)
            return {"role": "assistant", "content": content, "tool_calls": calls}
        raise MeteredCallError(
            "completion finish_reason is outside the native response contract"
        )

    @staticmethod
    def _tool_calls(
        tool_calls: object,
        *,
        tool_names: frozenset[str],
    ) -> list[dict[str, object]]:
        if not isinstance(tool_calls, list) or not tool_calls:
            raise MeteredCallError("response tool_calls must be a nonempty list")
        accepted: list[dict[str, object]] = []
        call_ids: set[str] = set()
        for tool_call in tool_calls:
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if (
                not isinstance(tool_call, dict)
                or tool_call.get("type") != "function"
                or not isinstance(call_id, str)
                or not _TOOL_CALL_ID.fullmatch(call_id)
            ):
                raise MeteredCallError("response tool_calls have a malformed id or type")
            if call_id in call_ids:
                raise MeteredCallError("response contains duplicate tool-call ids")
            call_ids.add(call_id)
            if (
                not isinstance(name, str)
                or not _FUNCTION_NAME.fullmatch(name)
                or name not in tool_names
            ):
                raise MeteredCallError("response tool-call name is not declared")
            if not isinstance(arguments, str):
                raise MeteredCallError("response tool-call arguments must be a JSON object")
            try:
                decoded_arguments = _decode_json_arguments(arguments)
            except (ValueError, RecursionError):
                raise MeteredCallError(
                    "response tool-call arguments must be a JSON object"
                ) from None
            if not isinstance(decoded_arguments, dict):
                raise MeteredCallError("response tool-call arguments must be a JSON object")
            accepted.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        return accepted

    def _usage(
        self, data: object, *, max_tokens: int
    ) -> tuple[dict[str, Any], int, int, int, bool]:
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            raise MeteredCallError("response usage is absent or malformed")
        usage = cast(dict[str, Any], usage)
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if not _token_count(input_tokens) or not _token_count(output_tokens):
            raise MeteredCallError("response usage is absent or malformed")
        if input_tokens + output_tokens > CONTEXT_TOKENS or output_tokens > max_tokens:
            raise MeteredCallError("response usage exceeds the reserved token bounds")
        details = usage.get("prompt_tokens_details")
        cached_tokens = 0
        cached_rate_applied = False
        if details is not None:
            if not isinstance(details, dict):
                raise MeteredCallError("response cached-token usage is malformed")
            cached = details.get("cached_tokens")
            if cached is not None and (not _token_count(cached) or cached > input_tokens):
                raise MeteredCallError("response cached-token usage is malformed")
            if cached is not None:
                cached_tokens = cached
                cached_rate_applied = True
        return usage, input_tokens, output_tokens, cached_tokens, cached_rate_applied

    def _openrouter_accounting(
        self, usage: dict[str, Any], *, reservation: float
    ) -> tuple[float, bool | None, float | None]:
        cost = usage.get("cost")
        if not _finite_nonnegative_number(cost) or cost > reservation:
            raise MeteredCallError(
                "OpenRouter usage cost is absent, malformed, or exceeds the reservation"
            )
        is_byok = usage.get("is_byok")
        if is_byok is not None and not isinstance(is_byok, bool):
            raise MeteredCallError("OpenRouter BYOK usage metadata is malformed")
        details = usage.get("cost_details")
        upstream_cost: float | None = None
        if details is not None:
            if not isinstance(details, dict):
                raise MeteredCallError("OpenRouter upstream cost metadata is malformed")
            upstream = details.get("upstream_inference_cost")
            if upstream is not None:
                if not _finite_nonnegative_number(upstream):
                    raise MeteredCallError("OpenRouter upstream cost metadata is malformed")
                upstream_cost = float(upstream)
        return float(cost), is_byok, upstream_cost

    @staticmethod
    def _api_error(reply: object, api_key: str) -> dict[str, str] | None:
        try:
            payload = reply.json()  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return None
        diagnostic: dict[str, str] = {}
        for field in ("code", "type"):
            value = error.get(field)
            if not isinstance(value, str) or api_key in value:
                continue
            if _SAFE_DIAGNOSTIC.fullmatch(value):
                diagnostic[field] = value
        return diagnostic or None

    def _settle(self, operation_id: str, *, reservation: float, actual: float) -> None:
        with self._lock:
            self.ledger.commit(
                ts=_now(),
                seq=self._next_seq(),
                actor=self.name,
                op=operation_id,
                units=actual,
            )
            self.ledger.release(
                ts=_now(),
                seq=self._next_seq(),
                actor=self.name,
                op=operation_id,
                units=reservation - actual,
            )
            self._privatize(self.run_dir)

    def _commit_known(self, operation_id: str, *, actual: float) -> None:
        with self._lock:
            self.ledger.commit(
                ts=_now(),
                seq=self._next_seq(),
                actor=self.name,
                op=operation_id,
                units=actual,
            )
            self._privatize(self.run_dir)

    def _settle_split(
        self,
        operation_id: str,
        *,
        reservation: float,
        account_charge: float,
        upstream_charge: float,
    ) -> None:
        with self._lock:
            self.ledger.commit(
                ts=_now(),
                seq=self._next_seq(),
                actor=self.name,
                op=operation_id,
                units=account_charge,
            )
            self.ledger.commit(
                ts=_now(),
                seq=self._next_seq(),
                actor=self.name,
                op=f"{operation_id}-external",
                units=upstream_charge,
            )
            total = account_charge + upstream_charge
            if total < reservation:
                self.ledger.release(
                    ts=_now(),
                    seq=self._next_seq(),
                    actor=self.name,
                    op=operation_id,
                    units=reservation - total,
                )
            self._privatize(self.run_dir)

    def _uncertain_failure(
        self,
        artifact_root: Path,
        *,
        operation_id: str,
        operation_seq: int,
        request_id: str | None,
        kind: str,
        reason: str,
        api_error: dict[str, str] | None = None,
    ) -> None:
        self._record_failure(
            artifact_root,
            operation_id=operation_id,
            operation_seq=operation_seq,
            request_id=request_id,
            kind=kind,
            reason=reason,
            charged="reservation",
            api_error=api_error,
        )
        self._write_stop(reason)

    def _record_failure(
        self,
        artifact_root: Path,
        *,
        operation_id: str,
        operation_seq: int,
        request_id: str | None,
        kind: str,
        reason: str,
        charged: str,
        finish_reason: object = None,
        api_error: dict[str, str] | None = None,
    ) -> None:
        record: dict[str, object] = {
            "charged": charged,
            "finish_reason": finish_reason,
            "kind": kind,
            "operation_id": operation_id,
            "reason": reason,
            "request_id": request_id,
            "seq": operation_seq,
            "ts": _now(),
        }
        if api_error is not None:
            record["api_error"] = api_error
        self._append_jsonl(artifact_root / "failures.jsonl", record)

    def _write_stop(self, reason: str) -> None:
        self._write_private(self.ledger.stop_path, reason + "\n", exclusive=False)

    def _ledger_approve(self, operation: str) -> None:
        with self._lock:
            self.ledger.approve(
                ts=_now(),
                seq=self._next_seq(),
                actor=self.name,
                op=operation,
            )
            self._privatize(self.run_dir)

    def _next_seq(self) -> int:
        if not self.ledger.ledger_path.is_file():
            return 0
        entries = self.ledger._entries()
        return max((entry.seq for entry in entries), default=-1) + 1

    def _owned_root(self, run_dir: Path) -> Path:
        target = Path(run_dir).resolve()
        if not target.is_relative_to(self.run_dir):
            raise MeteredConfigurationError("phase run_dir must stay under the metered root")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any((target / name).exists() for name in ("requests.jsonl", "results.jsonl")):
            raise FileExistsError(f"metered phase artifacts are immutable under {target}")
        os.chmod(target, 0o700)
        return target

    def _append_jsonl(self, path: Path, record: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            os.write(descriptor, payload.encode("utf-8"))
        finally:
            os.close(descriptor)

    def _write_once(self, path: Path, content: str) -> None:
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise MeteredConfigurationError(f"metered artifact is immutable: {path.name}")
            os.chmod(path, 0o600)
            return
        self._write_private(path, content, exclusive=True)

    @staticmethod
    def _write_private(path: Path, content: str, *, exclusive: bool) -> None:
        flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, content.encode("utf-8"))
        finally:
            os.close(descriptor)

    @staticmethod
    def _privatize(root: Path) -> None:
        os.chmod(root, 0o700)
        for path in root.rglob("*"):
            os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _token_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_nonnegative_number(value: object) -> TypeGuard[int | float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _json_copy(value: object, *, label: str) -> Any:
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError, RecursionError):
        raise MeteredConfigurationError(f"{label} must contain valid JSON values") from None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _decode_json_arguments(arguments: str) -> object:
    return json.loads(arguments, parse_constant=_reject_json_constant)


def _decoded_tool_arguments_contain_secret(message: object, secret: str) -> bool:
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(tool_calls, list):
        return False
    for tool_call in tool_calls:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if not isinstance(arguments, str):
            continue
        try:
            decoded = _decode_json_arguments(arguments)
        except (ValueError, RecursionError):
            continue
        if _tree_contains_secret(decoded, secret):
            return True
    return False


def _redact_native_message(message: object, secret: str) -> object:
    redacted = _redact_tree(message, secret)
    source_calls = message.get("tool_calls") if isinstance(message, dict) else None
    redacted_calls = redacted.get("tool_calls") if isinstance(redacted, dict) else None
    if not isinstance(source_calls, list) or not isinstance(redacted_calls, list):
        return redacted
    for source_call, redacted_call in zip(source_calls, redacted_calls, strict=True):
        source_function = (
            source_call.get("function") if isinstance(source_call, dict) else None
        )
        redacted_function = (
            redacted_call.get("function") if isinstance(redacted_call, dict) else None
        )
        arguments = (
            source_function.get("arguments")
            if isinstance(source_function, dict)
            else None
        )
        if not isinstance(arguments, str) or not isinstance(redacted_function, dict):
            continue
        try:
            decoded = _decode_json_arguments(arguments)
        except (ValueError, RecursionError):
            continue
        if _tree_contains_secret(decoded, secret):
            redacted_function["arguments"] = json.dumps(
                _redact_tree(decoded, secret),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
    return redacted


def _serialized_chat_tokens(
    messages: list[dict[str, object]], tools: list[dict[str, object]]
) -> int:
    import tiktoken

    serialized = json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    encoder = tiktoken.get_encoding("o200k_base")
    framing = _CHAT_FRAMING_BASE_TOKENS + _CHAT_FRAMING_ITEM_TOKENS * (
        len(messages) + len(tools)
    )
    return len(encoder.encode_ordinary(serialized)) + framing


def _tree_contains_secret(value: object, secret: str) -> bool:
    if not secret:
        return False
    if isinstance(value, str):
        return secret in value
    if isinstance(value, list):
        return any(_tree_contains_secret(item, secret) for item in value)
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and secret in key)
            or _tree_contains_secret(item, secret)
            for key, item in value.items()
        )
    return False


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value


def _redact_tree(value: object, secret: str) -> object:
    if isinstance(value, str):
        return _redact(value, secret)
    if isinstance(value, list):
        return [_redact_tree(item, secret) for item in value]
    if isinstance(value, dict):
        return {
            _redact(key, secret) if isinstance(key, str) else key: _redact_tree(item, secret)
            for key, item in value.items()
        }
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()
