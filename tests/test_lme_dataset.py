from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lme.dataset import DatasetValidationError, QUESTION_TYPES, dump_dataset, load_dataset
from lme.fetch import ChecksumMismatch, fetch_instructions, verify_sha256


FIXTURE = Path("benchmarks/lme/fixtures/mini.json")


def test_fixture_schema_round_trip(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE)
    assert len(dataset.questions) == 6
    assert {question.question_type for question in dataset.questions} == set(QUESTION_TYPES)
    assert sum(question.is_abstention for question in dataset.questions) == 1
    assert all(2 <= len(question.sessions) <= 3 for question in dataset.questions)

    target = tmp_path / "round-trip.json"
    dump_dataset(dataset, target)
    assert load_dataset(target) == dataset


def test_fetch_is_instruction_only_and_verifies_recorded_checksum(tmp_path: Path) -> None:
    instructions = fetch_instructions()
    assert "xiaowu0162/longmemeval-cleaned" in instructions
    assert "LongMemEval-S" in instructions
    assert "download" not in verify_sha256.__doc__.lower()

    payload = tmp_path / "dataset.json"
    payload.write_text(json.dumps([]), encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    assert verify_sha256(payload, digest) == digest
    with pytest.raises(ChecksumMismatch, match="sha256"):
        verify_sha256(payload, "0" * 64)


def test_abstention_rows_tolerate_missing_and_unknown_answer_sessions(tmp_path: Path) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    abstention = next(row for row in rows if row["question_id"].endswith("_abs"))
    abstention.pop("answer_session_ids")
    missing_path = tmp_path / "missing.json"
    missing_path.write_text(json.dumps(rows), encoding="utf-8")
    loaded = load_dataset(missing_path)
    loaded_abstention = next(q for q in loaded.questions if q.is_abstention)
    assert loaded_abstention.answer_session_ids == ()

    abstention["answer_session_ids"] = ["not-in-the-haystack"]
    unknown_path = tmp_path / "unknown.json"
    unknown_path.write_text(json.dumps(rows), encoding="utf-8")
    loaded = load_dataset(unknown_path)
    loaded_abstention = next(q for q in loaded.questions if q.is_abstention)
    assert "not-in-the-haystack" in loaded_abstention.validation_warnings[0]

    non_abstention = next(row for row in rows if not row["question_id"].endswith("_abs"))
    non_abstention["answer_session_ids"] = ["not-in-the-haystack"]
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(rows), encoding="utf-8")
    # The refusal moved from load time to point of use: a cohort run must not be
    # blocked by a row it never selects, but an invalid row can never be used.
    invalid = load_dataset(invalid_path)
    identity = non_abstention["question_id"]
    assert identity in invalid.deferred_errors
    assert "unknown answer sessions" in invalid.deferred_errors[identity]
    with pytest.raises(DatasetValidationError, match="unknown answer sessions"):
        invalid.require(identity)
