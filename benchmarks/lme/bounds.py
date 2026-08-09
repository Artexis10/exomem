"""Gold-evidence ceiling and null-abstain floor for LongMemEval-S."""

from __future__ import annotations

from dataclasses import dataclass

from .dataset import LmeDataset, render_session
from .reader import ABSTENTION, Reader


@dataclass(frozen=True)
class Hypothesis:
    question_id: str
    hypothesis: str

    def as_dict(self) -> dict[str, str]:
        return {"question_id": self.question_id, "hypothesis": self.hypothesis}


@dataclass(frozen=True)
class BoundRun:
    ceiling: tuple[Hypothesis, ...]
    floor: tuple[Hypothesis, ...]
    failures: tuple[dict[str, str], ...] = ()


def run_bounds(dataset: LmeDataset, reader: Reader) -> BoundRun:
    """Run both retrieval bounds through the exact same Reader instance."""

    ceiling: list[Hypothesis] = []
    floor: list[Hypothesis] = []
    failures: list[dict[str, str]] = []
    for question in dataset.questions:
        gold_context = [render_session(session) for session in question.gold_sessions()]
        try:
            ceiling_hypothesis = reader.answer(question, gold_context)
        except Exception as exc:
            ceiling_hypothesis = ABSTENTION
            failures.append(
                {
                    "question_id": question.question_id,
                    "phase": "gold-evidence-ceiling-reader",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        ceiling.append(
            Hypothesis(
                question_id=question.question_id,
                hypothesis=ceiling_hypothesis,
            )
        )
        try:
            floor_hypothesis = reader.answer(question, [])
        except Exception as exc:
            floor_hypothesis = ABSTENTION
            failures.append(
                {
                    "question_id": question.question_id,
                    "phase": "null-abstain-floor-reader",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        floor.append(
            Hypothesis(
                question_id=question.question_id,
                hypothesis=floor_hypothesis,
            )
        )
    return BoundRun(
        ceiling=tuple(ceiling),
        floor=tuple(floor),
        failures=tuple(failures),
    )
