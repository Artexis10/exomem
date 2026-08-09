"""Leakage-safe LongMemEval normalization for provider ingestion."""

from __future__ import annotations

from collections.abc import Iterable

from protocol.events import assert_no_evidence_marked_ids, neutralize_dataset
from protocol.models import CaseHandle, DatasetIdentity, ProtocolEvent

from .dataset import LmeDataset, LmeQuestion


def neutralize(dataset: LmeDataset | LmeQuestion, dataset_identity: DatasetIdentity) -> list[ProtocolEvent]:
    """Return canonical events under caller-pinned, real dataset identity."""

    if isinstance(dataset, LmeQuestion):
        dataset = LmeDataset((dataset,))
    if not isinstance(dataset, LmeDataset):
        raise TypeError("neutralize accepts LmeDataset/LmeQuestion with DatasetIdentity")
    events = neutralize_dataset(dataset, dataset_identity)
    assert_no_evidence_marked_ids(events)
    return events


def refuse_if_evidence_marked(dataset: LmeDataset | LmeQuestion, dataset_identity: DatasetIdentity) -> None:
    """Fail closed if neutral public identity fields contain evidence markers."""

    neutralize(dataset, dataset_identity)


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


def ingest_field_groups(events: Iterable[ProtocolEvent], handle: CaseHandle) -> tuple[list[str], dict[str, object]]:
    """Keep verbatim dataset messages separate from harness-authored fields."""

    grouped: dict[int, list[ProtocolEvent]] = {}
    for event in events:
        if event.case_id != handle.case_id:
            raise ValueError("event case does not match neutral handle")
        grouped.setdefault(event.session_ordinal, []).append(event)
    content = [event.content for ordinal in sorted(grouped) for event in grouped[ordinal]]
    harness = {
        "titles": [neutral_title(handle.case_ordinal, ordinal) for ordinal in sorted(grouped)],
        "tags": neutral_tags(),
        "prefixes": [
            f"Session timestamp: {grouped[ordinal][0].original_timestamp}\nSession ordinal: {ordinal}"
            for ordinal in sorted(grouped)
        ],
    }
    return content, harness
