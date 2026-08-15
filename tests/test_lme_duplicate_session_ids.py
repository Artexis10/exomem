"""A repeated haystack session id is data in the pinned release, not a fault.

13 of the 500 rows in the pinned, cleaned LongMemEval-S repeat a
`haystack_session_ids` entry, and one of them — `91b15a6e`, whose
`sharegpt_fgnzLtE_0` appears twice with byte-identical contents — sits inside
the frozen 25-case comparative cohort. MemoryBench's own loader ingests those
rows, so refusing them here would put an asymmetry directly under an
equivalence comparison.

Distinctness was never what the loader needed: `answer_session_ids` resolve
against `set(session_ids)` and are unaffected by a repeat.
"""

from __future__ import annotations

import json

import pytest


def _row(session_ids, sessions=None, **overrides):
    sessions = sessions or [
        [{"role": "user", "content": f"turn {index}"}] for index in range(len(session_ids))
    ]
    row = {
        "question_id": "q-dup",
        "question_type": "single-session-user",
        "question": "which lantern?",
        "answer": "the violet one",
        "question_date": "2026-01-01",
        "haystack_session_ids": list(session_ids),
        "haystack_sessions": sessions,
        "haystack_dates": ["2026-01-01"] * len(session_ids),
        "answer_session_ids": [session_ids[0]],
    }
    row.update(overrides)
    return row


def _load(rows):
    from lme.dataset import load_dataset_bytes

    return load_dataset_bytes(json.dumps(rows).encode("utf-8"))


def test_a_repeated_session_id_loads_instead_of_refusing() -> None:
    dataset = _load([_row(["s1", "s2", "s1"])])

    question = dataset.questions[0]
    assert [session.session_id for session in question.sessions] == ["s1", "s2", "s1"]


def test_each_occurrence_survives_as_its_own_session() -> None:
    """Position, not id, is what distinguishes them downstream."""

    dataset = _load([_row(
        ["s1", "s1"],
        sessions=[[{"role": "user", "content": "first"}], [{"role": "user", "content": "second"}]],
    )])

    sessions = dataset.questions[0].sessions
    assert len(sessions) == 2
    assert [m.content for session in sessions for m in session.messages] == ["first", "second"]


def test_a_repeated_session_reaches_the_provider_at_each_position() -> None:
    """Neutralized events keep one session_ordinal per occurrence."""

    import hashlib

    from lme.normalize import neutralize
    from protocol.models import DatasetIdentity

    dataset = _load([_row(
        ["s1", "s1"],
        sessions=[[{"role": "user", "content": "first"}], [{"role": "user", "content": "second"}]],
    )])
    identity = DatasetIdentity(
        id="longmemeval", variant="fixture", source="local", revision="pin",
        sha256=hashlib.sha256(b"fixture").hexdigest(), case_count=1,
    )
    events = neutralize(dataset.questions[0], identity)

    assert len({event.session_ordinal for event in events}) == 2
    assert sorted(event.content for event in events) == ["first", "second"]


def test_answer_sessions_still_resolve_against_a_repeated_id() -> None:
    dataset = _load([_row(["s1", "s1", "s2"], answer_session_ids=["s2"])])
    assert dataset.questions[0].answer_session_ids == ("s2",)


def test_an_unknown_answer_session_is_still_refused() -> None:
    """Relaxing distinctness must not weaken the check that actually mattered."""

    from lme.dataset import DatasetValidationError

    with pytest.raises(DatasetValidationError, match="unknown answer sessions"):
        _load([_row(["s1", "s1"], answer_session_ids=["nope"])])


def test_a_repeated_question_id_is_still_refused() -> None:
    from lme.dataset import DatasetValidationError

    with pytest.raises(DatasetValidationError, match="repeats a question_id"):
        _load([_row(["s1"]), _row(["s2"])])


def test_an_invalid_row_outside_the_selection_does_not_block_a_run() -> None:
    """A row we never intended to run must not refuse the cohort."""

    from lme.dataset import DatasetValidationError

    good = _row(["s1"], question_id="keep")
    broken = _row(["s1"], question_id="broken", question_type="not-a-real-type")
    dataset = _load([good, broken])

    # The broken row is carried, not raised, until something actually uses it.
    assert [question.question_id for question in dataset.questions] == ["keep"]
    assert dataset.deferred_errors and "broken" in dataset.deferred_errors

    with pytest.raises(DatasetValidationError, match="broken"):
        dataset.require("broken")
    assert dataset.require("keep").question_id == "keep"


def test_the_census_covers_every_source_row_including_deferred_ones() -> None:
    """Cohort selection is a property of the source, not of what we could parse.

    Regenerating from the loaded subset would silently select from a smaller
    universe and produce a different cohort.
    """

    good = _row(["s1"], question_id="keep")
    broken = _row(["s1"], question_id="broken", question_type="not-a-real-type")
    dataset = _load([good, broken])

    assert [identity for identity, _kind in dataset.census] == ["keep", "broken"]
    assert len(dataset.census) == 2
    assert len(dataset.questions) == 1


def test_a_numeric_gold_is_read_at_face_value() -> None:
    """32 pinned rows are counting questions whose gold is a JSON number."""

    dataset = _load([_row(["s1"], answer=2)])
    assert dataset.questions[0].answer == "2"


def test_a_boolean_gold_is_still_refused() -> None:
    """`True` is an int in Python; it is not a numeric answer here."""

    from lme.dataset import DatasetValidationError

    with pytest.raises(DatasetValidationError, match="must be a string"):
        _load([_row(["s1"], answer=True)])


def test_a_non_cohort_run_still_refuses_a_deferred_row(tmp_path) -> None:
    """Deferral is scoped to cohort runs; elsewhere a bad row must not vanish.

    Without this, a full run would quietly cover 493 of 500 rows and drop the
    rest from the denominator.
    """

    from lme.dataset import DatasetValidationError
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    rows = [_row(["s1"], question_id="keep"),
            _row(["s1"], question_id="broken", question_type="not-a-real-type")]
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(rows), encoding="utf-8")

    import hashlib

    with pytest.raises(DatasetValidationError, match="not-a-real-type"):
        execute_run(
            RunConfig(
                dataset=dataset_path, out=tmp_path / "out", run_id="run",
                dataset_sha256=hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                dataset_revision="test-pin",
            ),
            reader=StubReader(),
        )


def test_the_real_pinned_cohort_row_loads() -> None:
    """91b15a6e is the in-cohort row that blocked the gate."""

    dataset = _load([_row(
        ["a", "b", "c", "sharegpt_fgnzLtE_0", "d", "sharegpt_fgnzLtE_0"],
        question_id="91b15a6e",
        answer_session_ids=["sharegpt_fgnzLtE_0"],
    )])
    ids = [session.session_id for session in dataset.questions[0].sessions]
    assert ids.count("sharegpt_fgnzLtE_0") == 2
