from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lme.dataset import load_dataset
from lme.normalize import neutral_tags, neutral_title, neutralize, refuse_if_evidence_marked, render_neutral_session
from protocol.models import CaseHandle, DatasetIdentity


FIXTURE = Path("benchmarks/lme/fixtures/leaky.json")


def _identity() -> DatasetIdentity:
    return DatasetIdentity(
        id="longmemeval", variant="fixture", source="local", revision="fixture-pin",
        sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), case_count=2,
    )


def test_ingestion_payloads_carry_no_harness_gold_labels() -> None:
    dataset = load_dataset(FIXTURE)
    for case_ordinal, question in enumerate(dataset.questions, 1):
        forbidden = {*[session.session_id for session in question.sessions], "answer_", question.question_type, question.question}
        events = neutralize(question, _identity())
        for session_ordinal, _session in enumerate(question.sessions, 1):
            session_events = [event for event in events if event.session_ordinal == session_ordinal]
            payload = "\n".join((render_neutral_session(session_events), neutral_title(case_ordinal, session_ordinal), ",".join(neutral_tags())))
            assert all(token not in payload for token in forbidden)
        assert question.answer in "\n".join(event.content for event in events)


def test_real_adapter_ingest_keeps_gold_in_content_but_no_identity_or_label_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.init import init_vault
    from exomem.schema import load_source_schema
    from lme.adapter import LmeExomemAdapter

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    dataset = load_dataset(FIXTURE)
    vault = tmp_path / "vault"
    init_vault(vault)
    adapter = LmeExomemAdapter()
    adapter._vault = vault
    adapter._schema = load_source_schema(vault)
    for case_ordinal, question in enumerate(dataset.questions, 1):
        events = neutralize(question, _identity())
        adapter.ingest_case(events, CaseHandle(case_id=question.question_id, case_ordinal=case_ordinal, question_date=question.question_date_text))
    state = "\n".join(page.text for page in adapter.export_state().pages)
    assert dataset.questions[0].answer in state
    assert "answer_" not in state
    assert all(session_id not in state for question in dataset.questions for session_id in (session.session_id for session in question.sessions))
    assert all(question.question_type not in state for question in dataset.questions)


def test_refuse_if_evidence_marked_rejects_raw_evidence_session_ids() -> None:
    """This explicit preflight must reject LME's evidence-marked source IDs."""

    from protocol.events import LeakageError

    with pytest.raises(LeakageError):
        refuse_if_evidence_marked(load_dataset(FIXTURE), _identity())
