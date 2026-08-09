"""Judge/answer execution backends. Default: none — the judge never runs
unless explicitly configured.

Executing backends NEVER fabricate a response: a missing binary or unset
credential env var yields a ``skip`` result carrying the verbatim command
the user can run desk-side, and malformed model output is a recorded error
that stays in the denominators — never guessed, never repaired.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, ValidationError

from membench.judge.blinding import SourceNumbering, normalize_for_judge
from membench.judge.handshake import (
    BATCH_NAME,
    HandshakeRequest,
    HandshakeResponse,
    RequestItem,
    load_requests,
    responses_dir,
    write_requests,
)
from membench.schema import StrictModel

DEFAULT_BACKEND_NAME = "none"

JUDGE_PROMPT_TEMPLATE = (
    "You are grading one candidate answer from an anonymized memory system.\n"
    "Sources appear as neutral [ctx:N] tokens and system identities are\n"
    "blinded; grade only what is written.\n"
    "\n"
    "Question:\n"
    "{question}\n"
    "\n"
    "Expected answer (summary):\n"
    "{expected_summary}\n"
    "\n"
    "Candidate answer:\n"
    "{candidate_answer}\n"
    "\n"
    "Reply with STRICT JSON only — one object, no prose, no code fences:\n"
    '{{"semantic_match": true|false, "explanation_quality": 1-5, "reason": "short reason"}}\n'
)


def build_judge_prompt(question: str, expected_summary: str, candidate_answer: str) -> str:
    return JUDGE_PROMPT_TEMPLATE.format(
        question=question,
        expected_summary=expected_summary,
        candidate_answer=candidate_answer,
    )


def make_judge_item(
    query_id: str,
    *,
    question: str,
    expected_summary: str,
    candidate_answer: str,
    provider_token: str,
) -> RequestItem:
    """Blind one judge item; one shared numbering per request keeps a given
    source at the same ``[ctx:N]`` across question/expected/candidate."""

    numbering = SourceNumbering()
    blinded_question = normalize_for_judge(question, numbering)
    blinded_expected = normalize_for_judge(expected_summary, numbering)
    blinded_candidate = normalize_for_judge(candidate_answer, numbering)
    payload: dict[str, Any] = {
        "task": "judge",
        "question": blinded_question,
        "expected_summary": blinded_expected,
        "candidate_answer": blinded_candidate,
        "prompt": build_judge_prompt(blinded_question, blinded_expected, blinded_candidate),
    }
    return RequestItem(
        item_id=query_id, blinded_provider_token=provider_token, payload=payload
    )


class JudgeVerdict(StrictModel):
    """The judge's STRICT-JSON reply schema."""

    semantic_match: bool
    explanation_quality: int = Field(ge=1, le=5)
    reason: str


def parse_judge_verdict(text: str) -> JudgeVerdict:
    """Parse strict judge JSON; anything else raises ``ValueError``.

    A single surrounding code fence is stripped (models add it despite
    instructions; removing wrapping is not guessing content). No other
    repair is attempted — malformed output must surface as an error.
    """

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
        else:
            raise ValueError("malformed judge verdict: unterminated code fence")
    try:
        return JudgeVerdict.model_validate_json(stripped)
    except ValidationError as exc:
        raise ValueError(
            f"malformed judge verdict: {exc.error_count()} validation error(s)"
        ) from exc


@dataclass(frozen=True)
class BackendRequestResult:
    """Per-(request, sample) execution outcome; ``skip`` carries the exact
    user-run command and never a fabricated response."""

    request_id: str
    sample_index: int
    status: str  # "ok" | "skip" | "error"
    model_id: str | None = None
    response: str | None = None
    command: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class PhaseOutcome:
    kind: str  # "answer" | "judge"
    backend: str
    status: str  # "not_run" | "prepared" | "executed" | "skipped"
    note: str
    results: tuple[BackendRequestResult, ...] = ()
    requests_path: Path | None = None
    responses_path: Path | None = None


@runtime_checkable
class JudgeBackend(Protocol):
    """Structural contract shared by every backend (judge or answer kind)."""

    name: str

    def run_phase(
        self,
        run_dir: Path,
        kind: str,
        items: list[RequestItem] | tuple[RequestItem, ...],
        *,
        samples: int = 1,
        seed: str = "membench",
    ) -> PhaseOutcome: ...


#: Same file-handshake contract; the alias documents which phase is meant.
AnswerBackend = JudgeBackend


class NoneBackend:
    """Default backend: the phase does not run and says so explicitly."""

    name = "none"

    def run_phase(
        self,
        run_dir: Path,
        kind: str,
        items: list[RequestItem] | tuple[RequestItem, ...],
        *,
        samples: int = 1,
        seed: str = "membench",
    ) -> PhaseOutcome:
        return PhaseOutcome(
            kind=kind,
            backend=self.name,
            status="not_run",
            note=f"{kind}: not run (backend 'none' is the default; enable one explicitly)",
        )


def default_backend() -> NoneBackend:
    """The judge is optional, desk-side, and DEFAULT OFF."""

    return NoneBackend()


class SubagentBackend:
    """Prepare blinded request files and exit — nothing is executed here.

    An orchestrating session (which holds its own model access) fans the
    requests out externally and writes ``<kind>-responses/`` per HANDOFF.md.
    """

    name = "subagent"

    def run_phase(
        self,
        run_dir: Path,
        kind: str,
        items: list[RequestItem] | tuple[RequestItem, ...],
        *,
        samples: int = 1,
        seed: str = "membench",
    ) -> PhaseOutcome:
        batch = write_requests(run_dir, kind, items, samples=samples, seed=seed)
        return PhaseOutcome(
            kind=kind,
            backend=self.name,
            status="prepared",
            note=(
                f"{len(items)} item(s) × {samples} sample(s) prepared at {batch}; "
                f"an external executor follows {batch.parent / 'HANDOFF.md'}"
            ),
            requests_path=batch,
        )


def _prompt_of(request: HandshakeRequest) -> str | None:
    prompt = request.payload.get("prompt")
    return prompt if isinstance(prompt, str) and prompt else None


def _write_responses(
    run_dir: Path, kind: str, results: tuple[BackendRequestResult, ...]
) -> Path | None:
    ok = [result for result in results if result.status == "ok"]
    if not ok:
        return None
    directory = responses_dir(run_dir, kind)
    directory.mkdir(parents=True, exist_ok=True)
    batch = directory / BATCH_NAME
    if batch.exists():
        raise FileExistsError(f"{batch} already exists; response batches are immutable")
    with batch.open("w", encoding="utf-8", newline="\n") as handle:
        for result in ok:
            response = HandshakeResponse(
                request_id=result.request_id,
                sample_index=result.sample_index,
                model_id=result.model_id or "unknown",
                response=result.response or "",
            )
            handle.write(
                json.dumps(response.model_dump(), ensure_ascii=False, sort_keys=True) + "\n"
            )
    return batch


def _outcome_status(results: tuple[BackendRequestResult, ...]) -> str:
    if results and all(result.status == "skip" for result in results):
        return "skipped"
    return "executed"


def _summary_note(kind: str, results: tuple[BackendRequestResult, ...]) -> str:
    counts = {"ok": 0, "skip": 0, "error": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return (
        f"{kind}: {counts['ok']} ok, {counts['skip']} skipped, "
        f"{counts['error']} error(s) of {len(results)} request(s); skipped requests "
        "carry the exact user-run command and are never answered on their behalf"
    )


@dataclass
class ClaudeCliBackend:
    """Execute requests through the Claude CLI when the binary exists.

    Builds the exact ``claude -p <prompt> --output-format json`` argv. If
    the binary is absent, every request returns a ``skip`` result carrying
    the verbatim command for the user to run desk-side — a missing binary
    is never converted into a fake response.
    """

    binary: str = "claude"
    model: str | None = None
    timeout_s: float = 120.0
    name: str = field(default="claude-cli", init=False)

    def argv_for(self, prompt: str) -> list[str]:
        argv = [self.binary, "-p", prompt, "--output-format", "json"]
        if self.model:
            argv.extend(["--model", self.model])
        return argv

    def run_phase(
        self,
        run_dir: Path,
        kind: str,
        items: list[RequestItem] | tuple[RequestItem, ...],
        *,
        samples: int = 1,
        seed: str = "membench",
    ) -> PhaseOutcome:
        batch = write_requests(run_dir, kind, items, samples=samples, seed=seed)
        requests = load_requests(run_dir, kind)
        resolved = shutil.which(self.binary)
        results: list[BackendRequestResult] = []
        for request in requests:
            prompt = _prompt_of(request)
            if prompt is None:
                results.append(
                    BackendRequestResult(
                        request_id=request.request_id,
                        sample_index=request.sample_index,
                        status="error",
                        detail="request payload has no 'prompt' field",
                    )
                )
                continue
            argv = self.argv_for(prompt)
            command = shlex.join(argv)
            if resolved is None:
                results.append(
                    BackendRequestResult(
                        request_id=request.request_id,
                        sample_index=request.sample_index,
                        status="skip",
                        command=command,
                        detail=(
                            f"binary {self.binary!r} not found on PATH; run the command "
                            f"yourself and append its JSON 'result' text to "
                            f"{kind}-responses/{BATCH_NAME} per HANDOFF.md"
                        ),
                    )
                )
                continue
            results.append(self._execute(request, argv, command))
        results_tuple = tuple(results)
        responses_path = _write_responses(run_dir, kind, results_tuple)
        return PhaseOutcome(
            kind=kind,
            backend=self.name,
            status=_outcome_status(results_tuple),
            note=_summary_note(kind, results_tuple),
            results=results_tuple,
            requests_path=batch,
            responses_path=responses_path,
        )

    def _execute(
        self, request: HandshakeRequest, argv: list[str], command: str
    ) -> BackendRequestResult:
        base = {
            "request_id": request.request_id,
            "sample_index": request.sample_index,
            "command": command,
        }
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout_s, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendRequestResult(
                status="error", detail=f"{type(exc).__name__}: {exc}", **base
            )
        if proc.returncode != 0:
            return BackendRequestResult(
                status="error",
                detail=f"exit {proc.returncode}: {proc.stderr.strip()[:300]}",
                **base,
            )
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return BackendRequestResult(
                status="error", detail=f"non-JSON CLI output: {exc}", **base
            )
        result_text = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(result_text, str):
            return BackendRequestResult(
                status="error",
                detail="CLI JSON envelope has no string 'result' field",
                **base,
            )
        model_id = envelope.get("model") if isinstance(envelope, dict) else None
        return BackendRequestResult(
            status="ok",
            model_id=model_id if isinstance(model_id, str) else (self.model or "claude"),
            response=result_text,
            **base,
        )


@dataclass
class OpenAICompatBackend:
    """POST ``{base_url}/chat/completions`` (temperature 0) via httpx.

    The API key is read from the env var NAMED in the config — the runner
    never stores the credential itself. When that env var is unset, every
    request returns a ``skip`` result carrying a verbatim curl command the
    user can run desk-side; a missing key never yields a fake response.
    """

    base_url: str
    model: str
    api_key_env: str
    timeout_s: float = 60.0
    name: str = field(default="openai-compat", init=False)

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _body(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }

    def command_for(self, prompt: str) -> str:
        body = json.dumps(self._body(prompt), ensure_ascii=False, sort_keys=True)
        return (
            f"curl -sS -X POST {shlex.quote(self._url())} "
            f"-H {shlex.quote('Content-Type: application/json')} "
            f'-H "Authorization: Bearer ${self.api_key_env}" '
            f"-d {shlex.quote(body)}"
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
        batch = write_requests(run_dir, kind, items, samples=samples, seed=seed)
        requests = load_requests(run_dir, kind)
        api_key = os.environ.get(self.api_key_env)
        results: list[BackendRequestResult] = []
        for request in requests:
            prompt = _prompt_of(request)
            if prompt is None:
                results.append(
                    BackendRequestResult(
                        request_id=request.request_id,
                        sample_index=request.sample_index,
                        status="error",
                        detail="request payload has no 'prompt' field",
                    )
                )
                continue
            command = self.command_for(prompt)
            if not api_key:
                results.append(
                    BackendRequestResult(
                        request_id=request.request_id,
                        sample_index=request.sample_index,
                        status="skip",
                        command=command,
                        detail=(
                            f"environment variable {self.api_key_env} is unset; run the "
                            f"command yourself and append choices[0].message.content to "
                            f"{kind}-responses/{BATCH_NAME} per HANDOFF.md"
                        ),
                    )
                )
                continue
            results.append(self._execute(request, prompt, api_key, command))
        results_tuple = tuple(results)
        responses_path = _write_responses(run_dir, kind, results_tuple)
        return PhaseOutcome(
            kind=kind,
            backend=self.name,
            status=_outcome_status(results_tuple),
            note=_summary_note(kind, results_tuple),
            results=results_tuple,
            requests_path=batch,
            responses_path=responses_path,
        )

    def _execute(
        self, request: HandshakeRequest, prompt: str, api_key: str, command: str
    ) -> BackendRequestResult:
        import httpx  # lazy: offline flows must not require the dependency

        base = {
            "request_id": request.request_id,
            "sample_index": request.sample_index,
            "command": command,
        }
        try:
            reply = httpx.post(
                self._url(),
                json=self._body(prompt),
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:
            return BackendRequestResult(
                status="error", detail=f"{type(exc).__name__}: {exc}", **base
            )
        if reply.status_code != 200:
            return BackendRequestResult(
                status="error",
                detail=f"HTTP {reply.status_code}: {reply.text[:300]}",
                **base,
            )
        try:
            data = reply.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return BackendRequestResult(
                status="error",
                detail=f"unexpected completion shape: {type(exc).__name__}: {exc}",
                **base,
            )
        if not isinstance(content, str):
            return BackendRequestResult(
                status="error", detail="completion content is not a string", **base
            )
        model_id = data.get("model") if isinstance(data, dict) else None
        return BackendRequestResult(
            status="ok",
            model_id=model_id if isinstance(model_id, str) else self.model,
            response=content,
            **base,
        )
