from __future__ import annotations

from pathlib import Path

from lme.bounds import run_bounds
from lme.dataset import load_dataset
from lme.reader import StubReader
from lme.report import render_report


FIXTURE = Path("benchmarks/lme/fixtures/mini.json")


def test_bounds_use_gold_sessions_and_null_context_through_the_same_reader() -> None:
    dataset = load_dataset(FIXTURE)
    bounds = run_bounds(dataset, StubReader())
    ceiling = {row.question_id: row.hypothesis for row in bounds.ceiling}
    floor = {row.question_id: row.hypothesis for row in bounds.floor}
    assert "Vorstead" in ceiling["mini-single-user"]
    assert set(floor.values()) == {"I don't know."}


def test_bounds_never_derive_force_abstain_from_question_identity() -> None:
    dataset = load_dataset(FIXTURE)

    class RecordingReader:
        name = "recording"

        def __init__(self) -> None:
            self.calls = []

        def answer(self, question, retrieved_text):
            self.calls.append((question.question_id, list(retrieved_text)))
            return "reader-produced"

    reader = RecordingReader()
    bounds = run_bounds(dataset, reader)
    assert not bounds.failures
    assert {row.hypothesis for row in bounds.ceiling} == {"reader-produced"}
    assert len(reader.calls) == len(dataset.questions) * 2


def test_report_blocks_an_ability_without_both_bounds() -> None:
    dataset = load_dataset(FIXTURE)
    report = render_report(
        dataset,
        labels={},
        ceiling_question_ids={q.question_id for q in dataset.questions},
        floor_question_ids=set(),
    )
    assert "blocked: missing null-abstain floor" in report
    assert "publishable" not in report.lower()
    assert "aggregate" not in report.lower()


def test_bound_reader_failures_remain_in_the_denominator() -> None:
    dataset = load_dataset(FIXTURE)

    class BrokenReader:
        name = "broken"

        def answer(self, question, retrieved_text):
            raise RuntimeError("reader unavailable")

    bounds = run_bounds(dataset, BrokenReader())
    assert len(bounds.ceiling) == len(dataset.questions)
    assert len(bounds.floor) == len(dataset.questions)
    assert len(bounds.failures) == len(dataset.questions) * 2
