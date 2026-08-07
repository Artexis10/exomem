"""Gate goldens: deterministic verdicts, add-only extraction, no overrides."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from membench import oracle
from membench.generate import generate_corpus
from membench.schema import (
    ArtifactKind,
    Ask,
    Assertion,
    AuthorityTier,
    ClaimRecord,
    ClaimStatus,
    EntityRecord,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
    UncertaintyExpectation,
    load_jsonl,
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
    entities_by_id={
        "ENT-00000001": EntityRecord(
            entity_id="ENT-00000001",
            kind="project",
            domain="delivery",
            canonical_name="Baseline",
        )
    },
)


def _query(
    world_week: int | None = None,
    kind: str = "current_truth",
    *,
    prompt_text: str = "deadline?",
    knowledge_week: int = 8,
) -> QueryRecord:
    return QueryRecord(
        query_id="QRY-1",
        template_id="t00",
        family="temporal",
        query_kind=kind,
        prompt_text=prompt_text,
        ask=Ask(world_week=world_week, knowledge_week=knowledge_week),
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


def test_value_gate_honors_tolerance_inclusively() -> None:
    """A numeric expected answer with a non-zero tolerance must accept any
    candidate within tolerance -- via ``quant.within_tolerance``, the same
    rule the oracle used to compute the tolerance -- not just the literal
    canonical string."""

    tolerant = _expected(
        answer=ExpectedAnswer(kind="value", values=["2.5"], tolerance=0.05)
    )

    exact = gate_value(_query(), tolerant, _answer("The ratio is 2.5."), CTX)
    assert exact.status is GateStatus.PASS

    inside = gate_value(_query(), tolerant, _answer("The ratio is 2.52."), CTX)
    assert inside.status is GateStatus.PASS

    boundary = gate_value(_query(), tolerant, _answer("The ratio is 2.55."), CTX)
    assert boundary.status is GateStatus.PASS  # inclusive boundary

    outside = gate_value(_query(), tolerant, _answer("The ratio is 2.56."), CTX)
    assert outside.status is GateStatus.FAIL
    assert "tolerance" in (outside.evidence or "")

    far_outside = gate_value(_query(), tolerant, _answer("The ratio is 9.9."), CTX)
    assert far_outside.status is GateStatus.FAIL


def test_value_gate_zero_tolerance_still_requires_exact_match() -> None:
    """Zero tolerance (the exact-arithmetic default) keeps the plain
    substring rule -- wiring tolerance in must not loosen exact answers."""

    exact_only = _expected(
        answer=ExpectedAnswer(kind="value", values=["75"], tolerance=0.0)
    )
    assert (
        gate_value(_query(), exact_only, _answer("The total is 75 min."), CTX).status
        is GateStatus.PASS
    )
    assert (
        gate_value(_query(), exact_only, _answer("The total is 80 min."), CTX).status
        is GateStatus.FAIL
    )


def test_value_gate_degrades_instead_of_raising_on_a_non_numeric_tolerance() -> None:
    """``quant.within_tolerance`` parses the expected side unguarded, so a
    record like ``values=["2.5 kg"]`` with a tolerance used to raise
    ``InvalidOperation`` out of the gate. expected.jsonl is consumed by third
    parties: one malformed record must not abort a whole run, so the tolerance
    rule declines and the literal rule decides."""

    malformed = _expected(
        answer=ExpectedAnswer(kind="value", values=["2.5 kg"], tolerance=0.05)
    )
    hit = gate_value(_query(), malformed, _answer("The mass is 2.5 kg."), CTX)
    assert hit.status is GateStatus.PASS
    miss = gate_value(_query(), malformed, _answer("The mass is 9.9 kg."), CTX)
    assert miss.status is GateStatus.FAIL


def test_numeric_token_does_not_absorb_a_preceding_hyphen() -> None:
    """``-?\\d+`` without a boundary guard reads "SRC-3.5" as -3.5 and
    "2x-3.5x" as -3.5, failing an answer that states the expected value."""

    tolerant = _expected(answer=ExpectedAnswer(kind="value", values=["3.5"], tolerance=0.05))
    for text in ("Ref SRC-3.5 only", "a 2x-3.5x increase", "the value is 3.5"):
        assert gate_value(_query(), tolerant, _answer(text), CTX).status is GateStatus.PASS, text
    # A real negative sign still parses as negative.
    negative = _expected(answer=ExpectedAnswer(kind="value", values=["-3.5"], tolerance=0.05))
    assert gate_value(_query(), negative, _answer("delta of -3.5"), CTX).status is GateStatus.PASS
    assert gate_value(_query(), tolerant, _answer("delta of -3.5"), CTX).status is GateStatus.FAIL


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


# --- the state gate: what "the retired value is absent" can and cannot prove --
#
# The bare rule (every required value present by substring, every forbidden one
# absent by substring) scored correct product behaviour as failure three ways
# at once. The fixture below is the shape that exposes all three: a hosting
# decision revised twice, so there is a direct predecessor and a transitive one.

HOST_ENTITY = "ENT-00000123"
HOST_OLD, HOST_MID, HOST_NEW = "CLM-HOST0001", "CLM-HOST0002", "CLM-HOST0003"
HOST_SRC_OLD, HOST_SRC_MID, HOST_SRC_NEW = "SRC-HOST0001", "SRC-HOST0002", "SRC-HOST0003"
PRIOR_READING, CURRENT_READING = "CLM-READ0001", "CLM-READ0002"

# Recorded weeks: the first revision lands at week 4, the second at week 6, so
# an ask at week 3 predates both and an ask at week 8 sees both.
_FIRST_REVISION_WEEK, _SECOND_REVISION_WEEK = 4, 6


def _hosting_claim(
    claim_id: str,
    value: str,
    source_id: str,
    recorded_week: int,
    *,
    supersedes: str | None = None,
    superseded_by: str | None = None,
    retired_week: int | None = None,
    retired_by: str | None = None,
) -> ClaimRecord:
    timeline = [
        StatusSpan(
            status=ClaimStatus.CURRENT,
            valid_from=date(2025, 1, 6),
            recorded_week=recorded_week,
            cause=SpanCause(kind=SpanCauseKind.INITIAL, by=source_id),
        )
    ]
    if retired_week is not None:
        timeline.append(
            StatusSpan(
                status=ClaimStatus.SUPERSEDED,
                valid_from=date(2025, 1, 6),
                recorded_week=retired_week,
                cause=SpanCause(kind=SpanCauseKind.SUPERSESSION, by=retired_by),
            )
        )
    return ClaimRecord(
        claim_id=claim_id,
        subject=HOST_ENTITY,
        predicate="hosting_provider",
        object=TypedValue(kind="entity_ref", value=value),
        assertions=[_assert_supports(source_id, recorded_week)],
        status_timeline=timeline,
        supersedes=supersedes,
        superseded_by=superseded_by,
    )


def _assert_supports(source_id: str, week: int) -> Assertion:
    return Assertion(
        source_id=source_id, stance=Stance.SUPPORTS, asserted_at=date(2025, 1, 7), recorded_week=week
    )


def _reading_claim(claim_id: str, predicate: str, value: str) -> ClaimRecord:
    """A measurement with no supersession edge of any kind (the t15 shape).

    ``prior-*`` and its current sibling are two separate current claims, not a
    documented change, so the oracle has nothing to excuse co-presence with.
    """

    return ClaimRecord(
        claim_id=claim_id,
        subject=HOST_ENTITY,
        predicate=predicate,
        object=TypedValue(kind="quantity", value=value, unit="points"),
        assertions=[_assert_supports(HOST_SRC_OLD, 3)],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                recorded_week=3,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=HOST_SRC_OLD),
            )
        ],
    )


HOST_CTX = ScoringContext(
    claims_by_id={
        HOST_OLD: _hosting_claim(
            HOST_OLD,
            "Petra Group",
            HOST_SRC_OLD,
            0,
            superseded_by=HOST_MID,
            retired_week=_FIRST_REVISION_WEEK,
            retired_by=HOST_SRC_MID,
        ),
        HOST_MID: _hosting_claim(
            HOST_MID,
            "Cinder Group",
            HOST_SRC_MID,
            _FIRST_REVISION_WEEK,
            supersedes=HOST_OLD,
            superseded_by=HOST_NEW,
            retired_week=_SECOND_REVISION_WEEK,
            retired_by=HOST_SRC_NEW,
        ),
        HOST_NEW: _hosting_claim(
            HOST_NEW, "Lumo Group", HOST_SRC_NEW, _SECOND_REVISION_WEEK, supersedes=HOST_MID
        ),
        PRIOR_READING: _reading_claim(PRIOR_READING, "prior-burn-score", "44"),
        CURRENT_READING: _reading_claim(CURRENT_READING, "burn-score", "55"),
    },
    sources_by_id={},
    entities_by_id={
        HOST_ENTITY: EntityRecord(
            entity_id=HOST_ENTITY, kind="project", domain="delivery", canonical_name="Cindergate"
        )
    },
)

HOSTING_ASK = "Which provider currently hosts Project Cindergate?"


def _hosting_query(**kwargs) -> QueryRecord:
    kwargs.setdefault("prompt_text", HOSTING_ASK)
    return _query(**kwargs)


def _hosting_expected(forbidden: list[str]) -> ExpectedRecord:
    return _expected(
        answer=ExpectedAnswer(kind="entity", values=["Lumo Group"]),
        required_claims=[HOST_NEW],
        forbidden_claims=forbidden,
    )


def test_the_current_value_alone_passes_and_the_retired_one_alone_fails() -> None:
    """Both ends of the gate, unchanged. Nothing below may soften either."""

    expected = _hosting_expected([HOST_OLD])
    clean = _answer("The hosting provider is Lumo Group.")
    assert gate_state(_hosting_query(), expected, clean, HOST_CTX).status is GateStatus.PASS

    stale = _answer("The hosting provider is Petra Group.")
    item = gate_state(_hosting_query(), expected, stale, HOST_CTX)
    assert item.status is GateStatus.FAIL
    assert "required" in (item.evidence or "") and "Lumo Group" in (item.evidence or "")


def test_a_record_returned_with_its_own_supersession_is_unsupported_not_failed() -> None:
    """The measured defect: 23 of 120 state-gate failures on the reference run
    were responses containing *every* required current value that failed only
    because a superseded value was also present.

    This run's answerer is extractive — it returns retrieved documents — so the
    hosting decision and the memo that reversed it come back together. That is
    reasonable retrieval behaviour, arguably better than hiding the history.
    Whether the answer *asserts* the retired value or reports it as history is
    a fact about rhetoric that no deterministic rule can read, so the honest
    verdict is UNSUPPORTED: unsupported-never-zero cuts both ways, and it is
    emphatically not banked as a PASS.
    """

    dump = _answer(
        "The hosting provider for Project Cindergate is Petra Group.\n\n"
        "The earlier hosting decision is fully reversed. The hosting provider "
        "for Project Cindergate is now Lumo Group."
    )
    item = gate_state(_hosting_query(), _hosting_expected([HOST_OLD]), dump, HOST_CTX)
    assert item.status is GateStatus.UNSUPPORTED
    # The chain the oracle walked is in the evidence, so the verdict is
    # auditable without rerunning.
    assert HOST_OLD in (item.evidence or "") and HOST_NEW in (item.evidence or "")
    assert "not\nderivable" not in (item.evidence or "")  # single-line evidence


def test_supersession_toward_the_answer_is_walked_transitively() -> None:
    """Corpora revise more than once (t15 moves a reading 197 -> 209 -> 217).
    A one-hop test sees only the last retired value and fails the answer for
    the one before it, which is the same defect one step further back."""

    assert HOST_CTX.claims_by_id[HOST_OLD].superseded_by == HOST_MID  # two hops away
    dump = _answer(
        "Petra Group hosts it. Then Cinder Group. The provider is now Lumo Group."
    )
    item = gate_state(
        _hosting_query(), _hosting_expected([HOST_OLD, HOST_MID]), dump, HOST_CTX
    )
    assert item.status is GateStatus.UNSUPPORTED, item.evidence
    assert HOST_MID in (item.evidence or "")


def test_supersession_must_be_visible_at_the_ask_to_excuse_co_presence() -> None:
    """The excuse is bitemporal, not structural. At week 3 neither revision has
    been recorded, so the oracle cannot see that Petra Group was ever retired —
    and it must not reach into the future to forgive an answer."""

    early = _hosting_query(knowledge_week=_FIRST_REVISION_WEEK - 1)
    assert (
        oracle.superseded_toward(
            HOST_OLD,
            [HOST_NEW],
            claims_by_id=HOST_CTX.claims_by_id,
            world_t=oracle.world_cutoff(early.ask.knowledge_week),
            knowledge_week=early.ask.knowledge_week,
        )
        == ()
    )
    dump = _answer("Petra Group hosts it. The provider is now Lumo Group.")
    item = gate_state(early, _hosting_expected([HOST_OLD]), dump, HOST_CTX)
    assert item.status is GateStatus.FAIL, item.evidence
    assert "no supersession" in (item.evidence or "")
    # …and at week 8, with both revisions recorded, the same answer is excused.
    assert (
        gate_state(_hosting_query(), _hosting_expected([HOST_OLD]), dump, HOST_CTX).status
        is GateStatus.UNSUPPORTED
    )


def test_a_forbidden_value_with_no_documented_supersession_still_fails() -> None:
    """The narrowing, and the reason this is not just a weakened gate. The t15
    ``prior-burn-score`` is a separate current claim, not a retired predecessor:
    the oracle has no supersession edge to point at, so an answer that puts the
    wrong reading forward is still provably wrong."""

    prior = HOST_CTX.claims_by_id[PRIOR_READING]
    current = HOST_CTX.claims_by_id[CURRENT_READING]
    assert prior.superseded_by is None and current.supersedes is None

    expected = _expected(
        answer=ExpectedAnswer(kind="value", values=["55"]),
        required_claims=[CURRENT_READING],
        forbidden_claims=[PRIOR_READING],
    )
    both = _answer("The burn score is 55 points; the prior reading was 44 points.")
    item = gate_state(
        _query(prompt_text="What is the burn score?"), expected, both, HOST_CTX
    )
    assert item.status is GateStatus.FAIL, item.evidence
    assert "no supersession" in (item.evidence or "")


def test_absence_only_records_still_fail_on_the_forbidden_value() -> None:
    """28 records in the reference corpus expect absence and nothing else — a
    fact not yet knowable at the ask, or one since retracted. They name no
    required claim, so there is no successor for a supersession to point at and
    nothing to be undecided about: surfacing the value is the failure."""

    expected = _expected(
        answer=ExpectedAnswer(kind="none"), abstain=True, forbidden_claims=[HOST_OLD]
    )
    assert not expected.required_claims
    leaked = _answer("The hosting provider for Project Cindergate is Petra Group.")
    item = gate_state(_hosting_query(), expected, leaked, HOST_CTX)
    assert item.status is GateStatus.FAIL
    assert "Petra Group" in (item.evidence or "")


# --- forbidden values the question itself hands over -------------------------


def test_a_forbidden_value_supplied_by_the_prompt_is_not_measurable() -> None:
    """Four ``identity`` asks name the retired project name in the question
    ("the project once called X"), so *every possible* answer echoes it —
    including one produced with no memory at all. Scoring its presence made
    those four asks unwinnable: no correct answer could pass. Presence of a
    string the question supplied measures nothing, so it is excluded, and the
    exclusion is stated in the evidence rather than hidden."""

    expected = _expected(
        answer=ExpectedAnswer(kind="text", values=["Lumo Group"]),
        required_claims=[HOST_NEW],
        forbidden_claims=[HOST_OLD],
    )
    asked = _hosting_query(
        prompt_text="Who hosts the project once served by Petra Group?"
    )
    correct = _answer("Petra Group has been replaced; the host is now Lumo Group.")
    item = gate_state(asked, expected, correct, HOST_CTX)
    assert item.status is GateStatus.PASS, item.evidence
    assert "supplied by the query prompt" in (item.evidence or "")
    assert "Petra Group" in (item.evidence or "")


def test_the_prompt_exclusion_does_not_leak_to_a_value_the_prompt_never_names() -> None:
    """The control. The exclusion is keyed on the question's own text, so an
    identical answer to a question that does *not* hand over the retired name
    is still measured — here as UNSUPPORTED, because the supersession is
    documented, and it would be a FAIL without one."""

    expected = _expected(
        answer=ExpectedAnswer(kind="text", values=["Lumo Group"]),
        required_claims=[HOST_NEW],
        forbidden_claims=[HOST_OLD],
    )
    correct = _answer("Petra Group has been replaced; the host is now Lumo Group.")
    assert HOST_OLD not in HOSTING_ASK and "Petra" not in HOSTING_ASK
    item = gate_state(_hosting_query(), expected, correct, HOST_CTX)
    assert item.status is GateStatus.UNSUPPORTED, item.evidence
    assert "supplied by the query prompt" not in (item.evidence or "")


# --- boundary-aware matching (4b.14) ----------------------------------------


def _numeric_state(required: str, forbidden: str, text: str, prompt: str = "how many?"):
    claims = {
        "CLM-NUMR0001": _reading_claim("CLM-NUMR0001", "count", required),
        "CLM-NUMF0001": _reading_claim("CLM-NUMF0001", "count", forbidden),
    }
    ctx = ScoringContext(claims_by_id=claims, sources_by_id={}, entities_by_id={})
    expected = _expected(
        answer=ExpectedAnswer(kind="value", values=[required]),
        required_claims=["CLM-NUMR0001"],
        forbidden_claims=["CLM-NUMF0001"],
    )
    return gate_state(_query(prompt_text=prompt), expected, _answer(text), ctx)


def test_numbers_are_matched_as_values_not_as_substrings() -> None:
    """Bare substring containment is wrong in both directions at once: a
    forbidden ``10`` fires on ``105``, ``2010`` and ``SRC-10``, and an expected
    ``10`` passes on ``105``. On the reference corpus the forbidden values
    include ``3``, ``4`` and ``8``, so the old rule fired on the ``03`` of a
    date — and on whatever digits the provider's own ingest ids happened to
    carry, which made a temporal verdict depend on a UUID."""

    # Forbidden 10 must not fire on longer numbers or on an id fragment.
    for text in (
        "The count is 76 out of 105 items.",
        "The count is 76; see the 2010 archive.",
        "The count is 76. Ref SRC-10 and record 4c10f2b1.",
        "The count is 76 as of 2025-10-01.",
    ):
        assert _numeric_state("76", "10", text).status is GateStatus.PASS, text

    # …and still fires on the value actually stated, including at end of
    # sentence and inside a comma-separated list.
    for text in ("The count is 76, previously 10.", "The count is 76. It was 10."):
        item = _numeric_state("76", "10", text)
        assert item.status is GateStatus.FAIL, text
        assert "'10'" in (item.evidence or "")

    # The required half is boundary-aware too: 105 does not state 10.
    assert _numeric_state("10", "99", "The count is 105.").status is GateStatus.FAIL
    assert _numeric_state("10", "99", "The count is 10.").status is GateStatus.PASS
    # A thousands separator is part of the number, not a boundary.
    assert _numeric_state("76", "84", "The count is 76 of 84,000.").status is GateStatus.PASS


def test_a_sentence_final_period_does_not_hide_a_number() -> None:
    """The regression that would silently gut the gate: every generated corpus
    sentence ends in a period, so treating ``.`` as token continuation lost the
    value on most rows at once (measured: 11 gate_value rows and 4 gate_state
    rows flipped to spurious failures before this was fixed)."""

    assert _numeric_state("197", "44", "The flux index measured 197.").status is GateStatus.PASS
    # A real decimal is still one number: 197.5 does not state 197.
    assert _numeric_state("197", "44", "The flux index measured 197.5").status is GateStatus.FAIL


def test_value_gate_matches_on_boundaries_too() -> None:
    """The same defect, same rule, other gate. Without a tolerance the value
    gate fell back to bare substring, so an expected ``10`` passed on ``105``."""

    exact = _expected(answer=ExpectedAnswer(kind="value", values=["10"]))
    assert gate_value(_query(), exact, _answer("The total is 105."), CTX).status is GateStatus.FAIL
    assert gate_value(_query(), exact, _answer("The total is 10."), CTX).status is GateStatus.PASS

    listed = _expected(answer=ExpectedAnswer(kind="list", values=["10", "Lumo Group"]))
    partial = _answer("Saw 105 and Lumo Groupings.")
    item = gate_value(_query(), listed, partial, CTX)
    assert item.status is GateStatus.FAIL
    assert "10" in (item.evidence or "") and "Lumo Group" in (item.evidence or "")
    whole = _answer("Saw 10 and Lumo Group.")
    assert gate_value(_query(), listed, whole, CTX).status is GateStatus.PASS


def test_text_values_are_matched_on_word_boundaries() -> None:
    expected = _hosting_expected([HOST_OLD])
    # "Lumo Groupings" is not "Lumo Group".
    near = _answer("The provider is Lumo Groupings.")
    assert gate_state(_hosting_query(), expected, near, HOST_CTX).status is GateStatus.FAIL
    exact = _answer("The provider is Lumo Group.")
    assert gate_state(_hosting_query(), expected, exact, HOST_CTX).status is GateStatus.PASS


def test_missing_mandatory_citation_fails() -> None:
    # The claim basis is what makes the PASS half meaningful: without it the
    # gate can verify recall but not precision, and reports UNSUPPORTED rather
    # than banking an unverifiable provenance verdict as a pass (see
    # test_precision_without_a_claim_basis_is_unsupported_not_passed).
    expected = _expected(required_claims=[NEW], required_citations=[SRC])
    without = _answer("The deadline is 2025-03-14.")
    assert gate_citations(_query(), expected, without, CTX).status is GateStatus.FAIL
    with_citation = _answer("The deadline is 2025-03-14.", citations=[SRC])
    assert gate_citations(_query(), expected, with_citation, CTX).status is GateStatus.PASS


# --- citation precision -----------------------------------------------------
#
# Recall alone is not attribution: a contender that cites every source it
# retrieved (or the whole corpus) passed the old provenance gate on every
# citation query. The gate is precision AND recall, with precision measured
# against the oracle's evidence neighbourhood -- never against required-only,
# which would punish honest providers for citing a source the corpus itself
# asks them to name.

DISPUTED = "CLM-DISP0001"
NAMED = "CLM-NAME0001"
DIGEST = "CLM-DGST0001"
MEASURED = "CLM-MEAS0001"
ENTITY = "ENT-00000042"
OTHER_ENTITY = "ENT-00000099"

SUPPORTING = "SRC-SUPP0001"
DISPUTING = "SRC-DISP0001"
RENAME = "SRC-RENM0001"
DIGEST_SRC = "SRC-DGST0001"
LATE = "SRC-LATE0001"
MEASUREMENT = "SRC-MEAS0001"
UNRELATED = "SRC-NOISE001"
REVISION = "SRC-REVN0001"

LATE_WEEK = 9


def _assert(source_id: str, week: int, stance: Stance = Stance.SUPPORTS) -> Assertion:
    return Assertion(
        source_id=source_id, stance=stance, asserted_at=date(2025, 1, 7), recorded_week=week
    )


def _disputed_claim() -> ClaimRecord:
    """The claim under discussion.

    Three assertions the oracle can see at week 8 (a supporter and an
    objector) and one recorded at week 9 that it cannot. ``required_citations``
    names only the supporter.
    """

    return ClaimRecord(
        claim_id=DISPUTED,
        subject=ENTITY,
        predicate="deadline",
        object=TypedValue(kind="date", value="2025-03-14"),
        assertions=[
            _assert(SUPPORTING, 0),
            _assert(DISPUTING, 3, Stance.DISPUTES),
            _assert(LATE, LATE_WEEK, Stance.DISPUTES),
        ],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                recorded_week=0,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=SUPPORTING),
            ),
            StatusSpan(
                status=ClaimStatus.DISPROVED,
                valid_from=date(2025, 3, 3),
                recorded_week=LATE_WEEK,
                cause=SpanCause(kind=SpanCauseKind.DISPROOF, by=LATE),
            ),
        ],
    )


def _rename_claim() -> ClaimRecord:
    """A *different* claim about the *same* entity: what it is called.

    This is the t14 shape. An answer that names the entity rests on whatever
    established the name, so the rename memo is on-topic provenance even
    though it asserts nothing about the deadline.
    """

    return ClaimRecord(
        claim_id=NAMED,
        subject=ENTITY,
        predicate="official_name",
        object=TypedValue(kind="text", value="Northwind"),
        assertions=[_assert(RENAME, 1)],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                recorded_week=1,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=RENAME),
            )
        ],
    )


def _digest_claim() -> ClaimRecord:
    """A claim derived *from* the one under discussion, restating its value.

    This is the t11 shape: the digest is downstream, so a walk that follows
    ``derived_from`` upward only never reaches it.
    """

    return ClaimRecord(
        claim_id=DIGEST,
        subject=OTHER_ENTITY,
        predicate="deadline_digest",
        object=TypedValue(kind="date", value="2025-03-14"),
        assertions=[_assert(DIGEST_SRC, 2)],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                recorded_week=2,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=DIGEST_SRC),
            )
        ],
        derived_from=[DISPUTED],
    )


def _superseded_claim() -> ClaimRecord:
    """A claim about a third entity whose supersession was recorded later."""

    return ClaimRecord(
        claim_id=OLD,
        subject="ENT-00000007",
        predicate="deadline",
        object=TypedValue(kind="date", value="2025-02-01"),
        assertions=[_assert(SRC, 0)],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                valid_to=date(2025, 2, 10),
                recorded_week=0,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=SRC),
            ),
            StatusSpan(
                status=ClaimStatus.SUPERSEDED,
                valid_from=date(2025, 2, 10),
                recorded_week=4,
                cause=SpanCause(kind=SpanCauseKind.SUPERSESSION, by=REVISION),
            ),
        ],
    )


def _attribute_claim() -> ClaimRecord:
    """A measurement about the same entity, and nothing more.

    The counterpart to ``_rename_claim``: same subject, but its object is a
    quantity, so it resolves no reference. Admitting it would make
    shotgunning free inside an entity — a field report about a yield score is
    not provenance for a question about a date.
    """

    return ClaimRecord(
        claim_id=MEASURED,
        subject=ENTITY,
        predicate="yield-score",
        object=TypedValue(kind="quantity", value="25.1", unit="points"),
        assertions=[_assert(MEASUREMENT, 1)],
        status_timeline=[
            StatusSpan(
                status=ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                recorded_week=1,
                cause=SpanCause(kind=SpanCauseKind.INITIAL, by=MEASUREMENT),
            )
        ],
    )


PRECISION_ENTITIES = {
    ENTITY: EntityRecord(
        entity_id=ENTITY,
        kind="project",
        domain="delivery",
        canonical_name="Northwind",
        aliases=["Project Northwind"],
    ),
    OTHER_ENTITY: EntityRecord(
        entity_id=OTHER_ENTITY,
        kind="project",
        domain="delivery",
        canonical_name="Southgale",
    ),
}

PRECISION_CTX = ScoringContext(
    claims_by_id={
        DISPUTED: _disputed_claim(),
        NAMED: _rename_claim(),
        MEASURED: _attribute_claim(),
        DIGEST: _digest_claim(),
        OLD: _superseded_claim(),
    },
    sources_by_id={},
    entities_by_id=PRECISION_ENTITIES,
)

DEADLINE_REQUIRED = [SUPPORTING]


def _cite(*sources: str) -> AnswerRecord:
    return _answer("Deadline 2025-03-14.", citations=list(sources))


def _deadline_expected(**kwargs) -> ExpectedRecord:
    return _expected(
        required_claims=[DISPUTED], required_citations=list(DEADLINE_REQUIRED), **kwargs
    )


def test_required_citations_come_from_the_oracle_for_this_fixture() -> None:
    """Anchor: the fixture's required set is what the generator would write,
    so the precision tests below are not arguing with a hand-picked list."""

    claim = PRECISION_CTX.claims_by_id[DISPUTED]
    view = oracle.current_truth(claim, 8)
    assert (
        list(
            oracle.required_citations(
                claim, view, claims_by_id=PRECISION_CTX.claims_by_id, knowledge_week=8
            )
        )
        == DEADLINE_REQUIRED
    )


def test_exact_citation_match_passes_and_publishes_both_ratios() -> None:
    item = gate_citations(_query(), _deadline_expected(), _cite(SUPPORTING), PRECISION_CTX)
    assert item.status is GateStatus.PASS
    # Auditable without rerunning: both halves are in the evidence.
    assert "recall 1/1" in (item.evidence or "")
    assert "precision 1/1" in (item.evidence or "")


def test_missing_required_citation_still_fails_with_a_claim_basis() -> None:
    """Adding precision must not weaken the recall half of the gate."""

    item = gate_citations(_query(), _deadline_expected(), _cite(DISPUTING), PRECISION_CTX)
    assert item.status is GateStatus.FAIL
    assert "missing citations" in (item.evidence or "")
    assert SUPPORTING in (item.evidence or "")
    assert "recall 0/1" in (item.evidence or "")


def test_extra_but_supporting_citation_is_never_punished() -> None:
    """The objecting source is real provenance the oracle can justify, so an
    answer that cites both sides is precise, not sloppy. Failing this would
    score an honest provider — one doing exactly what ``cite_both_sides`` asks
    for — as a provenance failure."""

    assert DISPUTING not in DEADLINE_REQUIRED
    item = gate_citations(
        _query(), _deadline_expected(), _cite(SUPPORTING, DISPUTING), PRECISION_CTX
    )
    assert item.status is GateStatus.PASS
    assert "precision 2/2" in (item.evidence or "")


def test_a_downstream_derived_claims_source_is_permitted() -> None:
    """H1a, the t11 shape. ``CLM-DGST0001`` is ``derived_from`` the claim under
    discussion and restates its value, so its source is better provenance, not
    worse. A ``derived_from`` walk that only goes upward never reaches it and
    scores this answer as a provenance failure."""

    digest = PRECISION_CTX.claims_by_id[DIGEST]
    assert DISPUTED in digest.derived_from  # downstream, not upstream
    assert digest.object.value == PRECISION_CTX.claims_by_id[DISPUTED].object.value
    assert DIGEST_SRC not in DEADLINE_REQUIRED

    item = gate_citations(
        _query(), _deadline_expected(), _cite(SUPPORTING, DIGEST_SRC), PRECISION_CTX
    )
    assert item.status is GateStatus.PASS, item.evidence
    assert "precision 2/2" in (item.evidence or "")


def test_a_same_entity_claims_source_is_permitted() -> None:
    """H1b, the t14 shape. The rename memo asserts a different claim about the
    same entity; an answer naming that entity legitimately rests on it."""

    named = PRECISION_CTX.claims_by_id[NAMED]
    disputed = PRECISION_CTX.claims_by_id[DISPUTED]
    assert named.subject == disputed.subject and named.claim_id != disputed.claim_id
    assert RENAME not in DEADLINE_REQUIRED

    item = gate_citations(
        _query(), _deadline_expected(), _cite(SUPPORTING, RENAME), PRECISION_CTX
    )
    assert item.status is GateStatus.PASS, item.evidence
    assert "precision 2/2" in (item.evidence or "")


def test_a_same_entity_attribute_claims_source_is_not_permitted() -> None:
    """The narrowing, minimally. ``CLM-MEAS0001`` is about the same entity as
    the claim under discussion, but its object is a measurement, so it resolves
    no reference and backs nothing in a date answer. The wide subject edge
    admitted it and made shotgunning free inside an entity."""

    measured = PRECISION_CTX.claims_by_id[MEASURED]
    disputed = PRECISION_CTX.claims_by_id[DISPUTED]
    assert measured.subject == disputed.subject  # same entity...
    assert measured.object.kind == "quantity"  # ...but names no entity

    item = gate_citations(
        _query(), _deadline_expected(), _cite(SUPPORTING, MEASUREMENT), PRECISION_CTX
    )
    assert item.status is GateStatus.FAIL, item.evidence
    assert MEASUREMENT in (item.evidence or "")
    assert "precision 1/2" in (item.evidence or "")


def test_precision_is_unsupported_when_entity_records_are_missing() -> None:
    """Resolving a name to an entity needs entity records. Without them the
    edge cannot be evaluated, and neither guess is safe: dropping it fails
    honest multi-hop answers, keeping it wide waves the shotgun through. So the
    gate declines to decide rather than picking one."""

    blind = ScoringContext(
        claims_by_id=PRECISION_CTX.claims_by_id, sources_by_id={}, entities_by_id={}
    )
    item = gate_citations(
        _query(), _deadline_expected(), _cite(SUPPORTING, RENAME), blind
    )
    assert item.status is GateStatus.UNSUPPORTED
    assert "entity records unavailable" in (item.evidence or "")
    # With the records present the same answer is decidable, and precise.
    assert (
        gate_citations(
            _query(), _deadline_expected(), _cite(SUPPORTING, RENAME), PRECISION_CTX
        ).status
        is GateStatus.PASS
    )


def test_derived_from_source_refs_honour_the_knowledge_week() -> None:
    """``derived_from`` may reference a source directly. That branch skipped the
    visibility filter, so a contender could cite a source recorded after the
    ask and still be scored precise."""

    late_source = SourceRecord(
        source_id=LATE,
        title="late",
        artifact_kind=ArtifactKind.MARKDOWN,
        path="late.md",
        authority=AuthorityTier.OFFICIAL,
        event_time=date(2025, 3, 3),
        recorded_week=LATE_WEEK,
    )
    derived = _disputed_claim().model_copy(update={"derived_from": [LATE], "assertions": [_assert(SUPPORTING, 0)]})
    ctx = ScoringContext(
        claims_by_id={DISPUTED: derived},
        sources_by_id={LATE: late_source},
        entities_by_id=PRECISION_ENTITIES,
    )
    early = QueryRecord(
        query_id="QRY-1",
        template_id="t00",
        family="temporal",
        query_kind="current_truth",
        prompt_text="deadline?",
        ask=Ask(world_week=None, knowledge_week=LATE_WEEK - 1),
    )
    item = gate_citations(early, _deadline_expected(), _cite(SUPPORTING, LATE), ctx)
    assert item.status is GateStatus.FAIL, item.evidence
    assert LATE in (item.evidence or "")

    late_ask = early.model_copy(update={"ask": Ask(world_week=None, knowledge_week=LATE_WEEK)})
    assert (
        gate_citations(late_ask, _deadline_expected(), _cite(SUPPORTING, LATE), ctx).status
        is GateStatus.PASS
    )


def test_supersession_cause_source_is_permitted_on_an_as_of_answer() -> None:
    """An as-of answer may name the source that later superseded the fact:
    the oracle sees that span, so it cannot call the citation wrong."""

    expected = _expected(
        required_claims=[OLD],
        required_citations=[SRC],
        answer=ExpectedAnswer(kind="date", values=["2025-02-01"]),
    )
    explained = _answer(
        "As of week 2 the deadline was 2025-02-01; it was later revised.",
        citations=[SRC, REVISION],
    )
    item = gate_citations(_query(world_week=2, kind="as_of"), expected, explained, PRECISION_CTX)
    assert item.status is GateStatus.PASS
    assert "precision 2/2" in (item.evidence or "")


def test_extra_and_unsupporting_citation_fails() -> None:
    """The shotgun, minimally: perfect recall plus one source the oracle can
    prove says nothing about anything the answer is about. Recall-only scored
    this PASS."""

    item = gate_citations(
        _query(), _deadline_expected(), _cite(SUPPORTING, UNRELATED), PRECISION_CTX
    )
    assert item.status is GateStatus.FAIL
    assert "unsupported citations" in (item.evidence or "")
    assert UNRELATED in (item.evidence or "")
    # The recall half is still reported, and still perfect — the verdict is
    # attributable to precision alone.
    assert "recall 1/1" in (item.evidence or "")
    assert "precision 1/2" in (item.evidence or "")


# --- bitemporal visibility inside the permitted set -------------------------


def test_citing_evidence_recorded_after_the_knowledge_week_fails() -> None:
    """``SRC-LATE0001`` disproves the claim, but only from week 9. A contender
    answering at week 8 cannot have seen it, so naming it is a provenance
    failure — not a bonus. This pins the ``recorded_week <= knowledge_week``
    filter inside the permitted set; without it the citation is admitted."""

    early = QueryRecord(
        query_id="QRY-1",
        template_id="t00",
        family="temporal",
        query_kind="current_truth",
        prompt_text="deadline?",
        ask=Ask(world_week=None, knowledge_week=LATE_WEEK - 1),
    )
    item = gate_citations(early, _deadline_expected(), _cite(SUPPORTING, LATE), PRECISION_CTX)
    assert item.status is GateStatus.FAIL, item.evidence
    assert LATE in (item.evidence or "")
    assert "precision 1/2" in (item.evidence or "")


def test_the_same_citation_is_permitted_once_the_knowledge_week_reaches_it() -> None:
    """The other half of the same filter: at week 9 the disproof is visible,
    so the identical answer is now precise. A gate that ignores recorded_week
    cannot tell these two cases apart."""

    late = QueryRecord(
        query_id="QRY-1",
        template_id="t00",
        family="temporal",
        query_kind="current_truth",
        prompt_text="deadline?",
        ask=Ask(world_week=None, knowledge_week=LATE_WEEK),
    )
    item = gate_citations(late, _deadline_expected(), _cite(SUPPORTING, LATE), PRECISION_CTX)
    assert item.status is GateStatus.PASS, item.evidence
    assert "precision 2/2" in (item.evidence or "")


# --- unmeasurable precision --------------------------------------------------


def test_no_required_citations_and_none_given_is_not_applicable() -> None:
    """Nothing was required and nothing was cited: there is no verdict to
    reach, and the absence stays auditable."""

    expected = _expected(required_claims=[DISPUTED])
    item = gate_citations(_query(), expected, _cite(), PRECISION_CTX)
    assert item.status is GateStatus.NOT_APPLICABLE
    assert "none cited" in (item.evidence or "")


def test_naming_sources_with_no_claim_basis_is_unsupported_not_inapplicable() -> None:
    """An attribution that was made but cannot be checked is unmeasurable.

    Filing this as NOT_APPLICABLE made the same unverifiability read two
    different ways depending on whether a citation happened to be required,
    and it hid exactly the rows where a shotgun is least visible: no claim
    basis means precision cannot be computed, so cite-everything went
    unrecorded rather than unresolved.
    """

    expected = _expected()  # no required_claims: the oracle has no basis to check against
    item = gate_citations(_query(), expected, _cite(SUPPORTING, DISPUTING), PRECISION_CTX)
    assert item.status is GateStatus.UNSUPPORTED
    assert "precision unverifiable" in (item.evidence or "")
    assert "2 cited" in (item.evidence or "")


def test_precision_is_scored_even_when_no_citation_was_required() -> None:
    """Requiring no citations does not license citing anything. Returning
    NOT_APPLICABLE before computing precision made a shotgun free on every
    such record, and a claim basis is present here, so the oracle can prove
    the extra citation wrong."""

    expected = _expected(required_claims=[DISPUTED])
    assert not expected.required_citations
    item = gate_citations(_query(), expected, _cite(SUPPORTING, UNRELATED), PRECISION_CTX)
    assert item.status is GateStatus.FAIL
    assert "recall n/a" in (item.evidence or "")
    assert "precision 1/2" in (item.evidence or "")
    assert UNRELATED in (item.evidence or "")
    # …and a precise answer to the same record still passes.
    good = gate_citations(_query(), expected, _cite(SUPPORTING), PRECISION_CTX)
    assert good.status is GateStatus.PASS


def test_precision_without_a_claim_basis_is_unsupported_not_passed() -> None:
    """Unsupported-never-zero cuts both ways. A record that names citations but
    no claims gives the oracle nothing to reason from, so the verdict is
    UNSUPPORTED — banking it as a PASS would let a contender that shotguns
    those records show a clean provenance sheet."""

    expected = _expected(required_citations=[SUPPORTING])
    assert not expected.required_claims and not expected.forbidden_claims
    item = gate_citations(
        _query(), expected, _cite(SUPPORTING, UNRELATED), PRECISION_CTX
    )
    assert item.status is GateStatus.UNSUPPORTED
    assert "precision unverifiable" in (item.evidence or "")
    assert "recall 1/1" in (item.evidence or "")


def test_unverifiable_precision_still_fails_a_missing_required_citation() -> None:
    """Recall is provable whatever precision could be established."""

    expected = _expected(required_citations=[SUPPORTING])
    item = gate_citations(_query(), expected, _cite(UNRELATED), PRECISION_CTX)
    assert item.status is GateStatus.FAIL
    assert "recall 0/1" in (item.evidence or "")


def test_precision_is_unsupported_when_the_claim_is_outside_the_index() -> None:
    expected = _expected(required_claims=["CLM-ABSENT01"], required_citations=[SUPPORTING])
    item = gate_citations(
        _query(), expected, _cite(SUPPORTING, UNRELATED), PRECISION_CTX
    )
    assert item.status is GateStatus.UNSUPPORTED
    assert "precision unverifiable" in (item.evidence or "")
    assert "CLM-ABSENT01" in (item.evidence or "")


def test_duplicate_citations_do_not_distort_the_ratios() -> None:
    item = gate_citations(
        _query(), _deadline_expected(), _cite(SUPPORTING, SUPPORTING), PRECISION_CTX
    )
    assert item.status is GateStatus.PASS
    assert "precision 1/1" in (item.evidence or "")


def test_permitted_citations_always_contains_the_required_ones() -> None:
    """The superset guarantee is a property of the oracle function, not of the
    graph walk. A record may require a citation the neighbourhood does not
    reach; no caller may then punish an answer for citing exactly what the
    record demands."""

    stranded = _expected(required_claims=[DISPUTED], required_citations=[SUPPORTING, UNRELATED])
    permitted, reason = oracle.permitted_citations(
        stranded,
        claims_by_id=PRECISION_CTX.claims_by_id,
        knowledge_week=8,
        entities_by_id=PRECISION_CTX.entities_by_id,
    )
    assert reason is None
    assert {SUPPORTING, UNRELATED} <= permitted
    item = gate_citations(_query(), stranded, _cite(SUPPORTING, UNRELATED), PRECISION_CTX)
    assert item.status is GateStatus.PASS


# --- citation precision against a real generated corpus ---------------------

_SHOTGUN_TEMPLATES = [
    "t01_temporal_reversal",
    "t07_authority_conflict",
    "t10_retraction",
    "t11_transitive_provenance",
    "t14_identity_graph",
]


@pytest.fixture(scope="module")
def shotgun_corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("shotgun") / "corpus"
    generate_corpus(1, out, template_ids=_SHOTGUN_TEMPLATES)
    return out


def _corpus_records(
    corpus: Path,
) -> tuple[list[QueryRecord], dict[str, ExpectedRecord], ScoringContext, list[str]]:
    queries = load_jsonl(QueryRecord, corpus / "queries.jsonl")
    expected = {
        record.query_id: record
        for record in load_jsonl(ExpectedRecord, corpus / "expected.jsonl")
    }
    claims = load_jsonl(ClaimRecord, corpus / "claims.jsonl")
    sources = load_jsonl(SourceRecord, corpus / "sources.jsonl")
    entities = load_jsonl(EntityRecord, corpus / "entities.jsonl")
    ctx = ScoringContext(
        claims_by_id={claim.claim_id: claim for claim in claims},
        sources_by_id={source.source_id: source for source in sources},
        entities_by_id={entity.entity_id: entity for entity in entities},
    )
    return queries, expected, ctx, [source.source_id for source in sources]


def test_citing_every_source_in_the_corpus_fails(shotgun_corpus: Path) -> None:
    """The shotgun, at full scale: cite the entire corpus on every citation
    query. Recall is perfect by construction, so the old recall-only gate
    scored a contender with no provenance at all as a clean sweep."""

    queries, expected, ctx, every_source = _corpus_records(shotgun_corpus)
    assert len(every_source) > 20  # a shotgun worth the name

    checked = 0
    for query in queries:
        record = expected[query.query_id]
        if not record.required_citations:
            continue
        if not (record.required_claims or record.forbidden_claims):
            continue  # precision unmeasurable — covered by its own test
        shotgun = _answer("Some answer.", citations=list(every_source))
        item = gate_citations(query, record, shotgun, ctx)
        assert item.status is GateStatus.FAIL, f"{query.query_id} passed the shotgun"
        assert "unsupported citations" in (item.evidence or "")
        # The listing is capped so one shotgun row cannot bloat the run
        # artifacts; the precision ratio still carries the full count.
        assert "more)" in (item.evidence or "")
        assert len(item.evidence or "") < 500
        checked += 1
    assert checked > 0


def test_citing_another_scenarios_sources_fails(shotgun_corpus: Path) -> None:
    """Templates build isolated scenario graphs, so one record's claims and
    another template's sources share no entity, no derivation and no
    supersession. Those sources are therefore provably unrelated *from corpus
    facts* — this test never consults the permitted set to decide what should
    be rejected."""

    queries, expected, ctx, _ = _corpus_records(shotgun_corpus)
    by_template: dict[str, list[QueryRecord]] = {}
    for query in queries:
        if expected[query.query_id].required_citations:
            by_template.setdefault(query.template_id, []).append(query)
    assert len(by_template) >= 2

    def entities(record: ExpectedRecord) -> set[str]:
        return {
            ctx.claims_by_id[cid].subject
            for cid in [*record.required_claims, *record.forbidden_claims]
            if cid in ctx.claims_by_id
        }

    checked = 0
    template_ids = sorted(by_template)
    for index, template_id in enumerate(template_ids):
        foreign_id = template_ids[(index + 1) % len(template_ids)]
        foreign = by_template[foreign_id][0]
        foreign_record = expected[foreign.query_id]
        for query in by_template[template_id]:
            record = expected[query.query_id]
            if not (record.required_claims or record.forbidden_claims):
                continue
            if entities(record) & entities(foreign_record):
                continue  # not actually a foreign scenario
            intruders = [
                source
                for source in foreign_record.required_citations
                if source not in record.required_citations
            ]
            if not intruders:
                continue
            answer = _answer(
                "Some answer.", citations=[*record.required_citations, *intruders]
            )
            item = gate_citations(query, record, answer, ctx)
            assert item.status is GateStatus.FAIL, (query.query_id, item.evidence)
            assert intruders[0] in (item.evidence or "")
            checked += 1
    assert checked > 0


def _entity_name_index(ctx: ScoringContext) -> dict[str, set[str]]:
    """Name -> entity ids, straight from entities.jsonl. A corpus fact, so the
    tests below never ask the function under test what it considers related."""

    names: dict[str, set[str]] = {}
    for entity in ctx.entities_by_id.values():
        for name in (
            entity.canonical_name,
            *entity.aliases,
            *(span.name for span in entity.name_timeline),
        ):
            names.setdefault(name, set()).add(entity.entity_id)
    return names


def _basis_subjects(record: ExpectedRecord, ctx: ScoringContext) -> set[str]:
    return {
        ctx.claims_by_id[cid].subject
        for cid in [*record.required_claims, *record.forbidden_claims]
        if cid in ctx.claims_by_id
    }


def test_reference_resolving_same_entity_sources_are_admitted(
    shotgun_corpus: Path,
) -> None:
    """The t14 shape over real records. A same-entity claim whose object *names*
    an entity resolves reference — that is what lets a system prove the entity
    in one source is the entity in another — so its sources must be admitted.
    Membership is decided from entities.jsonl, not from the permitted set."""

    queries, expected, ctx, _ = _corpus_records(shotgun_corpus)
    names = _entity_name_index(ctx)
    checked = 0
    for query in queries:
        record = expected[query.query_id]
        if not record.required_citations:
            continue
        subjects = _basis_subjects(record, ctx)
        if not subjects:
            continue
        kin = [
            assertion.source_id
            for claim in ctx.claims_by_id.values()
            if claim.subject in subjects and names.get(claim.object.value, set()) & subjects
            for assertion in claim.assertions
            if assertion.recorded_week <= query.ask.knowledge_week
        ]
        if not kin:
            continue
        answer = _answer("Some answer.", citations=[*record.required_citations, *kin])
        item = gate_citations(query, record, answer, ctx)
        assert item.status is GateStatus.PASS, (query.query_id, item.evidence)
        checked += 1
    assert checked > 0


def _relation_closure(basis: set[str], ctx: ScoringContext) -> set[str]:
    """Claims reachable from ``basis`` by derivation (either direction) and
    supersession, walked straight off the corpus records. Used to subtract the
    edges that are not under test, so a test about the entity edge is about
    the entity edge."""

    children: dict[str, list[str]] = {}
    for claim in ctx.claims_by_id.values():
        for parent in claim.derived_from:
            children.setdefault(parent, []).append(claim.claim_id)
    seen: set[str] = set()
    frontier = list(basis)
    while frontier:
        cid = frontier.pop()
        if cid in seen or cid not in ctx.claims_by_id:
            continue
        seen.add(cid)
        claim = ctx.claims_by_id[cid]
        frontier.extend(claim.derived_from)
        frontier.extend(children.get(cid, ()))
        frontier.extend(r for r in (claim.supersedes, claim.superseded_by) if r)
    return seen


def test_same_entity_attribute_sources_are_not_admitted(shotgun_corpus: Path) -> None:
    """The other side of the narrowed edge, and the hole it closes. A claim
    about the same entity whose object is a measurement rather than an entity
    name resolves no reference: a field report on a yield score is not
    provenance for a question about a date. Shotgunning inside one entity must
    not be free."""

    queries, expected, ctx, _ = _corpus_records(shotgun_corpus)
    names = _entity_name_index(ctx)
    checked = 0
    for query in queries:
        record = expected[query.query_id]
        if not record.required_citations:
            continue
        subjects = _basis_subjects(record, ctx)
        basis = {*record.required_claims, *record.forbidden_claims}
        if not subjects:
            continue
        related = _relation_closure(basis, ctx)
        strangers = []
        for claim in ctx.claims_by_id.values():
            if claim.subject not in subjects or claim.claim_id in related:
                continue  # reachable without the entity edge at all
            if names.get(claim.object.value):
                continue  # reference-resolving: legitimately admitted
            strangers.extend(
                a.source_id
                for a in claim.assertions
                if a.recorded_week <= query.ask.knowledge_week
                and a.source_id not in record.required_citations
            )
        if not strangers:
            continue
        answer = _answer("Some answer.", citations=[*record.required_citations, *strangers])
        item = gate_citations(query, record, answer, ctx)
        assert item.status is GateStatus.FAIL, (query.query_id, item.evidence)
        checked += 1
    assert checked > 0


def test_claim_closure_stays_bounded(shotgun_corpus: Path) -> None:
    """The closure is transitive, so a careless edge can make it swallow the
    corpus. Pin its size: the reviewer built 400 claims where a subject-only
    edge reached 100% of them from a single basis claim."""

    queries, expected, ctx, _ = _corpus_records(shotgun_corpus)
    worst = 0
    for query in queries:
        record = expected[query.query_id]
        basis = [
            cid
            for cid in [*record.required_claims, *record.forbidden_claims]
            if cid in ctx.claims_by_id
        ]
        if not basis:
            continue
        worst = max(
            worst,
            len(
                oracle.claim_neighbourhood(
                    basis, ctx.claims_by_id, entities_by_id=ctx.entities_by_id
                )
            ),
        )
    assert worst <= 8, f"closure grew to {worst} claims"
    assert worst < len(ctx.claims_by_id) / 4


def test_downstream_derived_sources_are_admitted_across_the_corpus(
    shotgun_corpus: Path,
) -> None:
    """The t11 shape over real records: any claim that is ``derived_from`` a
    basis claim is downstream evidence, and its visible sources must be
    admitted. Found by walking the corpus' own ``derived_from`` edges."""

    queries, expected, ctx, _ = _corpus_records(shotgun_corpus)
    checked = 0
    for query in queries:
        record = expected[query.query_id]
        if not record.required_citations:
            continue
        basis = {
            cid
            for cid in [*record.required_claims, *record.forbidden_claims]
            if cid in ctx.claims_by_id
        }
        if not basis:
            continue
        downstream = [
            assertion.source_id
            for claim in ctx.claims_by_id.values()
            if basis.intersection(claim.derived_from)
            for assertion in claim.assertions
            if assertion.recorded_week <= query.ask.knowledge_week
        ]
        if not downstream:
            continue
        answer = _answer("Some answer.", citations=[*record.required_citations, *downstream])
        item = gate_citations(query, record, answer, ctx)
        assert item.status is GateStatus.PASS, (query.query_id, item.evidence)
        checked += 1
    assert checked > 0


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
