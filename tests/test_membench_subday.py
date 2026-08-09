"""Sub-day knowledge time: four-valued order, visibility, and the t23 family.

Three things are pinned here, in the order they matter:

1. the finer axis **refines** the week rule rather than replacing it, so no
   v0.1–v0.2 verdict and no v0.1–v0.2 byte moves;
2. an order the recorded instants do not determine is reported as such and
   never guessed — including where the oracle's own legacy fallback would
   otherwise hide it;
3. the family actually discriminates: the answer to its central query cannot
   be reached without the intra-day order, and is not reachable by a heuristic
   over the values either.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from membench import families, oracle
from membench.clock import week_date
from membench.generate import GenerationError, generate_corpus
from membench.schema import (
    WEEK_SECONDS,
    Assertion,
    ClaimRecord,
    ClaimStatus,
    CorpusManifest,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
    load_jsonl,
)
from membench.templates import registry
from membench.templates.t23_sub_day_temporality import READINGS, TEMPLATE_ID

FAMILY = "sub_day_temporality"


def _assertion(week: int, offset: int | None = None, source: str = "SRC-A") -> Assertion:
    return Assertion(
        source_id=source,
        stance=Stance.SUPPORTS,
        asserted_at=week_date(week, 1),
        recorded_week=week,
        recorded_offset_s=offset,
    )


def _span(
    week: int,
    offset: int | None = None,
    *,
    status: ClaimStatus = ClaimStatus.CURRENT,
    kind: SpanCauseKind = SpanCauseKind.INITIAL,
    by: str = "SRC-A",
    from_week: int = 0,
) -> StatusSpan:
    return StatusSpan(
        status=status,
        valid_from=week_date(from_week, 0),
        recorded_week=week,
        recorded_offset_s=offset,
        cause=SpanCause(kind=kind, by=by),
    )


def _claim(claim_id: str, value: str, assertions: list[Assertion], spans: list[StatusSpan]):
    return ClaimRecord(
        claim_id=claim_id,
        subject="ENT-1",
        predicate="reading",
        object=TypedValue(kind="quantity", value=value, unit="points"),
        assertions=assertions,
        status_timeline=spans,
    )


# -- the comparison itself --------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ((3, None), (4, None), oracle.Order.BEFORE),
        ((4, None), (3, 500), oracle.Order.AFTER),
        ((3, 100), (3, 900), oracle.Order.BEFORE),
        ((3, 900), (3, 100), oracle.Order.AFTER),
        ((3, 900), (3, 900), oracle.Order.SAME),
        # Same week, one instant never captured: the unknown ranges over the
        # whole week, so nothing orders them. This is the row the product's
        # own comparison table calls indeterminate.
        ((3, None), (3, 900), oracle.Order.INDETERMINATE),
        ((3, 900), (3, None), oracle.Order.INDETERMINATE),
        ((3, None), (3, None), oracle.Order.INDETERMINATE),
    ],
)
def test_compare_recorded_is_four_valued(left, right, expected) -> None:  # type: ignore[no-untyped-def]
    assert oracle.compare_recorded(_assertion(*left), _assertion(*right)) is expected


def test_indeterminate_is_not_a_disguised_equality() -> None:
    """SAME and INDETERMINATE are different answers and must stay different.

    Collapsing them would make "logged in the same second" and "we never wrote
    down the time" the same fact, which is the precision loss this whole axis
    exists to stop.
    """

    assert oracle.compare_recorded(_assertion(3, 900), _assertion(3, 900)) is oracle.Order.SAME
    assert (
        oracle.compare_recorded(_assertion(3, None), _assertion(3, None))
        is oracle.Order.INDETERMINATE
    )


# -- visibility: the old rule is the new rule's default case ----------------


@pytest.mark.parametrize("offset", [None, 0, 1, WEEK_SECONDS - 1])
@pytest.mark.parametrize(("week", "knowledge_week"), [(0, 0), (3, 3), (3, 4), (4, 3), (0, 11)])
def test_week_visibility_is_unchanged_at_every_precision(
    offset: int | None, week: int, knowledge_week: int
) -> None:
    """``recorded_week <= knowledge_week`` still decides, whatever was captured."""

    assert oracle.is_recorded_by(_assertion(week, offset), knowledge_week) is (
        week <= knowledge_week
    )


def test_sub_day_cutoff_refuses_to_guess_about_an_uncaptured_instant() -> None:
    """A record with no captured instant is not provably before a mid-week cutoff."""

    cutoff = 3 * 24 * 3600
    assert oracle.is_recorded_by(_assertion(2, cutoff - 1), 2, cutoff) is True
    assert oracle.is_recorded_by(_assertion(2, cutoff), 2, cutoff) is True
    assert oracle.is_recorded_by(_assertion(2, cutoff + 1), 2, cutoff) is False
    assert oracle.is_recorded_by(_assertion(2, None), 2, cutoff) is False
    # Earlier weeks stay visible regardless: the whole week precedes the cutoff.
    assert oracle.is_recorded_by(_assertion(1, None), 2, cutoff) is True


# -- truth resolution -------------------------------------------------------


def test_same_week_spans_are_resolved_by_instant_not_row_order() -> None:
    """Instants decide, and reversing the rows does not change the verdict."""

    late = _span(3, 40_000, status=ClaimStatus.REVOKED, kind=SpanCauseKind.RETRACTION, by="SRC-B")
    early = _span(3, 10_000)
    forward = _claim("CLM-1", "5", [_assertion(3, 10_000)], [early, late])
    reversed_rows = _claim("CLM-1", "5", [_assertion(3, 10_000)], [late, early])
    for claim in (forward, reversed_rows):
        view = oracle.current_truth(claim, 5)
        assert view.status is ClaimStatus.REVOKED
        assert view.resolved_by_authoring_order is False


def test_authoring_order_fallback_is_reported_not_hidden() -> None:
    """Where nothing was captured the legacy rule still applies — and says so."""

    claim = _claim(
        "CLM-2",
        "5",
        [_assertion(3)],
        [
            _span(3),
            _span(3, status=ClaimStatus.DISPUTED, kind=SpanCauseKind.DISPUTE, by="SRC-B"),
        ],
    )
    view = oracle.current_truth(claim, 5)
    assert view.status is ClaimStatus.DISPUTED
    assert view.resolved_by_authoring_order is True
    assert oracle.positional_resolutions([claim], knowledge_week=5) == ("CLM-2",)


# -- latest_recorded: the family's ground truth -----------------------------


def test_latest_recorded_picks_the_last_instant() -> None:
    claims = [
        _claim(f"CLM-{i}", str(i), [_assertion(3, 10_000 + i)], [_span(3, 10_000 + i)])
        for i in range(4)
    ]
    assert oracle.latest_recorded(claims, knowledge_week=5).claim_id == "CLM-3"


@pytest.mark.parametrize("offsets", [(900, 900), (None, None), (None, 900)])
def test_latest_recorded_returns_none_when_the_data_cannot_decide(offsets) -> None:  # type: ignore[no-untyped-def]
    """Same second, no instants, or mixed precision inside one week: no answer."""

    claims = [
        _claim(f"CLM-{i}", str(i), [_assertion(3, off)], [_span(3, off)])
        for i, off in enumerate(offsets)
    ]
    assert oracle.latest_recorded(claims, knowledge_week=5) is None


def test_latest_recorded_honours_the_knowledge_cutoff() -> None:
    early = _claim("CLM-E", "1", [_assertion(3, 100)], [_span(3, 100)])
    late = _claim("CLM-L", "2", [_assertion(9, 100)], [_span(9, 100)])
    assert oracle.latest_recorded([early, late], knowledge_week=11).claim_id == "CLM-L"
    assert oracle.latest_recorded([early, late], knowledge_week=5).claim_id == "CLM-E"
    assert oracle.latest_recorded([early, late], knowledge_week=1) is None


# -- generation-time refusals ----------------------------------------------


def test_lint_refuses_a_stamped_claim_whose_spans_cannot_be_ordered() -> None:
    """Opting into instants forbids relying on the authoring-order fallback."""

    claim = _claim(
        "CLM-3",
        "5",
        [_assertion(3, 900), _assertion(3, 900, source="SRC-B")],
        [
            _span(3, 900),
            _span(3, 900, status=ClaimStatus.DISPUTED, kind=SpanCauseKind.DISPUTE, by="SRC-B"),
        ],
    )
    errors = oracle.lint_claim(claim, frozenset({"SRC-A", "SRC-B", "CLM-3"}))
    assert any("row order, not by data" in e for e in errors)


def test_lint_leaves_unstamped_claims_alone() -> None:
    """The refusal is scoped to claims that captured instants; v0.1 still builds."""

    claim = _claim(
        "CLM-4",
        "5",
        [_assertion(3), _assertion(3, source="SRC-B")],
        [
            _span(3),
            _span(3, status=ClaimStatus.DISPUTED, kind=SpanCauseKind.DISPUTE, by="SRC-B"),
        ],
    )
    assert oracle.lint_claim(claim, frozenset({"SRC-A", "SRC-B", "CLM-4"})) == []


def _stamped_source(week: int, offset: int | None, source_id: str) -> SourceRecord:
    from membench.schema import ArtifactKind, AuthorityTier

    return SourceRecord(
        source_id=source_id,
        title="log",
        artifact_kind=ArtifactKind.MARKDOWN,
        path="",
        authority=AuthorityTier.OFFICIAL,
        event_time=week_date(week, 1),
        recorded_week=week,
        recorded_offset_s=offset,
    )


def test_ingestion_order_lint_refuses_instants_that_contradict_the_stream() -> None:
    from membench.generate import _lint_ingestion_order
    from membench.schema import ScheduleOp, ScheduleOpKind

    sources = {
        "SRC-1": _stamped_source(3, 40_000, "SRC-1"),
        "SRC-2": _stamped_source(3, 10_000, "SRC-2"),
    }
    schedule = [
        ScheduleOp(week=3, seq=0, op=ScheduleOpKind.INGEST_SOURCE, source_id="SRC-1"),
        ScheduleOp(week=3, seq=1, op=ScheduleOpKind.INGEST_SOURCE, source_id="SRC-2"),
    ]
    assert _lint_ingestion_order(schedule, sources)
    # Uncaptured instants constrain nothing: they cannot be out of order.
    sources["SRC-2"] = _stamped_source(3, None, "SRC-2")
    assert _lint_ingestion_order(schedule, sources) == []


def test_expectation_refuses_when_the_template_contradicts_the_oracle(
    tmp_path: Path,
) -> None:
    """A template naming the wrong latest reading must not generate.

    The expected answer is derived from the oracle's ordering, so a template
    that disagrees with it is a bug in the template, caught before bytes land.
    """

    from membench.templates.base import Template
    from membench.templates.t23_sub_day_temporality import expect_latest_recorded

    def build(ctx) -> None:  # type: ignore[no-untyped-def]
        entity = ctx.entity("project", "operations")
        claims = []
        for index, offset in enumerate((10_000, 20_000)):
            source = ctx.source(3, f"log {index}", lines=[f"value {index}"])
            claim = ctx.claim(
                entity,
                "reading",
                TypedValue(kind="quantity", value=str(index), unit="points"),
                source,
            )
            source.recorded_offset_s = offset
            claim.assertions[0].recorded_offset_s = offset
            claim.status_timeline[0].recorded_offset_s = offset
            claims.append(claim)
        # Deliberately wrong: claims[0] was recorded first.
        ctx.query(
            "same_day_latest",
            "Which reading is current?",
            knowledge_week=11,
            family=FAMILY,
            expect=expect_latest_recorded(claims, latest=claims[0]),
        )

    probe = Template(
        template_id="t97_subday_probe",
        family=FAMILY,
        summary="oracle contradiction probe",
        variants=1,
        build=build,
    )
    with pytest.raises(GenerationError, match="latest recorded, oracle resolves"):
        generate_corpus(1, tmp_path / "corpus", templates={probe.template_id: probe})


# -- the shipped family -----------------------------------------------------


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, CorpusManifest]:
    root = tmp_path_factory.mktemp("subday") / "corpus"
    manifest = generate_corpus(1, root, template_ids=[TEMPLATE_ID])
    return root, manifest


def _family_rows(root: Path) -> tuple[list[QueryRecord], dict[str, ExpectedRecord], dict]:
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    return queries, expected, claims


def test_family_is_registered_active() -> None:
    entry = families.registry()[FAMILY]
    assert entry.status == "active"
    assert entry.classification == "deterministic-oracle"
    assert registry()[TEMPLATE_ID].family == FAMILY


def test_every_query_carries_the_family(corpus: tuple[Path, CorpusManifest]) -> None:
    root, _ = corpus
    queries, _, _ = _family_rows(root)
    assert queries
    assert {q.family for q in queries} == {FAMILY}


def test_same_day_answer_needs_the_intra_day_order(
    corpus: tuple[Path, CorpusManifest],
) -> None:
    """The decisive property: day granularity cannot reach the expected value.

    Every candidate shares a recorded week *and* a recorded day, so a store
    that keeps knowledge time to the day holds READINGS indistinguishable
    figures. Re-running the oracle with the instants erased must produce no
    answer at all — if it still produced one, some other signal was leaking
    the order and the family would be measuring that instead.
    """

    root, _ = corpus
    queries, expected, claims = _family_rows(root)
    asks = [q for q in queries if q.query_kind == "same_day_latest"]
    assert asks
    for query in asks:
        record = expected[query.query_id]
        candidates = [
            claims[cid] for cid in (*record.required_claims, *record.forbidden_claims)
        ]
        assert len(candidates) == READINGS
        recorded_days = {
            (a.recorded_week, a.recorded_offset_s // (24 * 3600))
            for claim in candidates
            for a in claim.assertions
            if a.stance is Stance.SUPPORTS
        }
        assert len(recorded_days) == 1, "candidates must share one calendar day"
        day_blind = [
            claim.model_copy(
                deep=True,
                update={
                    "assertions": [
                        a.model_copy(update={"recorded_offset_s": None})
                        for a in claim.assertions
                    ]
                },
            )
            for claim in candidates
        ]
        assert (
            oracle.latest_recorded(day_blind, knowledge_week=query.ask.knowledge_week) is None
        )


def test_no_value_heuristic_reaches_the_same_day_answer(
    corpus: tuple[Path, CorpusManifest],
) -> None:
    """Not the largest, not the smallest, and not the first or last on the page.

    A guess has to be a guess. ``claims.jsonl`` row order is the one ordering a
    contender never sees (it receives an ingestion stream, not the file), so
    the check that matters is that the *values* carry no shortcut.
    """

    root, _ = corpus
    queries, expected, claims = _family_rows(root)
    for query in (q for q in queries if q.query_kind == "same_day_latest"):
        record = expected[query.query_id]
        values = [
            int(claims[cid].object.value)
            for cid in (*record.required_claims, *record.forbidden_claims)
        ]
        answer = int(record.answer.values[0])
        assert answer != max(values)
        assert answer != min(values)


def test_same_instant_query_expects_abstention(corpus: tuple[Path, CorpusManifest]) -> None:
    """Two entries in the same second: no answer exists, so none may be given."""

    root, _ = corpus
    queries, expected, _ = _family_rows(root)
    asks = [q for q in queries if q.query_kind == "same_instant_indeterminate"]
    assert asks
    for query in asks:
        record = expected[query.query_id]
        assert record.abstain is True
        assert record.answer.kind == "none"
        assert record.answer.values == []


def test_control_query_is_answerable_at_day_granularity(
    corpus: tuple[Path, CorpusManifest],
) -> None:
    """Blanket abstention must not look like calibration.

    The control's two records sit weeks apart, so *every* contender can order
    them. A system that abstains everywhere fails here.
    """

    root, _ = corpus
    queries, expected, claims = _family_rows(root)
    asks = [q for q in queries if q.query_kind == "cross_day_latest"]
    assert asks
    for query in asks:
        record = expected[query.query_id]
        assert record.abstain is False
        assert record.answer.values
        candidates = [
            claims[cid] for cid in (*record.required_claims, *record.forbidden_claims)
        ]
        weeks = {a.recorded_week for c in candidates for a in c.assertions}
        assert len(weeks) > 1, "the control must be orderable without instants"
        day_blind = [
            claim.model_copy(
                deep=True,
                update={
                    "assertions": [
                        a.model_copy(update={"recorded_offset_s": None})
                        for a in claim.assertions
                    ]
                },
            )
            for claim in candidates
        ]
        resolved = oracle.latest_recorded(day_blind, knowledge_week=query.ask.knowledge_week)
        assert resolved is not None
        assert resolved.claim_id == record.required_claims[0]


def test_family_claims_never_depend_on_authoring_order(
    corpus: tuple[Path, CorpusManifest],
) -> None:
    root, _ = corpus
    _, _, claims = _family_rows(root)
    assert oracle.positional_resolutions(claims.values(), knowledge_week=11) == ()


def test_artifacts_carry_no_clock(corpus: tuple[Path, CorpusManifest]) -> None:
    """The order must not leak into the text, or this becomes a reading test."""

    root, _ = corpus
    for path in sorted((root / "sources").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert ":" not in text.replace("[ref:", ""), f"{path.name} looks like it carries a time"


def test_suite_wide_authoring_order_debt_does_not_grow(tmp_path: Path) -> None:
    """A ratchet on the defect this axis exposes.

    ``t08_equal_authority_dispute`` records a claim and the dispute against it
    in the same week with no instants, so which span is current comes from row
    order — four claims, one per variant. That is pre-existing and out of this
    change's scope to rewrite, but nothing new may join them.
    """

    generate_corpus(1, tmp_path / "corpus")
    claims = load_jsonl(ClaimRecord, tmp_path / "corpus" / "claims.jsonl")
    assert len(oracle.positional_resolutions(claims, knowledge_week=11)) == 4


def test_schema_export_describes_the_finer_axis(tmp_path: Path) -> None:
    from membench.schema import export_json_schemas

    export_json_schemas(tmp_path)
    claim_schema = json.loads((tmp_path / "claim.schema.json").read_text(encoding="utf-8"))
    offset = claim_schema["$defs"]["Assertion"]["properties"]["recorded_offset_s"]
    assert offset["default"] is None
    assert {"type": "null"} in offset["anyOf"]


def test_unknown_instant_is_absent_from_serialised_records() -> None:
    """Absence, not ``null``: the precision was never captured, not recorded as missing."""

    payload = json.loads(_assertion(3).model_dump_json(exclude_none=False))
    assert "recorded_offset_s" not in payload
    stamped = json.loads(_assertion(3, 900).model_dump_json(exclude_none=False))
    assert stamped["recorded_offset_s"] == 900


def test_offset_is_bounded_to_its_week() -> None:
    with pytest.raises(ValueError):
        _assertion(3, WEEK_SECONDS)
    with pytest.raises(ValueError):
        _assertion(3, -1)


def test_date_typing_is_untouched() -> None:
    """World time stays a calendar date; only knowledge time gained precision."""

    assert isinstance(_span(3, 900).valid_from, date)
