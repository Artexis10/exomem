from __future__ import annotations

from pathlib import Path

from lme.dataset import load_dataset
from lme.normalize import neutral_tags, neutral_title, neutralize, render_neutral_session


FIXTURE = Path("benchmarks/lme/fixtures/leaky.json")


def test_ingestion_payloads_carry_no_gold_labels() -> None:
    """The current ingestion payload must contain neither gold nor source labels."""

    dataset = load_dataset(FIXTURE)
    for question in dataset.questions:
        forbidden = {
            *[session.session_id for session in question.sessions],
            "answer_",
            question.question_type,
            question.question,
            question.answer,
        }
        events = neutralize(question)
        for session_ordinal, _session in enumerate(question.sessions, 1):
            session_events = [event for event in events if event.session_ordinal == session_ordinal]
            payload = "\n".join(
                (
                    render_neutral_session(session_events),
                    neutral_title(1, session_ordinal),
                    ",".join(neutral_tags()),
                )
            )
            assert all(token not in payload for token in forbidden)
