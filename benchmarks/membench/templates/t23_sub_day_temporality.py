"""T23 — a day's worth of readings, ordered only by when each one landed.

The discriminating shape. A monitoring desk logs the same metric six times on
one calendar day. The entries are textually identical apart from the figure,
they share an event date, and nothing inside any artifact says which came
first. The only record of their order is *when each one was ingested* — so a
memory system can answer "what is the current figure?" exactly when it stamped
its own knowledge time finely enough to separate them, and cannot when it
stamped only the day. That is the product capability this template exists to
measure, and the reason the benchmark could not measure it before: corpus
knowledge time was a week index, which cannot express order inside a day.

Why the artifacts carry no clock. Printing "logged 14:03:22" in the source
text would make this a reading-comprehension query that any retriever passes,
and it would measure nothing about knowledge time. The ordering therefore
reaches a contender only through the ingestion stream, which the generator
pins to the declared instants (``generate._lint_ingestion_order``).

Three asks per variant, and each is load-bearing:

- **same_day_latest** — the discriminator. Six candidate figures, ordered by
  captured instants alone.
- **same_instant_indeterminate** — two entries logged in the *same second*.
  Nothing determines which is current, so the oracle returns no answer and
  abstention is the expected behaviour. Without it a contender could guess its
  way through the family and look calibrated; with it, guessing costs.
- **cross_day_latest** — a correction a week apart, which day granularity
  orders perfectly well. Without it, abstaining on everything would look like
  the same honest behaviour as knowing when to abstain.

Values are shuffled so the current figure is neither the largest nor the
smallest, and neither is the day's first: "pick the extreme" is not a shortcut
past the ordering.
"""

from __future__ import annotations

from membench import oracle
from membench.clock import week_date
from membench.ids import slugify
from membench.schema import (
    AuthorityTier,
    ClaimRecord,
    ClaimStatus,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    SpanCause,
    SpanCauseKind,
    StatusSpan,
    TypedValue,
)
from membench.templates.base import (
    BuildContext,
    ExpectationBuilder,
    GenerationError,
    OracleCtx,
    Template,
    expect_value,
    register,
)

TEMPLATE_ID = "t23_sub_day_temporality"
FAMILY = "sub_day_temporality"

#: Entries in the same-day stream. The chance of naming the right current
#: figure without knowing the order is 1/READINGS.
READINGS = 6

_DAY_S = 24 * 60 * 60
#: Weekday the stream lands on, and the later weekday for the tied pair; the
#: tie must sit after the stream so the ingestion stream stays in instant order.
_STREAM_DAY = 2
_TIE_DAY = 4
#: Every variant gets its own ingestion week, so no two variants' captured
#: instants interleave in the shared schedule.
_STREAM_WEEKS = (3, 5, 7, 9)
_CONTROL_WEEKS = (1, 11)
_KNOWLEDGE_WEEK = 11


def _stamp(source: SourceRecord, claim: ClaimRecord, offset_s: int) -> None:
    """Capture the intra-day instant on a source and the claim it opened.

    ``BuildContext`` authors knowledge time to the week, which is the right
    default for every template that has no sub-day story; this template is the
    one that does, so it stamps the three records the oracle's visibility rule
    reads after the standard helpers have built them.
    """

    source.recorded_offset_s = offset_s
    for assertion in claim.assertions:
        if assertion.source_id == source.source_id:
            assertion.recorded_offset_s = offset_s
    for span in claim.status_timeline:
        if span.cause.by == source.source_id:
            span.recorded_offset_s = offset_s


def _retire(old: ClaimRecord, new: ClaimRecord, *, week: int, offset_s: int) -> None:
    """Supersede ``old`` at a captured instant.

    ``BuildContext.supersede`` stamps the retiring span to the week only, which
    would leave this claim's two spans indeterminate against each other and be
    refused by :func:`membench.oracle.lint_claim`. The instant used is the
    successor's: the corpus learns the correction exactly when it learns the
    figure that causes it.
    """

    old.status_timeline.append(
        StatusSpan(
            status=ClaimStatus.SUPERSEDED,
            valid_from=week_date(week, 0),
            recorded_week=week,
            recorded_offset_s=offset_s,
            cause=SpanCause(kind=SpanCauseKind.SUPERSESSION, by=new.claim_id),
        )
    )
    old.superseded_by = new.claim_id
    new.supersedes = old.claim_id


def _reading_values(ctx: BuildContext, count: int) -> list[int]:
    """Distinct figures whose order on the page is not their order in time.

    The current figure (last) and the day's opening figure (first) are both
    kept off the extremes, so neither "the biggest number" nor "the smallest"
    is a way past the ordering.
    """

    values = ctx.rng.sample(range(120, 260), count)
    for _ in range(64):
        low, high = min(values), max(values)
        if values[-1] not in (low, high) and values[0] not in (low, high):
            return values
        ctx.rng.shuffle(values)
    raise GenerationError(f"{TEMPLATE_ID}: could not place the extremes off the endpoints")


def _instants(ctx: BuildContext, day: int, count: int) -> list[int]:
    """``count`` increasing instants inside ``day``, as seconds from the week."""

    seconds = sorted(ctx.rng.sample(range(8 * 3600, 18 * 3600), count))
    return [day * _DAY_S + second for second in seconds]


def _answer(value: TypedValue) -> ExpectedAnswer:
    kind = {"quantity": "value", "date": "date", "entity_ref": "entity", "text": "text"}[
        value.kind
    ]
    return ExpectedAnswer(kind=kind, values=[value.value], unit=value.unit)  # type: ignore[arg-type]


def expect_latest_recorded(
    candidates: list[ClaimRecord], *, latest: ClaimRecord
) -> ExpectationBuilder:
    """The figure the corpus learned last, as the oracle orders the candidates.

    The template says which claim it believes is current; the oracle recomputes
    it from the captured instants and generation fails if the two disagree, so
    the expected answer is derived, never asserted. It also refuses a winner
    the oracle could only pick by authoring order — that would be the very
    ambiguity this family exists to measure, resolved by an accident of row
    order.
    """

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        knowledge = query.ask.knowledge_week
        resolved = oracle.latest_recorded(candidates, knowledge_week=knowledge)
        if resolved is None:
            raise GenerationError(
                f"{query.query_id}: the oracle cannot order these records, so no "
                "single figure is current"
            )
        if resolved is not latest:
            raise GenerationError(
                f"{query.query_id}: template expects {latest.claim_id} to be the "
                f"latest recorded, oracle resolves {resolved.claim_id}"
            )
        view = oracle.current_truth(resolved, knowledge)
        if not view.is_active:
            raise GenerationError(
                f"{query.query_id}: latest-recorded {resolved.claim_id} is "
                f"{view.status.value}, not an active answer"
            )
        if view.resolved_by_authoring_order:
            raise GenerationError(
                f"{query.query_id}: {resolved.claim_id} is current only by authoring "
                "order; capture intra-day instants or do not ask this"
            )
        retired = [c for c in candidates if c is not resolved]
        still_active = [
            c.claim_id for c in retired if oracle.current_truth(c, knowledge).is_active
        ]
        if still_active:
            raise GenerationError(
                f"{query.query_id}: superseded candidates {still_active} are still "
                "active; the corpus offers more than one current figure"
            )
        citations = oracle.required_citations(
            resolved, view, claims_by_id=ctx.claims_by_id, knowledge_week=knowledge
        )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=_answer(view.value) if view.value is not None else ExpectedAnswer(kind="none"),
            required_claims=[resolved.claim_id],
            forbidden_claims=[c.claim_id for c in retired],
            required_citations=list(citations),
            gates=["current_state", "citations"],
        )

    return build


def expect_indeterminate_order(candidates: list[ClaimRecord]) -> ExpectationBuilder:
    """No answer, because the recorded instants coincide exactly.

    Abstention here is oracle-derived like everything else: the record is built
    only after :func:`membench.oracle.latest_recorded` reports that it cannot
    order the candidates. If a template ever made one of them determinate, the
    expectation would be telling a contender to abstain from a question the
    corpus answers, so generation fails instead.
    """

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        resolved = oracle.latest_recorded(candidates, knowledge_week=query.ask.knowledge_week)
        if resolved is not None:
            raise GenerationError(
                f"{query.query_id}: expected an order the corpus cannot determine, "
                f"but the oracle resolves {resolved.claim_id}"
            )
        _ = ctx
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="none"),
            abstain=True,
            gates=["abstention"],
        )

    return build


def build(ctx: BuildContext) -> None:
    desk = ctx.entity("organization", "operations")
    project = ctx.entity("project", "operations")
    stream_metric = ctx.metric()
    tie_metric = ctx.metric()
    while tie_metric == stream_metric:
        tie_metric = ctx.metric()
    control_metric = ctx.metric()
    while control_metric in {stream_metric, tie_metric}:
        control_metric = ctx.metric()

    week = _STREAM_WEEKS[ctx.variant % len(_STREAM_WEEKS)]
    log_title = f"{desk.canonical_name} monitoring log"

    # -- the same-day stream: six identical entries, six instants -----------
    values = _reading_values(ctx, READINGS)
    instants = _instants(ctx, _STREAM_DAY, READINGS)
    stream_predicate = slugify(stream_metric).replace("-", "_")
    readings: list[ClaimRecord] = []
    for value, offset_s in zip(values, instants, strict=True):
        source = ctx.source(
            week,
            log_title,
            authority=AuthorityTier.OFFICIAL,
            event_day=_STREAM_DAY,
            lines=[
                f"{desk.canonical_name} logs the {stream_metric} for "
                f"{project.canonical_name} at {value} points."
            ],
        )
        claim = ctx.claim(
            project,
            stream_predicate,
            TypedValue(kind="quantity", value=str(value), unit="points"),
            source,
        )
        _stamp(source, claim, offset_s)
        if readings:
            _retire(readings[-1], claim, week=week, offset_s=offset_s)
        readings.append(claim)

    # -- the tied pair: two entries logged in the same second ---------------
    tie_offset = _TIE_DAY * _DAY_S + ctx.rng.randrange(9 * 3600, 16 * 3600)
    tie_values = ctx.rng.sample(range(300, 400), 2)
    tie_predicate = slugify(tie_metric).replace("-", "_")
    tied: list[ClaimRecord] = []
    for value in tie_values:
        source = ctx.source(
            week,
            log_title,
            authority=AuthorityTier.OFFICIAL,
            event_day=_TIE_DAY,
            lines=[
                f"{desk.canonical_name} logs the {tie_metric} for "
                f"{project.canonical_name} at {value} points."
            ],
        )
        claim = ctx.claim(
            project,
            tie_predicate,
            TypedValue(kind="quantity", value=str(value), unit="points"),
            source,
        )
        _stamp(source, claim, tie_offset)
        tied.append(claim)

    # -- the control: a correction a week apart, orderable by day alone -----
    control_predicate = slugify(control_metric).replace("-", "_")
    control_values = ctx.rng.sample(range(400, 500), 2)
    first_week, second_week = _CONTROL_WEEKS
    s_control_old = ctx.source(
        first_week,
        f"{desk.canonical_name} quarterly return",
        authority=AuthorityTier.SYSTEM_OF_RECORD,
        lines=[
            f"{desk.canonical_name} returns the {control_metric} for "
            f"{project.canonical_name} as {control_values[0]} points."
        ],
    )
    c_control_old = ctx.claim(
        project,
        control_predicate,
        TypedValue(kind="quantity", value=str(control_values[0]), unit="points"),
        s_control_old,
    )
    s_control_new = ctx.source(
        second_week,
        f"{desk.canonical_name} quarterly return",
        authority=AuthorityTier.SYSTEM_OF_RECORD,
        supersedes_source=s_control_old.source_id,
        version=2,
        lines=[
            f"{desk.canonical_name} restates the {control_metric} for "
            f"{project.canonical_name} as {control_values[1]} points."
        ],
    )
    c_control_new = ctx.claim(
        project,
        control_predicate,
        TypedValue(kind="quantity", value=str(control_values[1]), unit="points"),
        s_control_new,
    )
    ctx.supersede(c_control_old, c_control_new, week=second_week)

    ctx.query(
        "same_day_latest",
        f"What is the current {stream_metric} for {project.canonical_name}?",
        knowledge_week=_KNOWLEDGE_WEEK,
        family=FAMILY,
        expect=expect_latest_recorded(readings, latest=readings[-1]),
    )
    ctx.query(
        "same_instant_indeterminate",
        f"What is the current {tie_metric} for {project.canonical_name}?",
        knowledge_week=_KNOWLEDGE_WEEK,
        family=FAMILY,
        expect=expect_indeterminate_order(tied),
    )
    ctx.query(
        "cross_day_latest",
        f"What is the current {control_metric} for {project.canonical_name}?",
        knowledge_week=_KNOWLEDGE_WEEK,
        family=FAMILY,
        expect=expect_value(c_control_new, forbidden=[c_control_old]),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Same-day reading stream ordered only by captured intra-day instants",
        variants=len(_STREAM_WEEKS),
        build=build,
    )
)
