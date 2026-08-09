"""Bitemporal oracle edge cases: as-of, late evidence, expiry, dispute, lint."""

from __future__ import annotations

from datetime import date

from membench import oracle
from membench.clock import week_date
from membench.schema import (
    Assertion,
    ClaimRecord,
    ClaimStatus,
    Persona,
    PolicyRule,
    PolicySet,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TombstoneRequest,
    TypedValue,
)

S1, S2, S3, S4 = "SRC-AAAA0001", "SRC-AAAA0002", "SRC-AAAA0003", "SRC-AAAA0004"
OTHER_CLAIM = "CLM-BBBB0002"


def _assert_(source: str, stance: Stance, week: int) -> Assertion:
    return Assertion(
        source_id=source, stance=stance, asserted_at=week_date(week, 1), recorded_week=week
    )


def _span(
    status: ClaimStatus,
    *,
    from_week: int,
    to_week: int | None = None,
    recorded: int,
    kind: SpanCauseKind,
    by: str,
) -> StatusSpan:
    return StatusSpan(
        status=status,
        valid_from=week_date(from_week, 0),
        valid_to=None if to_week is None else week_date(to_week, 0),
        recorded_week=recorded,
        cause=SpanCause(kind=kind, by=by),
    )


def _claim(
    spans: list[StatusSpan],
    assertions: list[Assertion],
    *,
    claim_id: str = "CLM-AAAA0001",
    superseded_by: str | None = None,
    derived_from: list[str] | None = None,
) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        subject="ENT-00000001",
        predicate="policy_of",
        object=TypedValue(kind="text", value="remote-first"),
        assertions=assertions,
        status_timeline=spans,
        superseded_by=superseded_by,
        derived_from=derived_from or [],
    )


def _reversed_claim() -> ClaimRecord:
    """Decided in week 0, reversed (superseded) from week 6, learned in week 6."""

    return _claim(
        [
            _span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1),
            _span(
                ClaimStatus.SUPERSEDED,
                from_week=6,
                recorded=6,
                kind=SpanCauseKind.SUPERSESSION,
                by=OTHER_CLAIM,
            ),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0)],
        superseded_by=OTHER_CLAIM,
    )


def test_as_of_before_and_after_reversal() -> None:
    claim = _reversed_claim()
    before = oracle.truth_at(claim, oracle.world_cutoff(4), knowledge_week=8)
    after = oracle.truth_at(claim, oracle.world_cutoff(8), knowledge_week=8)
    assert before.status is ClaimStatus.CURRENT
    assert after.status is ClaimStatus.SUPERSEDED
    assert after.superseded_by == OTHER_CLAIM


def test_knowledge_cutoff_hides_the_future() -> None:
    claim = _reversed_claim()
    view = oracle.current_truth(claim, knowledge_week=4)
    assert view.status is ClaimStatus.CURRENT
    assert view.superseded_by is None


def test_boundary_instants_inclusive_from_exclusive_to() -> None:
    claim = _claim(
        [
            _span(
                ClaimStatus.CURRENT,
                from_week=0,
                to_week=5,
                recorded=0,
                kind=SpanCauseKind.INITIAL,
                by=S1,
            )
        ],
        [_assert_(S1, Stance.SUPPORTS, 0)],
    )
    assert oracle.truth_at(claim, week_date(0, 0), 8).status is ClaimStatus.CURRENT
    assert oracle.truth_at(claim, week_date(5, 0), 8).status is ClaimStatus.UNKNOWN


def test_expiring_fact_goes_unknown_after_valid_to() -> None:
    claim = _claim(
        [
            _span(
                ClaimStatus.CURRENT,
                from_week=0,
                to_week=5,
                recorded=0,
                kind=SpanCauseKind.INITIAL,
                by=S1,
            )
        ],
        [_assert_(S1, Stance.SUPPORTS, 0)],
    )
    assert oracle.current_truth(claim, knowledge_week=3).status is ClaimStatus.CURRENT
    assert oracle.current_truth(claim, knowledge_week=6).status is ClaimStatus.UNKNOWN


def test_late_evidence_rewrites_history_retroactively() -> None:
    claim = _claim(
        [
            _span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1),
            _span(
                ClaimStatus.DISPROVED, from_week=0, recorded=8, kind=SpanCauseKind.DISPROOF, by=S3
            ),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0), _assert_(S3, Stance.DISPUTES, 8)],
    )
    world_w1 = oracle.world_cutoff(1)
    assert oracle.truth_at(claim, world_w1, knowledge_week=4).status is ClaimStatus.CURRENT
    assert oracle.truth_at(claim, world_w1, knowledge_week=8).status is ClaimStatus.DISPROVED


def test_tentative_confirmed_and_disproved_paths() -> None:
    confirmed = _claim(
        [
            _span(
                ClaimStatus.TENTATIVE, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1
            ),
            _span(
                ClaimStatus.CONFIRMED,
                from_week=3,
                recorded=3,
                kind=SpanCauseKind.CONFIRMATION,
                by=S2,
            ),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0), _assert_(S2, Stance.SUPPORTS, 3)],
    )
    assert oracle.current_truth(confirmed, 2).status is ClaimStatus.TENTATIVE
    assert oracle.current_truth(confirmed, 4).status is ClaimStatus.CONFIRMED

    disproved = _claim(
        [
            _span(
                ClaimStatus.TENTATIVE, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1
            ),
            _span(
                ClaimStatus.DISPROVED, from_week=3, recorded=3, kind=SpanCauseKind.DISPROOF, by=S2
            ),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0), _assert_(S2, Stance.DISPUTES, 3)],
    )
    assert oracle.current_truth(disproved, 4).status is ClaimStatus.DISPROVED
    assert not oracle.current_truth(disproved, 4).is_active


def test_equal_authority_dispute_remains_disputed_with_both_sides() -> None:
    claim = _claim(
        [
            _span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1),
            _span(ClaimStatus.DISPUTED, from_week=4, recorded=4, kind=SpanCauseKind.DISPUTE, by=S4),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0), _assert_(S4, Stance.DISPUTES, 4)],
    )
    view = oracle.current_truth(claim, 5)
    assert view.status is ClaimStatus.DISPUTED
    assert view.supporting == (S1,)
    assert view.disputing == (S4,)
    assert view.is_active  # disputed is unresolved, not resolved-away


def test_evolution_orders_belief_history() -> None:
    claim = _reversed_claim()
    history = [v.status for v in oracle.evolution(claim, knowledge_week=8)]
    assert history == [ClaimStatus.CURRENT, ClaimStatus.SUPERSEDED]
    assert [v.status for v in oracle.evolution(claim, knowledge_week=4)] == [ClaimStatus.CURRENT]


def test_required_citations_expand_transitively() -> None:
    parent = _claim(
        [_span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1)],
        [_assert_(S1, Stance.SUPPORTS, 0)],
        claim_id=OTHER_CLAIM,
    )
    derived = _claim(
        [_span(ClaimStatus.CURRENT, from_week=1, recorded=1, kind=SpanCauseKind.INITIAL, by=S2)],
        [_assert_(S2, Stance.SUPPORTS, 1)],
        derived_from=[OTHER_CLAIM],
    )
    cited = oracle.required_citations(
        derived,
        oracle.current_truth(derived, 4),
        claims_by_id={OTHER_CLAIM: parent},
        knowledge_week=4,
    )
    assert cited == (S2, S1)
    shallow = oracle.required_citations(
        derived,
        oracle.current_truth(derived, 4),
        claims_by_id={OTHER_CLAIM: parent},
        knowledge_week=4,
        transitive=False,
    )
    assert shallow == (S2,)


def test_what_changed_names_the_cause() -> None:
    claim = _reversed_claim()
    change = oracle.what_changed(claim, oracle.world_cutoff(4), oracle.world_cutoff(8), 8)
    assert change.changed
    assert change.causes == (OTHER_CLAIM,)


def test_visibility_rules_declassification_and_tombstones() -> None:
    policy = PolicySet(
        audiences=["owner", "board", "team"],
        personas=[
            Persona(persona_id="owner", audiences=["owner", "board", "team"]),
            Persona(persona_id="assistant", audiences=["team"]),
        ],
        rules=[
            PolicyRule(
                rule_id="board-only",
                target_claims=["CLM-AAAA0001"],
                allow=["board"],
                declassify_at=week_date(8, 0),
            )
        ],
        tombstones=[TombstoneRequest(target_sources=[S1], requested_at=week_date(6, 0))],
    )
    early = oracle.visibility(
        policy, "assistant", claim_id="CLM-AAAA0001", at=week_date(4, 0)
    )
    assert not early.allowed and early.withhold_notice and early.rule_id == "board-only"
    declassified = oracle.visibility(
        policy, "assistant", claim_id="CLM-AAAA0001", at=week_date(9, 0)
    )
    assert declassified.allowed
    board = oracle.visibility(policy, "owner", claim_id="CLM-AAAA0001", at=week_date(4, 0))
    assert board.allowed
    tombstoned = oracle.visibility(policy, "owner", source_id=S1, at=week_date(7, 0))
    assert tombstoned.tombstoned and not tombstoned.allowed
    pre_tombstone = oracle.visibility(policy, "owner", source_id=S1, at=week_date(5, 0))
    assert pre_tombstone.allowed


def test_lint_rejects_unjustified_and_incompatible_spans() -> None:
    known = frozenset({S1, S2, OTHER_CLAIM, "CLM-AAAA0001"})
    unjustified = _claim(
        [
            _span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1),
            StatusSpan(
                status=ClaimStatus.SUPERSEDED,
                valid_from=date(2025, 2, 1),
                recorded_week=4,
                cause=SpanCause(kind=SpanCauseKind.SUPERSESSION, by=None),
            ),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0)],
        superseded_by=OTHER_CLAIM,
    )
    errors = oracle.lint_claim(unjustified, known)
    assert any("no justifying reference" in e for e in errors)

    wrong_kind = _claim(
        [
            _span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1),
            _span(
                ClaimStatus.REVOKED, from_week=2, recorded=2, kind=SpanCauseKind.DISPUTE, by=S2
            ),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0), _assert_(S2, Stance.RETRACTS, 2)],
    )
    assert any("cannot be caused by" in e for e in oracle.lint_claim(wrong_kind, known))

    missing_pointer = _reversed_claim().model_copy(update={"superseded_by": None})
    assert any("superseded_by is unset" in e for e in oracle.lint_claim(missing_pointer, known))

    foreign_source = _claim(
        [_span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S3)],
        [_assert_(S1, Stance.SUPPORTS, 0)],
    )
    assert any("missing from assertions" in e for e in oracle.lint_claim(foreign_source, known | {S3}))

    twin_initials = _claim(
        [
            _span(ClaimStatus.CURRENT, from_week=0, recorded=0, kind=SpanCauseKind.INITIAL, by=S1),
            _span(ClaimStatus.CURRENT, from_week=1, recorded=1, kind=SpanCauseKind.INITIAL, by=S1),
        ],
        [_assert_(S1, Stance.SUPPORTS, 0)],
    )
    assert any("exactly one initial" in e for e in oracle.lint_claim(twin_initials, known))


def test_lint_accepts_a_well_formed_corpus() -> None:
    claim = _reversed_claim()
    other = _claim(
        [_span(ClaimStatus.CURRENT, from_week=6, recorded=6, kind=SpanCauseKind.INITIAL, by=S2)],
        [_assert_(S2, Stance.SUPPORTS, 6)],
        claim_id=OTHER_CLAIM,
    )
    sources = []
    from membench.schema import ArtifactKind, AuthorityTier, SourceRecord

    for sid in (S1, S2):
        sources.append(
            SourceRecord(
                source_id=sid,
                title=f"note {sid}",
                artifact_kind=ArtifactKind.MARKDOWN,
                path=f"sources/{sid}.md",
                authority=AuthorityTier.FIRSTHAND,
                event_time=week_date(0, 1),
                recorded_week=0,
            )
        )
    assert oracle.lint_corpus([claim, other], sources) == []
