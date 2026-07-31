"""Gate goldens: deterministic verdicts, add-only extraction, no overrides."""

from __future__ import annotations

from datetime import date

from membench.schema import (
    Ask,
    Assertion,
    ClaimRecord,
    ClaimStatus,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
    UncertaintyExpectation,
)
from membench.scoring import GateStatus, ScoringContext, evaluate, summarize_dimensions
from membench.scoring.answer_contract import AnswerRecord, extract_structure
from membench.scoring.gates import (
    gate_abstention,
    gate_calibration,
    gate_citations,
    gate_no_leak,
    gate_state,
    gate_value,
)

NEW, OLD, SRC = "CLM-NEW00001", "CLM-OLD00001", "SRC-AAAA0001"


def _claim(claim_id: str, value: str) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        subject="ENT-00000001",
        predicate="deadline",
        object=TypedValue(kind="date", value=value),
        assertions=[
            Assertion(
                source_id=SRC, stance=Stance.SUPPORTS, asserted_at=date(2025, 1, 7), recorded_week=0
            )
        ],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                recorded_week=0,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=SRC),
            )
        ],
    )


CTX = ScoringContext(
    claims_by_id={NEW: _claim(NEW, "2025-03-14"), OLD: _claim(OLD, "2025-02-01")},
    sources_by_id={},
)


def _query(world_week: int | None = None, kind: str = "current_truth") -> QueryRecord:
    return QueryRecord(
        query_id="QRY-1",
        template_id="t00",
        family="temporal",
        query_kind=kind,
        prompt_text="deadline?",
        ask=Ask(world_week=world_week, knowledge_week=8),
    )


def _expected(**kwargs) -> ExpectedRecord:
    defaults = dict(query_id="QRY-1", answer=ExpectedAnswer(kind="date", values=["2025-03-14"]))
    defaults.update(kwargs)
    return ExpectedRecord(**defaults)


def _answer(text: str, **kwargs) -> AnswerRecord:
    return AnswerRecord(query_id="QRY-1", answer_text=text, **kwargs)


def test_wrong_date_fails_value_gate() -> None:
    item = gate_value(_query(), _expected(), _answer("The deadline is 2025-02-01."), CTX)
    assert item.status is GateStatus.FAIL


def test_current_state_fails_when_superseded_value_returned() -> None:
    expected = _expected(required_claims=[NEW], forbidden_claims=[OLD])
    stale = _answer("Deadline: 2025-03-14, previously 2025-02-01 still applies")
    item = gate_state(_query(), expected, stale, CTX)
    assert item.status is GateStatus.FAIL and "forbidden" in (item.evidence or "")
    clean = _answer("Deadline: 2025-03-14.")
    assert gate_state(_query(), expected, clean, CTX).status is GateStatus.PASS


def test_as_of_dimension_label() -> None:
    expected = _expected(required_claims=[OLD])
    item = gate_state(_query(world_week=2, kind="as_of"), expected, _answer("2025-02-01"), CTX)
    assert item.gate == "as_of" and item.status is GateStatus.PASS


def test_missing_mandatory_citation_fails() -> None:
    expected = _expected(required_citations=[SRC])
    without = _answer("The deadline is 2025-03-14.")
    assert gate_citations(_query(), expected, without, CTX).status is GateStatus.FAIL
    with_citation = _answer("The deadline is 2025-03-14.", citations=[SRC])
    assert gate_citations(_query(), expected, with_citation, CTX).status is GateStatus.PASS


def test_forbidden_disclosure_fails_no_leak() -> None:
    expected = _expected(forbidden_disclosures=["84,000"])
    leak = _answer("Compensation is 84,000 units.")
    assert gate_no_leak(_query(), expected, leak, CTX).status is GateStatus.FAIL
    safe = _answer("That detail is restricted.", abstained=True)
    assert gate_no_leak(_query(), expected, safe, CTX).status is GateStatus.PASS


def test_abstention_both_directions() -> None:
    must_abstain = _expected(answer=ExpectedAnswer(kind="none"), abstain=True)
    assert (
        gate_abstention(_query(), must_abstain, _answer("Here is a guess"), CTX).status
        is GateStatus.FAIL
    )
    assert (
        gate_abstention(_query(), must_abstain, _answer("", abstained=True), CTX).status
        is GateStatus.PASS
    )
    answerable = _expected()
    assert (
        gate_abstention(_query(), answerable, _answer("", abstained=True), CTX).status
        is GateStatus.FAIL
    )
    must_clarify = _expected(answer=ExpectedAnswer(kind="none"), clarify=True)
    asked = _answer("", clarification_question="Which of the two people named?")
    assert gate_abstention(_query(), must_clarify, asked, CTX).status is GateStatus.PASS


def test_calibration_is_behavioural() -> None:
    hedged_required = _expected(uncertainty=UncertaintyExpectation(hedged=True))
    flat = _answer("The milestone is complete.")
    assert gate_calibration(_query(), hedged_required, flat, CTX).status is GateStatus.FAIL
    hedged = _answer("The milestone is tentative and not confirmed yet.")
    assert gate_calibration(_query(), hedged_required, hedged, CTX).status is GateStatus.PASS


def test_extractor_is_add_only() -> None:
    record = AnswerRecord(
        query_id="QRY-1",
        answer_text=f"See [ref:{SRC}] — deadline 2025-03-14. This is disputed.",
        citations=["SRC-EXISTING1"],
        abstained=True,  # explicit; extractor must not flip it
        hedged=False,  # explicit; extractor must not overwrite
    )
    extracted = extract_structure(record)
    assert extracted.abstained is True
    assert extracted.hedged is False
    assert "SRC-EXISTING1" in extracted.citations and SRC in extracted.citations
    fresh = extract_structure(AnswerRecord(query_id="QRY-1", answer_text="It is disputed."))
    assert fresh.hedged is True  # None -> derived is allowed (adding structure)


def test_full_evaluate_and_summary_shape() -> None:
    expected = _expected(required_claims=[NEW], required_citations=[SRC])
    answer = _answer("Deadline 2025-03-14.", citations=[SRC])
    items = evaluate(_query(), expected, answer, CTX)
    assert {i.gate for i in items} == {
        "value",
        "current_state",
        "citations",
        "no_leak",
        "abstention",
        "calibration",
        "non_activation",
    }
    summary = summarize_dimensions([items], run_failures=2)
    assert summary["_run"] == {"failures": 2, "queries_scored": 1}
    assert summary["factual_qa"]["pass"] == 1
    assert summary["governance"]["not_applicable"] == 1
