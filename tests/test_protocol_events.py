from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lme.dataset import load_dataset


def test_content_hash_is_nfc_stable_and_neutralizer_is_idempotent() -> None:
    from protocol.events import content_sha256, neutralize_dataset

    assert content_sha256("Cafe\u0301") == content_sha256("Caf\u00e9")
    dataset = load_dataset(Path("benchmarks/lme/fixtures/leaky.json"))
    events = neutralize_dataset(dataset)
    assert events == neutralize_dataset(dataset)
    assert all(event.session_ordinal > 0 for event in events)
    assert all("answer_" not in event.content for event in events)


def test_upstream_id_is_hashed_and_gold_is_rejected_at_adapter_boundary(tmp_path: Path) -> None:
    from protocol.models import CaseGold
    from lme.adapter import LmeExomemAdapter
    from lme.normalize import neutralize

    question = load_dataset(Path("benchmarks/lme/fixtures/leaky.json")).questions[0]
    event = neutralize(question)[0]
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
    with pytest.raises(TypeError, match="LmeQuestion"):
        LmeExomemAdapter().ingest_question(gold)  # type: ignore[arg-type]
