"""Private writer-neutral semantic creation preflight and commit routing."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import (
    memory_schema,
    relation_registry,
    relation_review,
    semantic_contract,
    semantic_language_registry,
    vault,
)

_TOKEN_VERSION = 1
_MAX_TOKEN_BYTES = 12 * 1024
_COMPILED_TYPES = frozenset(
    {
        "research-note",
        "insight",
        "failure",
        "pattern",
        "experiment",
        "production-log",
    }
)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class SemanticWriteError(ValueError):
    code: str
    reason: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")


@dataclass(frozen=True, slots=True)
class DraftRegistration:
    key: str
    category: str
    folder: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "category": self.category, "folder": self.folder}


@dataclass(frozen=True, slots=True)
class DraftToken:
    writer: str
    operation: str
    destination: str
    render_date: str
    registrations: tuple[DraftRegistration, ...] = ()

    def encode(self) -> str:
        value = {
            "version": _TOKEN_VERSION,
            "writer": self.writer,
            "operation": self.operation,
            "destination": self.destination,
            "render_date": self.render_date,
            "registrations": [item.as_dict() for item in self.registrations],
        }
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(raw) > _MAX_TOKEN_BYTES:
            raise SemanticWriteError("DRAFT_TOKEN_TOO_LARGE", "draft token exceeds its bound")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: object) -> DraftToken:
        if type(token) is not str or not token or len(token) > _MAX_TOKEN_BYTES * 2:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token is invalid")
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            if len(raw) > _MAX_TOKEN_BYTES:
                raise ValueError
            value = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (
            binascii.Error,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token is invalid") from error
        if type(value) is not dict or set(value) != {
            "version",
            "writer",
            "operation",
            "destination",
            "render_date",
            "registrations",
        }:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid fields")
        if value["version"] != _TOKEN_VERSION or any(
            type(value[key]) is not str
            for key in ("writer", "operation", "destination", "render_date")
        ):
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid fields")
        try:
            render_date = dt.date.fromisoformat(value["render_date"])
        except ValueError as error:
            raise SemanticWriteError(
                "INVALID_DRAFT_TOKEN", "draft token has invalid render date"
            ) from error
        if (
            render_date.isoformat() != value["render_date"]
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value["writer"])
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value["operation"])
            or len(value["destination"]) > 1024
        ):
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid fields")
        registrations_raw = value["registrations"]
        if type(registrations_raw) is not list or len(registrations_raw) > 64:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid registrations")
        registrations: list[DraftRegistration] = []
        for item in registrations_raw:
            if type(item) is not dict or set(item) != {"key", "category", "folder"} or any(
                type(item[key]) is not str for key in ("key", "category", "folder")
            ):
                raise SemanticWriteError(
                    "INVALID_DRAFT_TOKEN", "draft token has invalid registrations"
                )
            registrations.append(DraftRegistration(item["key"], item["category"], item["folder"]))
        decoded = cls(
            value["writer"],
            value["operation"],
            value["destination"],
            value["render_date"],
            tuple(registrations),
        )
        if decoded.encode() != token:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token is not canonical")
        return decoded


@dataclass(frozen=True, slots=True)
class CreationPreflight:
    applicability: Literal["full", "structural", "not_semantic"]
    destination: str
    source: str
    draft_id: str | None
    draft_token: str
    mutated: Literal[False]
    contract_result: semantic_contract.SemanticContractResult | None
    creation_validation: relation_review.CreationDraftValidation | None

    @property
    def draft_hash(self) -> str | None:
        return (
            self.creation_validation.draft_hash
            if self.creation_validation is not None
            else None
        )

    @property
    def relation_candidates(self) -> tuple[relation_review.RelationCandidate, ...]:
        return (
            self.creation_validation.relation_candidates
            if self.creation_validation is not None
            else ()
        )

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "applicability": self.applicability,
            "destination": self.destination,
            "draft_id": self.draft_id,
            "draft_hash": self.draft_hash,
            "draft_token": self.draft_token,
            "mutated": self.mutated,
            "contract_result": (
                self.contract_result.as_dict() if self.contract_result is not None else None
            ),
        }
        if self.creation_validation is not None:
            value.update(self.creation_validation.as_dict())
            value["draft_token"] = self.draft_token
            value["applicability"] = self.applicability
        return value


@dataclass(frozen=True, slots=True)
class CreationCommit:
    applicability: Literal["full", "structural", "not_semantic"]
    mutated: Literal[True]
    written_paths: tuple[str, ...]
    contract_result: semantic_contract.SemanticContractResult | None
    creation_commit: relation_review.CreationDraftCommit | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "applicability": self.applicability,
            "mutated": self.mutated,
            "written_paths": list(self.written_paths),
            "contract_result": (
                self.contract_result.as_dict() if self.contract_result is not None else None
            ),
            "creation": (
                self.creation_commit.as_dict() if self.creation_commit is not None else None
            ),
        }


def _evaluate_structural(
    root: Path,
    *,
    destination: str,
    source: str,
    operation: str,
) -> semantic_contract.SemanticContractResult:
    registry = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    contracts = memory_schema.load_saved_contracts(root)
    before = semantic_contract.build_corpus_context(
        root, registry=registry, language_registry=language
    )
    candidate = semantic_contract.build_page_state(
        root,
        destination,
        source,
        relation_registry=registry,
        language_registry=language,
    )
    resolved = memory_schema.resolve_contracts(
        contracts,
        projects=candidate.projects,
        page_type=candidate.page_type,
        language_registry=language,
    )
    return semantic_contract.evaluate(
        before=None,
        after=candidate,
        operation=operation,
        mode="precommit",
        before_contracts=resolved,
        after_contracts=resolved,
        before_corpus=before,
        after_corpus=before.with_candidate(candidate),
    )


def preflight_creation(
    vault_root: Path,
    *,
    path: str,
    source: str,
    operation: str,
    writer: str,
    draft_id: str | None,
    draft_token: str,
    registrations: tuple[DraftRegistration, ...] = (),
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> CreationPreflight:
    root = Path(vault_root)
    token = DraftToken.decode(draft_token)
    if (
        token.writer != writer
        or token.operation != operation
        or token.destination != path
        or token.registrations != registrations
    ):
        raise SemanticWriteError(
            "INVALID_DRAFT_TOKEN", "draft token does not match this creation"
        )
    try:
        frontmatter, _, _ = vault.parse_frontmatter(source, strict=True)
    except vault.FrontmatterError as error:
        raise SemanticWriteError(error.code, "draft frontmatter is invalid") from error
    page_type = frontmatter.get("type")
    result = _evaluate_structural(root, destination=path, source=source, operation=operation)
    if result.should_block and not (
        page_type in _COMPILED_TYPES
        and result.relation_disposition.kind in {"missing", "stale"}
        and all(
            item.resolved_rule == ("relations", "*", "disposition")
            for item in result.blocking_findings
        )
    ):
        raise SemanticWriteError(
            "SEMANTIC_CONTRACT_BLOCKED", "semantic contract has blocking findings"
        )
    state = semantic_contract.build_page_state(root, path, source)
    if page_type in _COMPILED_TYPES and state.eligible_compiled:
        if draft_id is None:
            raise SemanticWriteError("DRAFT_IDENTITY_MISMATCH", "active draft requires identity")
        validation = relation_review.revalidate_prepared_creation_draft(
            root,
            path=path,
            source=source,
            draft_id=draft_id,
            operation=operation,
            draft_token=draft_token,
            predecessor_path=predecessor_path,
            predecessor_content_hash=predecessor_content_hash,
        )
        return CreationPreflight(
            "full", path, source, draft_id, draft_token, False,
            validation.contract_result, validation,
        )
    applicability: Literal["structural", "not_semantic"] = (
        "structural" if page_type is not None else "not_semantic"
    )
    return CreationPreflight(
        applicability, path, source, draft_id, draft_token, False, result, None
    )


def commit_creation(
    vault_root: Path,
    *,
    preflight: CreationPreflight,
    auxiliary_writes: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite] = (),
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
    operation: str,
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> CreationCommit:
    root = Path(vault_root)
    if preflight.applicability == "full":
        assert preflight.draft_id is not None
        committed = relation_review.commit_creation_draft(
            root,
            path=preflight.destination,
            source=preflight.source,
            draft_id=preflight.draft_id,
            operation=operation,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
            auxiliary_writes=auxiliary_writes,
            draft_token=preflight.draft_token,
            predecessor_path=predecessor_path,
            predecessor_content_hash=predecessor_content_hash,
        )
        return CreationCommit(
            "full", True, committed.written_paths, committed.contract_result, committed
        )
    writes = [*auxiliary_writes]
    writes.append(
        vault.PlannedWrite(
            root / preflight.destination,
            preflight.source,
            create_only=True,
            guard=vault.PathGuard.capture(
                root, preflight.destination, leaf_policy="absent"
            ),
        )
    )
    written = vault.batch_atomic_write(writes, vault_root=root)
    paths = tuple(path.relative_to(root).as_posix() for path in written)
    return CreationCommit(
        preflight.applicability,
        True,
        paths,
        preflight.contract_result,
        None,
    )
