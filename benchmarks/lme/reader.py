"""Reader seam for offline stubs and explicitly approved metered backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from membench.judge.backends import ClaudeCliBackend, OpenAICompatBackend
from membench.judge.handshake import RequestItem

from .dataset import LmeQuestion


ABSTENTION = "I don't know."


class MeteredApprovalRequired(PermissionError):
    """A metered reader was requested without the founder approval gate."""


@dataclass(frozen=True)
class ReaderCallMetrics:
    """Per-question reader accounting exposed to the immutable outcome log."""

    call_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def _require_approval(token: str | None) -> str:
    if not isinstance(token, str) or not token.strip():
        raise MeteredApprovalRequired(
            "Pilot And Spend Gates Precede The Full Run: missing explicit "
            "metered-approval token before a potentially billable reader call"
        )
    return token.strip()


@runtime_checkable
class Reader(Protocol):
    name: str

    def answer(
        self,
        question: LmeQuestion,
        retrieved_text: list[str],
    ) -> str: ...


class StubReader:
    """Deterministic offline reader used by fixtures and every test."""

    name = "stub"

    def __init__(self) -> None:
        self.last_call_metrics = ReaderCallMetrics(call_count=0)

    def answer(
        self,
        question: LmeQuestion,
        retrieved_text: list[str],
        *,
        force_abstain: bool = False,
    ) -> str:
        del question
        self.last_call_metrics = ReaderCallMetrics(call_count=1)
        context = next((text.strip() for text in retrieved_text if text.strip()), "")
        if force_abstain or not context:
            return ABSTENTION
        return context[:4000]


class ApiReader:
    """Metered reader over membench's OpenAI-compatible or Claude CLI backend."""

    name = "api"

    def __init__(
        self,
        *,
        backend: OpenAICompatBackend | ClaudeCliBackend | object,
        approval_token: str | None,
        run_dir: Path | None = None,
    ) -> None:
        self.approval_token = _require_approval(approval_token)
        if not callable(getattr(backend, "run_phase", None)):
            raise TypeError("ApiReader backend must implement run_phase")
        self.backend = backend
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self._call_index = 0
        self.last_call_metrics = ReaderCallMetrics(call_count=0)

    @staticmethod
    def _prompt(question: LmeQuestion, retrieved_text: list[str]) -> str:
        context = "\n\n--- retrieved session ---\n\n".join(retrieved_text)
        if not context.strip():
            context = "[no retrieved context]"
        return (
            "Answer the LongMemEval question using only the retrieved session context. "
            "If the context does not support an answer, say exactly: I don't know.\n\n"
            f"Question date: {question.question_date_text}\n"
            f"Question: {question.question}\n\n"
            f"Retrieved context:\n{context}"
        )

    def answer(
        self,
        question: LmeQuestion,
        retrieved_text: list[str],
    ) -> str:
        _require_approval(self.approval_token)
        if self.run_dir is None:
            raise RuntimeError("ApiReader requires a run_dir before invocation")
        self._call_index += 1
        phase_dir = self.run_dir / "reader" / f"{self._call_index:04d}-{question.question_id}"
        phase_dir.mkdir(parents=True, exist_ok=False)
        item = RequestItem(
            item_id=question.question_id,
            blinded_provider_token="exomem",
            payload={"task": "answer", "prompt": self._prompt(question, retrieved_text)},
        )
        outcome = self.backend.run_phase(phase_dir, "answer", [item])
        self.last_call_metrics = ReaderCallMetrics(
            call_count=1,
            input_tokens=_metric_total(outcome, "input_tokens"),
            output_tokens=_metric_total(outcome, "output_tokens"),
            cost_usd=_metric_total(outcome, "cost_usd", numeric=float),
        )
        ok = [result for result in outcome.results if result.status == "ok"]
        if not ok or not ok[0].response:
            details = "; ".join(result.detail or result.status for result in outcome.results)
            raise RuntimeError(f"reader backend returned no answer: {details or outcome.note}")
        return ok[0].response


def _metric_total(outcome: object, field: str, *, numeric: type = int):
    """Sum optional usage metadata without coupling LME to one backend shape."""

    direct = getattr(outcome, field, None)
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        return numeric(direct)
    values = []
    for result in getattr(outcome, "results", ()) or ():
        value = getattr(result, field, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(value)
    return numeric(sum(values)) if values else None
