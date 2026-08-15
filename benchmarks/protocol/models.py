"""Strict, versioned records shared by benchmark protocol lanes."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import PreregistrationIdentity
from .version import (
    BUDGET_LEDGER_SCHEMA_VERSION,
    CASE_GOLD_SCHEMA_VERSION,
    CASE_TRACE_SCHEMA_VERSION,
    CASE_TRACE_V2_SCHEMA_VERSION,
    EQUIVALENCE_DIFF_SCHEMA_VERSION,
    EQUIVALENCE_EXCEPTION_SCHEMA_VERSION,
    GAP_REPORT_SCHEMA_VERSION,
    GUEST_CLEANUP_PLAN_SCHEMA_VERSION,
    GUEST_CLEANUP_SCHEMA_VERSION,
    LME_SELECTION_SCHEMA_VERSION,
    MEMORYBENCH_EXPORT_SCHEMA_VERSION,
    MEMORYBENCH_PRIVATE_GOLD_SCHEMA_VERSION,
    MEMORYBENCH_RUN_PLAN_SCHEMA_VERSION,
    PROBE_RESULT_SCHEMA_VERSION,
    PROTOCOL_EVENT_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    PROVIDER_CLEANUP_OBSERVATION_SCHEMA_VERSION,
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


class LmeSelectionSource(StrictModel):
    repository: Literal["xiaowu0162/longmemeval-cleaned"]
    revision: Literal["98d7416c24c778c2fee6e6f3006e7a073259d48f"]
    filename: Literal["longmemeval_s_cleaned.json"]
    sha256: Literal["d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"]
    byte_count: Literal[277383467]
    row_count: Literal[500]
    type_census: dict[str, int]
    abstention_count: Literal[30]

    @field_validator("type_census")
    @classmethod
    def _frozen_census(cls, value: dict[str, int]) -> dict[str, int]:
        expected = {
            "knowledge-update": 78, "multi-session": 133,
            "single-session-assistant": 56, "single-session-preference": 30,
            "single-session-user": 70, "temporal-reasoning": 133,
        }
        if value != expected:
            raise ValueError("type_census differs from frozen LongMemEval-S source")
        return value

    @model_validator(mode="after")
    def _source_totals(self) -> LmeSelectionSource:
        if sum(self.type_census.values()) != self.row_count:
            raise ValueError("type census must total row count")
        return self


class LmeSelection(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[LME_SELECTION_SCHEMA_VERSION]
    artifact_type: Literal["lme-selection.v1"]
    source_identity: LmeSelectionSource
    selection_algorithm_version: Literal["lme-s-25.sha256-v1"]
    selection_algorithm: Literal["sha256(utf8(question_id + dataset_sha256)); sort (digest_hex, question_id)"]
    question_type_order: list[str]
    quotas: dict[str, int]
    target_question_ids: list[str]

    @model_validator(mode="after")
    def _closed_canonical_profile(self) -> LmeSelection:
        question_types = [
            "single-session-user", "single-session-assistant", "single-session-preference",
            "multi-session", "temporal-reasoning", "knowledge-update",
        ]
        if self.question_type_order != question_types:
            raise ValueError("question_type_order differs from canonical selection profile")
        if self.quotas != {**{question_type: 3 for question_type in question_types}, "abstention": 7}:
            raise ValueError("quotas differ from canonical selection profile")
        if len(self.target_question_ids) != 25 or len(set(self.target_question_ids)) != 25:
            raise ValueError("target_question_ids must be 25 unique IDs")
        if any(not value for value in self.target_question_ids):
            raise ValueError("target_question_ids must not contain blanks")
        return self


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
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PreregistrationLineage(StrictModel):
    """Additive manifest projection of the ordered amendment receipt chain."""

    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    amendment_receipt_sha256s: tuple[Sha256Digest, ...] = Field(min_length=1)
    effective_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("amendment_receipt_sha256s")
    @classmethod
    def _receipt_identities_are_sha256s(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in value):
            raise ValueError("amendment receipt identities must be sha256 digests")
        return value

    @classmethod
    def from_identity(cls, identity: PreregistrationIdentity) -> PreregistrationLineage:
        if not identity.amendments:
            raise ValueError("base-only identity has no amendment lineage")
        return cls(
            base_sha256=identity.original.sha256,
            amendment_receipt_sha256s=tuple(
                amendment.receipt.receipt_sha256 for amendment in identity.amendments
            ),
            effective_sha256=identity.effective.sha256,
        )


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
    #: Why a terminal status is not VALID.  A manifest that refuses a run must
    #: be able to say why without a reader consulting a second artifact.
    invalid_reason: str | None = None
    budget: BudgetSummary | None = None
    provider_variant: str | None = None
    control_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    preregistration_identity: PreregistrationIdentity
    preregistration_lineage: PreregistrationLineage | None = None

    @model_validator(mode="after")
    def _lineage_matches_identity(self) -> RunManifest:
        if self.preregistration_lineage is None:
            if self.preregistration_identity.amendments:
                raise ValueError("amended preregistration requires manifest lineage")
            return self
        expected = PreregistrationLineage.from_identity(self.preregistration_identity)
        if self.preregistration_lineage.base_sha256 != expected.base_sha256:
            raise ValueError("preregistration lineage base does not match typed identity")
        if (
            self.preregistration_lineage.amendment_receipt_sha256s
            != expected.amendment_receipt_sha256s
        ):
            raise ValueError("preregistration lineage receipt order does not match typed identity")
        if self.preregistration_lineage.effective_sha256 != expected.effective_sha256:
            raise ValueError("preregistration lineage effective sha does not match typed identity")
        return self


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


class IngestRecordV2(IngestRecord):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[CASE_TRACE_V2_SCHEMA_VERSION]


class SearchRecordV2(SearchRecord):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[CASE_TRACE_V2_SCHEMA_VERSION]


class AnswerRecordV2(AnswerRecord):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[CASE_TRACE_V2_SCHEMA_VERSION]


class JudgeRecordV2(JudgeRecord):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[CASE_TRACE_V2_SCHEMA_VERSION]


class TimingRecordV2(TimingRecord):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[CASE_TRACE_V2_SCHEMA_VERSION]


class CleanupRecordV2(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[CASE_TRACE_V2_SCHEMA_VERSION]
    record: Literal["cleanup"] = "cleanup"
    run_id: str
    session_id: str
    namespace: str
    requested_provider: str | None = None
    observation_path: str
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observation_path")
    @classmethod
    def _safe_observation_path(cls, value: str) -> str:
        return _canonical_relative_path(value)


TraceRecord = Annotated[
    IngestRecord | SearchRecord | AnswerRecord | JudgeRecord | TimingRecord | CleanupRecord,
    Field(discriminator="record"),
]

TraceRecordV2 = Annotated[
    IngestRecordV2 | SearchRecordV2 | AnswerRecordV2 | JudgeRecordV2 | TimingRecordV2 | CleanupRecordV2,
    Field(discriminator="record"),
]


class CaseTrace(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[CASE_TRACE_SCHEMA_VERSION] = CASE_TRACE_SCHEMA_VERSION
    case_id: str
    entries: list[TraceRecord] = Field(default_factory=list)


class CaseTraceV2(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[CASE_TRACE_V2_SCHEMA_VERSION] = CASE_TRACE_V2_SCHEMA_VERSION
    case_id: str
    entries: list[TraceRecordV2] = Field(default_factory=list)


def _canonical_relative_path(value: str) -> str:
    if not value or value.startswith("/") or value.endswith("/") or "\\" in value or "//" in value:
        raise ValueError("path must be a canonical relative POSIX path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("path must be a canonical relative POSIX path")
    return value


class ProviderCleanupNamespaceMembership(StrictModel):
    kind: Literal["namespace-membership"] = "namespace-membership"
    expected_namespace: str
    live_namespaces: list[str]

    @field_validator("live_namespaces")
    @classmethod
    def _namespaces_sorted_unique(cls, value: list[str]) -> list[str]:
        return _require_sorted_unique(value, "live_namespaces")


class ProviderCleanupProviderState(StrictModel):
    kind: Literal["provider-state"] = "provider-state"
    remaining_record_ids: list[str]
    backend_active: bool

    @field_validator("remaining_record_ids")
    @classmethod
    def _ids_sorted_unique(cls, value: list[str]) -> list[str]:
        return _require_sorted_unique(value, "remaining_record_ids")


class ProviderCleanupPathLstat(StrictModel):
    kind: Literal["path-lstat"] = "path-lstat"
    path: str
    raw_kind: Literal["missing", "directory", "regular", "symlink", "other"]
    entries: list[str]

    @field_validator("path")
    @classmethod
    def _path_is_canonical(cls, value: str) -> str:
        return _canonical_relative_path(value)

    @field_validator("entries")
    @classmethod
    def _entries_sorted_unique(cls, value: list[str]) -> list[str]:
        return _require_sorted_unique(value, "entries")


class ProviderCleanupProcessGroup(StrictModel):
    """Absence fact for a row declaring an owned-subprocess execution model.

    Carries a canonical logical ref and counts only: raw PIDs, ports, and
    bearer tokens are never serialized into cleanup evidence.
    """

    kind: Literal["process-group"] = "process-group"
    group_ref: str
    remaining_count: int = Field(ge=0)
    listener_bound: bool

    @field_validator("group_ref")
    @classmethod
    def _group_ref_is_canonical(cls, value: str) -> str:
        return _canonical_relative_path(value)


ProviderCleanupRawObservation = Annotated[
    ProviderCleanupNamespaceMembership
    | ProviderCleanupProviderState
    | ProviderCleanupPathLstat
    | ProviderCleanupProcessGroup,
    Field(discriminator="kind"),
]


class ProviderCleanupObservation(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[PROVIDER_CLEANUP_OBSERVATION_SCHEMA_VERSION] = PROVIDER_CLEANUP_OBSERVATION_SCHEMA_VERSION
    artifact_type: Literal["provider-cleanup-observation.v1"] = "provider-cleanup-observation.v1"
    run_id: str
    session_id: str
    requested_provider: str
    provider_variant: str | None
    namespace: str
    cleanup_called: bool
    required_surface_ids: list[str]
    observations: list[ProviderCleanupRawObservation]

    @field_validator("required_surface_ids")
    @classmethod
    def _surface_ids_sorted_unique(cls, value: list[str]) -> list[str]:
        return _require_sorted_unique(value, "required_surface_ids")

    @model_validator(mode="after")
    def _observation_kinds_unique(self) -> ProviderCleanupObservation:
        keys = [
            (item.kind, getattr(item, "path", getattr(item, "expected_namespace", "")))
            for item in self.observations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("cleanup observations must be unique")
        return self


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


class EquivalenceDifference(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[EQUIVALENCE_DIFF_SCHEMA_VERSION] = EQUIVALENCE_DIFF_SCHEMA_VERSION
    case_id: str
    field: str
    expected: str | None = None
    actual: str | None = None
    equal: bool
    classification: Literal["blocking", "reported"]
    explanation_required: bool = True
    #: The registered weaker predicate that was applied, when one was.
    compare_as: str | None = None


class EquivalenceDiff(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[EQUIVALENCE_DIFF_SCHEMA_VERSION] = EQUIVALENCE_DIFF_SCHEMA_VERSION
    kind: Literal["equivalence-diff.v1"] = "equivalence-diff.v1"
    mode: Literal["blocking", "reported"]
    diffs: list[EquivalenceDifference]


class EquivalenceException(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[EQUIVALENCE_EXCEPTION_SCHEMA_VERSION] = EQUIVALENCE_EXCEPTION_SCHEMA_VERSION
    case_id: str
    field: str
    compare_as: str
    evidence: str
    approver: str
    expires_at: str


class GapReport(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    schema_version: Literal[GAP_REPORT_SCHEMA_VERSION] = GAP_REPORT_SCHEMA_VERSION
    subject: str
    status: str
    reason: str
    unblock: str | None = None


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_PUBLIC_SOURCE_PATTERN = r"^(?:https://[^\\\s]+|[A-Za-z0-9][A-Za-z0-9._+-]*(?::[A-Za-z0-9][A-Za-z0-9._/@+-]*)?)$"
_ARTIFACT_PATH_PATTERN = r"^(?!/)(?!.*\\)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//).+$"
_ABSOLUTE_PATH_PATTERN = r"^(?:/|[A-Za-z]:[\\/])(?!.*(?:^|[\\/])\.{1,2}(?:[\\/]|$))(?!.*[\\/]{2}).+$"

MEMORYBENCH_REPOSITORY = "https://github.com/supermemoryai/memorybench"
MEMORYBENCH_COMMIT = "118209a746d97d0d85e5a7234267f0b6962857e9"
MEMORYBENCH_TREE = "2ee25bdbcb6bfaaecb32f917920c53775a299b37"
MEMORYBENCH_BUN_LOCK_SHA256 = "561d761fd16f895a6597227c6fc1e46064779284317fd479e079e3c86b9857da"

MemoryBenchProvider = Literal["basic-memory", "exomem"]
PhaseStatus = Literal["unobserved", "pending", "in_progress", "completed", "failed"]
ExportFailureCode = Literal[
    "stage_process_failed", "checkpoint_missing", "checkpoint_invalid",
    "checkpoint_identity_mismatch", "case_set_mismatch", "phase_incomplete",
    "phase_failed", "result_missing", "result_duplicate", "result_outside_root",
    "result_invalid", "result_identity_mismatch", "checkpoint_result_mismatch",
    "hit_invalid", "guest_evidence_invalid", "secure_descriptor_invalid",
    "private_gold_write_failed", "export_write_failed", "SIGINT", "SIGTERM",
]
CleanupFailureCode = Literal[
    "descriptor_missing", "descriptor_insecure", "descriptor_stale",
    "descriptor_binding_mismatch", "clear_failed", "namespace_absence_unproved",
    "corpus_absence_unproved", "config_absence_unproved",
    "process_group_absence_unproved", "work_root_absence_unproved",
    "cleanup_proof_write_failed",
]
MissingField = Literal[
    "question.question_date", "gold.answer_session_ids", "ingest.transmitted_payloads",
    "search.transmitted_query", "search.options.limit", "search.options.threshold",
    "search.normalized_hit_ids", "search.normalized_scores", "search.normalized_ranks",
    "search.retry_attempts", "search.http_status",
]
DiscoverySource = Literal["checkpoint", "guest_evidence", "secure_descriptor"]


def _require_sorted_unique(values: list[str], field: str) -> list[str]:
    if values != sorted(set(values)):
        raise ValueError(f"{field} must be sorted and unique")
    return values


def _require_sorted_unique_references(
    values: list[ArtifactReference], field: str
) -> list[ArtifactReference]:
    keys = [(value.root, value.path, value.path_hmac_sha256, value.sha256) for value in values]
    if keys != sorted(set(keys)):
        raise ValueError(f"{field} must be sorted and unique")
    return values


def _absolute_normalized(value: str, field: str) -> str:
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ValueError(f"{field} must be an absolute normalized path")
    return value


class MemoryBenchHarnessIdentity(StrictModel):
    repository: Literal[MEMORYBENCH_REPOSITORY]
    commit: Literal[MEMORYBENCH_COMMIT]
    tree: Literal[MEMORYBENCH_TREE]
    bun_lock_sha256: Literal[MEMORYBENCH_BUN_LOCK_SHA256]


class ProviderCheckoutIdentity(StrictModel):
    root: str
    repository: str = Field(pattern=_PUBLIC_SOURCE_PATTERN)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    lock_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("root")
    @classmethod
    def _root_absolute(cls, value: str) -> str:
        return _absolute_normalized(value, "provider_checkout.root")


class MemoryBenchSelection(StrictModel):
    mode: Literal["full", "explicit"]
    target_question_ids: list[str] | None

    @model_validator(mode="after")
    def _closed_union(self) -> MemoryBenchSelection:
        if self.mode == "full":
            if self.target_question_ids is not None:
                raise ValueError("full selection requires null target question IDs")
        elif (
            not self.target_question_ids
            or any(not isinstance(value, str) or not value for value in self.target_question_ids)
            or len(set(self.target_question_ids)) != len(self.target_question_ids)
        ):
            raise ValueError("explicit selection requires nonempty unique question IDs")
        return self


class MemoryBenchRunPlan(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[MEMORYBENCH_RUN_PLAN_SCHEMA_VERSION]
    artifact_type: Literal["memorybench-run-plan.v1"]
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    upstream_run_id: str = Field(pattern=_RUN_ID_PATTERN)
    provider: MemoryBenchProvider
    provider_variant: str = Field(min_length=1)
    benchmark: Literal["longmemeval"]
    selection: MemoryBenchSelection
    harness: MemoryBenchHarnessIdentity
    dataset: DatasetIdentity
    dataset_path: str
    provider_checkout: ProviderCheckoutIdentity
    memorybench_home: str
    output_root: str
    guest_work_root: str
    guest_evidence_root: str
    contract_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    preregistration_sha256: str = Field(pattern=_SHA256_PATTERN)
    privacy_hmac_key_hex: str = Field(pattern=_SHA256_PATTERN)

    @field_validator(
        "dataset_path", "memorybench_home", "output_root", "guest_work_root", "guest_evidence_root"
    )
    @classmethod
    def _paths_absolute(cls, value: str, info) -> str:
        return _absolute_normalized(value, info.field_name)

    @model_validator(mode="after")
    def _roots_are_distinct_and_contained(self) -> MemoryBenchRunPlan:
        output = Path(self.output_root)
        work = Path(self.guest_work_root)
        evidence = Path(self.guest_evidence_root)
        if work == evidence or work == output or evidence == output:
            raise ValueError("guest roots must be distinct strict children of output_root")
        if not work.is_relative_to(output) or not evidence.is_relative_to(output):
            raise ValueError("guest roots must be contained by output_root")
        if self.dataset.source.startswith(("/", "file:")) or "\\" in self.dataset.source:
            raise ValueError("dataset source must be a public HTTPS URI or registry identifier")
        return self


class ArtifactReference(StrictModel):
    root: Literal["memorybench_run", "output"]
    path: str | None = Field(
        json_schema_extra={"anyOf": [{"type": "string", "minLength": 1, "pattern": _ARTIFACT_PATH_PATTERN}, {"type": "null"}]}
    )
    path_hmac_sha256: str | None = Field(pattern=_SHA256_PATTERN)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def _path_is_canonical_relative(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or "//" in value
            or any(part in {".", ".."} for part in value.split("/"))
        ):
            raise ValueError("artifact path must be a canonical relative POSIX path")
        return value

    @model_validator(mode="after")
    def _root_path_union(self) -> ArtifactReference:
        if self.root == "output":
            if self.path is None or self.path_hmac_sha256 is not None:
                raise ValueError("output references require path and null path HMAC")
        elif self.path is not None or self.path_hmac_sha256 is None:
            raise ValueError("MemoryBench references require null path and path HMAC")
        return self


class MemoryBenchQuestion(StrictModel):
    text: str
    type: str
    date: str | None


class MemoryBenchPhase(StrictModel):
    status: PhaseStatus
    failure_code: ExportFailureCode | None

    @model_validator(mode="after")
    def _failure_code_matches_status(self) -> MemoryBenchPhase:
        if (self.status == "failed") != (self.failure_code is not None):
            raise ValueError("phase failure code contradicts phase status")
        return self


class MemoryBenchPhases(StrictModel):
    ingest: MemoryBenchPhase
    indexing: MemoryBenchPhase
    search: MemoryBenchPhase


class MemoryBenchHit(StrictModel):
    content: str = Field(min_length=1)
    score: float

    @field_validator("score")
    @classmethod
    def _finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("hit score must be finite")
        return value


class MemoryBenchSearchOptions(StrictModel):
    limit: int = Field(ge=1)


class MemoryBenchSearchObservation(StrictModel):
    """What the guest actually transmitted and got back, not what we assume."""

    transmitted_query: str = Field(min_length=1)
    options: MemoryBenchSearchOptions
    normalized_hit_ids: list[str]

    @model_validator(mode="after")
    def _hits_respect_the_transmitted_limit(self) -> "MemoryBenchSearchObservation":
        if len(self.normalized_hit_ids) > self.options.limit:
            raise ValueError("normalized hit ids exceed the transmitted search limit")
        return self


class MemoryBenchIngestObservation(StrictModel):
    transmitted_payload_sha256: list[str]

    @field_validator("transmitted_payload_sha256")
    @classmethod
    def _digests_are_sha256(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(_SHA256_PATTERN, item) for item in value):
            raise ValueError("transmitted payload digests must be sha256 hex")
        return value


#: Each optional observation owns the missing-field labels it answers for, so a
#: value can never be both published and declared absent.
_OBSERVATION_LABELS: dict[str, tuple[str, ...]] = {
    "search": (
        "search.normalized_hit_ids",
        "search.options.limit",
        "search.transmitted_query",
    ),
    "ingest": ("ingest.transmitted_payloads",),
}


class MemoryBenchExportCase(StrictModel):
    case_ordinal: int = Field(ge=1)
    case_id_hmac_sha256: str = Field(pattern=_SHA256_PATTERN)
    question: MemoryBenchQuestion
    container_tag_hmac_sha256: str | None = Field(pattern=_SHA256_PATTERN)
    checkpoint: ArtifactReference | None
    canonical_result: ArtifactReference | None
    private_gold: ArtifactReference | None
    phases: MemoryBenchPhases
    hits: list[MemoryBenchHit]
    failure_codes: list[ExportFailureCode]
    missing_fields: list[MissingField]
    search: MemoryBenchSearchObservation | None = None
    ingest: MemoryBenchIngestObservation | None = None

    @field_validator("failure_codes", "missing_fields")
    @classmethod
    def _canonical_arrays(cls, value: list[str], info):
        return _require_sorted_unique(value, info.field_name)

    @model_validator(mode="after")
    def _observations_agree_with_missing_fields(self) -> "MemoryBenchExportCase":
        declared = set(self.missing_fields)
        for name, labels in _OBSERVATION_LABELS.items():
            present = getattr(self, name) is not None
            overlap = declared & set(labels)
            if present and overlap:
                raise ValueError(
                    f"{name} is published but missing_fields still declares {sorted(overlap)}"
                )
            if not present and overlap != set(labels):
                raise ValueError(
                    f"{name} is absent, so missing_fields must declare {sorted(labels)}"
                )
        return self

    def is_complete(self) -> bool:
        return (
            self.container_tag_hmac_sha256 is not None
            and self.checkpoint is not None
            and self.canonical_result is not None
            and self.private_gold is not None
            and not self.failure_codes
            and all(
                getattr(self.phases, name).status == "completed"
                for name in ("ingest", "indexing", "search")
            )
        )


class MemoryBenchPrivacy(StrictModel):
    classification: Literal["provider_safe_reader_input"]
    contains_ground_truth: Literal[False]
    source_results_contain_ground_truth: Literal[True]


class MemoryBenchLatency(StrictModel):
    publishable: Literal[False]
    reason: Literal["host_unvalidated"]


class MemoryBenchExport(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[MEMORYBENCH_EXPORT_SCHEMA_VERSION]
    artifact_type: Literal["memorybench-export.v1"]
    status: Literal["complete", "partial"]
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    provider: MemoryBenchProvider
    provider_variant: str = Field(min_length=1)
    benchmark: Literal["longmemeval"]
    harness: MemoryBenchHarnessIdentity
    dataset: DatasetIdentity
    executed_stages: list[Literal["ingest", "indexing", "search"]]
    excluded_stages: list[Literal["answer", "evaluate", "report"]]
    privacy: MemoryBenchPrivacy
    latency: MemoryBenchLatency
    failure_codes: list[ExportFailureCode]
    cases: list[MemoryBenchExportCase] = Field(min_length=1)
    # Run-level facts the guest observes once, not per case.
    session_normalization: str | None = None
    readiness: list[LaneReadiness] | None = None

    @field_validator("failure_codes")
    @classmethod
    def _canonical_failures(cls, value: list[str]):
        return _require_sorted_unique(value, "failure_codes")

    @field_validator("executed_stages")
    @classmethod
    def _executed_stages_are_exact(cls, value: list[str]):
        if value != ["ingest", "indexing", "search"]:
            raise ValueError("executed stages must be ingest, indexing, search")
        return value

    @field_validator("excluded_stages")
    @classmethod
    def _excluded_stages_are_exact(cls, value: list[str]):
        if value != ["answer", "evaluate", "report"]:
            raise ValueError("excluded stages must be answer, evaluate, report")
        return value

    @model_validator(mode="after")
    def _status_recomputes(self) -> MemoryBenchExport:
        ordinals = [case.case_ordinal for case in self.cases]
        if ordinals != sorted(set(ordinals)):
            raise ValueError("cases must have unique ascending ordinals")
        complete = all(case.is_complete() for case in self.cases) and not self.failure_codes
        if (self.status == "complete") != complete:
            raise ValueError("export status contradicts evidence completeness")
        return self


class MemoryBenchPrivateGold(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[MEMORYBENCH_PRIVATE_GOLD_SCHEMA_VERSION]
    artifact_type: Literal["memorybench-private-gold.v1"]
    case_id_hmac_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_id: str
    container_tag: str
    question: str
    question_type: str
    ground_truth: str
    answer_session_ids: list[str] | None
    checkpoint_path: str = Field(min_length=1, json_schema_extra={"pattern": _ARTIFACT_PATH_PATTERN})
    checkpoint_sha256: str = Field(pattern=_SHA256_PATTERN)
    canonical_result_path: str = Field(min_length=1, json_schema_extra={"pattern": _ARTIFACT_PATH_PATTERN})
    canonical_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    missing_fields: list[Literal["gold.answer_session_ids"]]

    @field_validator("answer_session_ids", "missing_fields")
    @classmethod
    def _private_arrays(cls, value, info):
        if isinstance(value, list):
            if info.field_name == "answer_session_ids":
                if not value or len(set(value)) != len(value):
                    raise ValueError("present answer-session IDs must be nonempty and unique")
            else:
                _require_sorted_unique(value, info.field_name)
        return value

    @model_validator(mode="after")
    def _null_matrix(self) -> MemoryBenchPrivateGold:
        missing = "gold.answer_session_ids" in self.missing_fields
        if (self.answer_session_ids is None) != missing:
            raise ValueError("answer-session null/missing matrix is contradictory")
        return self

    @field_validator("checkpoint_path", "canonical_result_path")
    @classmethod
    def _source_path_is_safe(cls, value: str) -> str:
        if (
            value.startswith("/") or "\\" in value or "//" in value
            or any(part in {".", ".."} for part in value.split("/"))
        ):
            raise ValueError("private source path must be a canonical relative POSIX path")
        return value


class GuestCleanupTargetPlan(StrictModel):
    container_tag: str = Field(min_length=1)
    container_tag_hmac_sha256: str = Field(pattern=_SHA256_PATTERN)
    discovery_sources: list[DiscoverySource] = Field(min_length=1)
    namespace_expected: bool

    @field_validator("discovery_sources")
    @classmethod
    def _sources_canonical(cls, value: list[str]):
        return _require_sorted_unique(value, "discovery_sources")

class GuestCleanupPlan(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[GUEST_CLEANUP_PLAN_SCHEMA_VERSION]
    artifact_type: Literal["guest-cleanup-plan.v1"]
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    provider: MemoryBenchProvider
    provider_variant: str = Field(min_length=1)
    guest_work_root: str
    guest_evidence_root: str
    run_plan_path: str
    run_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    targets: list[GuestCleanupTargetPlan]

    @field_validator("guest_work_root", "guest_evidence_root", "run_plan_path")
    @classmethod
    def _private_paths_absolute(cls, value: str, info):
        return _absolute_normalized(value, info.field_name)

    @model_validator(mode="after")
    def _targets_canonical(self) -> GuestCleanupPlan:
        digests = [target.container_tag_hmac_sha256 for target in self.targets]
        if digests != sorted(set(digests)):
            raise ValueError("cleanup targets must be digest-sorted and unique")
        return self


class GuestCleanupAbsence(StrictModel):
    namespace: bool | None
    corpus: bool | None
    config: bool | None
    descriptor: bool | None
    process_group: bool | None
    work_root: bool | None


class GuestCleanupTargetProof(StrictModel):
    container_tag_hmac_sha256: str = Field(pattern=_SHA256_PATTERN)
    discovery_sources: list[DiscoverySource] = Field(min_length=1)
    outcome: Literal["cleared", "already_absent", "clear_failed", "absence_unproved"]
    failure_code: CleanupFailureCode | None
    artifacts: list[ArtifactReference]
    absence: GuestCleanupAbsence

    @field_validator("discovery_sources")
    @classmethod
    def _proof_sources_canonical(cls, value: list[str]):
        return _require_sorted_unique(value, "discovery_sources")

    @field_validator("artifacts")
    @classmethod
    def _artifacts_canonical(cls, value: list[ArtifactReference]):
        return _require_sorted_unique_references(value, "artifacts")

    @model_validator(mode="after")
    def _outcome_failure_matrix(self) -> GuestCleanupTargetProof:
        failed = self.outcome in {"clear_failed", "absence_unproved"}
        if failed != (self.failure_code is not None):
            raise ValueError("cleanup outcome contradicts failure code")
        return self


class GuestFinalAbsence(StrictModel):
    config: bool
    descriptor: bool
    process_group: bool
    work_root: bool
    artifacts: list[ArtifactReference]

    @field_validator("artifacts")
    @classmethod
    def _artifacts_canonical(cls, value: list[ArtifactReference]):
        return _require_sorted_unique_references(value, "artifacts")


class GuestCleanup(StrictModel):
    protocol_version: Literal[PROTOCOL_VERSION]
    schema_version: Literal[GUEST_CLEANUP_SCHEMA_VERSION]
    artifact_type: Literal["guest-cleanup.v1"]
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    provider: MemoryBenchProvider
    provider_variant: str = Field(min_length=1)
    trigger: Literal["success", "stage_failure", "export_failure", "SIGINT", "SIGTERM"]
    targets: list[GuestCleanupTargetProof]
    basic_public_cleanup_calls: int = Field(ge=0)
    failure_codes: list[CleanupFailureCode]
    final_absence: GuestFinalAbsence
    all_absent: bool

    @field_validator("failure_codes")
    @classmethod
    def _cleanup_failures_canonical(cls, value: list[str]):
        return _require_sorted_unique(value, "failure_codes")

    @model_validator(mode="after")
    def _recompute_absence(self) -> GuestCleanup:
        digests = [target.container_tag_hmac_sha256 for target in self.targets]
        if digests != sorted(set(digests)):
            raise ValueError("cleanup proof targets must be digest-sorted and unique")
        for target in self.targets:
            absence = target.absence
            if self.provider == "basic-memory":
                if any(value is not None for value in (
                    absence.config, absence.descriptor, absence.process_group, absence.work_root
                )) or absence.corpus is None:
                    raise ValueError("Basic cleanup applicability matrix is invalid")
            elif absence.corpus is not None or absence.config is not None or any(
                value is None for value in (absence.descriptor, absence.process_group, absence.work_root)
            ):
                raise ValueError("Exomem cleanup applicability matrix is invalid")
            if target.outcome == "already_absent":
                if self.provider == "basic-memory":
                    if absence.namespace is not True or absence.corpus is not True:
                        raise ValueError("Basic already_absent requires namespace and corpus absence")
                elif absence.descriptor is not True or absence.work_root is not True:
                    raise ValueError("Exomem already_absent requires descriptor and work-root absence")
        if self.provider == "exomem":
            count_ok = self.basic_public_cleanup_calls == 0
        else:
            failed = (
                any(target.outcome in {"clear_failed", "absence_unproved"} for target in self.targets)
                or bool(self.failure_codes)
                or any(value is False for value in (
                    self.final_absence.config,
                    self.final_absence.descriptor,
                    self.final_absence.process_group,
                    self.final_absence.work_root,
                ))
            )
            if failed:
                count_ok = self.basic_public_cleanup_calls in {0, 1}
            else:
                all_already = all(target.outcome == "already_absent" for target in self.targets)
                expected = 0 if all_already else 1
                count_ok = self.basic_public_cleanup_calls == expected
        if not count_ok:
            raise ValueError("provider cleanup-call count contradicts target outcomes")
        target_ok = all(
            target.outcome not in {"clear_failed", "absence_unproved"}
            and target.absence.namespace is True
            and (target.absence.corpus in {True, None})
            and (target.absence.config in {True, None})
            and (target.absence.descriptor in {True, None})
            and (target.absence.process_group in {True, None})
            and (target.absence.work_root in {True, None})
            for target in self.targets
        )
        final_ok = all((
            self.final_absence.config, self.final_absence.descriptor,
            self.final_absence.process_group, self.final_absence.work_root,
        ))
        computed = target_ok and final_ok and not self.failure_codes
        if self.all_absent != computed:
            raise ValueError("all_absent contradicts recomputed absence")
        return self


SCHEMA_EXPORTS: dict[str, type[BaseModel]] = {
    "protocol-event": ProtocolEvent, "case-gold": CaseGold, "run-manifest": RunManifest,
    "case-trace": CaseTrace, "readiness-report": ReadinessReport, "probe-result": ProbeResult,
    "case-trace-v2": CaseTraceV2,
    "provider-cleanup-observation": ProviderCleanupObservation,
    "budget-ledger": BudgetLedgerEntry, "equivalence-diff": EquivalenceDiff,
    "equivalence-exception": EquivalenceException, "gap-report": GapReport,
    "memorybench-run-plan": MemoryBenchRunPlan,
    "lme-selection": LmeSelection,
    "memorybench-export": MemoryBenchExport,
    "memorybench-private-gold": MemoryBenchPrivateGold,
    "guest-cleanup-plan": GuestCleanupPlan,
    "guest-cleanup": GuestCleanup,
}


def _non_null() -> dict[str, Any]:
    return {"not": {"type": "null"}}


def _phase_completed() -> dict[str, Any]:
    return {
        "properties": {
            "status": {"const": "completed"},
            "failure_code": {"type": "null"},
        },
        "required": ["status", "failure_code"],
    }


def _complete_export_evidence() -> dict[str, Any]:
    complete_case = {
        "properties": {
            "container_tag_hmac_sha256": _non_null(),
            "checkpoint": _non_null(),
            "canonical_result": _non_null(),
            "private_gold": _non_null(),
            "failure_codes": {"maxItems": 0},
            "phases": {
                "properties": {
                    "ingest": _phase_completed(),
                    "indexing": _phase_completed(),
                    "search": _phase_completed(),
                },
                "required": ["ingest", "indexing", "search"],
            },
        },
        "required": [
            "container_tag_hmac_sha256", "checkpoint", "canonical_result", "private_gold",
            "failure_codes", "phases",
        ],
    }
    return {
        "properties": {
            "failure_codes": {"maxItems": 0},
            "cases": {"items": complete_case},
        },
        "required": ["failure_codes", "cases"],
    }


def _target_outcome_rules() -> list[dict[str, Any]]:
    return [{
            "if": {
                "properties": {"outcome": {"enum": ["clear_failed", "absence_unproved"]}},
                "required": ["outcome"],
            },
            "then": {"properties": {"failure_code": _non_null()}},
            "else": {"properties": {"failure_code": {"type": "null"}}},
        }]


def _phase_status_rule() -> dict[str, Any]:
    return {
        "if": {"properties": {"status": {"const": "failed"}}, "required": ["status"]},
        "then": {"properties": {"failure_code": _non_null()}},
        "else": {"properties": {"failure_code": {"type": "null"}}},
    }


def _basic_cleanup_count_rule() -> dict[str, Any]:
    any_failed_target = {
        "properties": {
            "targets": {
                "contains": {
                    "properties": {
                        "outcome": {"enum": ["clear_failed", "absence_unproved"]}
                    },
                    "required": ["outcome"],
                }
            }
        },
        "required": ["targets"],
    }
    any_failure_code = {
        "properties": {"failure_codes": {"minItems": 1}},
        "required": ["failure_codes"],
    }
    any_failed_final_surface = {
        "properties": {
            "final_absence": {
                "anyOf": [
                    {"properties": {surface: {"const": False}}, "required": [surface]}
                    for surface in ("config", "descriptor", "process_group", "work_root")
                ]
            }
        },
        "required": ["final_absence"],
    }
    any_failed_proof = {
        "anyOf": [any_failed_target, any_failure_code, any_failed_final_surface],
    }
    every_target_already_absent = {
        "properties": {
            "targets": {
                "not": {
                    "contains": {
                        "properties": {"outcome": {"not": {"const": "already_absent"}}},
                        "required": ["outcome"],
                    }
                }
            }
        },
        "required": ["targets"],
    }
    success_count = {
        "if": every_target_already_absent,
        "then": {"properties": {"basic_public_cleanup_calls": {"const": 0}}},
        "else": {"properties": {"basic_public_cleanup_calls": {"const": 1}}},
    }
    return {
        "if": any_failed_proof,
        "then": {"properties": {"basic_public_cleanup_calls": {"minimum": 0, "maximum": 1}}},
        "else": success_count,
    }


def _cleanup_provider_rule(provider: str) -> dict[str, Any]:
    if provider == "basic-memory":
        absence = {
            "properties": {
                "namespace": {"type": "boolean"},
                "corpus": {"type": "boolean"},
                "config": {"type": "null"},
                "descriptor": {"type": "null"},
                "process_group": {"type": "null"},
                "work_root": {"type": "null"},
            }
        }
        already_rule = {
            "if": {"properties": {"outcome": {"const": "already_absent"}}, "required": ["outcome"]},
            "then": {"properties": {"absence": {"properties": {
                "namespace": {"const": True}, "corpus": {"const": True},
            }}}},
        }
        return {
            "properties": {"targets": {"items": {
                "properties": {"absence": absence}, "allOf": [already_rule],
            }}},
            "allOf": [_basic_cleanup_count_rule()],
        }
    absence = {
        "properties": {
            "namespace": {"type": "boolean"},
            "corpus": {"type": "null"},
            "config": {"type": "null"},
            "descriptor": {"type": "boolean"},
            "process_group": {"type": "boolean"},
            "work_root": {"type": "boolean"},
        }
    }
    already_rule = {
        "if": {"properties": {"outcome": {"const": "already_absent"}}, "required": ["outcome"]},
        "then": {"properties": {"absence": {"properties": {
            "descriptor": {"const": True}, "work_root": {"const": True},
        }}}},
    }
    return {
        "properties": {
            "targets": {"items": {
                "properties": {"absence": absence}, "allOf": [already_rule],
            }},
            "basic_public_cleanup_calls": {"const": 0},
        }
    }


def _cleanup_success_condition(provider: str) -> dict[str, Any]:
    if provider == "basic-memory":
        target_absence = {
            "namespace": {"const": True}, "corpus": {"const": True},
            "config": {"type": "null"}, "descriptor": {"type": "null"},
            "process_group": {"type": "null"}, "work_root": {"type": "null"},
        }
        count_rule = _basic_cleanup_count_rule()
    else:
        target_absence = {
            "namespace": {"const": True}, "corpus": {"type": "null"},
            "config": {"type": "null"}, "descriptor": {"const": True},
            "process_group": {"const": True}, "work_root": {"const": True},
        }
        count_rule = {"properties": {"basic_public_cleanup_calls": {"const": 0}}}
    return {
        "properties": {
            "provider": {"const": provider},
            "failure_codes": {"maxItems": 0},
            "targets": {
                "items": {
                    "properties": {
                        "outcome": {"enum": ["cleared", "already_absent"]},
                        "failure_code": {"type": "null"},
                        "absence": {"properties": target_absence},
                    },
                    "required": ["outcome", "failure_code", "absence"],
                }
            },
            "final_absence": {
                "properties": {
                    "config": {"const": True}, "descriptor": {"const": True},
                    "process_group": {"const": True}, "work_root": {"const": True},
                },
                "required": ["config", "descriptor", "process_group", "work_root"],
            },
        },
        "required": ["provider", "failure_codes", "targets", "final_absence"],
        "allOf": [count_rule],
    }


def _enhance_memorybench_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Add standard Draft 2020-12 constraints that Pydantic cannot infer."""

    defs = schema.get("$defs", {})
    artifact_reference = defs.get("ArtifactReference")
    if artifact_reference is not None:
        artifact_reference.setdefault("allOf", []).append({
            "if": {"properties": {"root": {"const": "output"}}, "required": ["root"]},
            "then": {
                "properties": {
                    "path": {"type": "string", "minLength": 1, "pattern": _ARTIFACT_PATH_PATTERN},
                    "path_hmac_sha256": {"type": "null"},
                }
            },
            "else": {
                "properties": {
                    "path": {"type": "null"},
                    "path_hmac_sha256": {"type": "string", "pattern": _SHA256_PATTERN},
                }
            },
        })
    for definition in defs.values():
        properties = definition.get("properties", {})
        for field in ("failure_codes", "missing_fields", "discovery_sources", "artifacts"):
            if field in properties and properties[field].get("type") == "array":
                properties[field]["uniqueItems"] = True

    if name == "memorybench-run-plan":
        defs["DatasetIdentity"]["properties"]["source"]["pattern"] = _PUBLIC_SOURCE_PATTERN
        for field in (
            "dataset_path", "memorybench_home", "output_root", "guest_work_root",
            "guest_evidence_root",
        ):
            schema["properties"][field]["pattern"] = _ABSOLUTE_PATH_PATTERN
        defs["ProviderCheckoutIdentity"]["properties"]["root"]["pattern"] = _ABSOLUTE_PATH_PATTERN
        selection = defs["MemoryBenchSelection"]
        for branch in selection["properties"]["target_question_ids"].get("anyOf", []):
            if branch.get("type") == "array":
                branch.update({"minItems": 1, "uniqueItems": True})
        selection.setdefault("allOf", []).append({
            "if": {"properties": {"mode": {"const": "full"}}, "required": ["mode"]},
            "then": {"properties": {"target_question_ids": {"type": "null"}}},
            "else": {"properties": {"target_question_ids": {
                "type": "array", "minItems": 1, "uniqueItems": True,
            }}},
        })
    elif name == "lme-selection":
        source = defs["LmeSelectionSource"]
        if "abstention_count" not in source["required"]:
            source["required"].append("abstention_count")
        source["properties"]["type_census"] = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "knowledge-update": {"const": 78}, "multi-session": {"const": 133},
                "single-session-assistant": {"const": 56}, "single-session-preference": {"const": 30},
                "single-session-user": {"const": 70}, "temporal-reasoning": {"const": 133},
            },
            "required": ["knowledge-update", "multi-session", "single-session-assistant", "single-session-preference", "single-session-user", "temporal-reasoning"],
        }
        schema["properties"]["question_type_order"] = {"const": ["single-session-user", "single-session-assistant", "single-session-preference", "multi-session", "temporal-reasoning", "knowledge-update"]}
        schema["properties"]["quotas"] = {"type": "object", "additionalProperties": False, "properties": {**{item: {"const": 3} for item in ("single-session-user", "single-session-assistant", "single-session-preference", "multi-session", "temporal-reasoning", "knowledge-update")}, "abstention": {"const": 7}}, "required": ["single-session-user", "single-session-assistant", "single-session-preference", "multi-session", "temporal-reasoning", "knowledge-update", "abstention"]}
        schema["properties"]["target_question_ids"].update({"minItems": 25, "maxItems": 25, "uniqueItems": True, "items": {"type": "string", "minLength": 1}})
    elif name == "memorybench-export":
        schema["properties"]["executed_stages"]["const"] = ["ingest", "indexing", "search"]
        schema["properties"]["excluded_stages"]["const"] = ["answer", "evaluate", "report"]
        schema["properties"]["failure_codes"]["uniqueItems"] = True
        schema["properties"]["cases"]["uniqueItems"] = True
        phase = defs["MemoryBenchPhase"]
        phase.setdefault("allOf", []).append(_phase_status_rule())
        complete = _complete_export_evidence()
        schema.setdefault("allOf", []).append({
            "if": {"properties": {"status": {"const": "complete"}}, "required": ["status"]},
            "then": complete,
            "else": {"not": complete},
        })
    elif name == "memorybench-private-gold":
        schema["properties"]["missing_fields"]["uniqueItems"] = True
        answer_ids = schema["properties"]["answer_session_ids"]
        for branch in answer_ids.get("anyOf", []):
            if branch.get("type") == "array":
                branch.update({"minItems": 1, "uniqueItems": True})
        schema.setdefault("allOf", []).append({
            "if": {"properties": {"answer_session_ids": {"type": "null"}}, "required": ["answer_session_ids"]},
            "then": {"properties": {"missing_fields": {"contains": {"const": "gold.answer_session_ids"}}}},
            "else": {"properties": {"missing_fields": {"not": {"contains": {"const": "gold.answer_session_ids"}}}}},
        })
    elif name == "guest-cleanup-plan":
        for field in ("guest_work_root", "guest_evidence_root", "run_plan_path"):
            schema["properties"][field]["pattern"] = _ABSOLUTE_PATH_PATTERN
        schema["properties"]["targets"]["uniqueItems"] = True
        defs["GuestCleanupTargetPlan"]["properties"]["discovery_sources"]["uniqueItems"] = True
    elif name == "guest-cleanup":
        schema["properties"]["targets"]["uniqueItems"] = True
        schema["properties"]["failure_codes"]["uniqueItems"] = True
        target = defs["GuestCleanupTargetProof"]
        target["properties"]["discovery_sources"]["uniqueItems"] = True
        target["properties"]["artifacts"]["uniqueItems"] = True
        target.setdefault("allOf", []).extend(_target_outcome_rules())
        defs["GuestFinalAbsence"]["properties"]["artifacts"]["uniqueItems"] = True
        schema.setdefault("allOf", []).extend([
            {
                "if": {"properties": {"provider": {"const": "basic-memory"}}, "required": ["provider"]},
                "then": _cleanup_provider_rule("basic-memory"),
                "else": _cleanup_provider_rule("exomem"),
            },
            {
                "if": {
                    "anyOf": [
                        _cleanup_success_condition("basic-memory"),
                        _cleanup_success_condition("exomem"),
                    ]
                },
                "then": {"properties": {"all_absent": {"const": True}}},
                "else": {"properties": {"all_absent": {"const": False}}},
            },
        ])
    elif name == "provider-cleanup-observation":
        schema["properties"]["required_surface_ids"]["uniqueItems"] = True
        definitions = schema["$defs"]
        definitions["ProviderCleanupNamespaceMembership"]["properties"]["live_namespaces"]["uniqueItems"] = True
        definitions["ProviderCleanupProviderState"]["properties"]["remaining_record_ids"]["uniqueItems"] = True
        definitions["ProviderCleanupPathLstat"]["properties"]["entries"]["uniqueItems"] = True
        definitions["ProviderCleanupPathLstat"]["properties"]["path"]["pattern"] = r"^(?!/)(?!.*[\\\\])(?!.*//)(?!.*\/$)(?!\\.?\.?$)(?!.*(?:^|/)\.{1,2}(?:/|$)).+$"
    elif name == "case-trace-v2":
        schema["$defs"]["CleanupRecordV2"]["properties"]["observation_path"]["pattern"] = r"^(?!/)(?!.*[\\\\])(?!.*//)(?!.*\/$)(?!\\.?\.?$)(?!.*(?:^|/)\.{1,2}(?:/|$)).+$"
    return schema


def export_json_schemas(out_dir: Path) -> list[Path]:
    """Write stable, sorted JSON Schema files named from declared schema versions."""

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in sorted(SCHEMA_EXPORTS.items()):
        version = get_args(model.model_fields["schema_version"].annotation)[0]
        basename = "case-trace" if name == "case-trace-v2" else name
        path = out_dir / f"{basename}.v{version}.schema.json"
        schema = _enhance_memorybench_schema(name, model.model_json_schema())
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
