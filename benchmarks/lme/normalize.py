"""Leakage-safe LongMemEval normalization for provider ingestion."""

from __future__ import annotations

from collections.abc import Iterable

from protocol.events import assert_no_evidence_marked_ids, neutralize_dataset
from protocol.models import ProtocolEvent

from .dataset import LmeDataset, LmeQuestion


def neutralize(dataset: LmeDataset | LmeQuestion) -> list[ProtocolEvent]:
    """Return canonical events; this adapter boundary does not accept CaseGold."""

    if isinstance(dataset, LmeQuestion):
        dataset = LmeDataset((dataset,))
    if not isinstance(dataset, LmeDataset):
        raise TypeError("neutralize accepts an LmeDataset or LmeQuestion, never gold-bearing records")
    events = neutralize_dataset(dataset)
    raw_ids = [session.session_id for question in dataset.questions for session in question.sessions]
    assert_no_evidence_marked_ids(events, raw_upstream_session_ids=raw_ids)
    return events


def refuse_if_evidence_marked(dataset: LmeDataset | LmeQuestion) -> None:
    """Fail closed if a normalizer would expose raw evidence-marked identities."""

    neutralize(dataset)


def render_neutral_session(events_for_session: Iterable[ProtocolEvent]) -> str:
    events = list(events_for_session)
    if not events:
        raise ValueError("cannot render an empty session")
    first = events[0]
    lines = [f"Session timestamp: {first.original_timestamp}", f"Session ordinal: {first.session_ordinal}", ""]
    lines.extend(f"{event.role}: {event.content}" for event in events)
    return "\n".join(lines)


def neutral_title(case_ordinal: int, session_ordinal: int) -> str:
    return f"LongMemEval case {case_ordinal} session {session_ordinal}"


def neutral_tags() -> list[str]:
    return ["longmemeval"]
