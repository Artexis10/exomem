from __future__ import annotations

import pytest

from membench.judge.backends import BackendRequestResult, PhaseOutcome
from lme.dataset import load_dataset
from lme.reader import ApiReader, MeteredApprovalRequired, StubReader
from lme.runner import FullRunApprovalRequired, validate_full_run_gate


def test_stub_reader_answers_from_context_and_abstains_when_required() -> None:
    question = load_dataset("benchmarks/lme/fixtures/mini.json").questions[0]
    reader = StubReader()
    assert "Vorstead" in reader.answer(question, ["The workshop visits Vorstead."])
    assert reader.answer(question, []) == "I don't know."
    assert reader.answer(question, ["context"], force_abstain=True) == "I don't know."


def test_api_reader_refuses_without_explicit_metered_approval() -> None:
    with pytest.raises(MeteredApprovalRequired, match="Pilot And Spend Gates"):
        ApiReader(backend=object(), approval_token=None)


def test_api_reader_calls_backend_with_an_explicitly_empty_context(tmp_path) -> None:
    question = load_dataset("benchmarks/lme/fixtures/mini.json").questions[0]

    class RecordingBackend:
        def __init__(self) -> None:
            self.prompt = None

        def run_phase(self, run_dir, kind, items):
            self.prompt = items[0].payload["prompt"]
            return PhaseOutcome(
                kind=kind,
                backend="recording",
                status="executed",
                note="offline test",
                results=(
                    BackendRequestResult(
                        request_id=question.question_id,
                        sample_index=0,
                        status="ok",
                        response="parametric answer",
                    ),
                ),
            )

    backend = RecordingBackend()
    reader = ApiReader(backend=backend, approval_token="approved", run_dir=tmp_path)
    assert reader.answer(question, []) == "parametric answer"
    assert "[no retrieved context]" in backend.prompt


def test_full_run_refuses_without_post_pilot_evidence_and_approval(tmp_path) -> None:
    with pytest.raises(FullRunApprovalRequired, match="pilot evidence"):
        validate_full_run_gate(
            question_count=6,
            reader_name="openai",
            pilot_evidence=None,
            full_run_approval=None,
            is_pilot=False,
        )


def test_declared_pilot_does_not_require_the_full_run_gate() -> None:
    assert (
        validate_full_run_gate(
            question_count=6,
            reader_name="openai",
            pilot_evidence=None,
            full_run_approval=None,
            is_pilot=True,
        )
        is None
    )
