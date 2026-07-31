"""Typed corpus records (pydantic v2, strict) and JSON-Schema export.

Time model, used consistently everywhere:

- **World time** (when a fact held / an event happened) is a calendar
  ``datetime.date``: ``valid_from``/``valid_to``, ``asserted_at``,
  ``event_time``, ``occurred_at``.
- **Knowledge time** (when the corpus learned something) is an integer
  simulated-week index 0..11: ``recorded_week`` on sources, assertions, and
  status spans; ``knowledge_week`` on queries.

Templates author both axes explicitly; the oracle only evaluates and lints —
it never infers spans.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class SpanCause(StrictModel):
    kind: SpanCauseKind
    by: str | None = None  # source_id or claim_id that justifies the span


class StatusSpan(StrictModel):
    status: ClaimStatus
    valid_from: date
    valid_to: date | None = None
    recorded_week: int = Field(ge=0)
    cause: SpanCause

    @model_validator(mode="after")
    def _window_ordered(self) -> StatusSpan:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


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
    audiences: list[str] = Field(default_factory=list)
    adversarial: AdversarialFlags = Field(default_factory=AdversarialFlags)


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


SCHEMA_EXPORTS: dict[str, type[BaseModel]] = {
    "entity": EntityRecord,
    "source": SourceRecord,
    "event": EventRecord,
    "claim": ClaimRecord,
    "policy-set": PolicySet,
    "schedule-op": ScheduleOp,
    "query": QueryRecord,
    "expected": ExpectedRecord,
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
