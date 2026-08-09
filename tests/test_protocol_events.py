from __future__ import annotations

import hashlib
from types import SimpleNamespace
from pathlib import Path

import pytest

from lme.dataset import load_dataset


def test_content_hash_is_nfc_stable_and_neutralizer_is_idempotent() -> None:
    from protocol.events import content_sha256, neutralize_dataset
    from protocol.models import DatasetIdentity

    assert content_sha256("Cafe\u0301") == content_sha256("Caf\u00e9")
    dataset = load_dataset(Path("benchmarks/lme/fixtures/leaky.json"))
    identity = DatasetIdentity(id="fixture", variant="leaky", source="local", revision="pin", sha256=hashlib.sha256(Path("benchmarks/lme/fixtures/leaky.json").read_bytes()).hexdigest(), case_count=2)
    events = neutralize_dataset(dataset, identity)
    assert events == neutralize_dataset(dataset, identity)
    assert all(event.session_ordinal > 0 for event in events)
    assert all(event.dataset.sha256 == identity.sha256 for event in events)


def test_upstream_id_is_hashed_and_gold_is_rejected_at_adapter_boundary(tmp_path: Path) -> None:
    from protocol.models import CaseGold
    from lme.adapter import LmeExomemAdapter
    from lme.normalize import neutralize

    question = load_dataset(Path("benchmarks/lme/fixtures/leaky.json")).questions[0]
    from protocol.models import DatasetIdentity
    identity = DatasetIdentity(id="fixture", variant="leaky", source="local", revision="pin", sha256=hashlib.sha256(Path("benchmarks/lme/fixtures/leaky.json").read_bytes()).hexdigest(), case_count=2)
    event = neutralize(question, identity)[0]
    assert event.provenance.upstream_session_id_sha256 == hashlib.sha256(
        question.sessions[0].session_id.encode("utf-8")
    ).hexdigest()
    gold = CaseGold(
        case_id=question.question_id,
        answer=question.answer,
        answer_session_ids=list(question.answer_session_ids),
        question_type=question.question_type,
        question=question.question,
    )
    from protocol.models import CaseHandle
    with pytest.raises(TypeError):
        LmeExomemAdapter().ingest_case(gold, CaseHandle(case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text))  # type: ignore[arg-type]


def test_neutralize_dataset_can_record_ingestion_order_only_for_timestampless_input() -> None:
    """The official LME parser requires timestamps; this minimal protocol input reaches its no-time branch."""

    from protocol.events import neutralize_dataset
    from protocol.models import DatasetIdentity

    dataset = SimpleNamespace(questions=[SimpleNamespace(
        question_id="timestampless-case",
        sessions=[SimpleNamespace(
            session_id="plain-session",
            timestamp_text=None,
            messages=[SimpleNamespace(role="user", content="timestamp is unavailable")],
        )],
    )])
    identity = DatasetIdentity(id="synthetic", variant="timestampless", source="test", revision="1", sha256="a" * 64, case_count=1)
    events = neutralize_dataset(dataset, identity)
    assert events[0].timestamp_semantics == "ingestion_order_only"
    assert events[0].original_timestamp is None
