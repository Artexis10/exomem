"""Portable reviewed-none state and the internal semantic creation coordinator."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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

_SCHEMA_VERSION = 1
_OPERATIONS = frozenset({"create", "adoption_compile", "tier2_create"})
_KINDS = frozenset({"reviewed_none", "bootstrap"})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARTIFACT_BYTES = 16 * 1024
_MAX_REASON_POINTS = 2_000
_MAX_REASON_BYTES = 8_192
_MAX_CANDIDATES = 64
_MAX_RAW_TARGET = 1_024
_RECORD_KEYS = frozenset(
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
    kind: Literal["reviewed_none", "bootstrap"]
    page_identity: str
    page_path_at_review: str
    content_fingerprint: str
    draft_hash: str
    auxiliary_hash: str
    reason: str | None
    reference: str

    def storage_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "page_identity": self.page_identity,
            "page_path_at_review": self.page_path_at_review,
            "content_fingerprint": self.content_fingerprint,
            "draft_hash": self.draft_hash,
            "auxiliary_hash": self.auxiliary_hash,
            "reason": self.reason,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.storage_dict(), "reference": self.reference}


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


def _draft_hash(draft_id: str, destination: str, fingerprint: str) -> str:
    return _canonical_hash(
        {
            "schema_version": 1,
            "draft_id": draft_id,
            "destination": destination,
            "content_fingerprint": fingerprint,
        }
    )


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


def review_artifact_path(vault_root: Path, page_identity: str) -> Path:
    identity = _canonical_id(page_identity, code="RELATION_REVIEW_INVALID_ID")
    return Path(vault_root) / kb_dirname() / "_Schema" / "relation-reviews" / f"{identity}.json"


def _review_directory(root: Path) -> Path:
    directory = root / kb_dirname() / "_Schema" / "relation-reviews"
    current = root
    components = (kb_dirname(), "_Schema", "relation-reviews")
    for component in (None, *components):
        if component is not None:
            current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return directory
        except OSError as error:
            raise RelationReviewError(
                "RELATION_REVIEW_DIRECTORY_UNSAFE",
                "review directory cannot be inspected",
            ) from error
        if stat.S_ISLNK(info.st_mode) or vault._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise RelationReviewError(
                "RELATION_REVIEW_DIRECTORY_UNSAFE", "review directory is unsafe"
            )
    return directory


def _directory_children(root: Path) -> tuple[Path, ...]:
    directory = _review_directory(root)
    if not os.path.lexists(directory):
        return ()
    try:
        return tuple(
            Path(entry.path)
            for entry in sorted(os.scandir(directory), key=lambda item: item.name.encode("utf-8"))
        )
    except (OSError, UnicodeEncodeError) as error:
        raise RelationReviewError(
            "RELATION_REVIEW_DIRECTORY_UNSAFE", "review directory cannot be enumerated"
        ) from error


def _artifact_children_for(root: Path, identity: str) -> tuple[Path, ...]:
    expected = f"{identity}.json"
    matches = tuple(path for path in _directory_children(root) if path.name.casefold() == expected)
    if any(path.name != expected for path in matches) or len(matches) > 1:
        raise RelationReviewError("RELATION_REVIEW_ALIAS", "review identity has a filename alias")
    return matches


def _read_artifact_bytes(path: Path) -> bytes:
    try:
        before = path.lstat()
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
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_IO", "review artifact cannot be opened"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RelationReviewError("RELATION_REVIEW_UNSAFE_FILE", "review artifact is unsafe")
        if not vault._same_identity(vault._identity(path.name, before), opened):
            raise RelationReviewError(
                "RELATION_REVIEW_SWAPPED", "review artifact changed during read"
            )
        raw = os.read(descriptor, _MAX_ARTIFACT_BYTES + 1)
    except OSError as error:
        raise RelationReviewError("RELATION_REVIEW_IO", "review artifact cannot be read") from error
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as error:
        raise RelationReviewError(
            "RELATION_REVIEW_SWAPPED", "review artifact changed during read"
        ) from error
    if not vault._same_identity(vault._identity(path.name, before), after):
        raise RelationReviewError("RELATION_REVIEW_SWAPPED", "review artifact changed during read")
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise RelationReviewError(
            "RELATION_REVIEW_TOO_LARGE", "review artifact exceeds its size limit"
        )
    return raw


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


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


def _parse_record(path: Path, reference: str) -> tuple[RelationReviewRecord, str]:
    raw = _read_artifact_bytes(path)
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
    if type(version) is not int or version != _SCHEMA_VERSION:
        raise RelationReviewError(
            "RELATION_REVIEW_UNSUPPORTED_VERSION", "review artifact schema version is unsupported"
        )
    if set(value) != _RECORD_KEYS:
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
        if kind == "bootstrap"
        else type(reason) is str
        and reason == reason.strip()
        and bool(reason)
        and len(reason) <= _MAX_REASON_POINTS
        and len(reason.encode("utf-8")) <= _MAX_REASON_BYTES
    )
    if (
        kind not in _KINDS
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
    if path.stem != identity:
        raise RelationReviewError(
            "RELATION_REVIEW_FILENAME_MISMATCH",
            "review artifact filename does not match its identity",
        )
    record = RelationReviewRecord(
        _SCHEMA_VERSION,
        kind,
        identity,
        value["page_path_at_review"],
        value["content_fingerprint"],
        value["draft_hash"],
        value["auxiliary_hash"],
        reason,
        reference,
    )
    return record, hashlib.sha256(raw).hexdigest()


def _load_one(root: Path, identity: str) -> tuple[RelationReviewRecord | None, str | None]:
    matches = _artifact_children_for(root, identity)
    if not matches:
        return None, None
    path = matches[0]
    reference = path.relative_to(root).as_posix()
    return _parse_record(path, reference)


def load_relation_reviews(vault_root: Path) -> tuple[RelationReviewRecord, ...]:
    root = Path(vault_root)
    try:
        records: list[RelationReviewRecord] = []
        logical: set[str] = set()
        for path in _directory_children(root):
            if path.name.endswith((".tmp", ".bak")):
                continue
            if path.suffix.casefold() != ".json":
                continue
            identity = path.stem
            if path.name != f"{identity}.json" or normalize_id(identity) != identity:
                raise RelationReviewError(
                    "RELATION_REVIEW_ALIAS", "review directory contains a filename alias"
                )
            if identity.casefold() in logical:
                raise RelationReviewError(
                    "RELATION_REVIEW_ALIAS", "review directory contains a logical collision"
                )
            logical.add(identity.casefold())
            record, _ = _parse_record(path, path.relative_to(root).as_posix())
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
        record, _ = _load_one(Path(vault_root), page_state.identity)
        if record is None:
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
        _draft_hash(draft_id, destination, fingerprint),
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
    if artifact is not None:
        review = semantic_contract.RelationReviewState(
            artifact.kind,
            artifact.page_identity,
            artifact.content_fingerprint,
            reason=artifact.reason,
            reference=artifact.reference,
        )
    result = _evaluate(candidate, before, after, contracts, operation, review)
    validation = _validation(identity, destination, fingerprint, result, candidate, after)
    if exact_destination:
        expected_artifact = result.relation_disposition.kind in {"reviewed_none", "bootstrap"}
        artifact_matches = bool(
            artifact
            and artifact.draft_hash == validation.draft_hash
            and artifact.content_fingerprint == fingerprint
            and artifact.page_path_at_review == destination
        )
        exact_replay = (expected_artifact and artifact_matches) or (
            result.relation_disposition.kind == "qualifying_relation" and artifact is None
        )
        if exact_replay and commit_auxiliaries is not None:
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
                exact_replay = commit_disposition is None
            exact_replay = exact_replay and _all_auxiliaries_match(commit_auxiliaries)
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
            Path(vault_root),
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


def _normalize_auxiliary_path(root: Path, path: Path) -> tuple[Path, str]:
    raw = str(path)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.absolute().relative_to(root.absolute()).as_posix()
    except ValueError as error:
        raise RelationReviewError(
            "INVALID_AUXILIARY_WRITE", "auxiliary target is outside the vault"
        ) from error
    posix = PurePosixPath(relative)
    if not relative or "\0" in raw or any(part in {"", ".", ".."} for part in posix.parts):
        raise RelationReviewError("INVALID_AUXILIARY_WRITE", "auxiliary target is unsafe")
    return root / relative, relative


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
        if value.create_only or value.guard is not None:
            raise RelationReviewError(
                "INVALID_AUXILIARY_WRITE", "auxiliary write cannot carry coordinator controls"
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
        seen.add(alias)
        detached.append(vault.PlannedWrite(absolute, value.content))
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


def _all_auxiliaries_match(writes: tuple[vault.PlannedWrite, ...]) -> bool:
    for write in writes:
        try:
            if write.path.read_text(encoding="utf-8") != write.content:
                return False
        except (OSError, UnicodeDecodeError):
            return False
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
            1,
            "reviewed_none",
            validation.draft_id,
            validation.destination,
            validation.content_fingerprint,
            validation.draft_hash,
            auxiliary_digest,
            reason,
            artifact_reference,
        )
    elif result.relation_disposition.kind == "bootstrap":
        record = RelationReviewRecord(
            1,
            "bootstrap",
            validation.draft_id,
            validation.destination,
            validation.content_fingerprint,
            validation.draft_hash,
            auxiliary_digest,
            None,
            artifact_reference,
        )
    elif result.relation_disposition.kind == "qualifying_relation":
        record = None
    elif attempt.artifact is not None:
        raise RelationReviewError("DRAFT_ID_IN_USE", "draft identity is already reserved")
    elif result.should_block:
        raise RelationReviewError(
            "SEMANTIC_CONTRACT_BLOCKED", "semantic contract has blocking findings"
        )
    else:
        raise RelationReviewError("INVALID_RELATION_REVIEW", "reviewed-none approval is required")
    if attempt.artifact is not None and (
        record is None or attempt.artifact.storage_dict() != record.storage_dict()
    ):
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
) -> CreationDraftCommit:
    root = Path(vault_root)
    try:
        identity = _canonical_id(draft_id)
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
        )
        _commit_plan(
            preliminary,
            operation=operation,
            requested_review=requested_review,
            supplied_hash=relation_review_hash,
            reason=reason,
            auxiliary_digest=auxiliary_digest,
            artifact_reference=artifact_rel,
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
            )

            prepared = attempt.artifact
            resumed = False
            required_guards: tuple[vault.PathGuard, ...] = ()
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
            guarded_auxiliaries: list[vault.PlannedWrite] = []
            for auxiliary in auxiliaries:
                relative = auxiliary.path.relative_to(root).as_posix()
                policy = "stable" if os.path.lexists(auxiliary.path) else "absent"
                guarded_auxiliaries.append(
                    vault.PlannedWrite(
                        auxiliary.path,
                        auxiliary.content,
                        guard=vault.PathGuard.capture(root, relative, leaf_policy=policy),
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
            written_paths = tuple(item.relative_to(root).as_posix() for item in written)
            reference = record.reference if record is not None else None
            return CreationDraftCommit(
                identity,
                validation.draft_hash,
                validation.content_fingerprint,
                destination,
                True,
                result.relation_disposition.kind,
                reference,
                record is not None,
                resumed,
                written_paths,
                result,
            )
    except RelationReviewError:
        raise
    except Exception as error:
        raise _translate(error) from error
