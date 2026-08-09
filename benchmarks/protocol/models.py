"""Strict, versioned records shared by benchmark protocol lanes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .version import (
    BUDGET_LEDGER_SCHEMA_VERSION, CASE_GOLD_SCHEMA_VERSION, CASE_TRACE_SCHEMA_VERSION,
    EQUIVALENCE_DIFF_SCHEMA_VERSION, EQUIVALENCE_EXCEPTION_SCHEMA_VERSION,
    GAP_REPORT_SCHEMA_VERSION, PROBE_RESULT_SCHEMA_VERSION, PROTOCOL_EVENT_SCHEMA_VERSION,
    PROTOCOL_VERSION, READINESS_REPORT_SCHEMA_VERSION, RUN_MANIFEST_SCHEMA_VERSION,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DatasetIdentity(StrictModel):
    id: str
    variant: str
    source: str
    revision: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: int = Field(ge=0)


class CaseHandle(StrictModel):
    """The only case context allowed across the provider-ingest boundary."""

    case_id: str
    case_ordinal: int = Field(ge=1)
    question_date: str


class EventProvenance(StrictModel):
    dataset_row_index: int = Field(ge=0)
    upstream_session_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    converter: str
    converter_version: str


class ProtocolEvent(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[PROTOCOL_EVENT_SCHEMA_VERSION] = PROTOCOL_EVENT_SCHEMA_VERSION
    dataset: DatasetIdentity
    case_id: str
    session_ordinal: int = Field(ge=1)
    sequence: int = Field(ge=0)
    role: str
    turn_ordinal: int = Field(ge=1)
    content: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_timestamp: str | None = None
    timestamp_semantics: Literal["event_time_declared_by_dataset", "ingestion_order_only"]
    ingestion_ordinal: int = Field(ge=0)
    provenance: EventProvenance

    @field_validator("original_timestamp")
    @classmethod
    def _timestamp_is_rfc3339_or_null(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("original_timestamp must be RFC3339 or null")
        return value


class CaseGold(StrictModel):
    """Private scoring record; adapter-facing APIs deliberately never accept it."""

    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[CASE_GOLD_SCHEMA_VERSION] = CASE_GOLD_SCHEMA_VERSION
    case_id: str
    answer: str
    answer_session_ids: list[str]
    question_type: str
    question: str


ReadinessMethod = Literal[
    "index-count", "config-state", "semantic-probe", "memories-canary",
    "doctor-check", "readiness-unverifiable",
]


class LaneReadiness(StrictModel):
    lane: str
    requested: bool
    verified: bool
    method: ReadinessMethod
    evidence: str
    fallback_detected: bool = False

    @field_validator("evidence")
    @classmethod
    def _requested_needs_evidence(cls, value: str, info) -> str:
        if info.data.get("requested") and not value:
            raise ValueError("requested readiness needs positive evidence or an explicit unverifiable reason")
        return value


class LeakageSummary(StrictModel):
    scanned_cases: int = Field(ge=0)
    invalidated_cases: int = Field(ge=0)
    detectors_fired: dict[str, int] = Field(default_factory=dict)


class BudgetSummary(StrictModel):
    cap_usd: float = Field(ge=0)
    committed_usd: float = Field(ge=0)
    refusals: int = Field(ge=0)


ManifestStatus = Literal["started", "VALID", "INVALID", "READINESS_UNVERIFIABLE", "ABORTED_BUDGET", "BLOCKED"]


class RunManifest(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[RUN_MANIFEST_SCHEMA_VERSION] = RUN_MANIFEST_SCHEMA_VERSION
    run_id: str
    dataset: DatasetIdentity
    status: ManifestStatus
    started_at: str
    finalized_at: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    pins: dict[str, str] = Field(default_factory=dict)
    readiness: list[LaneReadiness] = Field(default_factory=list)
    leakage: LeakageSummary = Field(default_factory=lambda: LeakageSummary(scanned_cases=0, invalidated_cases=0))
    contamination: Literal["isolated", "contaminated", "unverifiable"] | None = None
    budget: BudgetSummary | None = None
    provider_variant: str | None = None
    control_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pre_registration_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class IngestRecord(StrictModel):
    record: Literal["ingest"] = "ingest"
    session_ordinal: int = Field(ge=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_ids: list[str]


class SearchRecord(StrictModel):
    record: Literal["search"] = "search"
    query: str
    raw_response_ref: str
    normalized_hit_ids: list[str]
    normalized_hit_shas: list[str]
    top_k: int = Field(ge=0)


class AnswerRecord(StrictModel):
    record: Literal["answer"] = "answer"
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str
    response_ref: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class JudgeRecord(StrictModel):
    record: Literal["judge"] = "judge"
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str
    verdict_label: str
    parser_warnings: list[str] = Field(default_factory=list)


class TimingRecord(StrictModel):
    record: Literal["timing"] = "timing"
    phase: str
    ms: float = Field(ge=0)


class CleanupRecord(StrictModel):
    record: Literal["cleanup"] = "cleanup"
    verified: bool


TraceRecord = Annotated[
    IngestRecord | SearchRecord | AnswerRecord | JudgeRecord | TimingRecord | CleanupRecord,
    Field(discriminator="record"),
]


class CaseTrace(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[CASE_TRACE_SCHEMA_VERSION] = CASE_TRACE_SCHEMA_VERSION
    case_id: str
    entries: list[TraceRecord] = Field(default_factory=list)


class ReadinessReport(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[READINESS_REPORT_SCHEMA_VERSION] = READINESS_REPORT_SCHEMA_VERSION
    status: Literal["VALID", "INVALID", "READINESS_UNVERIFIABLE"]
    lanes: list[LaneReadiness]
    reasons: list[str] = Field(default_factory=list)


ProbeKind = Literal["lexical-rare-token", "semantic-zero-overlap", "update-current-state"]
ProbeOutcome = Literal["pass", "fail", "inconclusive-by-design", "superseded", "both_returned", "stale_only", "unresolvable"]


class ProbeResult(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[PROBE_RESULT_SCHEMA_VERSION] = PROBE_RESULT_SCHEMA_VERSION
    case_id: str
    probe_kind: ProbeKind
    outcome: ProbeOutcome
    hits: list[str] = Field(default_factory=list)
    detail: str | None = None


class BudgetLedgerEntry(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[BUDGET_LEDGER_SCHEMA_VERSION] = BUDGET_LEDGER_SCHEMA_VERSION
    ts: str
    seq: int = Field(ge=0)
    actor: str
    op: str
    kind: Literal["reserve", "commit", "release", "approval"]
    units: float = Field(ge=0)
    running_total: float = Field(ge=0)
    decision: str


class EquivalenceDiff(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[EQUIVALENCE_DIFF_SCHEMA_VERSION] = EQUIVALENCE_DIFF_SCHEMA_VERSION
    case_id: str
    field: str
    expected: str | None = None
    actual: str | None = None
    equal: bool


class EquivalenceException(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[EQUIVALENCE_EXCEPTION_SCHEMA_VERSION] = EQUIVALENCE_EXCEPTION_SCHEMA_VERSION
    case_id: str
    field: str
    rationale: str
    evidence: str
    expires_at: str


class GapReport(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[GAP_REPORT_SCHEMA_VERSION] = GAP_REPORT_SCHEMA_VERSION
    subject: str
    status: str
    reason: str
    unblock: str | None = None


SCHEMA_EXPORTS: dict[str, type[BaseModel]] = {
    "protocol-event": ProtocolEvent, "case-gold": CaseGold, "run-manifest": RunManifest,
    "case-trace": CaseTrace, "readiness-report": ReadinessReport, "probe-result": ProbeResult,
    "budget-ledger": BudgetLedgerEntry, "equivalence-diff": EquivalenceDiff,
    "equivalence-exception": EquivalenceException, "gap-report": GapReport,
}


def export_json_schemas(out_dir: Path) -> list[Path]:
    """Write stable, sorted JSON Schema files named from declared schema versions."""

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in sorted(SCHEMA_EXPORTS.items()):
        version = model.model_fields["schema_version"].default
        path = out_dir / f"{name}.v{version}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
