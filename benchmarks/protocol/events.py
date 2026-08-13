"""Canonical event normalization and provider-visible identity checks."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from .models import DatasetIdentity, EventProvenance, ProtocolEvent


class LeakageError(ValueError):
    """Provider-bound identity data carries an upstream evidence marker."""


def normalize_text(content: str) -> str:
    return unicodedata.normalize("NFC", content)


def content_sha256(content: str) -> str:
    return hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest()


def neutralize_dataset(dataset: Any, dataset_identity: DatasetIdentity) -> list[ProtocolEvent]:
    """Convert LME-shaped data using caller-pinned, real dataset identity."""

    events: list[ProtocolEvent] = []
    sequence = 0
    ingestion_ordinal = 0
    for row_index, question in enumerate(dataset.questions):
        for session_ordinal, session in enumerate(question.sessions, 1):
            upstream_hash = hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()
            for turn_ordinal, message in enumerate(session.messages, 1):
                content = normalize_text(message.content)
                timestamp = session.timestamp_text
                events.append(
                    ProtocolEvent(
                        dataset=dataset_identity, case_id=question.question_id,
                        session_ordinal=session_ordinal, sequence=sequence, role=message.role,
                        turn_ordinal=turn_ordinal, content=content, content_sha256=content_sha256(content),
                        original_timestamp=timestamp,
                        timestamp_semantics=("event_time_declared_by_dataset" if timestamp else "ingestion_order_only"),
                        ingestion_ordinal=ingestion_ordinal,
                        provenance=EventProvenance(
                            dataset_row_index=row_index, upstream_session_id_sha256=upstream_hash,
                            converter="lme-neutralizer", converter_version="1",
                        ),
                    )
                )
                sequence += 1
            ingestion_ordinal += 1
    return events


def assert_no_evidence_marked_ids(
    events: Iterable[ProtocolEvent], *, raw_upstream_session_ids: Iterable[str] = ()
) -> None:
    """Inspect only provider identity/provenance fields, never message content."""

    raw_ids = {item for item in raw_upstream_session_ids if item}
    for event in events:
        identities = (event.case_id, event.provenance.converter, event.provenance.converter_version)
        for value in identities:
            if value in raw_ids or re.search(r"\banswer_[A-Za-z0-9]", value, re.IGNORECASE):
                raise LeakageError("provider identity carries an evidence-marked upstream identifier")
