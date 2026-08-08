"""Typed corpus records (pydantic v2, strict) and JSON-Schema export.

Time model, used consistently everywhere:

- **World time** (when a fact held / an event happened) is a calendar
  ``datetime.date``: ``valid_from``/``valid_to``, ``asserted_at``,
  ``event_time``, ``occurred_at``.
- **Knowledge time** (when the corpus learned something) is an integer
  simulated-week index 0..11: ``recorded_week`` on sources, assertions, and
  status spans; ``knowledge_week`` on queries — refined to the second by
  ``recorded_offset_s`` (see below).

Templates author both axes explicitly; the oracle only evaluates and lints —
it never infers spans.

Sub-day knowledge time
----------------------

``recorded_week`` alone cannot express *order within a day*, so a corpus built
only from it is structurally unable to ask whether a memory system kept the
order in which it learned two same-day facts. ``recorded_offset_s`` refines
the same axis rather than opening a parallel one: seconds after 00:00:00 on
the Monday that opens ``recorded_week``, so a record's knowledge time is the
pair ``(recorded_week, recorded_offset_s)``.

**Precision is part of the data.** ``recorded_offset_s`` is ``None`` — and is
then *omitted from the serialised record entirely* — when the intra-day
instant was never captured. ``None`` does not mean midnight; it means an
unknown instant somewhere inside ``recorded_week``, which is exactly what
every v0.1–v0.2 record has always meant. Defaulting it to ``0`` would assert a
precision the corpus never had, and would have restamped 300 existing claims
as "Monday 00:00:00". Omission on serialisation is what keeps every v0.1–v0.2
corpus byte-identical, and it mirrors the product's own no-backfill rule
(``src/exomem/temporal.py``): a mixed-precision store is the permanent state,
not a migration window.

Because precision is data, **ordering is not total** and the oracle compares
these values four-valued (:func:`membench.oracle.compare_recorded`:
``before | after | same | indeterminate``). Two records sharing a week where
either lacks an instant genuinely cannot be ordered, and the oracle says so
rather than guessing. Visibility, by contrast, stays total and unchanged: an
ask carries no sub-day cutoff, so it is evaluated at the *end* of its
knowledge week, and every record of week ``rw <= kw`` lies determinately
before that instant whatever its precision. ``recorded_week <= knowledge_week``
is the new rule's default case, not a second rule beside it.

Only the three records the oracle's visibility rule reads carry the finer
field: :class:`Assertion`, :class:`StatusSpan`, and :class:`SourceRecord`.
:class:`EventRecord` also has a ``recorded_week``, but no oracle rule consults
it, so refining it would add unexercised surface; that gap is deliberate and
should be closed by whichever change first gives events an oracle rule.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

#: Seconds in one simulated week; the exclusive bound on ``recorded_offset_s``
#: and the source of the end-of-week knowledge cutoff.
WEEK_SECONDS: int = 7 * 24 * 60 * 60

#: Seconds after the Monday 00:00:00 that opens ``recorded_week``, or ``None``
#: when the intra-day instant was never captured (see the module docstring:
#: ``None`` is "unknown instant within the week", never midnight).
RecordedOffset = Annotated[int, Field(ge=0, lt=WEEK_SECONDS)] | None


def _drop_unknown_instant(data: dict[str, Any]) -> dict[str, Any]:
    """Serialise an uncaptured intra-day instant as absence, not as ``null``.

    A record that never had a sub-day instant is written exactly as it was
    before the field existed. That is not a cosmetic choice: it keeps every
    v0.1–v0.2 corpus byte-identical (so a new template provably perturbs no
    existing one), and it is the honest encoding — an absent key says the
    precision was never captured, where ``"recorded_offset_s": null`` on 300
    claims would say the corpus considered and recorded its absence.
    """

    if data.get("recorded_offset_s", 0) is None:
        data.pop("recorded_offset_s")
    return data


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimStatus(str, enum.Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    PARTIALLY_SUPERSEDED = "partially_superseded"
    REVOKED = "revoked"
    DISPUTED = "disputed"
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    DISPROVED = "disproved"
    UNKNOWN = "unknown"


class AuthorityTier(str, enum.Enum):
    SYSTEM_OF_RECORD = "system_of_record"
    OFFICIAL = "official"
    FIRSTHAND = "firsthand"
    SECONDHAND = "secondhand"
    RUMOR = "rumor"


class Stance(str, enum.Enum):
    SUPPORTS = "supports"
    DISPUTES = "disputes"
    RETRACTS = "retracts"


class ArtifactKind(str, enum.Enum):
    MARKDOWN = "markdown"
    CSV = "csv"
    PNG = "png"
    PNG_UNAVAILABLE = "png_unavailable"
    PDF = "pdf"
    PDF_UNAVAILABLE = "pdf_unavailable"
    TRANSCRIPT = "transcript"


class SpanCauseKind(str, enum.Enum):
    INITIAL = "initial"
    SUPERSESSION = "supersession"
    RETRACTION = "retraction"
    CONFIRMATION = "confirmation"
    DISPROOF = "disproof"
    DISPUTE = "dispute"
    EXPIRY = "expiry"
    CORRECTION = "correction"
    LATE_EVIDENCE = "late_evidence"


class NameSpan(StrictModel):
    name: str
    valid_from: date
    valid_to: date | None = None


class EntityRecord(StrictModel):
    entity_id: str
    kind: Literal["person", "organization", "project", "place", "product", "concept", "team"]
    domain: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    name_timeline: list[NameSpan] = Field(default_factory=list)
    merged_into: str | None = None
    split_into: list[str] = Field(default_factory=list)


class Assertion(StrictModel):
    source_id: str
    stance: Stance
    asserted_at: date
    recorded_week: int = Field(ge=0)
    recorded_offset_s: RecordedOffset = None

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _drop_unknown_instant(handler(self))


class SpanCause(StrictModel):
    kind: SpanCauseKind
    by: str | None = None  # source_id or claim_id that justifies the span


class StatusSpan(StrictModel):
    status: ClaimStatus
    valid_from: date
    valid_to: date | None = None
    recorded_week: int = Field(ge=0)
    recorded_offset_s: RecordedOffset = None
    cause: SpanCause

    @model_validator(mode="after")
    def _window_ordered(self) -> StatusSpan:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _drop_unknown_instant(handler(self))


class TypedValue(StrictModel):
    kind: Literal["text", "quantity", "date", "entity_ref"]
    value: str  # canonical string form; scorers parse quantities/dates from it
    unit: str | None = None


class ClaimRecord(StrictModel):
    claim_id: str
    subject: str  # entity_id
    predicate: str
    object: TypedValue
    assertions: list[Assertion] = Field(min_length=1)
    status_timeline: list[StatusSpan] = Field(min_length=1)
    supersedes: str | None = None
    superseded_by: str | None = None
    derived_from: list[str] = Field(default_factory=list)  # claim ids and/or source ids
    audiences: list[str] = Field(default_factory=list)  # empty = unrestricted


class AdversarialFlags(StrictModel):
    injected_instructions: bool = False
    malicious_metadata: bool = False


class SourceRecord(StrictModel):
    source_id: str
    title: str
    version: int = Field(default=1, ge=1)
    supersedes_source: str | None = None
    artifact_kind: ArtifactKind
    path: str  # corpus-relative artifact path
    authority: AuthorityTier
    event_time: date
    recorded_week: int = Field(ge=0)
    recorded_offset_s: RecordedOffset = None
    audiences: list[str] = Field(default_factory=list)
    adversarial: AdversarialFlags = Field(default_factory=AdversarialFlags)

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _drop_unknown_instant(handler(self))


class EventRecord(StrictModel):
    event_id: str
    kind: str
    occurred_at: date
    recorded_week: int = Field(ge=0)
    participants: list[str] = Field(default_factory=list)
    causes: list[str] = Field(default_factory=list)  # event ids (causal, not correlated)


class Persona(StrictModel):
    persona_id: str
    audiences: list[str] = Field(min_length=1)


class PolicyRule(StrictModel):
    rule_id: str
    target_claims: list[str] = Field(default_factory=list)
    target_sources: list[str] = Field(default_factory=list)
    allow: list[str] = Field(min_length=1)  # audiences allowed before declassification
    withhold_notice: bool = True
    declassify_at: date | None = None


class TombstoneRequest(StrictModel):
    target_sources: list[str] = Field(min_length=1)
    requested_at: date


class PolicySet(StrictModel):
    audiences: list[str] = Field(default_factory=lambda: ["owner"])
    personas: list[Persona] = Field(default_factory=list)
    rules: list[PolicyRule] = Field(default_factory=list)
    tombstones: list[TombstoneRequest] = Field(default_factory=list)


class ScheduleOpKind(str, enum.Enum):
    INGEST_SOURCE = "ingest_source"
    CORRECT_SOURCE = "correct_source"
    DELETE_SOURCE = "delete_source"
    DUPLICATE_SOURCE = "duplicate_source"
    MERGE_ENTITIES = "merge_entities"
    SPLIT_ENTITY = "split_entity"
    USER_CORRECTION = "user_correction"
    SNAPSHOT = "snapshot"


class ScheduleOp(StrictModel):
    week: int = Field(ge=0)
    seq: int = Field(ge=0)
    op: ScheduleOpKind
    source_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class Ask(StrictModel):
    world_week: int | None = None  # None = "now" (current truth at knowledge cutoff)
    knowledge_week: int = Field(ge=0)


class UncertaintyExpectation(StrictModel):
    hedged: bool | None = None  # None = no requirement either way
    mention_dispute: bool = False
    cite_both_sides: bool = False


class ExpectedAnswer(StrictModel):
    kind: Literal["value", "date", "entity", "text", "list", "none"]
    values: list[str] = Field(default_factory=list)
    unit: str | None = None
    tolerance: float | None = None


class ExpectedRecord(StrictModel):
    query_id: str
    answer: ExpectedAnswer
    required_claims: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    required_citations: list[str] = Field(default_factory=list)  # sentinel source ids
    transitive_citations_ok: bool = True
    forbidden_disclosures: list[str] = Field(default_factory=list)
    abstain: bool = False
    clarify: bool = False
    uncertainty: UncertaintyExpectation = Field(default_factory=UncertaintyExpectation)
    gates: list[str] = Field(default_factory=list)


class QueryRecord(StrictModel):
    query_id: str
    template_id: str
    family: str
    query_kind: str
    prompt_text: str
    ask: Ask
    persona: str = "owner"
    tracks: list[str] = Field(default_factory=lambda: ["B"])
    modes: list[str] = Field(default_factory=lambda: ["retrieval", "qa"])
    should_activate: bool = True
    followup_of: str | None = None
    canary: bool = False


class TemplateInfo(StrictModel):
    template_id: str
    family: str
    summary: str
    variants: int = Field(ge=1)


class ArtifactEntry(StrictModel):
    path: str
    kind: ArtifactKind
    bytes_sha256: str
    logical_sha256: str
    source_id: str | None = None


class CorpusManifest(StrictModel):
    generator_version: str
    master_seed: int
    templates: list[TemplateInfo]
    counts: dict[str, int]
    renderer_versions: dict[str, str]
    degradations: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactEntry] = Field(default_factory=list)


class ConclusionRecord(StrictModel):
    """A durable conclusion, as a knowledge store would hold it.

    Field names are deliberately product-neutral. ``cites`` rather than
    ``sources``, because the latter is exomem's ``remember`` parameter and would
    bias the whole contract toward one contender's grammar.
    """

    conclusion_id: str
    #: The oracle claim this conclusion states. Keeps the plan joinable back to
    #: expectations without duplicating the claim's contents.
    claim_id: str
    #: The week at which this revision's basis was complete. A claim yields one
    #: conclusion per point at which its basis changed, so an ask at knowledge
    #: week k must be served the latest revision with `knowledge_week <= k` --
    #: any later one rests on evidence that did not yet exist (4b.39).
    knowledge_week: int = 0
    title: str
    body: str
    #: Source ids this conclusion draws from, in recorded order.
    cites: tuple[str, ...] = ()
    #: The conclusion this one replaces, when the underlying claim superseded
    #: another. Lineage, not a dispute.
    supersedes: str | None = None
    #: Conclusions asserting an incompatible value for the same subject and
    #: predicate, live at the same time. Symmetric.
    disputes: tuple[str, ...] = ()
    #: Ordering key, so a plan is stable regardless of input order.
    sort_key: int = Field(default=0, ge=0)


SCHEMA_EXPORTS: dict[str, type[BaseModel]] = {
    "entity": EntityRecord,
    "source": SourceRecord,
    "event": EventRecord,
    "claim": ClaimRecord,
    "policy-set": PolicySet,
    "schedule-op": ScheduleOp,
    "query": QueryRecord,
    "expected": ExpectedRecord,
    "conclusion": ConclusionRecord,
    "corpus-manifest": CorpusManifest,
}

_M = TypeVar("_M", bound=BaseModel)


def export_json_schemas(out_dir: Path) -> list[Path]:
    """Write one ``<name>.schema.json`` per exported record type; sorted keys."""

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in sorted(SCHEMA_EXPORTS.items()):
        target = out_dir / f"{name}.schema.json"
        payload = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        target.write_text(payload, encoding="utf-8")
        written.append(target)
    return written


def dump_jsonl(records: Iterable[BaseModel], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(record.model_dump_json(exclude_none=False) + "\n")
            count += 1
    return count


def load_jsonl(model: type[_M], path: Path) -> list[_M]:
    records: list[_M] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(model.model_validate_json(line))
    return records


def index_by(records: Sequence[_M], attribute: str) -> dict[str, _M]:
    index: dict[str, _M] = {}
    for record in records:
        key = getattr(record, attribute)
        if key in index:
            raise ValueError(f"duplicate {attribute}: {key}")
        index[key] = record
    return index
