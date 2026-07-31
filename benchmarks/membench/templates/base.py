"""Template protocol: declarative scenario authoring with oracle-derived
expectations.

A template module declares entities, sources, claims, lifecycle transitions,
schedule ops, and queries against a :class:`BuildContext`. Expected records
are never hand-written: ``ctx.query`` takes an expectation *builder* produced
by the ``expect_*`` helpers, and every builder is evaluated at finalize time
through :mod:`membench.oracle` over the finished corpus.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from random import Random

from membench import oracle, wordbank
from membench.clock import week_date
from membench.ids import child_seed, stable_id
from membench.schema import (
    AdversarialFlags,
    ArtifactKind,
    Assertion,
    AuthorityTier,
    ClaimRecord,
    ClaimStatus,
    EntityRecord,
    EventRecord,
    ExpectedAnswer,
    ExpectedRecord,
    PolicySet,
    QueryRecord,
    ScheduleOp,
    ScheduleOpKind,
    SourceRecord,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
    UncertaintyExpectation,
)


class GenerationError(RuntimeError):
    """Raised when a template authors an inconsistent scenario."""


@dataclass
class SourceContent:
    """Logical content of one source artifact (identity for hashing)."""

    kind: ArtifactKind
    title: str
    lines: list[str] = field(default_factory=list)
    table: list[list[str]] | None = None


@dataclass
class ScenarioGraph:
    entities: list[EntityRecord] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)
    claims: list[ClaimRecord] = field(default_factory=list)
    queries: list[QueryRecord] = field(default_factory=list)
    schedule: list[ScheduleOp] = field(default_factory=list)
    policy: PolicySet = field(default_factory=PolicySet)
    contents: dict[str, SourceContent] = field(default_factory=dict)
    expectation_builders: dict[str, ExpectationBuilder] = field(default_factory=dict)

    def claims_by_id(self) -> dict[str, ClaimRecord]:
        return {c.claim_id: c for c in self.claims}


@dataclass(frozen=True)
class OracleCtx:
    """What expectation builders may consult at finalize time."""

    graph: ScenarioGraph
    claims_by_id: dict[str, ClaimRecord]


ExpectationBuilder = Callable[[OracleCtx, QueryRecord], ExpectedRecord]


@dataclass(frozen=True)
class Template:
    template_id: str
    family: str
    summary: str
    variants: int
    build: Callable[["BuildContext"], None]


class BuildContext:
    """Authoring surface handed to a template's ``build`` function."""

    def __init__(self, template_id: str, variant: int, master_seed: int) -> None:
        self.template_id = template_id
        self.variant = variant
        self.rng = Random(child_seed(master_seed, template_id, variant))
        self.graph = ScenarioGraph()
        self._counter = 0

    # -- identity helpers -------------------------------------------------
    def _next(self) -> str:
        self._counter += 1
        return f"{self.template_id}-v{self.variant}-{self._counter:03d}"

    def _id(self, prefix: str) -> str:
        return stable_id(prefix, self._next())

    # -- vocabulary -------------------------------------------------------
    def person(self) -> str:
        return wordbank.person_name(self.rng)

    def org(self) -> str:
        return wordbank.org_name(self.rng)

    def project(self) -> str:
        return wordbank.project_name(self.rng)

    def metric(self) -> str:
        return wordbank.metric_name(self.rng)

    # -- records ----------------------------------------------------------
    def entity(
        self,
        kind: str,
        domain: str,
        name: str | None = None,
        *,
        aliases: list[str] | None = None,
    ) -> EntityRecord:
        if name is None:
            name = {
                "person": self.person,
                "organization": self.org,
                "project": self.project,
            }.get(kind, lambda: wordbank.noun(self.rng).title())()
        record = EntityRecord(
            entity_id=self._id("ENT"),
            kind=kind,  # type: ignore[arg-type]
            domain=domain,
            canonical_name=name,
            aliases=aliases or [],
        )
        self.graph.entities.append(record)
        return record

    def source(
        self,
        week: int,
        title: str,
        *,
        authority: AuthorityTier = AuthorityTier.FIRSTHAND,
        kind: ArtifactKind = ArtifactKind.MARKDOWN,
        lines: list[str] | None = None,
        table: list[list[str]] | None = None,
        audiences: list[str] | None = None,
        event_day: int = 1,
        adversarial: AdversarialFlags | None = None,
        supersedes_source: str | None = None,
        version: int = 1,
        schedule_op: ScheduleOpKind = ScheduleOpKind.INGEST_SOURCE,
    ) -> SourceRecord:
        source_id = self._id("SRC")
        record = SourceRecord(
            source_id=source_id,
            title=title,
            version=version,
            supersedes_source=supersedes_source,
            artifact_kind=kind,
            path="",  # assigned by the generator when the artifact is rendered
            authority=authority,
            event_time=week_date(week, event_day),
            recorded_week=week,
            audiences=audiences or [],
            adversarial=adversarial or AdversarialFlags(),
        )
        self.graph.sources.append(record)
        self.graph.contents[source_id] = SourceContent(
            kind=kind, title=title, lines=list(lines or []), table=table
        )
        self.graph.schedule.append(
            ScheduleOp(
                week=week, seq=len(self.graph.schedule), op=schedule_op, source_id=source_id
            )
        )
        return record

    def claim(
        self,
        subject: EntityRecord,
        predicate: str,
        value: TypedValue | str,
        source: SourceRecord,
        *,
        week: int | None = None,
        status: ClaimStatus = ClaimStatus.CURRENT,
        valid_to_week: int | None = None,
        audiences: list[str] | None = None,
        derived_from: list[str] | None = None,
    ) -> ClaimRecord:
        if isinstance(value, str):
            value = TypedValue(kind="text", value=value)
        if status not in (ClaimStatus.CURRENT, ClaimStatus.TENTATIVE):
            raise GenerationError("claims start current or tentative; use lifecycle helpers")
        start_week = source.recorded_week if week is None else week
        record = ClaimRecord(
            claim_id=self._id("CLM"),
            subject=subject.entity_id,
            predicate=predicate,
            object=value,
            assertions=[
                Assertion(
                    source_id=source.source_id,
                    stance=Stance.SUPPORTS,
                    asserted_at=source.event_time,
                    recorded_week=source.recorded_week,
                )
            ],
            status_timeline=[
                StatusSpan(
                    status=status,
                    valid_from=week_date(start_week, 0),
                    valid_to=None if valid_to_week is None else week_date(valid_to_week, 0),
                    recorded_week=source.recorded_week,
                    cause=SpanCause(kind=SpanCauseKind.INITIAL, by=source.source_id),
                )
            ],
            audiences=audiences or [],
            derived_from=derived_from or [],
        )
        self.graph.claims.append(record)
        return record

    # -- lifecycle helpers ------------------------------------------------
    def _add_assertion(self, claim: ClaimRecord, source: SourceRecord, stance: Stance) -> None:
        claim.assertions.append(
            Assertion(
                source_id=source.source_id,
                stance=stance,
                asserted_at=source.event_time,
                recorded_week=source.recorded_week,
            )
        )

    def _add_span(
        self,
        claim: ClaimRecord,
        status: ClaimStatus,
        *,
        from_week: int,
        recorded_week: int,
        kind: SpanCauseKind,
        by: str,
    ) -> None:
        claim.status_timeline.append(
            StatusSpan(
                status=status,
                valid_from=week_date(from_week, 0),
                recorded_week=recorded_week,
                cause=SpanCause(kind=kind, by=by),
            )
        )

    def supersede(
        self,
        old: ClaimRecord,
        new: ClaimRecord,
        *,
        week: int,
        partial: bool = False,
    ) -> None:
        status = ClaimStatus.PARTIALLY_SUPERSEDED if partial else ClaimStatus.SUPERSEDED
        self._add_span(
            old,
            status,
            from_week=week,
            recorded_week=week,
            kind=SpanCauseKind.SUPERSESSION,
            by=new.claim_id,
        )
        old.superseded_by = new.claim_id
        new.supersedes = old.claim_id

    def confirm(self, claim: ClaimRecord, source: SourceRecord, *, week: int) -> None:
        self._add_assertion(claim, source, Stance.SUPPORTS)
        self._add_span(
            claim,
            ClaimStatus.CONFIRMED,
            from_week=week,
            recorded_week=week,
            kind=SpanCauseKind.CONFIRMATION,
            by=source.source_id,
        )

    def dispute(self, claim: ClaimRecord, source: SourceRecord, *, week: int) -> None:
        self._add_assertion(claim, source, Stance.DISPUTES)
        self._add_span(
            claim,
            ClaimStatus.DISPUTED,
            from_week=week,
            recorded_week=week,
            kind=SpanCauseKind.DISPUTE,
            by=source.source_id,
        )

    def disprove(
        self, claim: ClaimRecord, source: SourceRecord, *, week: int, retroactive_week: int | None = None
    ) -> None:
        self._add_assertion(claim, source, Stance.DISPUTES)
        self._add_span(
            claim,
            ClaimStatus.DISPROVED,
            from_week=week if retroactive_week is None else retroactive_week,
            recorded_week=week,
            kind=SpanCauseKind.DISPROOF,
            by=source.source_id,
        )

    def revoke(self, claim: ClaimRecord, source: SourceRecord, *, week: int) -> None:
        self._add_assertion(claim, source, Stance.RETRACTS)
        self._add_span(
            claim,
            ClaimStatus.REVOKED,
            from_week=week,
            recorded_week=week,
            kind=SpanCauseKind.RETRACTION,
            by=source.source_id,
        )

    def snapshot(self, week: int) -> None:
        self.graph.schedule.append(
            ScheduleOp(week=week, seq=len(self.graph.schedule), op=ScheduleOpKind.SNAPSHOT)
        )

    # -- queries ----------------------------------------------------------
    def query(
        self,
        query_kind: str,
        prompt: str,
        *,
        knowledge_week: int,
        expect: ExpectationBuilder,
        world_week: int | None = None,
        persona: str = "owner",
        family: str | None = None,
        tracks: list[str] | None = None,
        modes: list[str] | None = None,
        should_activate: bool = True,
        canary: bool = False,
    ) -> QueryRecord:
        from membench.schema import Ask

        record = QueryRecord(
            query_id=self._id("QRY"),
            template_id=self.template_id,
            family=family or self.template_id.split("_", 1)[-1],
            query_kind=query_kind,
            prompt_text=prompt,
            ask=Ask(world_week=world_week, knowledge_week=knowledge_week),
            persona=persona,
            tracks=tracks or ["B"],
            modes=modes or ["retrieval", "qa"],
            should_activate=should_activate,
            canary=canary,
        )
        self.graph.queries.append(record)
        self.graph.expectation_builders[record.query_id] = expect
        return record


# -- expectation builders (oracle-derived, evaluated at finalize) ---------


def _answer_from_value(value: TypedValue | None) -> ExpectedAnswer:
    if value is None:
        return ExpectedAnswer(kind="none")
    kind = {"quantity": "value", "date": "date", "entity_ref": "entity", "text": "text"}[
        value.kind
    ]
    return ExpectedAnswer(kind=kind, values=[value.value], unit=value.unit)  # type: ignore[arg-type]


def _view_for(
    ctx: OracleCtx, claim: ClaimRecord, query: QueryRecord
) -> oracle.TruthView:
    knowledge = query.ask.knowledge_week
    if query.ask.world_week is None:
        return oracle.current_truth(claim, knowledge)
    return oracle.truth_at(claim, oracle.world_cutoff(query.ask.world_week), knowledge)


def expect_value(
    claim: ClaimRecord,
    *,
    forbidden: list[ClaimRecord] | None = None,
    require_active: bool = True,
    hedged: bool | None = None,
) -> ExpectationBuilder:
    """The answer is this claim's value at the asked time, with citations."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        view = _view_for(ctx, claim, query)
        if require_active and not view.is_active:
            raise GenerationError(
                f"{query.query_id}: expected active truth for {claim.claim_id}, "
                f"got {view.status.value}"
            )
        citations = oracle.required_citations(
            claim, view, claims_by_id=ctx.claims_by_id, knowledge_week=query.ask.knowledge_week
        )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=_answer_from_value(view.value),
            required_claims=[claim.claim_id],
            forbidden_claims=[c.claim_id for c in forbidden or []],
            required_citations=list(citations),
            uncertainty=UncertaintyExpectation(
                hedged=hedged,
                mention_dispute=view.status is ClaimStatus.DISPUTED,
                cite_both_sides=view.status is ClaimStatus.DISPUTED,
            ),
            gates=["current_state", "citations"],
        )

    return build


def expect_superseded_history(old: ClaimRecord, new: ClaimRecord) -> ExpectationBuilder:
    """Historical/as-of answer: the old value, marked as since-superseded."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        view = _view_for(ctx, old, query)
        if view.status is not ClaimStatus.CURRENT:
            raise GenerationError(
                f"{query.query_id}: as-of view of {old.claim_id} should be current, "
                f"got {view.status.value}"
            )
        citations = oracle.required_citations(
            old, view, claims_by_id=ctx.claims_by_id, knowledge_week=query.ask.knowledge_week
        )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=_answer_from_value(old.object),
            required_claims=[old.claim_id],
            forbidden_claims=[],
            required_citations=list(citations),
            gates=["as_of", "citations"],
        )

    return build


def expect_abstain(*, clarify: bool = False) -> ExpectationBuilder:
    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="none"),
            abstain=not clarify,
            clarify=clarify,
            gates=["abstention"],
        )

    return build


def expect_no_disclosure(
    claim: ClaimRecord, *, forbidden_values: list[str]
) -> ExpectationBuilder:
    """Persona must not receive the restricted value (governance gate)."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        vis = oracle.visibility(
            ctx.graph.policy,
            query.persona,
            claim_id=claim.claim_id,
            at=oracle.world_cutoff(query.ask.knowledge_week),
        )
        if vis.allowed:
            raise GenerationError(
                f"{query.query_id}: persona {query.persona} unexpectedly allowed "
                f"{claim.claim_id}"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="none"),
            abstain=True,
            forbidden_disclosures=list(forbidden_values),
            gates=["no_leak", "abstention"],
        )

    return build


def finalize_expected(graph: ScenarioGraph) -> list[ExpectedRecord]:
    ctx = OracleCtx(graph=graph, claims_by_id=graph.claims_by_id())
    expected: list[ExpectedRecord] = []
    for query in graph.queries:
        builder = graph.expectation_builders.get(query.query_id)
        if builder is None:
            raise GenerationError(f"{query.query_id}: no expectation builder registered")
        expected.append(builder(ctx, query))
    return expected


_REGISTRY: dict[str, Template] = {}


def register(template: Template) -> Template:
    if template.template_id in _REGISTRY:
        raise GenerationError(f"duplicate template id {template.template_id}")
    _REGISTRY[template.template_id] = template
    return template


def registry() -> dict[str, Template]:
    return dict(_REGISTRY)
