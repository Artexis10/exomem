"""Private writer-neutral semantic creation preflight and commit routing."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import (
    activation_manifest,
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
_EXISTING_OPERATIONS = frozenset({"edit", "tier2_overwrite", "tier2_append"})
_FEEDBACK_FINDING_LIMIT = 32
_FEEDBACK_RELATION_FACT_LIMIT = 16
_FEEDBACK_ITEM_LIMIT = 32
_FEEDBACK_COUNT_LIMIT = 32


def _bounded_semantic_feedback(
    result: semantic_contract.SemanticContractResult,
) -> dict[str, Any]:
    """Project a full internal result into deterministic bounded writer feedback."""
    omitted: dict[str, int] = {}

    def findings(
        name: str, values: tuple[semantic_contract.ContractFinding, ...]
    ) -> list[dict[str, Any]]:
        if len(values) > _FEEDBACK_FINDING_LIMIT:
            omitted[name] = len(values) - _FEEDBACK_FINDING_LIMIT
        return [item.as_dict() for item in values[:_FEEDBACK_FINDING_LIMIT]]

    def items(name: str, values: tuple[str, ...]) -> list[str]:
        if len(values) > _FEEDBACK_ITEM_LIMIT:
            omitted[name] = len(values) - _FEEDBACK_ITEM_LIMIT
        return list(values[:_FEEDBACK_ITEM_LIMIT])

    kind_counts = result.kind_counts[:_FEEDBACK_COUNT_LIMIT]
    category_counts = result.category_counts[:_FEEDBACK_COUNT_LIMIT]
    if len(result.kind_counts) > _FEEDBACK_COUNT_LIMIT:
        omitted["kind_counts"] = len(result.kind_counts) - _FEEDBACK_COUNT_LIMIT
    if len(result.category_counts) > _FEEDBACK_COUNT_LIMIT:
        omitted["category_counts"] = (
            len(result.category_counts) - _FEEDBACK_COUNT_LIMIT
        )

    relation_value: dict[str, Any] | None = None
    disposition = result.relation_disposition
    if disposition is not None:
        relation_omitted: dict[str, int] = {}

        def relation_items(name: str, values: tuple[str, ...]) -> list[str]:
            if len(values) > _FEEDBACK_ITEM_LIMIT:
                relation_omitted[name] = len(values) - _FEEDBACK_ITEM_LIMIT
            return list(values[:_FEEDBACK_ITEM_LIMIT])

        if len(disposition.qualifying_facts) > _FEEDBACK_RELATION_FACT_LIMIT:
            relation_omitted["qualifying_facts"] = (
                len(disposition.qualifying_facts) - _FEEDBACK_RELATION_FACT_LIMIT
            )
        if len(disposition.rejected_facts) > _FEEDBACK_RELATION_FACT_LIMIT:
            relation_omitted["rejected_facts"] = (
                len(disposition.rejected_facts) - _FEEDBACK_RELATION_FACT_LIMIT
            )
        relation_value = {
            "kind": disposition.kind,
            "satisfied": disposition.satisfied,
            "current": disposition.current,
            "qualifying_directions": relation_items(
                "qualifying_directions", disposition.qualifying_directions
            ),
            "qualifying_facts": [
                fact.as_dict()
                for fact in disposition.qualifying_facts[
                    :_FEEDBACK_RELATION_FACT_LIMIT
                ]
            ],
            "rejected_facts": [
                item.as_dict()
                for item in disposition.rejected_facts[:_FEEDBACK_RELATION_FACT_LIMIT]
            ],
            "actions": relation_items("actions", disposition.actions),
            "omitted_counts": dict(sorted(relation_omitted.items())),
        }

    return {
        "mode": result.mode,
        "operation": result.operation,
        "findings": findings("findings", result.findings),
        "errors": findings("errors", result.errors),
        "warnings": findings("warnings", result.warnings),
        "blocking_findings": findings(
            "blocking_findings", result.blocking_findings
        ),
        "should_block": result.should_block,
        "semantic_unit_count": result.semantic_unit_count,
        "kind_counts": dict(kind_counts),
        "category_counts": dict(category_counts),
        "relation_disposition": relation_value,
        "actions": items("actions", result.actions),
        "omitted_counts": dict(sorted(omitted.items())),
    }


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
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if len(encoded.encode("utf-8")) > relation_review.MAX_DRAFT_TOKEN_ENCODED_BYTES:
            raise SemanticWriteError("DRAFT_TOKEN_TOO_LARGE", "draft token exceeds its bound")
        return encoded

    @classmethod
    def decode(cls, token: object) -> DraftToken:
        if (
            type(token) is not str
            or not token
            or len(token.encode("utf-8"))
            > relation_review.MAX_DRAFT_TOKEN_ENCODED_BYTES
        ):
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


@dataclass(frozen=True, slots=True)
class ExistingPreflight:
    """Detached before/after semantic decision for one existing Markdown write."""

    applicability: Literal["full", "structural", "not_semantic"]
    operation: Literal["edit", "tier2_overwrite", "tier2_append"]
    path: str
    before_source: str
    after_source: str
    before: semantic_contract.SemanticPageState
    after: semantic_contract.SemanticPageState
    grandfathered: bool
    transition_token: str
    transition_hash: str
    mutated: Literal[False]
    contract_result: semantic_contract.SemanticContractResult
    before_corpus: semantic_contract.SemanticCorpusContext
    after_corpus: semantic_contract.SemanticCorpusContext
    before_contracts: memory_schema.ResolvedMemoryContracts
    after_contracts: memory_schema.ResolvedMemoryContracts
    before_review: semantic_contract.RelationReviewState | None
    after_review: semantic_contract.RelationReviewState | None
    requested_decision: relation_review.LifecycleDecision | None
    activation_census: activation_manifest.ActivationCensus
    prospective_manifest: activation_manifest.ActivationManifest
    manifest_install_required: bool
    primary_guard: vault.PathGuard
    committed_replay: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "applicability": self.applicability,
            "operation": self.operation,
            "path": self.path,
            "grandfathered": self.grandfathered,
            "transition_token": self.transition_token,
            "transition_hash": self.transition_hash,
            "mutated": self.mutated,
            "contract_result": _bounded_semantic_feedback(self.contract_result),
        }


@dataclass(frozen=True, slots=True)
class ExistingCommit:
    applicability: Literal["full", "structural", "not_semantic"]
    operation: Literal["edit", "tier2_overwrite", "tier2_append"]
    path: str
    mutated: bool
    written_paths: tuple[str, ...]
    contract_result: semantic_contract.SemanticContractResult
    index_report: Any | None
    lifecycle_state: str | None
    transition_token: str

    def as_dict(self) -> dict[str, Any]:
        value = {
            "applicability": self.applicability,
            "operation": self.operation,
            "path": self.path,
            "mutated": self.mutated,
            "written_paths": list(self.written_paths),
            "contract_result": _bounded_semantic_feedback(self.contract_result),
            "lifecycle_state": self.lifecycle_state,
            "transition_token": self.transition_token,
        }
        if self.index_report is not None:
            value["index"] = self.index_report.as_dict()
        return value


def _existing_transition_token(
    *, operation: str, path: str, before_hash: str, after_hash: str
) -> str:
    payload = {
        "version": 1,
        "transition_id": str(uuid.uuid4()),
        "operation": operation,
        "path": path,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_existing_transition_token(token: str) -> dict[str, Any]:
    if (
        type(token) is not str
        or not token
        or len(token.encode("utf-8"))
        > relation_review.MAX_DRAFT_TOKEN_ENCODED_BYTES
    ):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError, UnicodeError, binascii.Error) as error:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        ) from error
    if type(value) is not dict or set(value) != {
        "version",
        "transition_id",
        "operation",
        "path",
        "before_hash",
        "after_hash",
    }:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    if value["version"] != 1 or any(
        type(value[key]) is not str
        for key in ("transition_id", "operation", "path", "before_hash", "after_hash")
    ):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    try:
        transition_id = str(uuid.UUID(value["transition_id"]))
    except (AttributeError, TypeError, ValueError) as error:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        ) from error
    if transition_id != value["transition_id"]:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    return value


def _existing_transition_id(token: str) -> str:
    return str(_decode_existing_transition_token(token)["transition_id"])


def _existing_applicability(
    before: semantic_contract.SemanticPageState,
    after: semantic_contract.SemanticPageState,
) -> Literal["full", "structural", "not_semantic"]:
    if after.eligible_compiled:
        return "full"
    if (
        before.page_type in _COMPILED_TYPES
        or after.page_type in _COMPILED_TYPES
        or before.page_type == "entity"
        or after.page_type == "entity"
    ):
        return "structural"
    # This coordinator is entered only for governed Markdown. Untyped/arbitrary
    # Markdown still receives the structural/safety contract; non-Markdown
    # writers preserve their legacy path and never enter this seam.
    return "structural"


def preflight_existing(
    vault_root: Path,
    *,
    path: str,
    after_source: str,
    operation: Literal["edit", "tier2_overwrite", "tier2_append"],
    expected_before_hash: str | None = None,
    transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> ExistingPreflight:
    """Evaluate an existing-page transition without mutating any shared state."""
    if operation not in _EXISTING_OPERATIONS:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_OPERATION",
            "existing semantic write operation is unsupported",
        )
    if relation_disposition not in {None, "reviewed_none"}:
        raise SemanticWriteError(
            "INVALID_RELATION_REVIEW", "relation disposition is invalid"
        )
    root = Path(vault_root)
    before_source, primary_guard = vault.read_guarded_text(root, root / path)
    before_hash = vault.content_hash(before_source)
    if expected_before_hash is not None and expected_before_hash != before_hash:
        raise SemanticWriteError(
            "STALE_SEMANTIC_WRITE", "page changed before semantic preflight"
        )

    registry = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    loaded_contracts = memory_schema.load_saved_contracts(root)
    before_corpus = semantic_contract.build_corpus_context(
        root, registry=registry, language_registry=language
    )
    before = semantic_contract.build_page_state(
        root,
        path,
        before_source,
        relation_registry=registry,
        language_registry=language,
    )
    after = semantic_contract.build_page_state(
        root,
        path,
        after_source,
        relation_registry=registry,
        language_registry=language,
    )
    after_corpus = before_corpus.with_candidate(after)
    before_contracts = memory_schema.resolve_contracts(
        loaded_contracts,
        projects=before.projects,
        page_type=before.page_type,
        language_registry=language,
    )
    after_contracts = memory_schema.resolve_contracts(
        loaded_contracts,
        projects=after.projects,
        page_type=after.page_type,
        language_registry=language,
    )
    applicability = _existing_applicability(before, after)
    before_review: semantic_contract.RelationReviewState | None = None
    after_review: semantic_contract.RelationReviewState | None = None
    if applicability == "full":
        before_review = relation_review.load_relation_review(
            root, before, corpus=before_corpus
        )
        after_review = relation_review.load_relation_review(
            root, after, corpus=after_corpus
        )

    token = transition_token or _existing_transition_token(
        operation=operation,
        path=path,
        before_hash=before.source_hash,
        after_hash=after.source_hash,
    )
    token_value = _decode_existing_transition_token(token)
    committed_replay = bool(
        transition_token is not None
        and token_value["operation"] == operation
        and token_value["path"] == path
        and token_value["after_hash"] == before.source_hash
        and token_value["after_hash"] == after.source_hash
    )
    if (
        token_value["operation"] != operation
        or token_value["path"] != path
        or token_value["after_hash"] != after.source_hash
        or (
            token_value["before_hash"] != before.source_hash
            and not committed_replay
        )
    ):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "transition token does not match the exact before and after state",
        )
    transition_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    requested_decision: relation_review.LifecycleDecision | None = None
    if relation_disposition == "reviewed_none":
        if applicability != "full":
            raise SemanticWriteError(
                "INVALID_RELATION_REVIEW",
                "reviewed-none applies only to an active compiled result",
            )
        if relation_review_hash != transition_hash:
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_REVIEW_MISMATCH",
                "review hash does not match the validated transition",
            )
        if (
            after.identity_kind != "exomem_id"
            or after.review_fingerprint is None
            or after_corpus.identity_census.paths_by_identity.get(after.identity)
            != (after.path,)
        ):
            raise SemanticWriteError(
                "RELATION_REVIEW_STABLE_ID_REQUIRED",
                "lifecycle reviewed-none requires one unique stable page identity",
            )
        try:
            requested_decision = relation_review.build_lifecycle_decision(
                page_identity=after.identity,
                after_fingerprint=after.review_fingerprint,
                reason=relation_review_reason or "",
            )
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        after_review = semantic_contract.RelationReviewState(
            "reviewed_none",
            requested_decision.page_identity,
            requested_decision.after_fingerprint,
            reason=requested_decision.reason,
            reference=requested_decision.reference,
        )

    manifest = activation_manifest.load_manifest(root)
    boundary = activation_manifest.plan_activation_boundary(
        before_corpus.activation_census, manifest=manifest
    )
    grandfathered = activation_manifest.is_grandfathered(
        root,
        path,
        source_hash=before.source_hash,
        exomem_id=before.identity if before.identity_kind == "exomem_id" else None,
        manifest=boundary.manifest,
        census=before_corpus.activation_census,
    )
    result = semantic_contract.evaluate(
        before=before,
        after=after,
        operation=operation,
        mode="precommit",
        before_contracts=before_contracts,
        after_contracts=after_contracts,
        before_corpus=before_corpus,
        after_corpus=after_corpus,
        before_review=before_review,
        after_review=after_review,
        grandfathered=grandfathered and before.eligible_compiled,
        include_relation_disposition=applicability == "full",
    )
    return ExistingPreflight(
        applicability,
        operation,
        path,
        before_source,
        after_source,
        before,
        after,
        grandfathered,
        token,
        transition_hash,
        False,
        result,
        before_corpus,
        after_corpus,
        before_contracts,
        after_contracts,
        before_review,
        after_review,
        requested_decision,
        before_corpus.activation_census,
        boundary.manifest,
        boundary.install_required,
        primary_guard,
        committed_replay,
    )


def _reevaluate_existing(
    preflight: ExistingPreflight,
    *,
    manifest: activation_manifest.ActivationManifest,
) -> tuple[semantic_contract.SemanticContractResult, bool]:
    grandfathered = activation_manifest.is_grandfathered(
        preflight.before_corpus.vault_root,
        preflight.path,
        source_hash=preflight.before.source_hash,
        exomem_id=(
            preflight.before.identity
            if preflight.before.identity_kind == "exomem_id"
            else None
        ),
        manifest=manifest,
        census=preflight.activation_census,
    )
    result = semantic_contract.evaluate(
        before=preflight.before,
        after=preflight.after,
        operation=preflight.operation,
        mode="precommit",
        before_contracts=preflight.before_contracts,
        after_contracts=preflight.after_contracts,
        before_corpus=preflight.before_corpus,
        after_corpus=preflight.after_corpus,
        before_review=preflight.before_review,
        after_review=preflight.after_review,
        grandfathered=grandfathered and preflight.before.eligible_compiled,
        include_relation_disposition=preflight.applicability == "full",
    )
    return result, grandfathered


def _commit_existing_locked(
    root: Path,
    *,
    preflight: ExistingPreflight,
    auxiliaries: tuple[vault.PlannedWrite, ...],
    result: semantic_contract.SemanticContractResult,
) -> ExistingCommit:
    """Plan lifecycle state and commit while the semantic namespace is held."""
    if preflight.committed_replay:
        if preflight.applicability != "full":
            return ExistingCommit(
                preflight.applicability,
                preflight.operation,
                preflight.path,
                False,
                (),
                result,
                None,
                "committed_replay",
                preflight.transition_token,
            )
        if (
            preflight.after.identity_kind != "exomem_id"
            or preflight.after.review_fingerprint is None
        ):
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "committed replay requires the exact stable resulting identity",
            )
        try:
            prepared = relation_review.load_lifecycle_prepared(
                root, preflight.after.identity
            )
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        token_value = _decode_existing_transition_token(preflight.transition_token)
        requested_reference = (
            preflight.requested_decision.reference
            if preflight.requested_decision is not None
            else None
        )
        exact = bool(
            prepared is not None
            and prepared.transition_id == token_value["transition_id"]
            and prepared.operation == preflight.operation
            and prepared.page_identity == preflight.after.identity
            and prepared.before_path == preflight.path
            and prepared.before_source_hash == token_value["before_hash"]
            and prepared.after_path == preflight.path
            and prepared.after_source_hash == preflight.after.source_hash
            and prepared.after_fingerprint == preflight.after.review_fingerprint
            and prepared.decision_reference == requested_reference
            and prepared.transition_token_hash
            == hashlib.sha256(preflight.transition_token.encode("utf-8")).hexdigest()
            and prepared.auxiliary_hash
            == relation_review.lifecycle_auxiliary_hash(auxiliaries, root)
        )
        if not exact:
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "committed replay does not match the prepared transition",
            )
        return ExistingCommit(
            preflight.applicability,
            preflight.operation,
            preflight.path,
            False,
            (),
            result,
            None,
            "committed_replay",
            preflight.transition_token,
        )

    lifecycle_writes: tuple[vault.PlannedWrite, ...] = ()
    required_guards: tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...] = ()
    lifecycle_state: str | None = None
    stable_active = (
        preflight.applicability == "full"
        and preflight.after.identity_kind == "exomem_id"
        and preflight.after.review_fingerprint is not None
        and preflight.after_corpus.identity_census.paths_by_identity.get(
            preflight.after.identity
        )
        == (preflight.path,)
    )
    if stable_active:
        try:
            prepared = relation_review.build_lifecycle_prepared_transition(
                transition_id=_existing_transition_id(preflight.transition_token),
                operation=preflight.operation,
                page_identity=preflight.after.identity,
                before_path=preflight.before.path,
                before_source_hash=preflight.before.source_hash,
                after_path=preflight.after.path,
                after_source_hash=preflight.after.source_hash,
                after_fingerprint=preflight.after.review_fingerprint,
                decision=preflight.requested_decision,
                transition_token=preflight.transition_token,
                auxiliary_hash=relation_review.lifecycle_auxiliary_hash(
                    auxiliaries, root
                ),
            )
            current = relation_review.LifecyclePrimaryBinding(
                preflight.before.path,
                preflight.before.source_hash,
                preflight.before.review_fingerprint,
            )
            lifecycle = relation_review.plan_lifecycle_transition(
                root,
                decision=preflight.requested_decision,
                prepared=prepared,
                current=current,
            )
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        lifecycle_state = lifecycle.state
        if lifecycle.state == "committed_replay":
            return ExistingCommit(
                preflight.applicability,
                preflight.operation,
                preflight.path,
                False,
                (),
                result,
                None,
                lifecycle.state,
                preflight.transition_token,
            )
        lifecycle_writes = lifecycle.writes
        required_guards = lifecycle.required_guards

    writes = [*lifecycle_writes, *auxiliaries]
    writes.append(
        vault.PlannedWrite(
            root / preflight.path,
            preflight.after_source,
            guard=preflight.primary_guard,
        )
    )
    reports: list[Any] = []
    written = vault.batch_atomic_write(
        writes,
        vault_root=root,
        required_guards=required_guards,
        index_reports=reports,
    )
    report = reports[0] if reports else None
    return ExistingCommit(
        preflight.applicability,
        preflight.operation,
        preflight.path,
        True,
        tuple(path.relative_to(root).as_posix() for path in written),
        result,
        report,
        lifecycle_state,
        preflight.transition_token,
    )


def commit_existing(
    vault_root: Path,
    *,
    preflight: ExistingPreflight,
    auxiliary_writes: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite] = (),
) -> ExistingCommit:
    """Commit one preflighted existing-page transition, primary Markdown last."""
    root = Path(vault_root)
    if preflight.contract_result.should_block:
        raise SemanticWriteError(
            "SEMANTIC_CONTRACT_BLOCKED", "semantic contract has blocking findings"
        )

    result = preflight.contract_result
    auxiliaries = tuple(auxiliary_writes)
    if preflight.manifest_install_required:
        winner = activation_manifest.ensure_manifest(
            root, census=preflight.activation_census
        )
        result, _ = _reevaluate_existing(preflight, manifest=winner)
        if result.should_block:
            raise SemanticWriteError(
                "SEMANTIC_CONTRACT_BLOCKED",
                "semantic contract blocked against the activation boundary winner",
            )

    try:
        with vault.vault_creation_lock(root, "semantic-creation"):
            return _commit_existing_locked(
                root,
                preflight=preflight,
                auxiliaries=auxiliaries,
                result=result,
            )
    except vault.VaultLockTimeout as error:
        raise SemanticWriteError(
            "SEMANTIC_CREATION_LOCK_TIMEOUT",
            "timed out acquiring semantic creation lock",
        ) from error
    except vault.VaultLockError as error:
        raise SemanticWriteError(error.code, error.reason) from error


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
    relation_disposition: str | None = None,
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> CreationPreflight:
    root = Path(vault_root)
    if relation_disposition not in {None, "reviewed_none"}:
        raise SemanticWriteError(
            "INVALID_RELATION_REVIEW", "relation disposition is invalid"
        )
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
            requested_disposition=relation_disposition,
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
