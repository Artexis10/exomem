"""Canonical event normalization and provider-visible identity checks."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from .models import DatasetIdentity, EventProvenance, ProtocolEvent


class LeakageError(ValueError):
    """Provider-bound event data contains an upstream identity or evidence marker."""


def normalize_text(content: str) -> str:
    return unicodedata.normalize("NFC", content)


def content_sha256(content: str) -> str:
    return hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest()


def _dataset_identity(dataset: Any) -> DatasetIdentity:
    question_count = len(dataset.questions)
    digest_input = "\n".join(question.question_id for question in dataset.questions)
    return DatasetIdentity(
        id="longmemeval",
        variant="dataset-provided",
        source="local-fixture-or-pinned-dataset",
        revision="unspecified",
        sha256=hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
        case_count=question_count,
    )


def neutralize_dataset(dataset: Any) -> list[ProtocolEvent]:
    """Convert LME-shaped data into neutral events without exposing raw IDs."""

    identity = _dataset_identity(dataset)
    events: list[ProtocolEvent] = []
    sequence = 0
    ingestion_ordinal = 0
    for row_index, question in enumerate(dataset.questions):
        for session_ordinal, session in enumerate(question.sessions, 1):
            upstream_hash = hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()
            for turn_ordinal, message in enumerate(session.messages, 1):
                content = normalize_text(message.content)
                events.append(
                    ProtocolEvent(
                        dataset=identity,
                        case_id=question.question_id,
                        session_ordinal=session_ordinal,
                        sequence=sequence,
                        role=message.role,
                        turn_ordinal=turn_ordinal,
                        content=content,
                        content_sha256=content_sha256(content),
                        original_timestamp=session.timestamp_text,
                        timestamp_semantics="event_time_declared_by_dataset",
                        ingestion_ordinal=ingestion_ordinal,
                        provenance=EventProvenance(
                            dataset_row_index=row_index,
                            upstream_session_id_sha256=upstream_hash,
                            converter="lme-neutralizer",
                            converter_version="1",
                        ),
                    )
                )
                sequence += 1
            ingestion_ordinal += 1
    return events


def assert_no_evidence_marked_ids(
    events: Iterable[ProtocolEvent], *, raw_upstream_session_ids: Iterable[str] = ()
) -> None:
    """Reject provider-visible event values containing evidence-marked/raw IDs."""

    raw_ids = tuple(raw_upstream_session_ids)
    for event in events:
        visible = (event.case_id, event.role, event.content, event.original_timestamp or "")
        for value in visible:
            if re.match(r"^answer", value, re.IGNORECASE):
                raise LeakageError("provider-visible field carries an evidence-marked identifier")
            if any(raw_id and raw_id in value for raw_id in raw_ids):
                raise LeakageError("provider-visible field contains a raw upstream session id")
