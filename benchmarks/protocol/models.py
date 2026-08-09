"""Strict, versioned records shared by benchmark protocol lanes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .version import (
    BUDGET_LEDGER_SCHEMA_VERSION,
    CASE_GOLD_SCHEMA_VERSION,
    CASE_TRACE_SCHEMA_VERSION,
    EQUIVALENCE_DIFF_SCHEMA_VERSION,
    EQUIVALENCE_EXCEPTION_SCHEMA_VERSION,
    GAP_REPORT_SCHEMA_VERSION,
    PROBE_RESULT_SCHEMA_VERSION,
    PROTOCOL_EVENT_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    READINESS_REPORT_SCHEMA_VERSION,
    RUN_MANIFEST_SCHEMA_VERSION,
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
        if value is not None and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            value,
        ):
            raise ValueError("original_timestamp must be RFC3339 or null")
        return value


class CaseGold(StrictModel):
    """Private judging record. It deliberately is not an adapter input type."""

    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[CASE_GOLD_SCHEMA_VERSION] = CASE_GOLD_SCHEMA_VERSION
    case_id: str
    answer: str
    answer_session_ids: list[str]
    question_type: str
    question: str


class RunManifest(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[RUN_MANIFEST_SCHEMA_VERSION] = RUN_MANIFEST_SCHEMA_VERSION
    run_id: str
    dataset: DatasetIdentity
    status: str
    started_at: str
    finalized_at: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    pins: dict[str, str] = Field(default_factory=dict)


class CaseTrace(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[CASE_TRACE_SCHEMA_VERSION] = CASE_TRACE_SCHEMA_VERSION
    case_id: str
    entries: list[dict[str, Any]] = Field(default_factory=list)


class ReadinessReport(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[READINESS_REPORT_SCHEMA_VERSION] = READINESS_REPORT_SCHEMA_VERSION
    status: str
    lanes: list[dict[str, Any]]
    reasons: list[str] = Field(default_factory=list)


class ProbeResult(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[PROBE_RESULT_SCHEMA_VERSION] = PROBE_RESULT_SCHEMA_VERSION
    case_id: str
    probe_kind: str
    outcome: str
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
    "protocol-event": ProtocolEvent,
    "case-gold": CaseGold,
    "run-manifest": RunManifest,
    "case-trace": CaseTrace,
    "readiness-report": ReadinessReport,
    "probe-result": ProbeResult,
    "budget-ledger": BudgetLedgerEntry,
    "equivalence-diff": EquivalenceDiff,
    "equivalence-exception": EquivalenceException,
    "gap-report": GapReport,
}


def export_json_schemas(out_dir: Path) -> list[Path]:
    """Write stable, sorted JSON Schema files for every public record."""

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in sorted(SCHEMA_EXPORTS.items()):
        path = out_dir / f"{name}.v1.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
