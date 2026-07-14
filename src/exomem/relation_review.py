"""Portable reviewed-none state and the internal semantic creation coordinator."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

from . import (
    activation_manifest,
    memory_schema,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    vault,
)
from .kbdir import kb_dirname
from .memory_refs import ID_FIELD, new_id, normalize_id

_SCHEMA_VERSION = 2
_OPERATIONS = frozenset({"create", "replacement", "adoption_compile", "tier2_create"})
_KINDS = frozenset({"reviewed_none", "bootstrap", "qualifying"})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARTIFACT_BYTES = 16 * 1024
_MAX_REASON_POINTS = 2_000
_MAX_REASON_BYTES = 8_192
_MAX_CANDIDATES = 64
_MAX_RAW_TARGET = 1_024
MAX_DRAFT_TOKEN_ENCODED_BYTES = 16_384
_LIFECYCLE_SCHEMA_VERSION = 1
_LIFECYCLE_CONTRACT_VERSION = 1
_LIFECYCLE_MAX_DECISIONS = 256
_LIFECYCLE_MAX_RESIDUE = 3
_LIFECYCLE_MAX_DIRECTORY_ENTRIES = (
    _LIFECYCLE_MAX_DECISIONS + 1 + _LIFECYCLE_MAX_RESIDUE
)
_LIFECYCLE_MAX_IDENTITIES = 4_096
_LIFECYCLE_TRASH_MAX_DIRECTORY_ENTRIES = 1_024
_LIFECYCLE_TRASH_MAX_DIRECTORIES = 4_096
_LIFECYCLE_TRASH_MAX_FILES = 16_384
_LIFECYCLE_TRASH_MAX_SIDECAR_BYTES = 2 * 1024 * 1024
_LIFECYCLE_TRASH_MAX_SIDECAR_AGGREGATE_BYTES = 32 * 1024 * 1024
_LIFECYCLE_ATOMIC_RESIDUE = re.compile(
    r"^\.(?:prepared\.json|[0-9a-f]{64}\.json)\.[a-z0-9_]{8}\.(?:tmp|bak)$"
)
_LIFECYCLE_OPERATIONS = frozenset(
    {"edit", "tier2_overwrite", "tier2_append", "move", "recover"}
)
_LIFECYCLE_DECISION_KEYS = frozenset(
    {
        "schema_version",
        "contract_version",
        "kind",
        "page_identity",
        "after_fingerprint",
        "decision_hash",
        "reason",
    }
)
_LIFECYCLE_PREPARED_KEYS = frozenset(
    {
        "schema_version",
        "contract_version",
        "kind",
        "transition_id",
        "operation",
        "page_identity",
        "before_path",
        "before_source_hash",
        "after_path",
        "after_source_hash",
        "after_fingerprint",
        "decision_reference",
        "decision_bytes_hash",
        "transition_token_hash",
        "auxiliary_hash",
        "carried_from",
    }
)
_SUPPORTS_REVIEW_DIR_FD = bool(
    os.open in getattr(os, "supports_dir_fd", set())
    and os.stat in getattr(os, "supports_dir_fd", set())
)
_V1_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "page_identity",
        "page_path_at_review",
        "content_fingerprint",
        "draft_hash",
        "auxiliary_hash",
        "reason",
    }
)
_V2_RECORD_KEYS = _V1_RECORD_KEYS | frozenset(
    {
        "operation",
        "draft_token_hash",
        "predecessor_path",
        "predecessor_content_hash",
    }
)


@dataclass
class RelationReviewError(ValueError):
    code: str
    reason: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RelationCandidate:
    direction: Literal["inbound", "outbound"]
    fact_identity: str
    logical_source_path: str
    logical_target_path: str
    raw_relation: str
    canonical_relation: str | None
    raw_target: str
    resolved_target_path: str | None
    target_status: str
    qualifies: bool
    reasons: tuple[str, ...]
    target_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "fact_identity": self.fact_identity,
            "logical_source_path": self.logical_source_path,
            "logical_target_path": self.logical_target_path,
            "raw_relation": self.raw_relation,
            "canonical_relation": self.canonical_relation,
            "raw_target": self.raw_target,
            "resolved_target_path": self.resolved_target_path,
            "target_status": self.target_status,
            "qualifies": self.qualifies,
            "reasons": list(self.reasons),
            "target_truncated": self.target_truncated,
        }


@dataclass(frozen=True, slots=True)
class CreationDraftValidation:
    draft_id: str
    draft_hash: str
    content_fingerprint: str
    destination: str
    mutated: Literal[False]
    relation_disposition: str
    reviewed_none_required: bool
    has_non_review_blockers: bool
    committable_without_review: bool
    committable_after_review: bool
    relation_candidates: tuple[RelationCandidate, ...]
    candidate_total: int
    candidates_truncated: bool
    contract_result: semantic_contract.SemanticContractResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "draft_hash": self.draft_hash,
            "content_fingerprint": self.content_fingerprint,
            "destination": self.destination,
            "mutated": self.mutated,
            "relation_disposition": self.relation_disposition,
            "reviewed_none_required": self.reviewed_none_required,
            "has_non_review_blockers": self.has_non_review_blockers,
            "committable_without_review": self.committable_without_review,
            "committable_after_review": self.committable_after_review,
            "relation_candidates": [item.as_dict() for item in self.relation_candidates],
            "candidate_total": self.candidate_total,
            "candidates_truncated": self.candidates_truncated,
            "contract_result": self.contract_result.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CreationDraftCommit:
    draft_id: str
    draft_hash: str
    content_fingerprint: str
    destination: str
    mutated: Literal[True]
    relation_disposition: str
    review_reference: str | None
    review_state_current: bool
    resumed_prepared: bool
    written_paths: tuple[str, ...]
    contract_result: semantic_contract.SemanticContractResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "draft_hash": self.draft_hash,
            "content_fingerprint": self.content_fingerprint,
            "destination": self.destination,
            "mutated": self.mutated,
            "relation_disposition": self.relation_disposition,
            "review_reference": self.review_reference,
            "review_state_current": self.review_state_current,
            "resumed_prepared": self.resumed_prepared,
            "written_paths": list(self.written_paths),
            "contract_result": self.contract_result.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RelationReviewRecord:
    schema_version: int
    kind: Literal["reviewed_none", "bootstrap", "qualifying"]
    page_identity: str
    page_path_at_review: str
    content_fingerprint: str
    draft_hash: str
    auxiliary_hash: str
    reason: str | None
    reference: str
    operation: str | None = None
    draft_token_hash: str | None = None
    predecessor_path: str | None = None
    predecessor_content_hash: str | None = None

    def storage_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "page_identity": self.page_identity,
            "page_path_at_review": self.page_path_at_review,
            "content_fingerprint": self.content_fingerprint,
            "draft_hash": self.draft_hash,
            "auxiliary_hash": self.auxiliary_hash,
            "reason": self.reason,
        }
        if self.schema_version >= 2:
            value.update(
                {
                    "operation": self.operation,
                    "draft_token_hash": self.draft_token_hash,
                    "predecessor_path": self.predecessor_path,
                    "predecessor_content_hash": self.predecessor_content_hash,
                }
            )
        return value

    def as_dict(self) -> dict[str, Any]:
        return {**self.storage_dict(), "reference": self.reference}


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    schema_version: int
    contract_version: int
    kind: Literal["reviewed_none"]
    page_identity: str
    after_fingerprint: str
    decision_hash: str
    reason: str
    reference: str

    def storage_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "kind": self.kind,
            "page_identity": self.page_identity,
            "after_fingerprint": self.after_fingerprint,
            "decision_hash": self.decision_hash,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class LifecyclePreparedTransition:
    schema_version: int
    contract_version: int
    kind: Literal["prepared_transition"]
    transition_id: str
    operation: str
    page_identity: str
    before_path: str
    before_source_hash: str
    after_path: str
    after_source_hash: str
    after_fingerprint: str
    decision_reference: str | None
    decision_bytes_hash: str | None
    transition_token_hash: str
    auxiliary_hash: str
    carried_from: str | None
    reference: str

    def storage_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "kind": self.kind,
            "transition_id": self.transition_id,
            "operation": self.operation,
            "page_identity": self.page_identity,
            "before_path": self.before_path,
            "before_source_hash": self.before_source_hash,
            "after_path": self.after_path,
            "after_source_hash": self.after_source_hash,
            "after_fingerprint": self.after_fingerprint,
            "decision_reference": self.decision_reference,
            "decision_bytes_hash": self.decision_bytes_hash,
            "transition_token_hash": self.transition_token_hash,
            "auxiliary_hash": self.auxiliary_hash,
            "carried_from": self.carried_from,
        }


@dataclass(frozen=True, slots=True)
class LifecyclePrimaryBinding:
    path: str
    source_hash: str
    review_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class LifecycleTrashProof:
    """Exact proof that a committed primary was moved to a guarded trash entry."""

    page_identity: str
    original_path: str
    trash_path: str
    source_hash: str
    review_fingerprint: str
    source_guard: vault.PathGuard
    sidecar_source: str
    sidecar_guard: vault.PathGuard
    live_owner_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LifecycleTransitionPlan:
    state: Literal[
        "new", "pending_retry", "replace_committed", "committed_replay"
    ]
    decision: LifecycleDecision | None
    prepared: LifecyclePreparedTransition
    writes: tuple[vault.PlannedWrite, ...]
    required_guards: tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...]


@dataclass(frozen=True, slots=True)
class LifecyclePreparedInspection:
    """One directly loaded crash slot bound to its primary and trash snapshot."""

    prepared: LifecyclePreparedTransition
    state: Literal["pending", "committed", "trashed_committed", "stale"]
    cleanup_eligible: bool
    live_owner_paths: tuple[str, ...]
    prepared_guard: vault.PathGuard
    primary_guards: tuple[vault.PathGuard, ...]
    trash_guards: tuple[vault.DirectoryCensusGuard, ...]
    trash_content_guards: tuple[vault.PathGuard, ...]
    trash_target_guards: tuple[vault.PathGuard, ...]
    trash_proof: LifecycleTrashProof | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_identity": self.prepared.page_identity,
            "state": self.state,
            "reference": self.prepared.reference,
            "cleanup_eligible": self.cleanup_eligible,
        }


@dataclass(frozen=True, slots=True)
class LifecyclePreparedIssue:
    code: str
    page_identity: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {"code": self.code}
        if self.page_identity is not None:
            value["page_identity"] = self.page_identity
        return value


@dataclass(frozen=True, slots=True)
class LifecyclePreparedBatch:
    inspections: tuple[LifecyclePreparedInspection, ...]
    issues: tuple[LifecyclePreparedIssue, ...]
    cleanup_safe: bool


@dataclass(frozen=True, slots=True)
class LifecyclePreparedCleanupBatch:
    cleaned: tuple[str, ...]
    blocked: tuple[LifecyclePreparedIssue, ...]


@dataclass(frozen=True, slots=True)
class _Attempt:
    source: str
    destination: str
    candidate: semantic_contract.SemanticPageState
    before_corpus: semantic_contract.SemanticCorpusContext
    after_corpus: semantic_contract.SemanticCorpusContext
    contracts: memory_schema.ResolvedMemoryContracts
    result: semantic_contract.SemanticContractResult
    validation: CreationDraftValidation
    artifact: RelationReviewRecord | None
    artifact_bytes_hash: str | None
    lifecycle_guard: vault.DirectoryCensusGuard | None


class _DuplicateJsonKey(ValueError):
    pass


def new_draft_id() -> str:
    return new_id()


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def _normalize_source(source: str) -> str:
    if type(source) is not str:
        raise RelationReviewError("INVALID_DRAFT_SOURCE", "draft source must be text")
    if "\0" in source:
        raise RelationReviewError("INVALID_DRAFT_SOURCE", "draft source contains NUL")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RelationReviewError(
            "INVALID_DRAFT_SOURCE", "draft source contains invalid Unicode"
        ) from error
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if normalized.endswith("\n") else normalized + "\n"


def _canonical_id(value: object, *, code: str = "DRAFT_IDENTITY_MISMATCH") -> str:
    if type(value) is not str or normalize_id(value) != value:
        raise RelationReviewError(code, "identity must be an exact canonical UUID")
    return value


def _normalize_destination(root: Path, path: object) -> tuple[Path, str]:
    if type(path) is not str or not path or "\0" in path or "\\" in path:
        raise RelationReviewError("INVALID_DRAFT_PATH", "draft path is not a safe relative path")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise RelationReviewError("INVALID_DRAFT_PATH", "draft path is not a safe relative path")
    posix = PurePosixPath(path)
    if (
        posix.is_absolute()
        or any(part in {"", ".", ".."} for part in posix.parts)
        or posix.suffix.casefold() != ".md"
        or not posix.parts
        or posix.parts[0] != kb_dirname()
    ):
        raise RelationReviewError("INVALID_DRAFT_PATH", "draft path is not a managed Markdown path")
    try:
        absolute, relative = vault.resolve_under_vault(root, path, must_be_under_kb=True)
    except vault.VaultPathError as error:
        lexical = root / path
        if os.path.lexists(lexical):
            return lexical, path
        raise RelationReviewError(
            "INVALID_DRAFT_PATH", "draft path is outside the vault"
        ) from error
    return absolute, relative


def _validate_identity(source: str, draft_id: str) -> None:
    try:
        frontmatter, _, _ = vault.parse_frontmatter(source, strict=True)
    except vault.FrontmatterError as error:
        raise RelationReviewError(error.code, "draft frontmatter is invalid") from error
    raw = frontmatter.get(ID_FIELD)
    if type(raw) is not str or raw != draft_id or normalize_id(raw) != raw:
        raise RelationReviewError(
            "DRAFT_IDENTITY_MISMATCH",
            "draft frontmatter identity does not exactly match draft_id",
        )


_EMPTY_DRAFT_TOKEN_HASH = hashlib.sha256(b"").hexdigest()


def _draft_hash(
    draft_id: str,
    destination: str,
    fingerprint: str,
    draft_token_hash: str | None = None,
) -> str:
    value: dict[str, object] = {
        "schema_version": 1,
        "draft_id": draft_id,
        "destination": destination,
        "content_fingerprint": fingerprint,
    }
    if draft_token_hash not in {None, _EMPTY_DRAFT_TOKEN_HASH}:
        value["schema_version"] = 2
        value["draft_token_hash"] = draft_token_hash
    return _canonical_hash(value)


def _auxiliary_hash(writes: tuple[vault.PlannedWrite, ...], root: Path) -> str:
    return _canonical_hash(
        {
            "schema_version": 1,
            "writes": [
                {
                    "path": write.path.relative_to(root).as_posix(),
                    "content_hash": hashlib.sha256(write.content.encode("utf-8")).hexdigest(),
                }
                for write in writes
            ],
        }
    )


def lifecycle_auxiliary_hash(
    writes: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite],
    vault_root: Path,
) -> str:
    """Return the canonical ordered-write digest used by lifecycle records."""
    return _auxiliary_hash(tuple(writes), Path(vault_root))


def draft_token_hash(value: object) -> str:
    """Hash a bounded encoded draft token using the coordinator's public contract."""
    if (
        type(value) is not str
        or len(value.encode("utf-8")) > MAX_DRAFT_TOKEN_ENCODED_BYTES
    ):
        raise RelationReviewError("INVALID_DRAFT_TOKEN", "draft token is invalid or too large")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def review_artifact_path(vault_root: Path, page_identity: str) -> Path:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    return Path(vault_root) / kb_dirname() / "_Schema" / "relation-reviews" / f"{identity}.json"


def _lifecycle_reference(page_identity: str, name: str) -> str:
    return (
        f"{kb_dirname()}/_Schema/relation-reviews/lifecycle/"
        f"{page_identity}/{name}"
    )


def _canonical_fingerprint(value: object) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_FINGERPRINT",
            "review fingerprint must be a lowercase full SHA-256",
        )
    return value


def lifecycle_decision_path(
    vault_root: Path, page_identity: str, after_fingerprint: str
) -> Path:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    fingerprint = _canonical_fingerprint(after_fingerprint)
    return Path(vault_root) / _lifecycle_reference(identity, f"{fingerprint}.json")


def lifecycle_prepared_path(vault_root: Path, page_identity: str) -> Path:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    return Path(vault_root) / _lifecycle_reference(identity, "prepared.json")


def _lifecycle_decision_hash(
    *, page_identity: str, after_fingerprint: str, reason: str
) -> str:
    return _canonical_hash(
        {
            "schema_version": _LIFECYCLE_SCHEMA_VERSION,
            "contract_version": _LIFECYCLE_CONTRACT_VERSION,
            "kind": "reviewed_none",
            "page_identity": page_identity,
            "after_fingerprint": after_fingerprint,
            "reason": reason,
        }
    )


def build_lifecycle_decision(
    *, page_identity: str, after_fingerprint: str, reason: str
) -> LifecycleDecision:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    fingerprint = _canonical_fingerprint(after_fingerprint)
    normalized_reason = _review_reason(reason)
    decision = LifecycleDecision(
        _LIFECYCLE_SCHEMA_VERSION,
        _LIFECYCLE_CONTRACT_VERSION,
        "reviewed_none",
        identity,
        fingerprint,
        _lifecycle_decision_hash(
            page_identity=identity,
            after_fingerprint=fingerprint,
            reason=normalized_reason,
        ),
        normalized_reason,
        _lifecycle_reference(identity, f"{fingerprint}.json"),
    )
    serialize_lifecycle_decision(decision)
    return decision


def _canonical_lifecycle_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _bounded_lifecycle_text(value: dict[str, Any]) -> str:
    text = _canonical_lifecycle_json(value)
    if len(text.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise RelationReviewError(
            "RELATION_REVIEW_TOO_LARGE", "review artifact exceeds its size limit"
        )
    return text


def serialize_lifecycle_decision(decision: LifecycleDecision) -> str:
    _validate_lifecycle_decision(decision)
    return _bounded_lifecycle_text(decision.storage_dict())


def _decision_bytes_hash(decision: LifecycleDecision) -> str:
    return hashlib.sha256(
        serialize_lifecycle_decision(decision).encode("utf-8")
    ).hexdigest()


def _validate_lifecycle_reference(
    value: object,
    *,
    page_identity: str,
    fingerprint: str | None = None,
) -> str:
    if type(value) is not str:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    prefix = _lifecycle_reference(page_identity, "")
    if not value.startswith(prefix):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    name = value.removeprefix(prefix)
    if not re.fullmatch(r"[0-9a-f]{64}\.json", name):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    if fingerprint is not None and name != f"{fingerprint}.json":
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    return value


def _validate_lifecycle_decision(decision: LifecycleDecision) -> None:
    if not isinstance(decision, LifecycleDecision):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    reason = decision.reason
    valid_reason = (
        type(reason) is str
        and reason == reason.strip()
        and bool(reason)
        and len(reason) <= _MAX_REASON_POINTS
        and len(reason.encode("utf-8")) <= _MAX_REASON_BYTES
    )
    valid = (
        type(decision.schema_version) is int
        and decision.schema_version == _LIFECYCLE_SCHEMA_VERSION
        and type(decision.contract_version) is int
        and decision.contract_version == _LIFECYCLE_CONTRACT_VERSION
        and decision.kind == "reviewed_none"
        and normalize_id(decision.page_identity) == decision.page_identity
        and bool(_HASH.fullmatch(decision.after_fingerprint))
        and bool(_HASH.fullmatch(decision.decision_hash))
        and valid_reason
        and decision.decision_hash
        == _lifecycle_decision_hash(
            page_identity=decision.page_identity,
            after_fingerprint=decision.after_fingerprint,
            reason=reason,
        )
        and decision.reference
        == _lifecycle_reference(
            decision.page_identity, f"{decision.after_fingerprint}.json"
        )
    )
    if not valid:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )


def build_lifecycle_prepared_transition(
    *,
    transition_id: str,
    operation: str,
    page_identity: str,
    before_path: str,
    before_source_hash: str,
    after_path: str,
    after_source_hash: str,
    after_fingerprint: str,
    decision: LifecycleDecision | None,
    transition_token: str,
    auxiliary_hash: str,
    carried_from: LifecycleDecision | None = None,
) -> LifecyclePreparedTransition:
    transition = _canonical_id(
        transition_id, code="LIFECYCLE_TRANSITION_INVALID_ID"
    )
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    fingerprint = _canonical_fingerprint(after_fingerprint)
    if operation not in _LIFECYCLE_OPERATIONS:
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_INVALID_OPERATION",
            "lifecycle operation is unsupported",
        )
    if not _safe_record_path(before_path) or not _safe_record_path(after_path):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    if not _HASH.fullmatch(before_source_hash) or not _HASH.fullmatch(
        after_source_hash
    ):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    if not _HASH.fullmatch(auxiliary_hash):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    decision_reference: str | None = None
    decision_bytes_hash: str | None = None
    if decision is not None:
        _validate_lifecycle_decision(decision)
        if (
            decision.page_identity != identity
            or decision.after_fingerprint != fingerprint
        ):
            raise RelationReviewError(
                "LIFECYCLE_TRANSITION_DECISION_MISMATCH",
                "lifecycle decision does not match the resulting page",
            )
        decision_reference = decision.reference
        decision_bytes_hash = _decision_bytes_hash(decision)
    carried_reference: str | None = None
    if carried_from is not None:
        _validate_lifecycle_decision(carried_from)
        if carried_from.page_identity != identity:
            raise RelationReviewError(
                "LIFECYCLE_TRANSITION_DECISION_MISMATCH",
                "carried lifecycle decision has the wrong identity",
            )
        carried_reference = carried_from.reference
    prepared = LifecyclePreparedTransition(
        _LIFECYCLE_SCHEMA_VERSION,
        _LIFECYCLE_CONTRACT_VERSION,
        "prepared_transition",
        transition,
        operation,
        identity,
        before_path,
        before_source_hash,
        after_path,
        after_source_hash,
        fingerprint,
        decision_reference,
        decision_bytes_hash,
        draft_token_hash(transition_token),
        auxiliary_hash,
        carried_reference,
        _lifecycle_reference(identity, "prepared.json"),
    )
    serialize_lifecycle_prepared(prepared)
    return prepared


def _validate_lifecycle_prepared(prepared: LifecyclePreparedTransition) -> None:
    if not isinstance(prepared, LifecyclePreparedTransition):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    decision_pair = (
        prepared.decision_reference is not None,
        prepared.decision_bytes_hash is not None,
    )
    valid = (
        type(prepared.schema_version) is int
        and prepared.schema_version == _LIFECYCLE_SCHEMA_VERSION
        and type(prepared.contract_version) is int
        and prepared.contract_version == _LIFECYCLE_CONTRACT_VERSION
        and prepared.kind == "prepared_transition"
        and normalize_id(prepared.transition_id) == prepared.transition_id
        and prepared.operation in _LIFECYCLE_OPERATIONS
        and normalize_id(prepared.page_identity) == prepared.page_identity
        and _safe_record_path(prepared.before_path)
        and bool(_HASH.fullmatch(prepared.before_source_hash))
        and _safe_record_path(prepared.after_path)
        and bool(_HASH.fullmatch(prepared.after_source_hash))
        and bool(_HASH.fullmatch(prepared.after_fingerprint))
        and decision_pair in {(False, False), (True, True)}
        and bool(_HASH.fullmatch(prepared.transition_token_hash))
        and bool(_HASH.fullmatch(prepared.auxiliary_hash))
        and prepared.reference
        == _lifecycle_reference(prepared.page_identity, "prepared.json")
    )
    if not valid:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    if prepared.decision_reference is not None:
        _validate_lifecycle_reference(
            prepared.decision_reference,
            page_identity=prepared.page_identity,
            fingerprint=prepared.after_fingerprint,
        )
        if not _HASH.fullmatch(prepared.decision_bytes_hash or ""):
            raise RelationReviewError(
                "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
            )
    if prepared.carried_from is not None:
        _validate_lifecycle_reference(
            prepared.carried_from, page_identity=prepared.page_identity
        )


def serialize_lifecycle_prepared(prepared: LifecyclePreparedTransition) -> str:
    _validate_lifecycle_prepared(prepared)
    return _bounded_lifecycle_text(prepared.storage_dict())


def _validate_lifecycle_entry(
    directory: Path, entry: vault.PathIdentity, name: str
) -> None:
    try:
        info = (directory / name).lstat()
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_SWAPPED", "review artifact changed during inspection"
        ) from error
    if not vault._same_identity(entry, info):
        raise RelationReviewError(
            "RELATION_REVIEW_SWAPPED", "review artifact changed during inspection"
        )
    if (
        stat.S_ISLNK(info.st_mode)
        or vault._is_reparse(info)
        or not stat.S_ISREG(info.st_mode)
    ):
        raise RelationReviewError(
            "RELATION_REVIEW_UNSAFE_FILE", "review artifact is unsafe"
        )


def _inspect_lifecycle_identity(
    vault_root: Path, page_identity: str
) -> tuple[Path, tuple[str, ...], vault.DirectoryCensusGuard]:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    root = Path(vault_root).absolute()
    relative = _lifecycle_reference(identity, "").removesuffix("/")
    try:
        census = vault.DirectoryCensusGuard.capture(
            root,
            relative,
            max_entries=_LIFECYCLE_MAX_DIRECTORY_ENTRIES,
        )
    except vault.PathGuardError as error:
        if error.code == "PATH_GUARD_LIMIT":
            raise RelationReviewError(
                "RELATION_REVIEW_DIRECTORY_LIMIT",
                "lifecycle directory exceeds its bounded entry limit",
            ) from error
        raise RelationReviewError(
            "RELATION_REVIEW_DIRECTORY_UNSAFE",
            "review directory cannot be inspected",
        ) from error
    current = root / relative
    if census.directory_identity is None:
        return current, (), census
    raw_names = tuple(Path(entry.relative_path).name for entry in census.entries)
    logical: set[str] = set()
    decisions = 0
    residue = 0
    names: list[str] = []
    for entry, name in zip(census.entries, raw_names, strict=True):
        _validate_lifecycle_entry(current, entry, name)
        if _LIFECYCLE_ATOMIC_RESIDUE.fullmatch(name):
            residue += 1
            continue
        canonical = name == "prepared.json" or bool(
            re.fullmatch(r"[0-9a-f]{64}\.json", name)
        )
        if not canonical:
            raise RelationReviewError(
                "RELATION_REVIEW_ALIAS",
                "lifecycle directory contains a filename alias",
            )
        alias = name.casefold()
        if alias in logical:
            raise RelationReviewError(
                "RELATION_REVIEW_ALIAS",
                "lifecycle directory contains a logical collision",
            )
        logical.add(alias)
        if name != "prepared.json":
            decisions += 1
        names.append(name)
    if residue > _LIFECYCLE_MAX_RESIDUE:
        raise RelationReviewError(
            "RELATION_REVIEW_DIRECTORY_LIMIT",
            "lifecycle directory exceeds three atomic residue files",
        )
    if decisions > _LIFECYCLE_MAX_DECISIONS:
        raise RelationReviewError(
            "RELATION_REVIEW_HISTORY_LIMIT",
            "lifecycle review history exceeds 256 decisions; clean it up through "
            "governed review tooling",
        )
    try:
        census.recheck(root)
    except vault.PathGuardError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_SWAPPED", "review directory changed during access"
        ) from error
    return current, tuple(names), census


def _parse_lifecycle_json(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_ENCODING", "review artifact is not strict UTF-8"
        ) from error
    try:
        value = json.loads(text, object_pairs_hook=_object_no_duplicates)
    except _DuplicateJsonKey as error:
        raise RelationReviewError(
            "RELATION_REVIEW_DUPLICATE_KEY", "review artifact contains a duplicate key"
        ) from error
    except json.JSONDecodeError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_JSON", "review artifact is not valid JSON"
        ) from error
    if type(value) is not dict:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_JSON", "review artifact root is not an object"
        )
    return value


def _versioned_lifecycle_fields(value: dict[str, Any]) -> None:
    for key in ("schema_version", "contract_version"):
        if key not in value or type(value[key]) is not int:
            raise RelationReviewError(
                "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
            )
    if (
        value["schema_version"] != _LIFECYCLE_SCHEMA_VERSION
        or value["contract_version"] != _LIFECYCLE_CONTRACT_VERSION
    ):
        raise RelationReviewError(
            "RELATION_REVIEW_UNSUPPORTED_VERSION",
            "review artifact schema version is unsupported",
        )


def _parse_lifecycle_decision(
    raw: bytes, *, page_identity: str, after_fingerprint: str
) -> LifecycleDecision:
    value = _parse_lifecycle_json(raw)
    if set(value) != _LIFECYCLE_DECISION_KEYS:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid keys"
        )
    _versioned_lifecycle_fields(value)
    if any(
        type(value[key]) is not str
        for key in (
            "kind",
            "page_identity",
            "after_fingerprint",
            "decision_hash",
            "reason",
        )
    ):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid field types"
        )
    decision = LifecycleDecision(
        value["schema_version"],
        value["contract_version"],
        value["kind"],
        value["page_identity"],
        value["after_fingerprint"],
        value["decision_hash"],
        value["reason"],
        _lifecycle_reference(page_identity, f"{after_fingerprint}.json"),
    )
    _validate_lifecycle_decision(decision)
    if (
        decision.page_identity != page_identity
        or decision.after_fingerprint != after_fingerprint
    ):
        raise RelationReviewError(
            "RELATION_REVIEW_FILENAME_MISMATCH",
            "review artifact filename does not match its identity",
        )
    if raw != serialize_lifecycle_decision(decision).encode("utf-8"):
        raise RelationReviewError(
            "RELATION_REVIEW_NONCANONICAL", "review artifact is not canonical"
        )
    return decision


def _parse_lifecycle_prepared(
    raw: bytes, *, page_identity: str
) -> LifecyclePreparedTransition:
    value = _parse_lifecycle_json(raw)
    if set(value) != _LIFECYCLE_PREPARED_KEYS:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid keys"
        )
    _versioned_lifecycle_fields(value)
    string_keys = (
        "kind",
        "transition_id",
        "operation",
        "page_identity",
        "before_path",
        "before_source_hash",
        "after_path",
        "after_source_hash",
        "after_fingerprint",
        "transition_token_hash",
        "auxiliary_hash",
    )
    if any(type(value[key]) is not str for key in string_keys):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid field types"
        )
    for key in ("decision_reference", "decision_bytes_hash", "carried_from"):
        if value[key] is not None and type(value[key]) is not str:
            raise RelationReviewError(
                "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid field types"
            )
    prepared = LifecyclePreparedTransition(
        value["schema_version"],
        value["contract_version"],
        value["kind"],
        value["transition_id"],
        value["operation"],
        value["page_identity"],
        value["before_path"],
        value["before_source_hash"],
        value["after_path"],
        value["after_source_hash"],
        value["after_fingerprint"],
        value["decision_reference"],
        value["decision_bytes_hash"],
        value["transition_token_hash"],
        value["auxiliary_hash"],
        value["carried_from"],
        _lifecycle_reference(page_identity, "prepared.json"),
    )
    _validate_lifecycle_prepared(prepared)
    if prepared.page_identity != page_identity:
        raise RelationReviewError(
            "RELATION_REVIEW_FILENAME_MISMATCH",
            "review artifact filename does not match its identity",
        )
    if raw != serialize_lifecycle_prepared(prepared).encode("utf-8"):
        raise RelationReviewError(
            "RELATION_REVIEW_NONCANONICAL", "review artifact is not canonical"
        )
    return prepared


def _load_lifecycle_decision_bound(
    vault_root: Path, page_identity: str, after_fingerprint: str
) -> tuple[LifecycleDecision | None, vault.PathGuard | None]:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    fingerprint = _canonical_fingerprint(after_fingerprint)
    root = Path(vault_root).absolute()
    inspected = _inspect_lifecycle_identity(root, identity)
    _, names, census = inspected
    if census.directory_identity is None:
        return None, None
    name = f"{fingerprint}.json"
    if name not in names:
        return None, None
    with _open_review_directory(
        root,
        nested=("lifecycle", identity),
        max_entries=_LIFECYCLE_MAX_DIRECTORY_ENTRIES,
    ) as opened:
        if opened is None:
            raise RelationReviewError(
                "RELATION_REVIEW_SWAPPED", "review directory changed during access"
            )
        raw, guard = _read_artifact_bytes(opened, name)
    return (
        _parse_lifecycle_decision(
            raw, page_identity=identity, after_fingerprint=fingerprint
        ),
        guard,
    )


def load_lifecycle_decision(
    vault_root: Path, page_identity: str, after_fingerprint: str
) -> LifecycleDecision | None:
    decision, _ = _load_lifecycle_decision_bound(
        vault_root, page_identity, after_fingerprint
    )
    return decision


def _load_lifecycle_prepared_bound(
    vault_root: Path, page_identity: str
) -> tuple[LifecyclePreparedTransition | None, vault.PathGuard | None]:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    root = Path(vault_root).absolute()
    inspected = _inspect_lifecycle_identity(root, identity)
    _, names, census = inspected
    if census.directory_identity is None:
        return None, None
    if "prepared.json" not in names:
        return None, None
    with _open_review_directory(
        root,
        nested=("lifecycle", identity),
        max_entries=_LIFECYCLE_MAX_DIRECTORY_ENTRIES,
    ) as opened:
        if opened is None:
            raise RelationReviewError(
                "RELATION_REVIEW_SWAPPED", "review directory changed during access"
            )
        raw, guard = _read_artifact_bytes(opened, "prepared.json")
    return _parse_lifecycle_prepared(raw, page_identity=identity), guard


def load_lifecycle_prepared(
    vault_root: Path, page_identity: str
) -> LifecyclePreparedTransition | None:
    prepared, _ = _load_lifecycle_prepared_bound(vault_root, page_identity)
    return prepared


@dataclass(frozen=True, slots=True)
class _LifecycleTrashSnapshot:
    files: frozenset[str]
    directories: frozenset[str]
    guards: tuple[vault.DirectoryCensusGuard, ...]
    content_guards: tuple[vault.PathGuard, ...]
    sidecars_by_original: Mapping[str, tuple[_LifecycleTrashSidecar, ...]]


@dataclass(frozen=True, slots=True)
class _LifecycleTrashSidecar:
    original_path: str
    trash_root: str
    target_kind: Literal["file", "directory", "missing"]
    source: str
    guard: vault.PathGuard


def list_lifecycle_prepared_identities(vault_root: Path) -> tuple[str, ...]:
    """Enumerate bounded lifecycle UUID directories, then direct-load each slot."""
    root = Path(vault_root).absolute()
    identities: list[str] = []
    with _open_review_directory(
        root,
        nested=("lifecycle",),
        max_entries=_LIFECYCLE_MAX_IDENTITIES,
    ) as opened:
        if opened is None:
            return ()
        for name in opened.names:
            try:
                identity = _canonical_id(
                    name, code="RELATION_REVIEW_INVALID_ID"
                )
                info = (
                    os.stat(
                        name,
                        dir_fd=opened.descriptor,
                        follow_symlinks=False,
                    )
                    if opened.descriptor_relative
                    else (opened.path / name).lstat()
                )
            except (OSError, RelationReviewError) as error:
                raise RelationReviewError(
                    "RELATION_REVIEW_DIRECTORY_UNSAFE",
                    "lifecycle identity directory is unsafe",
                ) from error
            if (
                stat.S_ISLNK(info.st_mode)
                or vault._is_reparse(info)
                or not stat.S_ISDIR(info.st_mode)
            ):
                raise RelationReviewError(
                    "RELATION_REVIEW_DIRECTORY_UNSAFE",
                    "lifecycle identity directory is unsafe",
                )
            _, names, _ = _inspect_lifecycle_identity(root, identity)
            if "prepared.json" in names:
                identities.append(identity)
    return tuple(identities)


def _safe_trash_original_path(value: object) -> bool:
    if type(value) is not str:
        return False
    posix = PurePosixPath(value)
    return bool(
        value
        and "\\" not in value
        and "\0" not in value
        and posix.as_posix() == value
        and not posix.is_absolute()
        and not any(part in {"", ".", ".."} for part in posix.parts)
        and posix.parts
        and posix.parts[0] == kb_dirname()
    )


def _read_bounded_lifecycle_sidecar_bytes(
    root: Path,
    relative: str,
    *,
    expected_identity: vault.PathIdentity,
    byte_limit: int,
) -> bytes:
    """Read one exact sidecar without following or allocating past its cap."""
    path = root / relative
    if byte_limit < 0:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_LIMIT",
            "trash sidecar bytes exceed the bounded proof inventory",
        )
    try:
        before = path.lstat()
    except OSError as error:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed before bounded read",
        ) from error
    if (
        not vault._same_identity(expected_identity, before)
        or stat.S_ISLNK(before.st_mode)
        or vault._is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed before bounded read",
        )
    if before.st_size > byte_limit:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_LIMIT",
            "trash sidecar bytes exceed the bounded proof inventory",
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed before bounded read",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not vault._same_identity(expected_identity, opened)
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise RelationReviewError(
                "LIFECYCLE_TRASH_RACE",
                "trash sidecar changed before bounded read",
            )
        if opened.st_size > byte_limit:
            raise RelationReviewError(
                "LIFECYCLE_TRASH_LIMIT",
                "trash sidecar bytes exceed the bounded proof inventory",
            )
        remaining = byte_limit + 1
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if not vault._same_identity(expected_identity, after):
            raise RelationReviewError(
                "LIFECYCLE_TRASH_RACE",
                "trash sidecar changed during bounded read",
            )
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > byte_limit:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_LIMIT",
            "trash sidecar bytes exceed the bounded proof inventory",
        )
    try:
        current = path.lstat()
    except OSError as error:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed after bounded read",
        ) from error
    if (
        not vault._same_identity(expected_identity, current)
        or stat.S_ISLNK(current.st_mode)
        or vault._is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
    ):
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed after bounded read",
        )
    return raw


@dataclass(frozen=True, slots=True)
class _BoundedLifecycleSidecarGuard(vault.PathGuard):
    """Content guard whose exact hash recheck never streams past its cap."""

    byte_limit: int

    def recheck(self, vault_root: Path) -> None:
        if self.leaf_identity is None or self.expected_content_hash is None:
            raise vault.PathGuardError(
                "PATH_GUARD_CHANGED", "bounded sidecar guard is incomplete"
            )
        stable = vault.PathGuard(
            self.target,
            self.ancestors,
            self.missing_parents,
            self.leaf_identity,
            "stable",
            None,
        )
        stable.recheck(vault_root)
        try:
            raw = _read_bounded_lifecycle_sidecar_bytes(
                Path(vault_root),
                self.target,
                expected_identity=self.leaf_identity,
                byte_limit=self.byte_limit,
            )
        except RelationReviewError as error:
            raise vault.PathGuardError(
                "PATH_GUARD_CHANGED", "bounded sidecar guard changed"
            ) from error
        if hashlib.sha256(raw).hexdigest() != self.expected_content_hash:
            raise vault.PathGuardError(
                "PATH_GUARD_CONTENT", "guarded content changed"
            )


def _capture_bounded_lifecycle_sidecar_guard(
    root: Path,
    relative: str,
    *,
    expected_identity: vault.PathIdentity,
    expected_content_hash: str,
    byte_limit: int,
) -> vault.PathGuard:
    try:
        stable = vault.PathGuard.capture(root, relative, leaf_policy="stable")
    except vault.PathGuardError as error:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed before guard capture",
        ) from error
    if stable.leaf_identity != expected_identity:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed before guard capture",
        )
    guard = _BoundedLifecycleSidecarGuard(
        stable.target,
        stable.ancestors,
        stable.missing_parents,
        stable.leaf_identity,
        "content",
        expected_content_hash,
        byte_limit,
    )
    try:
        guard.recheck(root)
    except vault.PathGuardError as error:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_RACE",
            "trash sidecar changed during guard capture",
        ) from error
    return guard


def _capture_lifecycle_trash_snapshot(root: Path) -> _LifecycleTrashSnapshot:
    start = f"{kb_dirname()}/_trash"
    pending = [start]
    files: set[str] = set()
    file_identities: dict[str, vault.PathIdentity] = {}
    directories: set[str] = set()
    guards: list[vault.DirectoryCensusGuard] = []
    while pending:
        relative = pending.pop()
        if len(guards) >= _LIFECYCLE_TRASH_MAX_DIRECTORIES:
            raise RelationReviewError(
                "LIFECYCLE_TRASH_LIMIT",
                "trash proof census exceeds its bounded directory limit",
            )
        try:
            guard = vault.DirectoryCensusGuard.capture(
                root,
                relative,
                max_entries=_LIFECYCLE_TRASH_MAX_DIRECTORY_ENTRIES,
            )
        except vault.PathGuardError as error:
            raise RelationReviewError(
                "LIFECYCLE_TRASH_UNSAFE",
                "trash proof census cannot be inspected safely",
            ) from error
        guards.append(guard)
        if guard.directory_identity is None:
            continue
        directories.add(relative)
        for entry in guard.entries:
            child = root / entry.relative_path
            try:
                info = child.lstat()
            except OSError as error:
                raise RelationReviewError(
                    "LIFECYCLE_TRASH_RACE",
                    "trash proof census changed during inspection",
                ) from error
            if (
                not vault._same_identity(entry, info)
                or stat.S_ISLNK(info.st_mode)
                or vault._is_reparse(info)
            ):
                raise RelationReviewError(
                    "LIFECYCLE_TRASH_UNSAFE",
                    "trash proof census contains an unsafe entry",
                )
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry.relative_path)
            elif stat.S_ISREG(info.st_mode):
                files.add(entry.relative_path)
                file_identities[entry.relative_path] = entry
                if len(files) > _LIFECYCLE_TRASH_MAX_FILES:
                    raise RelationReviewError(
                        "LIFECYCLE_TRASH_LIMIT",
                        "trash proof census exceeds its bounded file limit",
                    )
            else:
                raise RelationReviewError(
                    "LIFECYCLE_TRASH_UNSAFE",
                    "trash proof census contains an unsafe entry",
                )
    content_guards: list[vault.PathGuard] = []
    sidecars: dict[str, list[_LifecycleTrashSidecar]] = {}
    aggregate_sidecar_bytes = 0
    for relative in sorted(files):
        if not relative.endswith(".meta.json"):
            continue
        try:
            remaining = (
                _LIFECYCLE_TRASH_MAX_SIDECAR_AGGREGATE_BYTES
                - aggregate_sidecar_bytes
            )
            byte_limit = min(
                _LIFECYCLE_TRASH_MAX_SIDECAR_BYTES,
                remaining,
            )
            raw = _read_bounded_lifecycle_sidecar_bytes(
                root,
                relative,
                expected_identity=file_identities[relative],
                byte_limit=byte_limit,
            )
            source = raw.decode("utf-8", errors="strict")
            metadata = parse_exact_json_object(source)
            original = metadata.get("original_path")
            if not _safe_trash_original_path(original):
                raise ValueError("trash sidecar original_path is invalid")
            guard = _capture_bounded_lifecycle_sidecar_guard(
                root,
                relative,
                expected_identity=file_identities[relative],
                expected_content_hash=hashlib.sha256(raw).hexdigest(),
                byte_limit=byte_limit,
            )
        except RelationReviewError:
            raise
        except (OSError, UnicodeDecodeError, ValueError, vault.PathGuardError) as error:
            raise RelationReviewError(
                "LIFECYCLE_TRASH_INVALID",
                "trash proof inventory contains an invalid sidecar or changed file",
            ) from error
        aggregate_sidecar_bytes += len(raw)
        content_guards.append(guard)
        assert isinstance(original, str)
        trash_root = relative.removesuffix(".meta.json")
        target_kind: Literal["file", "directory", "missing"] = "missing"
        if trash_root in files:
            target_kind = "file"
        elif trash_root in directories:
            target_kind = "directory"
        sidecars.setdefault(original, []).append(
            _LifecycleTrashSidecar(
                original,
                trash_root,
                target_kind,
                source,
                guard,
            )
        )
    return _LifecycleTrashSnapshot(
        frozenset(files),
        frozenset(directories),
        tuple(guards),
        tuple(content_guards),
        MappingProxyType(
            {
                original: tuple(entries)
                for original, entries in sorted(sidecars.items())
            }
        ),
    )


def _guard_lifecycle_primary_paths(
    root: Path, prepared: LifecyclePreparedTransition
) -> tuple[vault.PathGuard, ...]:
    guards: list[vault.PathGuard] = []
    for relative in sorted({prepared.before_path, prepared.after_path}):
        path = root / relative
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            guards.append(
                vault.PathGuard.capture(root, relative, leaf_policy="absent")
            )
            continue
        except OSError as error:
            raise RelationReviewError(
                "LIFECYCLE_PRIMARY_INVALID",
                "prepared transition primary cannot be inspected safely",
            ) from error
        try:
            guards.append(
                vault.PathGuard.capture(
                    root,
                    relative,
                    leaf_policy="content",
                    expected_content_hash=hashlib.sha256(raw).hexdigest(),
                )
            )
        except vault.PathGuardError as error:
            raise RelationReviewError(
                "LIFECYCLE_PRIMARY_INVALID",
                "prepared transition primary cannot be inspected safely",
            ) from error
    return tuple(guards)


def _prepared_primary_binding(
    prepared: LifecyclePreparedTransition,
    corpus: semantic_contract.SemanticCorpusContext,
) -> tuple[LifecyclePrimaryBinding, tuple[str, ...]]:
    owner_paths = tuple(
        path
        for path in corpus.identity_census.paths_by_identity.get(
            prepared.page_identity, ()
        )
        if not path.startswith(f"{kb_dirname()}/_trash/")
    )
    if len(owner_paths) == 1 and owner_paths[0] in corpus.pages:
        state = corpus.pages[owner_paths[0]]
        return (
            LifecyclePrimaryBinding(
                state.path,
                state.source_hash,
                state.review_fingerprint,
            ),
            owner_paths,
        )
    return (
        LifecyclePrimaryBinding(prepared.after_path, "0" * 64, None),
        owner_paths,
    )


def _trash_proof_for_prepared(
    root: Path,
    prepared: LifecyclePreparedTransition,
    *,
    corpus: semantic_contract.SemanticCorpusContext,
    snapshot: _LifecycleTrashSnapshot,
) -> tuple[LifecycleTrashProof | None, tuple[vault.PathGuard, ...]]:
    live_owner_paths = tuple(
        path
        for path in corpus.identity_census.paths_by_identity.get(
            prepared.page_identity, ()
        )
        if not path.startswith(f"{kb_dirname()}/_trash/")
    )
    target_guards: list[vault.PathGuard] = []
    parts = PurePosixPath(prepared.after_path).parts
    original_candidates = (
        prepared.after_path,
        *("/".join(parts[:index]) for index in range(len(parts) - 1, 0, -1)),
    )
    candidates = (
        sidecar
        for original in original_candidates
        for sidecar in snapshot.sidecars_by_original.get(original, ())
    )
    for sidecar in candidates:
        if sidecar.target_kind == "directory":
            prefix = f"{sidecar.original_path.rstrip('/')}/"
            if not prepared.after_path.startswith(prefix):
                continue
            suffix = prepared.after_path.removeprefix(prefix)
            trash_path = f"{sidecar.trash_root}/{suffix}"
        elif sidecar.target_kind == "file":
            if prepared.after_path != sidecar.original_path:
                continue
            trash_path = sidecar.trash_root
        else:
            continue
        if trash_path not in snapshot.files or not trash_path.endswith(".md"):
            continue
        try:
            raw = (root / trash_path).read_bytes()
            source_guard = vault.PathGuard.capture(
                root,
                trash_path,
                leaf_policy="content",
                expected_content_hash=hashlib.sha256(raw).hexdigest(),
            )
            target_guards.append(source_guard)
            source = raw.decode("utf-8", errors="strict")
            frontmatter, _, _ = vault.parse_frontmatter(source)
        except (UnicodeDecodeError, ValueError):
            continue
        except (OSError, vault.PathGuardError) as error:
            raise RelationReviewError(
                "LIFECYCLE_TRASH_RACE",
                "trash target changed during lifecycle inspection",
            ) from error
        if normalize_id(frontmatter.get(ID_FIELD)) != prepared.page_identity:
            continue
        proof = LifecycleTrashProof(
            page_identity=prepared.page_identity,
            original_path=prepared.after_path,
            trash_path=trash_path,
            source_hash=vault.content_hash(source),
            review_fingerprint=semantic_contract.review_content_fingerprint(
                prepared.page_identity, source
            ),
            source_guard=source_guard,
            sidecar_source=sidecar.source,
            sidecar_guard=sidecar.guard,
            live_owner_paths=live_owner_paths,
        )
        try:
            if trash_proof_commits_prepared(prepared, proof):
                return proof, tuple(target_guards)
        except RelationReviewError:
            continue
    return None, tuple(target_guards)


def inspect_lifecycle_prepared_slots(
    vault_root: Path,
    *,
    corpus: semantic_contract.SemanticCorpusContext,
) -> LifecyclePreparedBatch:
    """Classify all direct prepared slots against one already-built corpus."""
    root = Path(vault_root).absolute()
    try:
        identities = list_lifecycle_prepared_identities(root)
    except RelationReviewError as error:
        return LifecyclePreparedBatch(
            (), (LifecyclePreparedIssue(error.code),), False
        )
    if not identities:
        return LifecyclePreparedBatch((), (), True)
    issues: list[LifecyclePreparedIssue] = []
    try:
        trash = _capture_lifecycle_trash_snapshot(root)
    except RelationReviewError as error:
        trash = None
        issues.append(LifecyclePreparedIssue(error.code))
    inspections: list[LifecyclePreparedInspection] = []
    for identity in identities:
        try:
            prepared, prepared_guard = _load_lifecycle_prepared_bound(root, identity)
        except RelationReviewError as error:
            issues.append(LifecyclePreparedIssue(error.code, identity))
            continue
        if prepared is None or prepared_guard is None:
            issues.append(
                LifecyclePreparedIssue("LIFECYCLE_RECONCILE_RACE", identity)
            )
            continue
        try:
            _load_prepared_decision_guard(root, prepared)
        except RelationReviewError as error:
            issues.append(LifecyclePreparedIssue(error.code, identity))
            continue
        current, owner_paths = _prepared_primary_binding(prepared, corpus)
        if len(owner_paths) > 1 or (
            len(owner_paths) == 1 and owner_paths[0] not in corpus.pages
        ):
            issues.append(
                LifecyclePreparedIssue("LIFECYCLE_PRIMARY_AMBIGUOUS", identity)
            )
            continue
        state: Literal["pending", "committed", "trashed_committed", "stale"]
        state = lifecycle_prepared_state(prepared, current)
        proof = None
        target_guards: tuple[vault.PathGuard, ...] = ()
        if state == "stale":
            if trash is None:
                issues.append(
                    LifecyclePreparedIssue("LIFECYCLE_TRASH_INDETERMINATE", identity)
                )
                continue
            try:
                proof, target_guards = _trash_proof_for_prepared(
                    root,
                    prepared,
                    corpus=corpus,
                    snapshot=trash,
                )
            except RelationReviewError as error:
                issues.append(LifecyclePreparedIssue(error.code, identity))
                continue
            if proof is not None:
                state = "trashed_committed"
        try:
            primary_guards = _guard_lifecycle_primary_paths(root, prepared)
        except RelationReviewError as error:
            issues.append(LifecyclePreparedIssue(error.code, identity))
            continue
        inspections.append(
            LifecyclePreparedInspection(
                prepared=prepared,
                state=state,
                cleanup_eligible=state == "stale" and proof is None,
                live_owner_paths=owner_paths,
                prepared_guard=prepared_guard,
                primary_guards=primary_guards,
                trash_guards=trash.guards if trash is not None else (),
                trash_content_guards=(
                    trash.content_guards if trash is not None else ()
                ),
                trash_target_guards=target_guards,
                trash_proof=proof,
            )
        )
    cleanup_safe = not issues and trash is not None
    if not cleanup_safe:
        inspections = [replace(item, cleanup_eligible=False) for item in inspections]
    return LifecyclePreparedBatch(
        tuple(inspections), _bounded_lifecycle_prepared_issues(issues), cleanup_safe
    )


def _bounded_lifecycle_prepared_issues(
    issues: list[LifecyclePreparedIssue],
) -> tuple[LifecyclePreparedIssue, ...]:
    """Keep one batch issue in addition to the bounded per-identity issues."""
    return tuple(issues[: _LIFECYCLE_MAX_IDENTITIES + 1])


def _live_identity_owner_paths(
    census: semantic_contract.StableIdentityCensus,
    page_identity: str,
) -> tuple[str, ...]:
    return tuple(
        path
        for path in census.paths_by_identity.get(page_identity, ())
        if not path.startswith(f"{kb_dirname()}/_trash/")
    )


def _cleanup_stale_lifecycle_prepared_locked(
    root: Path,
    inspection: LifecyclePreparedInspection,
) -> str:
    inspection.prepared_guard.recheck(root)
    for guard in inspection.primary_guards:
        guard.recheck(root)
    for guard in inspection.trash_target_guards:
        guard.recheck(root)
    prepared, prepared_guard = _load_lifecycle_prepared_bound(
        root, inspection.prepared.page_identity
    )
    if prepared != inspection.prepared or prepared_guard is None:
        raise RelationReviewError(
            "LIFECYCLE_RECONCILE_RACE",
            "prepared transition changed before cleanup",
        )
    inspection.prepared_guard.recheck(root)
    with _open_review_directory(
        root,
        nested=("lifecycle", inspection.prepared.page_identity),
        max_entries=_LIFECYCLE_MAX_DIRECTORY_ENTRIES,
    ) as opened:
        if opened is None or "prepared.json" not in opened.names:
            raise RelationReviewError(
                "LIFECYCLE_RECONCILE_RACE",
                "prepared transition changed before cleanup",
            )
        raw, immediate_guard = _read_artifact_bytes(opened, "prepared.json")
        immediate = _parse_lifecycle_prepared(
            raw,
            page_identity=inspection.prepared.page_identity,
        )
        if immediate != inspection.prepared:
            raise RelationReviewError(
                "LIFECYCLE_RECONCILE_RACE",
                "prepared transition changed before cleanup",
            )
        inspection.prepared_guard.recheck(root)
        immediate_guard.recheck(root)
        if opened.descriptor_relative:
            os.unlink("prepared.json", dir_fd=opened.descriptor)
        else:  # pragma: no cover - Windows fallback
            (opened.path / "prepared.json").unlink()
    return inspection.prepared.reference


def cleanup_stale_lifecycle_prepared_batch(
    vault_root: Path,
    inspections: tuple[LifecyclePreparedInspection, ...],
) -> LifecyclePreparedCleanupBatch:
    """Clean stale slots under one lock and one shared trash recheck pass."""
    root = Path(vault_root).absolute()
    items = tuple(inspections)
    if not items:
        return LifecyclePreparedCleanupBatch((), ())
    if len(items) > _LIFECYCLE_MAX_IDENTITIES:
        raise RelationReviewError(
            "LIFECYCLE_RECONCILE_LIMIT",
            "lifecycle cleanup exceeds its bounded identity limit",
        )
    shared_directories = items[0].trash_guards
    shared_sidecars = items[0].trash_content_guards
    if any(
        item.trash_guards is not shared_directories
        or item.trash_content_guards is not shared_sidecars
        for item in items[1:]
    ):
        return LifecyclePreparedCleanupBatch(
            (),
            tuple(
                LifecyclePreparedIssue(
                    "LIFECYCLE_RECONCILE_SNAPSHOT_MISMATCH",
                    item.prepared.page_identity,
                )
                for item in items
            ),
        )
    cleaned: list[str] = []
    blocked: list[LifecyclePreparedIssue] = []
    with vault.vault_creation_lock(root, "semantic-creation"):
        try:
            census = semantic_contract.build_stable_identity_census(root)
        except activation_manifest.ActivationManifestError:
            return LifecyclePreparedCleanupBatch(
                (),
                tuple(
                    LifecyclePreparedIssue(
                        "LIFECYCLE_PRIMARY_CENSUS_FAILED",
                        item.prepared.page_identity,
                    )
                    for item in items
                ),
            )
        try:
            for guard in shared_directories:
                guard.recheck(root)
            for guard in shared_sidecars:
                guard.recheck(root)
        except vault.PathGuardError:
            return LifecyclePreparedCleanupBatch(
                (),
                tuple(
                    LifecyclePreparedIssue(
                        "LIFECYCLE_RECONCILE_RACE",
                        item.prepared.page_identity,
                    )
                    for item in items
                ),
            )
        for item in items:
            identity = item.prepared.page_identity
            if item.state != "stale" or not item.cleanup_eligible:
                blocked.append(
                    LifecyclePreparedIssue(
                        "LIFECYCLE_RECONCILE_NOT_STALE", identity
                    )
                )
                continue
            fresh_owners = _live_identity_owner_paths(census, identity)
            if fresh_owners != item.live_owner_paths:
                blocked.append(
                    LifecyclePreparedIssue("LIFECYCLE_PRIMARY_RACE", identity)
                )
                continue
            try:
                cleaned.append(
                    _cleanup_stale_lifecycle_prepared_locked(root, item)
                )
            except (OSError, RelationReviewError, vault.PathGuardError):
                blocked.append(
                    LifecyclePreparedIssue("LIFECYCLE_RECONCILE_RACE", identity)
                )
    return LifecyclePreparedCleanupBatch(tuple(cleaned), tuple(blocked))


def cleanup_stale_lifecycle_prepared(
    vault_root: Path,
    inspection: LifecyclePreparedInspection,
) -> str:
    """Compatibility wrapper routed through the guarded batch cleanup path."""
    result = cleanup_stale_lifecycle_prepared_batch(vault_root, (inspection,))
    if result.cleaned:
        return result.cleaned[0]
    issue = result.blocked[0]
    raise RelationReviewError(
        issue.code,
        "prepared lifecycle cleanup was blocked by changed or unsafe state",
    )


def lifecycle_identity_reserved(vault_root: Path, page_identity: str) -> bool:
    """Return whether any exact lifecycle identity directory reserves the UUID."""
    _, _, census = _inspect_lifecycle_identity(vault_root, page_identity)
    return census.directory_identity is not None


def _ensure_lifecycle_decision_capacity(root: Path, page_identity: str) -> None:
    inspected = _inspect_lifecycle_identity(root, page_identity)
    _, names, census = inspected
    if census.directory_identity is None:
        return
    count = sum(name != "prepared.json" for name in names)
    if count >= _LIFECYCLE_MAX_DECISIONS:
        raise RelationReviewError(
            "RELATION_REVIEW_HISTORY_LIMIT",
            "lifecycle review history already contains 256 decisions; clean it up "
            "through governed review tooling",
        )


def _plan_lifecycle_decision_install(
    root: Path, decision: LifecycleDecision
) -> vault.PlannedWrite:
    _ensure_lifecycle_decision_capacity(root, decision.page_identity)
    decision_path = lifecycle_decision_path(
        root, decision.page_identity, decision.after_fingerprint
    )
    relative = decision_path.relative_to(root).as_posix()
    return vault.PlannedWrite(
        decision_path,
        serialize_lifecycle_decision(decision),
        create_only=True,
        guard=vault.PathGuard.capture(root, relative, leaf_policy="absent"),
    )


def _validate_primary_binding(current: LifecyclePrimaryBinding) -> None:
    if (
        not isinstance(current, LifecyclePrimaryBinding)
        or not _safe_record_path(current.path)
        or not _HASH.fullmatch(current.source_hash)
        or (
            current.review_fingerprint is not None
            and not _HASH.fullmatch(current.review_fingerprint)
        )
    ):
        raise RelationReviewError(
            "LIFECYCLE_PRIMARY_INVALID", "current primary binding is invalid"
        )


def lifecycle_prepared_state(
    prepared: LifecyclePreparedTransition,
    current: LifecyclePrimaryBinding,
) -> Literal["pending", "committed", "stale"]:
    """Classify one current primary against a prepared transition, purely."""
    _validate_lifecycle_prepared(prepared)
    _validate_primary_binding(current)
    committed = (
        current.path == prepared.after_path
        and current.source_hash == prepared.after_source_hash
        and current.review_fingerprint == prepared.after_fingerprint
    )
    if committed:
        return "committed"
    if (
        current.path == prepared.before_path
        and current.source_hash == prepared.before_source_hash
    ):
        return "pending"
    return "stale"


def _validate_prepared_decision_binding(
    prepared: LifecyclePreparedTransition,
    decision: LifecycleDecision | None,
) -> None:
    if decision is None:
        if (
            prepared.decision_reference is not None
            or prepared.decision_bytes_hash is not None
        ):
            raise RelationReviewError(
                "LIFECYCLE_TRANSITION_DECISION_MISMATCH",
                "prepared transition decision binding is inconsistent",
            )
        return
    _validate_lifecycle_decision(decision)
    if (
        decision.page_identity != prepared.page_identity
        or decision.after_fingerprint != prepared.after_fingerprint
        or decision.reference != prepared.decision_reference
        or _decision_bytes_hash(decision) != prepared.decision_bytes_hash
    ):
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_DECISION_MISMATCH",
            "prepared transition decision binding is inconsistent",
        )


def _load_prepared_decision_guard(
    root: Path, prepared: LifecyclePreparedTransition
) -> vault.PathGuard | None:
    """Validate the immutable decision referenced by an existing prepared slot."""
    if prepared.decision_reference is None:
        return None
    fingerprint = prepared.decision_reference.rsplit("/", 1)[-1].removesuffix(".json")
    decision, guard = _load_lifecycle_decision_bound(
        root, prepared.page_identity, fingerprint
    )
    if (
        decision is None
        or guard is None
        or _decision_bytes_hash(decision) != prepared.decision_bytes_hash
    ):
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_DECISION_MISMATCH",
            "prepared transition references a missing or changed immutable decision",
        )
    return guard


def _validate_exact_lifecycle_trash_proof(
    proof: LifecycleTrashProof,
) -> None:
    if not isinstance(proof, LifecycleTrashProof):
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "trash proof does not bind exact sidecar, UUID, bytes, and ownership",
        )
    try:
        sidecar_bytes = proof.sidecar_source.encode("utf-8", errors="strict")
    except (AttributeError, UnicodeEncodeError) as error:
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "trash proof sidecar is not strict UTF-8 text",
        ) from error
    if len(sidecar_bytes) > _LIFECYCLE_TRASH_MAX_SIDECAR_BYTES:
        raise RelationReviewError(
            "LIFECYCLE_TRASH_LIMIT",
            "trash proof sidecar exceeds the accepted byte limit",
        )
    try:
        sidecar = parse_exact_json_object(proof.sidecar_source)
    except ValueError as error:
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "trash proof sidecar is not an exact JSON object",
        ) from error
    sidecar_object = sidecar
    snapshot = sidecar_object.get("frontmatter_snapshot")
    sidecar_identity = snapshot.get(ID_FIELD) if type(snapshot) is dict else None
    sidecar_target = proof.sidecar_guard.target
    trash_root = sidecar_target.removesuffix(".meta.json")
    directory_proof = proof.trash_path.startswith(f"{trash_root}/")
    if directory_proof:
        suffix = proof.trash_path.removeprefix(f"{trash_root}/")
        expected_original = f"{str(sidecar_object.get('original_path', '')).rstrip('/')}/{suffix}"
        original_matches = proof.original_path == expected_original
        identity_matches = sidecar_identity is None
    else:
        original_matches = proof.original_path == sidecar_object.get("original_path")
        identity_matches = proof.page_identity == sidecar_identity
    valid = (
        normalize_id(proof.page_identity) == proof.page_identity
        and identity_matches
        and original_matches
        and _safe_record_path(proof.original_path)
        and _safe_record_path(proof.trash_path)
        and bool(_HASH.fullmatch(proof.source_hash))
        and bool(_HASH.fullmatch(proof.review_fingerprint))
        and proof.source_guard.target == proof.trash_path
        and proof.source_guard.leaf_policy == "content"
        and proof.source_guard.expected_content_hash == proof.source_hash
        and sidecar_target == f"{trash_root}.meta.json"
        and (directory_proof or trash_root == proof.trash_path)
        and proof.sidecar_guard.leaf_policy == "content"
        and proof.sidecar_guard.expected_content_hash
        == vault.content_hash(proof.sidecar_source)
        and not proof.live_owner_paths
    )
    if not valid:
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "trash proof does not bind exact sidecar, UUID, bytes, and ownership",
        )


def _validate_lifecycle_trash_proof(
    *,
    prepared: LifecyclePreparedTransition,
    current: LifecyclePrimaryBinding,
    proof: LifecycleTrashProof,
) -> None:
    _validate_exact_lifecycle_trash_proof(proof)
    valid = (
        prepared.operation == "recover"
        and proof.page_identity == prepared.page_identity
        and proof.trash_path == prepared.before_path
        and proof.trash_path == current.path
        and proof.source_hash == prepared.before_source_hash
        and proof.source_hash == current.source_hash
        and proof.review_fingerprint == current.review_fingerprint
    )
    if not valid:
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "trash proof does not bind exact sidecar, UUID, bytes, and ownership",
        )


def _trash_proof_commits_prepared(
    prepared: LifecyclePreparedTransition,
    proof: LifecycleTrashProof,
) -> bool:
    return (
        prepared.page_identity == proof.page_identity
        and prepared.after_path == proof.original_path
        and prepared.after_source_hash == proof.source_hash
        and prepared.after_fingerprint == proof.review_fingerprint
    )


def trash_proof_commits_prepared(
    prepared: LifecyclePreparedTransition,
    proof: LifecycleTrashProof,
) -> bool:
    """Validate recovery's exact proof and bind it to a prior committed slot."""
    _validate_lifecycle_prepared(prepared)
    _validate_exact_lifecycle_trash_proof(proof)
    return _trash_proof_commits_prepared(prepared, proof)


def plan_lifecycle_transition(
    vault_root: Path,
    *,
    decision: LifecycleDecision | None,
    prepared: LifecyclePreparedTransition,
    current: LifecyclePrimaryBinding,
    trashed_committed: LifecycleTrashProof | None = None,
) -> LifecycleTransitionPlan:
    """Plan decision/slot writes without mutating lifecycle or primary state."""
    _validate_lifecycle_prepared(prepared)
    _validate_primary_binding(current)
    _validate_prepared_decision_binding(prepared, decision)
    root = Path(vault_root).absolute()
    if trashed_committed is not None:
        _validate_lifecycle_trash_proof(
            prepared=prepared,
            current=current,
            proof=trashed_committed,
        )

    writes: list[vault.PlannedWrite] = []
    _, _, identity_guard = _inspect_lifecycle_identity(root, prepared.page_identity)
    read_guards: list[vault.PathGuard | vault.DirectoryCensusGuard] = [
        identity_guard
    ]
    existing_prepared, prepared_guard = _load_lifecycle_prepared_bound(
        root, prepared.page_identity
    )
    if existing_prepared is not None:
        existing_decision_guard = _load_prepared_decision_guard(
            root, existing_prepared
        )
        if existing_decision_guard is not None:
            read_guards.append(existing_decision_guard)

    if existing_prepared == prepared:
        assert prepared_guard is not None
        state = lifecycle_prepared_state(prepared, current)
        if state == "stale":
            raise RelationReviewError(
                "LIFECYCLE_TRANSITION_STALE",
                "prepared transition matches neither live side; reconcile lifecycle state",
            )
        read_guards.append(prepared_guard)
        return LifecycleTransitionPlan(
            "pending_retry" if state == "pending" else "committed_replay",
            decision,
            prepared,
            (),
            tuple(read_guards),
        )

    if existing_prepared is not None:
        old_state = lifecycle_prepared_state(existing_prepared, current)
        if (
            old_state == "stale"
            and trashed_committed is not None
            and _trash_proof_commits_prepared(
                existing_prepared, trashed_committed
            )
        ):
            old_state = "committed"
        if old_state == "pending":
            if existing_prepared.transition_id == prepared.transition_id:
                code = "LIFECYCLE_TRANSITION_MISMATCH"
                reason = "prepared transition bindings changed for the same transition"
            else:
                code = "LIFECYCLE_TRANSITION_PENDING"
                reason = (
                    "retry the exact pending lifecycle transition before starting another"
                )
            raise RelationReviewError(code, reason)
        if old_state == "stale":
            if trashed_committed is not None:
                raise RelationReviewError(
                    "LIFECYCLE_TRANSITION_MISMATCH",
                    "trash proof does not bind the committed prepared transition",
                )
            raise RelationReviewError(
                "LIFECYCLE_TRANSITION_STALE",
                "prepared transition matches neither live side; reconcile lifecycle state",
            )
        if existing_prepared.transition_id == prepared.transition_id:
            raise RelationReviewError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "prepared transition bindings changed for the same transition",
            )

    existing_decision: LifecycleDecision | None = None
    decision_guard: vault.PathGuard | None = None
    if decision is not None:
        existing_decision, decision_guard = _load_lifecycle_decision_bound(
            root, decision.page_identity, decision.after_fingerprint
        )
        if existing_decision is not None and existing_decision != decision:
            raise RelationReviewError(
                "RELATION_REVIEW_DECISION_COLLISION",
                "immutable lifecycle decision collides with existing bytes",
            )
        if existing_decision is not None and decision_guard is not None:
            read_guards.append(decision_guard)

    prepared_path = lifecycle_prepared_path(root, prepared.page_identity)
    prepared_relative = prepared_path.relative_to(root).as_posix()
    if existing_prepared is None:
        if lifecycle_prepared_state(prepared, current) != "pending":
            raise RelationReviewError(
                "LIFECYCLE_TRANSITION_STALE",
                "new transition does not bind the current primary",
            )
        if decision is not None and existing_decision is None:
            writes.append(_plan_lifecycle_decision_install(root, decision))
        writes.append(
            vault.PlannedWrite(
                prepared_path,
                serialize_lifecycle_prepared(prepared),
                create_only=True,
                guard=vault.PathGuard.capture(
                    root, prepared_relative, leaf_policy="absent"
                ),
            )
        )
        return LifecycleTransitionPlan(
            "new", decision, prepared, tuple(writes), tuple(read_guards)
        )

    assert prepared_guard is not None
    if lifecycle_prepared_state(prepared, current) != "pending":
        raise RelationReviewError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "next transition does not bind the committed primary",
        )
    if decision is not None and existing_decision is None:
        writes.append(_plan_lifecycle_decision_install(root, decision))
    writes.append(
        vault.PlannedWrite(
            prepared_path,
            serialize_lifecycle_prepared(prepared),
            guard=prepared_guard,
        )
    )
    return LifecycleTransitionPlan(
        "replace_committed", decision, prepared, tuple(writes), tuple(read_guards)
    )


@dataclass(frozen=True, slots=True)
class _OpenReviewDirectory:
    root: Path
    path: Path
    descriptor: int
    names: tuple[str, ...]
    chain: tuple[vault.PathIdentity, ...]
    descriptor_relative: bool


def _capture_review_directory(
    root: Path,
    nested: tuple[str, ...] = (),
) -> tuple[Path, tuple[vault.PathIdentity, ...] | None]:
    directory = root / kb_dirname() / "_Schema" / "relation-reviews" / Path(*nested)
    current = root
    components = (kb_dirname(), "_Schema", "relation-reviews", *nested)
    identities: list[vault.PathIdentity] = []
    for component in (None, *components):
        if component is not None:
            current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return directory, None
        except OSError as error:
            raise RelationReviewError(
                "RELATION_REVIEW_DIRECTORY_UNSAFE",
                "review directory cannot be inspected",
            ) from error
        if stat.S_ISLNK(info.st_mode) or vault._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise RelationReviewError(
                "RELATION_REVIEW_DIRECTORY_UNSAFE", "review directory is unsafe"
            )
        relative = "." if component is None else current.relative_to(root).as_posix()
        identities.append(vault._identity(relative, info))
    return directory, tuple(identities)


def _recheck_review_directory(root: Path, chain: tuple[vault.PathIdentity, ...]) -> None:
    for expected in chain:
        path = root if expected.relative_path == "." else root / expected.relative_path
        try:
            current = path.lstat()
        except OSError as error:
            raise RelationReviewError(
                "RELATION_REVIEW_SWAPPED", "review directory changed during access"
            ) from error
        if (
            not vault._same_identity(expected, current)
            or stat.S_ISLNK(current.st_mode)
            or vault._is_reparse(current)
            or not stat.S_ISDIR(current.st_mode)
        ):
            raise RelationReviewError(
                "RELATION_REVIEW_SWAPPED", "review directory changed during access"
            )


@contextmanager
def _open_review_directory(
    root: Path,
    *,
    nested: tuple[str, ...] = (),
    max_entries: int | None = None,
) -> Iterator[_OpenReviewDirectory | None]:
    directory, chain = _capture_review_directory(root, nested)
    if chain is None:
        yield None
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_DIRECTORY_UNSAFE", "review directory cannot be opened"
        ) from error
    try:
        opened_info = os.fstat(descriptor)
        if not vault._same_identity(chain[-1], opened_info) or not stat.S_ISDIR(
            opened_info.st_mode
        ):
            raise RelationReviewError(
                "RELATION_REVIEW_SWAPPED", "review directory changed during access"
            )
        descriptor_relative = _SUPPORTS_REVIEW_DIR_FD
        if max_entries is None:
            try:
                raw_names = os.listdir(descriptor)
            except (TypeError, NotImplementedError):  # pragma: no cover - platform fallback
                raw_names = [entry.name for entry in os.scandir(directory)]
                descriptor_relative = False
        else:
            try:
                census = vault.DirectoryCensusGuard.capture(
                    root,
                    directory.relative_to(root).as_posix(),
                    max_entries=max_entries,
                )
            except vault.PathGuardError as error:
                code = (
                    "RELATION_REVIEW_DIRECTORY_LIMIT"
                    if error.code == "PATH_GUARD_LIMIT"
                    else "RELATION_REVIEW_DIRECTORY_UNSAFE"
                )
                raise RelationReviewError(
                    code, "review directory cannot be enumerated safely"
                ) from error
            raw_names = [Path(entry.relative_path).name for entry in census.entries]
        names = tuple(sorted(raw_names, key=lambda name: name.encode("utf-8")))
        _recheck_review_directory(root, chain)
        yield _OpenReviewDirectory(
            root, directory, descriptor, names, chain, descriptor_relative
        )
    except UnicodeEncodeError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_DIRECTORY_UNSAFE", "review directory name is invalid"
        ) from error
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_DIRECTORY_UNSAFE",
            "review directory cannot be enumerated",
        ) from error
    finally:
        os.close(descriptor)


def _artifact_children_for(opened: _OpenReviewDirectory | None, identity: str) -> tuple[str, ...]:
    if opened is None:
        return ()
    expected = f"{identity}.json"
    matches = tuple(name for name in opened.names if name.casefold() == expected)
    if any(name != expected for name in matches) or len(matches) > 1:
        raise RelationReviewError("RELATION_REVIEW_ALIAS", "review identity has a filename alias")
    return matches


def _read_artifact_bytes(
    opened: _OpenReviewDirectory, name: str
) -> tuple[bytes, vault.PathGuard]:
    path = opened.path / name
    try:
        before = (
            os.stat(name, dir_fd=opened.descriptor, follow_symlinks=False)
            if opened.descriptor_relative
            else path.lstat()
        )
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_IO", "review artifact cannot be inspected"
        ) from error
    if (
        stat.S_ISLNK(before.st_mode)
        or vault._is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise RelationReviewError("RELATION_REVIEW_UNSAFE_FILE", "review artifact is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = (
            os.open(name, flags, dir_fd=opened.descriptor)
            if opened.descriptor_relative
            else os.open(path, flags)
        )
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_IO", "review artifact cannot be opened"
        ) from error
    try:
        opened_info = os.fstat(descriptor)
        if not stat.S_ISREG(opened_info.st_mode):
            raise RelationReviewError("RELATION_REVIEW_UNSAFE_FILE", "review artifact is unsafe")
        if not vault._same_identity(vault._identity(name, before), opened_info):
            raise RelationReviewError(
                "RELATION_REVIEW_SWAPPED", "review artifact changed during read"
            )
        raw = os.read(descriptor, _MAX_ARTIFACT_BYTES + 1)
    except OSError as error:
        raise RelationReviewError("RELATION_REVIEW_IO", "review artifact cannot be read") from error
    finally:
        os.close(descriptor)
    try:
        after = (
            os.stat(name, dir_fd=opened.descriptor, follow_symlinks=False)
            if opened.descriptor_relative
            else path.lstat()
        )
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_SWAPPED", "review artifact changed during read"
        ) from error
    if not vault._same_identity(vault._identity(name, before), after):
        raise RelationReviewError("RELATION_REVIEW_SWAPPED", "review artifact changed during read")
    _recheck_review_directory(opened.root, opened.chain)
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise RelationReviewError(
            "RELATION_REVIEW_TOO_LARGE", "review artifact exceeds its size limit"
        )
    relative = path.relative_to(opened.root).as_posix()
    try:
        guard = vault.PathGuard.capture(
            opened.root,
            relative,
            leaf_policy="content",
            expected_content_hash=hashlib.sha256(raw).hexdigest(),
        )
    except vault.PathGuardError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_SWAPPED", "review artifact changed during read"
        ) from error
    _recheck_review_directory(opened.root, opened.chain)
    return raw, guard


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def parse_exact_json_object(text: str) -> dict[str, Any]:
    """Parse one strict JSON object, rejecting duplicate keys at every depth."""
    value = json.loads(text, object_pairs_hook=_object_no_duplicates)
    if type(value) is not dict:
        raise ValueError("JSON root must be an object")
    return value


def _safe_record_path(value: str) -> bool:
    posix = PurePosixPath(value)
    return bool(
        value
        and "\\" not in value
        and "\0" not in value
        and posix.as_posix() == value
        and not posix.is_absolute()
        and not any(part in {"", ".", ".."} for part in posix.parts)
        and posix.parts
        and posix.parts[0] == kb_dirname()
        and posix.suffix.casefold() == ".md"
    )


def _parse_record(
    opened: _OpenReviewDirectory, name: str, reference: str
) -> tuple[RelationReviewRecord, str]:
    raw, _ = _read_artifact_bytes(opened, name)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_ENCODING", "review artifact is not strict UTF-8"
        ) from error
    try:
        value = json.loads(text, object_pairs_hook=_object_no_duplicates)
    except _DuplicateJsonKey as error:
        raise RelationReviewError(
            "RELATION_REVIEW_DUPLICATE_KEY", "review artifact contains a duplicate key"
        ) from error
    except json.JSONDecodeError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_JSON", "review artifact is not valid JSON"
        ) from error
    if not isinstance(value, dict):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_JSON", "review artifact root is not an object"
        )
    if "schema_version" not in value:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid keys"
        )
    version = value["schema_version"]
    if type(version) is not int:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA",
            "review artifact schema version must be an integer",
        )
    if version not in {1, _SCHEMA_VERSION}:
        raise RelationReviewError(
            "RELATION_REVIEW_UNSUPPORTED_VERSION", "review artifact schema version is unsupported"
        )
    expected_keys = _V1_RECORD_KEYS if version == 1 else _V2_RECORD_KEYS
    if set(value) != expected_keys:
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid keys"
        )
    string_keys = (
        "kind",
        "page_identity",
        "page_path_at_review",
        "content_fingerprint",
        "draft_hash",
        "auxiliary_hash",
    )
    if any(type(value[key]) is not str for key in string_keys):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact has invalid field types"
        )
    identity = value["page_identity"]
    kind = value["kind"]
    reason = value["reason"]
    valid_reason = (
        reason is None
        if kind in {"bootstrap", "qualifying"}
        else type(reason) is str
        and reason == reason.strip()
        and bool(reason)
        and len(reason) <= _MAX_REASON_POINTS
        and len(reason.encode("utf-8")) <= _MAX_REASON_BYTES
    )
    if (
        kind not in (_KINDS if version == 2 else {"reviewed_none", "bootstrap"})
        or normalize_id(identity) != identity
        or not _safe_record_path(value["page_path_at_review"])
        or any(
            not _HASH.fullmatch(value[key])
            for key in ("content_fingerprint", "draft_hash", "auxiliary_hash")
        )
        or not valid_reason
    ):
        raise RelationReviewError(
            "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
        )
    if PurePosixPath(name).stem != identity:
        raise RelationReviewError(
            "RELATION_REVIEW_FILENAME_MISMATCH",
            "review artifact filename does not match its identity",
        )
    operation = value.get("operation")
    token_hash = value.get("draft_token_hash")
    predecessor_path = value.get("predecessor_path")
    predecessor_hash = value.get("predecessor_content_hash")
    if version == 2:
        predecessor_pair = (predecessor_path is not None, predecessor_hash is not None)
        valid_predecessor = (
            predecessor_pair == (False, False)
            if operation != "replacement"
            else predecessor_pair == (True, True)
            and type(predecessor_path) is str
            and _safe_record_path(predecessor_path)
            and type(predecessor_hash) is str
            and bool(_HASH.fullmatch(predecessor_hash))
        )
        if (
            type(operation) is not str
            or operation not in _OPERATIONS
            or type(token_hash) is not str
            or not _HASH.fullmatch(token_hash)
            or not valid_predecessor
        ):
            raise RelationReviewError(
                "RELATION_REVIEW_INVALID_SCHEMA", "review artifact schema is invalid"
            )
    record = RelationReviewRecord(
        version,
        kind,
        identity,
        value["page_path_at_review"],
        value["content_fingerprint"],
        value["draft_hash"],
        value["auxiliary_hash"],
        reason,
        reference,
        operation,
        token_hash,
        predecessor_path,
        predecessor_hash,
    )
    return record, hashlib.sha256(raw).hexdigest()


def _load_one(root: Path, identity: str) -> tuple[RelationReviewRecord | None, str | None]:
    with _open_review_directory(root) as opened:
        matches = _artifact_children_for(opened, identity)
        if not matches or opened is None:
            return None, None
        name = matches[0]
        reference = (opened.path / name).relative_to(root).as_posix()
        return _parse_record(opened, name, reference)


def load_relation_reviews(vault_root: Path) -> tuple[RelationReviewRecord, ...]:
    root = Path(vault_root).absolute()
    try:
        records: list[RelationReviewRecord] = []
        logical: set[str] = set()
        with _open_review_directory(root) as opened:
            if opened is not None:
                for name in opened.names:
                    path = PurePosixPath(name)
                    if name.endswith((".tmp", ".bak")):
                        continue
                    if path.suffix.casefold() != ".json":
                        continue
                    identity = path.stem
                    if name != f"{identity}.json" or normalize_id(identity) != identity:
                        raise RelationReviewError(
                            "RELATION_REVIEW_ALIAS",
                            "review directory contains a filename alias",
                        )
                    if identity.casefold() in logical:
                        raise RelationReviewError(
                            "RELATION_REVIEW_ALIAS",
                            "review directory contains a logical collision",
                        )
                    logical.add(identity.casefold())
                    reference = (opened.path / name).relative_to(root).as_posix()
                    record, _ = _parse_record(opened, name, reference)
                    if record.kind in {"reviewed_none", "bootstrap"}:
                        records.append(record)
        return tuple(sorted(records, key=lambda item: (item.page_identity, item.reference)))
    except RelationReviewError:
        raise
    except Exception as error:
        raise RelationReviewError(
            "RELATION_REVIEW_IO", "relation reviews could not be loaded"
        ) from error


def load_relation_review(
    vault_root: Path,
    page_state: semantic_contract.SemanticPageState,
    *,
    corpus: semantic_contract.SemanticCorpusContext,
) -> semantic_contract.RelationReviewState | None:
    try:
        if page_state.identity_kind != "exomem_id":
            return None
        paths = corpus.identity_census.paths_by_identity.get(page_state.identity)
        if paths != (page_state.path,):
            return None
        if page_state.review_fingerprint is not None:
            decision = load_lifecycle_decision(
                vault_root,
                page_state.identity,
                page_state.review_fingerprint,
            )
            if decision is not None:
                return semantic_contract.RelationReviewState(
                    "reviewed_none",
                    decision.page_identity,
                    decision.after_fingerprint,
                    reason=decision.reason,
                    reference=decision.reference,
                )
        record, _ = _load_one(Path(vault_root).absolute(), page_state.identity)
        if record is None or record.kind == "qualifying":
            return None
        return semantic_contract.RelationReviewState(
            record.kind,
            record.page_identity,
            record.content_fingerprint,
            reason=record.reason,
            reference=record.reference,
        )
    except RelationReviewError:
        raise
    except Exception as error:
        raise RelationReviewError(
            "RELATION_REVIEW_IO", "relation review could not be loaded"
        ) from error


def load_creation_receipt(
    vault_root: Path, page_identity: str
) -> RelationReviewRecord | None:
    """Load any creation receipt, including internal qualifying receipts."""
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    record, _ = _load_one(Path(vault_root).absolute(), identity)
    return record


def _relation_candidates(
    page: semantic_contract.SemanticPageState,
    corpus: semantic_contract.SemanticCorpusContext,
) -> tuple[tuple[RelationCandidate, ...], int]:
    candidates: list[RelationCandidate] = []
    for direction, facts in (
        ("inbound", corpus.inbound.get(page.path, ())),
        ("outbound", corpus.outbound.get(page.path, ())),
    ):
        for fact in facts:
            qualification = semantic_contract.qualify_relation(
                fact, registry=corpus.registry, corpus=corpus
            )
            truncated = len(fact.raw_target) > _MAX_RAW_TARGET
            candidates.append(
                RelationCandidate(
                    direction,
                    fact.identity,
                    fact.logical_source_path,
                    fact.logical_target_path,
                    fact.raw_relation,
                    fact.canonical_relation,
                    fact.raw_target[:_MAX_RAW_TARGET],
                    fact.resolved_target_path,
                    fact.target_status,
                    qualification.qualifies,
                    qualification.reasons,
                    truncated,
                )
            )
    candidates.sort(key=lambda item: (item.direction, item.fact_identity))
    return tuple(candidates[:_MAX_CANDIDATES]), len(candidates)


def _validation(
    draft_id: str,
    destination: str,
    fingerprint: str,
    result: semantic_contract.SemanticContractResult,
    candidate: semantic_contract.SemanticPageState,
    corpus: semantic_contract.SemanticCorpusContext,
    draft_token_hash: str | None,
) -> CreationDraftValidation:
    candidates, total = _relation_candidates(candidate, corpus)
    reviewed_required = result.relation_disposition.kind in {"missing", "stale"}
    non_review = tuple(
        finding
        for finding in result.blocking_findings
        if finding.resolved_rule != ("relations", "*", "disposition")
    )
    return CreationDraftValidation(
        draft_id,
        _draft_hash(draft_id, destination, fingerprint, draft_token_hash),
        fingerprint,
        destination,
        False,
        result.relation_disposition.kind,
        reviewed_required,
        bool(non_review),
        not result.should_block,
        reviewed_required and not non_review,
        candidates,
        total,
        total > _MAX_CANDIDATES,
        result,
    )


def _evaluate(
    candidate: semantic_contract.SemanticPageState,
    before: semantic_contract.SemanticCorpusContext,
    after: semantic_contract.SemanticCorpusContext,
    contracts: memory_schema.ResolvedMemoryContracts,
    operation: str,
    review: semantic_contract.RelationReviewState | None,
) -> semantic_contract.SemanticContractResult:
    return semantic_contract.evaluate(
        before=None,
        after=candidate,
        operation=operation,
        mode="precommit",
        before_contracts=contracts,
        after_contracts=contracts,
        before_corpus=before,
        after_corpus=after,
        after_review=review,
    )


def _attempt(
    root: Path,
    *,
    path: object,
    source: str,
    draft_id: str,
    operation: str,
    commit_auxiliaries: tuple[vault.PlannedWrite, ...] | None = None,
    commit_disposition: str | None = None,
    commit_reason: str | None = None,
    draft_token_hash: str | None = None,
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> _Attempt:
    if operation not in _OPERATIONS:
        raise RelationReviewError("INVALID_DRAFT_OPERATION", "unsupported creation operation")
    identity = _canonical_id(draft_id)
    normalized = _normalize_source(source)
    _validate_identity(normalized, identity)
    destination_path, destination = _normalize_destination(root, path)
    try:
        artifact, artifact_hash = _load_one(root, identity)
    except RelationReviewError as error:
        if error.code == "RELATION_REVIEW_DIRECTORY_UNSAFE":
            raise
        raise RelationReviewError(
            "DRAFT_ID_IN_USE", "draft identity is already reserved"
        ) from error
    lifecycle_guard: vault.DirectoryCensusGuard | None = None
    if artifact is None:
        try:
            _, _, lifecycle_guard = _inspect_lifecycle_identity(root, identity)
        except RelationReviewError as error:
            raise RelationReviewError(
                "DRAFT_ID_IN_USE", "draft identity is already reserved"
            ) from error
        if lifecycle_guard.directory_identity is not None:
            raise RelationReviewError(
                "DRAFT_ID_IN_USE", "draft identity is already reserved"
            )
    destination_exists = os.path.lexists(destination_path)
    if destination_exists:
        try:
            destination_info = destination_path.lstat()
        except OSError as error:
            raise RelationReviewError(
                "DRAFT_DESTINATION_OCCUPIED", "draft destination cannot be inspected"
            ) from error
        if (
            stat.S_ISLNK(destination_info.st_mode)
            or vault._is_reparse(destination_info)
            or not stat.S_ISREG(destination_info.st_mode)
        ):
            if artifact is not None:
                raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
            raise RelationReviewError(
                "DRAFT_DESTINATION_OCCUPIED", "draft destination is already occupied"
            )
    registry = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    loaded_contracts = memory_schema.load_saved_contracts(root)
    before = semantic_contract.build_corpus_context(
        root, registry=registry, language_registry=language
    )
    candidate = semantic_contract.build_page_state(
        root,
        destination,
        normalized,
        relation_registry=registry,
        language_registry=language,
    )
    if not candidate.eligible_compiled:
        raise RelationReviewError(
            "INVALID_DRAFT_PATH", "draft destination is not an eligible compiled page"
        )
    fingerprint = semantic_contract.review_content_fingerprint(identity, normalized)
    if candidate.review_fingerprint != fingerprint:
        raise RelationReviewError("INVALID_DRAFT_SOURCE", "draft fingerprint is inconsistent")
    id_paths = before.identity_census.paths_by_identity.get(identity, ())
    exact_destination = False
    if destination_exists and id_paths == (destination,):
        state = before.pages.get(destination)
        exact_destination = bool(
            state is not None
            and state.identity_kind == "exomem_id"
            and state.identity == identity
            and state.source_hash == vault.content_hash(normalized)
        )
    if id_paths and not exact_destination:
        raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already in use")
    if artifact is not None and destination_exists and not exact_destination:
        raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
    if destination_exists and not exact_destination:
        raise RelationReviewError(
            "DRAFT_DESTINATION_OCCUPIED", "draft destination is already occupied"
        )
    after = before.with_candidate(candidate)
    contracts = memory_schema.resolve_contracts(
        loaded_contracts,
        projects=candidate.projects,
        page_type=candidate.page_type,
        language_registry=language,
    )
    review = None
    if artifact is not None and artifact.kind != "qualifying":
        review = semantic_contract.RelationReviewState(
            artifact.kind,
            artifact.page_identity,
            artifact.content_fingerprint,
            reason=artifact.reason,
            reference=artifact.reference,
        )
    result = _evaluate(candidate, before, after, contracts, operation, review)
    validation = _validation(
        identity,
        destination,
        fingerprint,
        result,
        candidate,
        after,
        draft_token_hash,
    )
    if exact_destination:
        expected_kind = (
            "qualifying"
            if result.relation_disposition.kind == "qualifying_relation"
            else result.relation_disposition.kind
        )
        expected_artifact = expected_kind in _KINDS
        receipt_expected_kind = expected_kind
        if commit_disposition == "reviewed_none":
            receipt_expected_kind = "reviewed_none"
        elif (
            expected_kind not in {"bootstrap", "qualifying"}
            and artifact is not None
            and artifact.kind == "bootstrap"
        ):
            # A committed first page is no longer evaluated as bootstrap once
            # its own primary is visible, and remains immutable after growth.
            receipt_expected_kind = artifact.kind
        artifact_matches = bool(
            artifact
            and artifact.draft_hash == validation.draft_hash
            and artifact.content_fingerprint == fingerprint
            and artifact.page_path_at_review == destination
            and (
                (
                    artifact.schema_version == 1
                    and artifact.kind == expected_kind
                    and expected_artifact
                )
                or (
                    artifact.schema_version == 2
                    and artifact.kind == receipt_expected_kind
                    and artifact.operation == operation
                    and artifact.draft_token_hash == draft_token_hash
                    and artifact.predecessor_path == predecessor_path
                    and artifact.predecessor_content_hash == predecessor_content_hash
                )
            )
        )
        exact_replay = artifact_matches or (
            result.relation_disposition.kind == "qualifying_relation" and artifact is None
        )
        if (
            exact_replay
            and artifact is not None
            and artifact.schema_version == 1
            and commit_auxiliaries is not None
        ):
            auxiliary_digest = _auxiliary_hash(commit_auxiliaries, root)
            if result.relation_disposition.kind == "reviewed_none":
                exact_replay = bool(
                    commit_disposition == "reviewed_none"
                    and artifact is not None
                    and artifact.reason == commit_reason
                    and artifact.auxiliary_hash == auxiliary_digest
                )
            elif result.relation_disposition.kind == "bootstrap":
                exact_replay = bool(
                    commit_disposition is None
                    and artifact is not None
                    and artifact.auxiliary_hash == auxiliary_digest
                )
            else:
                exact_replay = False
            exact_replay = exact_replay and _all_auxiliaries_match(commit_auxiliaries, root)
        if exact_replay:
            raise RelationReviewError("DRAFT_ALREADY_COMMITTED", "draft is already committed")
        raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already in use")
    return _Attempt(
        normalized,
        destination,
        candidate,
        before,
        after,
        contracts,
        result,
        validation,
        artifact,
        artifact_hash,
        lifecycle_guard,
    )


def _translate(error: Exception) -> RelationReviewError:
    if isinstance(error, RelationReviewError):
        return error
    if isinstance(error, vault.VaultLockTimeout):
        return RelationReviewError(
            "SEMANTIC_CREATION_LOCK_TIMEOUT", "timed out acquiring semantic creation lock"
        )
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]+", code):
        return RelationReviewError(code, "semantic creation validation failed")
    return RelationReviewError("SEMANTIC_CREATION_FAILED", "semantic creation validation failed")


def validate_creation_draft(
    vault_root: Path,
    *,
    path: str,
    source: str,
    draft_id: str,
    operation: str,
) -> CreationDraftValidation:
    try:
        attempt = _attempt(
            Path(vault_root).absolute(),
            path=path,
            source=source,
            draft_id=draft_id,
            operation=operation,
        )
        if attempt.artifact is not None:
            raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
        return attempt.validation
    except RelationReviewError:
        raise
    except Exception as error:
        raise _translate(error) from error


def revalidate_prepared_creation_draft(
    vault_root: Path,
    *,
    path: str,
    source: str,
    draft_id: str,
    operation: str,
    draft_token: str,
    requested_disposition: str | None = None,
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> CreationDraftValidation:
    """Validate an exact page-less v1/v2 prepared retry without mutating it.

    The final auxiliary digest/reason equality remains commit-time authority;
    this seam only lets deterministic writer preparation proceed far enough to
    reconstruct that exact ordered batch.
    """
    try:
        if requested_disposition not in {None, "reviewed_none"}:
            raise RelationReviewError(
                "INVALID_RELATION_REVIEW", "relation disposition is invalid"
            )
        token_hash = draft_token_hash(draft_token)
        attempt = _attempt(
            Path(vault_root).absolute(),
            path=path,
            source=source,
            draft_id=draft_id,
            operation=operation,
            commit_disposition=requested_disposition,
            draft_token_hash=token_hash,
            predecessor_path=predecessor_path,
            predecessor_content_hash=predecessor_content_hash,
        )
        record = attempt.artifact
        if record is None:
            return attempt.validation
        validation = attempt.validation
        expected_kind = (
            "qualifying"
            if validation.relation_disposition == "qualifying_relation"
            else validation.relation_disposition
        )
        if requested_disposition == "reviewed_none":
            expected_kind = "reviewed_none"
        matches = bool(
            record.page_identity == validation.draft_id
            and record.page_path_at_review == validation.destination
            and record.content_fingerprint == validation.content_fingerprint
            and record.draft_hash == validation.draft_hash
            and record.kind == expected_kind
            and (
                record.schema_version == 1
                or (
                    record.operation == operation
                    and record.draft_token_hash == token_hash
                    and record.predecessor_path == predecessor_path
                    and record.predecessor_content_hash == predecessor_content_hash
                )
            )
        )
        if not matches:
            raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
        return validation
    except RelationReviewError:
        raise
    except Exception as error:
        raise _translate(error) from error


def _normalize_auxiliary_path(root: Path, path: Path) -> tuple[Path, str]:
    if not isinstance(path, Path):
        raise RelationReviewError("INVALID_AUXILIARY_WRITE", "auxiliary target must be a path")
    raw = str(path)
    if "\0" in raw or (os.name != "nt" and "\\" in raw):
        raise RelationReviewError("INVALID_AUXILIARY_WRITE", "auxiliary target is unsafe")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RelationReviewError("INVALID_AUXILIARY_WRITE", "auxiliary target is unsafe")
    root_absolute = Path(os.path.abspath(root))
    try:
        relative_path = path.relative_to(root_absolute) if path.is_absolute() else path
    except ValueError as error:
        raise RelationReviewError(
            "INVALID_AUXILIARY_WRITE", "auxiliary target is outside the vault"
        ) from error
    relative = relative_path.as_posix()
    posix = PurePosixPath(relative)
    if not relative or posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise RelationReviewError("INVALID_AUXILIARY_WRITE", "auxiliary target is unsafe")
    return root_absolute / relative, relative


def _portable_alias(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _detach_auxiliaries(
    root: Path,
    values: object,
    *,
    primary: str,
    artifact: str,
) -> tuple[vault.PlannedWrite, ...]:
    if type(values) not in {tuple, list}:
        raise RelationReviewError(
            "INVALID_AUXILIARY_WRITE", "auxiliary_writes must be a finite tuple or list"
        )
    detached: list[vault.PlannedWrite] = []
    seen = {_portable_alias(primary), _portable_alias(artifact)}
    for value in values:
        if type(value) is not vault.PlannedWrite or type(value.content) is not str:
            raise RelationReviewError(
                "INVALID_AUXILIARY_WRITE", "auxiliary write has an invalid type"
            )
        try:
            value.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise RelationReviewError(
                "INVALID_AUXILIARY_WRITE", "auxiliary content is invalid Unicode"
            ) from error
        absolute, relative = _normalize_auxiliary_path(root, value.path)
        alias = _portable_alias(relative)
        if alias in seen:
            raise RelationReviewError("INVALID_AUXILIARY_WRITE", "auxiliary targets collide")
        if value.guard is not None:
            if value.guard.target != relative:
                raise RelationReviewError(
                    "INVALID_AUXILIARY_WRITE", "auxiliary guard target does not match"
                )
            value.guard.recheck(root)
        else:
            leaf_policy = "stable" if os.path.lexists(absolute) else "absent"
            try:
                vault.PathGuard.capture(root, relative, leaf_policy=leaf_policy)
            except vault.PathGuardError as error:
                raise RelationReviewError(
                    "INVALID_AUXILIARY_WRITE", "auxiliary target is unsafe"
                ) from error
        seen.add(alias)
        detached.append(
            vault.PlannedWrite(
                absolute,
                value.content,
                create_only=value.create_only,
                guard=value.guard,
                expected_hash=value.expected_hash,
                ensure_directories=value.ensure_directories,
            )
        )
    return tuple(detached)


def _review_reason(value: object) -> str:
    if type(value) is not str:
        raise RelationReviewError("INVALID_RELATION_REVIEW", "reviewed-none requires a reason")
    reason = value.strip()
    if (
        not reason
        or len(reason) > _MAX_REASON_POINTS
        or len(reason.encode("utf-8")) > _MAX_REASON_BYTES
    ):
        raise RelationReviewError(
            "INVALID_RELATION_REVIEW", "reviewed-none reason is outside bounds"
        )
    return reason


def _serialize_record(record: RelationReviewRecord) -> str:
    return json.dumps(record.storage_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _all_auxiliaries_match(writes: tuple[vault.PlannedWrite, ...], root: Path) -> bool:
    for write in writes:
        if not os.path.lexists(write.path):
            return False
        relative = write.path.relative_to(root).as_posix()
        expected_hash = hashlib.sha256(write.content.encode("utf-8")).hexdigest()
        try:
            vault.PathGuard.capture(
                root,
                relative,
                leaf_policy="content",
                expected_content_hash=expected_hash,
            )
        except vault.PathGuardError as error:
            if error.code == "PATH_GUARD_CONTENT":
                return False
            raise RelationReviewError(
                "INVALID_AUXILIARY_WRITE", "auxiliary replay target is unsafe"
            ) from error
    return True


def _commit_plan(
    attempt: _Attempt,
    *,
    operation: str,
    requested_review: bool,
    supplied_hash: str | None,
    reason: str | None,
    auxiliary_digest: str,
    artifact_reference: str,
    draft_token_hash: str,
    predecessor_path: str | None,
    predecessor_content_hash: str | None,
) -> tuple[semantic_contract.SemanticContractResult, RelationReviewRecord | None]:
    validation = attempt.validation
    if requested_review and supplied_hash != validation.draft_hash:
        raise RelationReviewError("DRAFT_HASH_MISMATCH", "draft hash requires fresh validation")
    if validation.has_non_review_blockers:
        raise RelationReviewError(
            "SEMANTIC_CONTRACT_BLOCKED", "semantic contract has blocking findings"
        )
    result = attempt.result
    record: RelationReviewRecord | None = None
    if requested_review:
        if result.relation_disposition.kind not in {
            "missing",
            "stale",
            "reviewed_none",
        }:
            raise RelationReviewError(
                "RELATION_REVIEW_NOT_APPLICABLE", "reviewed-none is not applicable"
            )
        review_state = semantic_contract.RelationReviewState(
            "reviewed_none",
            validation.draft_id,
            validation.content_fingerprint,
            reason=reason,
            reference=artifact_reference,
        )
        if result.relation_disposition.kind != "reviewed_none":
            result = _evaluate(
                attempt.candidate,
                attempt.before_corpus,
                attempt.after_corpus,
                attempt.contracts,
                operation,
                review_state,
            )
        if result.should_block or result.relation_disposition.kind != "reviewed_none":
            raise RelationReviewError(
                "SEMANTIC_CONTRACT_BLOCKED", "semantic contract rejected reviewed-none"
            )
        record = RelationReviewRecord(
            2,
            "reviewed_none",
            validation.draft_id,
            validation.destination,
            validation.content_fingerprint,
            validation.draft_hash,
            auxiliary_digest,
            reason,
            artifact_reference,
            operation,
            draft_token_hash,
            predecessor_path,
            predecessor_content_hash,
        )
    elif result.relation_disposition.kind == "bootstrap":
        record = RelationReviewRecord(
            2,
            "bootstrap",
            validation.draft_id,
            validation.destination,
            validation.content_fingerprint,
            validation.draft_hash,
            auxiliary_digest,
            None,
            artifact_reference,
            operation,
            draft_token_hash,
            predecessor_path,
            predecessor_content_hash,
        )
    elif result.relation_disposition.kind == "qualifying_relation":
        record = RelationReviewRecord(
            2,
            "qualifying",
            validation.draft_id,
            validation.destination,
            validation.content_fingerprint,
            validation.draft_hash,
            auxiliary_digest,
            None,
            artifact_reference,
            operation,
            draft_token_hash,
            predecessor_path,
            predecessor_content_hash,
        )
    elif result.should_block:
        raise RelationReviewError(
            "SEMANTIC_CONTRACT_BLOCKED", "semantic contract has blocking findings"
        )
    elif attempt.artifact is not None:
        raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
    else:
        raise RelationReviewError("INVALID_RELATION_REVIEW", "reviewed-none approval is required")
    if attempt.artifact is not None:
        if attempt.artifact.schema_version == 1:
            legacy_match = bool(
                record is not None
                and record.kind == attempt.artifact.kind
                and record.page_identity == attempt.artifact.page_identity
                and record.page_path_at_review == attempt.artifact.page_path_at_review
                and record.content_fingerprint == attempt.artifact.content_fingerprint
                and record.draft_hash == attempt.artifact.draft_hash
                and record.auxiliary_hash == attempt.artifact.auxiliary_hash
                and record.reason == attempt.artifact.reason
            )
            if not legacy_match:
                raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
            record = attempt.artifact
        elif record is None or attempt.artifact.storage_dict() != record.storage_dict():
            raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
    return result, record


def commit_creation_draft(
    vault_root: Path,
    *,
    path: str,
    source: str,
    draft_id: str,
    operation: str,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
    auxiliary_writes: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite] = (),
    draft_token: str = "",
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> CreationDraftCommit:
    root = Path(vault_root).absolute()
    try:
        identity = _canonical_id(draft_id)
        token_hash = draft_token_hash(draft_token)
        predecessor_fields = (predecessor_path is not None, predecessor_content_hash is not None)
        if operation == "replacement":
            if predecessor_fields != (True, True):
                raise RelationReviewError(
                    "INVALID_PREDECESSOR", "replacement requires a bound predecessor"
                )
            if type(predecessor_path) is not str or not _safe_record_path(predecessor_path):
                raise RelationReviewError("INVALID_PREDECESSOR", "predecessor path is unsafe")
            if type(predecessor_content_hash) is not str or not _HASH.fullmatch(
                predecessor_content_hash
            ):
                raise RelationReviewError("INVALID_PREDECESSOR", "predecessor hash is invalid")
        elif predecessor_fields != (False, False):
            raise RelationReviewError(
                "INVALID_PREDECESSOR", "predecessor binding is only valid for replacement"
            )
        _, destination = _normalize_destination(root, path)
        artifact_rel = review_artifact_path(root, identity).relative_to(root).as_posix()
        auxiliaries = _detach_auxiliaries(
            root, auxiliary_writes, primary=destination, artifact=artifact_rel
        )
        review_fields = (
            relation_disposition is not None,
            relation_review_hash is not None,
            relation_review_reason is not None,
        )
        if any(review_fields) and not all(review_fields):
            raise RelationReviewError(
                "INVALID_RELATION_REVIEW", "relation review fields must be supplied together"
            )
        if relation_disposition not in {None, "reviewed_none"}:
            raise RelationReviewError("INVALID_RELATION_REVIEW", "relation disposition is invalid")
        reason = (
            _review_reason(relation_review_reason)
            if relation_disposition == "reviewed_none"
            else None
        )
        normalized_preview = _normalize_source(source)
        _validate_identity(normalized_preview, identity)
        preview_hash = _draft_hash(
            identity,
            destination,
            semantic_contract.review_content_fingerprint(identity, normalized_preview),
            token_hash,
        )
        if relation_disposition == "reviewed_none" and relation_review_hash != preview_hash:
            raise RelationReviewError("DRAFT_HASH_MISMATCH", "draft hash requires fresh validation")
        auxiliary_digest = _auxiliary_hash(auxiliaries, root)
        requested_review = relation_disposition == "reviewed_none"
        preliminary = _attempt(
            root,
            path=path,
            source=source,
            draft_id=identity,
            operation=operation,
            commit_auxiliaries=auxiliaries,
            commit_disposition=relation_disposition,
            commit_reason=reason,
            draft_token_hash=token_hash,
            predecessor_path=predecessor_path,
            predecessor_content_hash=predecessor_content_hash,
        )
        _commit_plan(
            preliminary,
            operation=operation,
            requested_review=requested_review,
            supplied_hash=relation_review_hash,
            reason=reason,
            auxiliary_digest=auxiliary_digest,
            artifact_reference=artifact_rel,
            draft_token_hash=token_hash,
            predecessor_path=predecessor_path,
            predecessor_content_hash=predecessor_content_hash,
        )
        activation_manifest.ensure_manifest(
            root, census=preliminary.before_corpus.activation_census
        )
        with vault.vault_creation_lock(root, "semantic-creation"):
            attempt = _attempt(
                root,
                path=path,
                source=source,
                draft_id=identity,
                operation=operation,
                commit_auxiliaries=auxiliaries,
                commit_disposition=relation_disposition,
                commit_reason=reason,
                draft_token_hash=token_hash,
                predecessor_path=predecessor_path,
                predecessor_content_hash=predecessor_content_hash,
            )
            validation = attempt.validation
            result, record = _commit_plan(
                attempt,
                operation=operation,
                requested_review=requested_review,
                supplied_hash=relation_review_hash,
                reason=reason,
                auxiliary_digest=auxiliary_digest,
                artifact_reference=artifact_rel,
                draft_token_hash=token_hash,
                predecessor_path=predecessor_path,
                predecessor_content_hash=predecessor_content_hash,
            )

            prepared = attempt.artifact
            resumed = False
            required_guards: tuple[
                vault.PathGuard | vault.DirectoryCensusGuard, ...
            ] = ()
            writes: list[vault.PlannedWrite] = []
            if prepared is not None:
                if attempt.artifact_bytes_hash is None:
                    raise RelationReviewError(
                        "DRAFT_ID_IN_USE", "draft identity is already reserved"
                    )
                required_guards = (
                    vault.PathGuard.capture(
                        root,
                        artifact_rel,
                        leaf_policy="content",
                        expected_content_hash=attempt.artifact_bytes_hash,
                    ),
                )
                resumed = True
            elif record is not None:
                writes.append(
                    vault.PlannedWrite(
                        review_artifact_path(root, identity),
                        _serialize_record(record),
                        create_only=True,
                        guard=vault.PathGuard.capture(root, artifact_rel, leaf_policy="absent"),
                    )
                )
                if attempt.lifecycle_guard is None:
                    raise RelationReviewError(
                        "DRAFT_ID_IN_USE", "draft identity reservation is unguarded"
                    )
                required_guards = (*required_guards, attempt.lifecycle_guard)
            guarded_auxiliaries: list[vault.PlannedWrite] = []
            for auxiliary in auxiliaries:
                if auxiliary.guard is not None:
                    guarded_auxiliaries.append(auxiliary)
                    continue
                relative = auxiliary.path.relative_to(root).as_posix()
                policy = "content" if os.path.lexists(auxiliary.path) else "absent"
                expected_hash = None
                if policy == "content":
                    stable = vault.PathGuard.capture(root, relative, leaf_policy="stable")
                    assert stable.leaf_identity is not None
                    expected_hash = vault._leaf_hash(auxiliary.path, stable.leaf_identity)
                guarded_auxiliaries.append(
                    vault.PlannedWrite(
                        auxiliary.path,
                        auxiliary.content,
                        create_only=auxiliary.create_only,
                        guard=vault.PathGuard.capture(
                            root,
                            relative,
                            leaf_policy=policy,
                            expected_content_hash=expected_hash,
                        ),
                        expected_hash=auxiliary.expected_hash,
                        ensure_directories=auxiliary.ensure_directories,
                    )
                )
            writes.extend(guarded_auxiliaries)
            primary_guard = vault.PathGuard.capture(root, destination, leaf_policy="absent")
            writes.append(
                vault.PlannedWrite(
                    root / destination,
                    attempt.source,
                    create_only=True,
                    guard=primary_guard,
                )
            )
            try:
                written = vault.batch_atomic_write(
                    writes, vault_root=root, required_guards=required_guards
                )
            except vault.CreateOnlyConflict as error:
                if error.target == destination:
                    raise RelationReviewError(
                        "DRAFT_DESTINATION_OCCUPIED", "draft destination became occupied"
                    ) from error
                raise RelationReviewError(
                    "DRAFT_ID_IN_USE", "draft identity became reserved"
                ) from error
            except vault.PathGuardError:
                if attempt.lifecycle_guard is not None:
                    try:
                        attempt.lifecycle_guard.recheck(root)
                    except vault.PathGuardError as lifecycle_error:
                        raise RelationReviewError(
                            "DRAFT_ID_IN_USE", "draft identity became reserved"
                        ) from lifecycle_error
                raise
            written_paths = tuple(item.relative_to(root).as_posix() for item in written)
            review_record = record if record is not None and record.kind != "qualifying" else None
            reference = review_record.reference if review_record is not None else None
            return CreationDraftCommit(
                identity,
                validation.draft_hash,
                validation.content_fingerprint,
                destination,
                True,
                result.relation_disposition.kind,
                reference,
                review_record is not None,
                resumed,
                written_paths,
                result,
            )
    except RelationReviewError:
        raise
    except Exception as error:
        raise _translate(error) from error
