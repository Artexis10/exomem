"""Deterministic governance-authoring router.

The language model chooses an operation and supplies user intent.  This module
owns every enforcement fact: operation coverage, caller/session authority,
proposal and token bounds, current membership, durable receipts, and recovery.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .. import (
    deferred_index,
    epistemic_graph,
    find_corpus,
    graph_sync,
    held_fs,
    index_paths,
    lexstore,
    media_jobs,
    memory_refs,
    reserved_paths,
    review_state,
    semantic_contract,
    vault,
)
from ..kbdir import kb_dirname
from . import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    catalog_publication,
    companion_backfill,
    decisions,
    graph_producer,
    membership,
    projection_store,
    projections,
    receipts,
    schema_v4,
    store,
)
from . import policy as policy_module
from . import tokens as tokens_module
from .operations import (
    HANDLER_STRATEGY_KEYS,
    OPERATION_SPECS,
    OperationSpec,
    OperationVariant,
    assert_operation_coverage,
    operation_variant,
    select_operation,
)
from .principal import (
    OWNER_AUDIENCE,
    UNNAMED_AUDIENCE_PROBE,
    RequestPrincipal,
    effective_principal,
)
from .transaction import (
    GovernanceCrash,
    GovernanceError,
    authorization_row,
    policy_target,
)
from .transaction import fsync_directory as _fsync_directory

PENDING_MARKER = ".policy-mutation.pending.json"
DEFAULT_PROPOSAL_TTL_SECONDS = 900
_SESSION_ACTIONS = frozenset({"open", "status", "rotate", "close"})
_SESSION_ARGUMENTS = frozenset(
    {"authorization_session", "now", "principal", "session_action", "ttl_seconds"}
)
_GRAPH_REBUILD_SIDECAR_RE = re.compile(
    rf"^{re.escape(graph_sync._TEMP_PREFIX)}[0-9a-f]{{64}}-[0-9a-f]{{24}}\.sqlite"
    r"(?:-(?:wal|shm|journal))?$"
)
_REVIEW_STATE_TEMP_RE = re.compile(r"^\.\.review-state\.json\.[a-z0-9_]{8}\.tmp$")
_LEXICAL_REBUILD_TEMP_RE = re.compile(
    r"^\.lexical\.sqlite\.rebuild-[0-9a-f]{32}\.tmp(?:-(?:wal|shm|journal))?$"
)
_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_V4_POLICY_PROPOSAL_SCHEMA_V3 = "exomem.governance-policy-proposal/v3"
_V4_POLICY_PROPOSAL_SCHEMA = "exomem.governance-policy-proposal/v4"
_V4_POLICY_PUBLICATION_RECEIPT_SCHEMA = (
    "exomem.governance-policy-publication-receipt/v1"
)
_V4_POLICY_PUBLICATION_RECEIPT_OPERATION = "governance_policy_publication"
_V4_POLICY_MIRROR_SCHEMA = "exomem.governance-policy-workspace-mirror/v1"
_V4_POLICY_MIRROR_OPERATION = "governance_policy_workspace_mirror"
_V4_POLICY_MIRROR_OUTCOMES = frozenset({"complete", "diverged"})
_V4_POLICY_RECOVERY_EXPIRES_AT = 253_402_300_799.0


@dataclass(frozen=True, slots=True)
class _DecodedV4PolicyProposal:
    payload: dict[str, Any]
    expected: schema_v4.VerifiedActiveGovernanceState
    policy: schema_v4.PolicyGenerationSeed
    catalog: schema_v4.CatalogGenerationSeed | None
    namespace: schema_v4.ProjectionNamespaceSeed
    direction: str
    authoring_snapshot: policy_module.AuthoringSnapshot
    authoring_snapshot_value: dict[str, Any]
    dependent_grants: tuple[schema_v4.DependentGrantTransition, ...] | None


@dataclass(frozen=True, slots=True)
class _StagedTargetProjection:
    catalog: schema_v4.CatalogGenerationSeed | None
    namespace: dict[str, Any]
    predecessor_items: tuple[projection_store.ProjectionItemVariants, ...]
    items: tuple[projection_store.ProjectionItemVariants, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedV4PolicyProposal:
    decoded: _DecodedV4PolicyProposal
    custody: authorization_custody.AuthorizationCustody

__all__ = [
    "GovernanceCrash",
    "GovernanceError",
    "OPERATION_SPECS",
    "OperationSpec",
    "assert_operation_coverage",
    "classify_transition_direction",
    "op_govern_memory",
    "reconcile_governance_operations",
]


def _principal(value: RequestPrincipal | None) -> RequestPrincipal:
    return value if value is not None else effective_principal()


def _require_owner(value: RequestPrincipal | None) -> RequestPrincipal:
    who = _principal(value)
    if not who.resolved or who.audience_id != OWNER_AUDIENCE:
        raise GovernanceError("GOVERNANCE_OWNER_REQUIRED", "operation is owner-only")
    return who


def _prospective_policy(
    vault_root: Path, documents: Mapping[str, str | None]
) -> policy_module.Policy:
    prospective = policy_module.compile_prospective(vault_root, dict(documents))
    if prospective is None:
        raise GovernanceError(
            "GOVERNANCE_AUTHORING_UNSTABLE",
            "the policy workspace changed or could not be acquired safely",
        )
    return prospective.policy


def _v4_active_authority_snapshot(
    vault_root: Path,
    *,
    now: int,
) -> tuple[
    authorization_custody.AuthorizationCustody,
    schema_v4.ActivePolicySnapshot,
]:
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(
            vault_root,
            now=now,
        )
        control = custody.control
        if (
            not control.governance_enrolled
            or control.activation_store_id is None
            or control.activation_epoch is None
            or control.activation_state_digest is None
        ):
            raise schema_v4.SchemaV4Error("external activation authority is incomplete")
        connection = store.open_authorization_session_connection(vault_root)
        connection.execute("BEGIN")
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=control.activation_store_id,
            expected_activation_epoch=control.activation_epoch,
            expected_activation_state_digest=control.activation_state_digest,
        )
        connection.commit()
        return custody, snapshot
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ):
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the active governance tuple cannot be verified",
        ) from None
    finally:
        if connection is not None:
            connection.close()


def _v4_active_policy_snapshot(
    vault_root: Path,
    *,
    now: int,
) -> schema_v4.ActivePolicySnapshot:
    return _v4_active_authority_snapshot(vault_root, now=now)[1]


def _stable_identity_value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "device": int(value.device),
        "inode": int(value.inode),
        "kind": str(value.kind),
        # A directory link count changes when excluded operational state (for
        # example the receipt events directory) is created beside the policy
        # workspace. Authoring identity deliberately binds the held directory
        # object, not that mutable count; files remain single-link exact.
        "link_count": int(value.link_count) if value.kind == "file" else 1,
    }


def _snapshot_value(snapshot: policy_module.AuthoringSnapshot) -> dict[str, Any]:
    return {
        "documents": [
            {
                "path": relative,
                "bytes": base64.b64encode(content).decode("ascii"),
            }
            for relative, content in snapshot.documents
        ],
        "source_fingerprint": snapshot.source_fingerprint,
        "conflict_set_digest": snapshot.conflict_set_digest,
        "guard_generation": snapshot.guard_generation,
        "file_identities": [
            {
                "path": item.path,
                "identity": _stable_identity_value(item.identity),
                "sha256": item.sha256,
            }
            for item in snapshot.file_identities
        ],
        "directory_identities": [
            {
                "path": relative,
                "identity": _stable_identity_value(identity),
            }
            for relative, identity in snapshot.directory_identities
        ],
        "governance_root_identity": _stable_identity_value(
            snapshot.governance_root_identity
        ),
    }


def _active_tuple_value(
    active: schema_v4.VerifiedActiveGovernanceState,
) -> dict[str, Any]:
    return {
        "logical_vault_id": active.logical_vault_id,
        "activation_store_id": active.activation_store_id,
        "activation_epoch": active.activation_epoch,
        "activation_state_digest": active.activation_state_digest,
        "policy_generation_id": active.policy_generation_id,
        "policy_fingerprint": active.policy_fingerprint,
        "projector_schema_version": active.projector_schema_version,
        "catalog_generation": active.catalog_generation,
        "projection_namespace_id": active.projection_namespace_id,
    }


def _policy_publication_identities(
    *,
    proposal_id: str,
    created_at: float,
    review_digest: str,
) -> tuple[str, str, str]:
    """Derive stable reviewed generation/event identities from one proposal."""

    if not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "proposal identity is malformed",
        )
    if _SHA256_RE.fullmatch(review_digest) is None:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "reviewed proposal digest is malformed",
        )
    milliseconds = int(created_at * 1000)
    if not 0 <= milliseconds < (1 << 48):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "proposal creation time is outside the ULID range",
        )
    identity = {
        "proposal_id": proposal_id,
        "created_at_ms": milliseconds,
        "review_digest": review_digest,
    }
    entropy = hashlib.sha256(
        b"exomem.governance-policy-generation.v1\0"
        + _canonical_json(identity).encode("utf-8")
    ).digest()[:10]
    value = (milliseconds << 80) | int.from_bytes(entropy, "big")
    generation_id = "".join(
        _CROCKFORD32[(value >> shift) & 31] for shift in range(125, -1, -5)
    )
    authoring_event_id = receipts.critical_event_id(
        {
            "operation": "governance_policy_authoring_review",
            "generation_id": generation_id,
            **identity,
        }
    )
    receipt_event_id = receipts.critical_event_id(
        {
            "operation": "governance_policy_publication",
            "authoring_event_id": authoring_event_id,
            "generation_id": generation_id,
            **identity,
        }
    )
    return generation_id, authoring_event_id, receipt_event_id


def _full_search_fields(
    item: projection_store.ProjectionItemVariants,
) -> Mapping[str, str]:
    candidates: list[Mapping[str, str]] = []
    for variant in item.variants:
        if variant.decision_level != policy_module.DISCLOSURE_MAX:
            continue
        try:
            value = json.loads(variant.value_jcs)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if value.get("release_strip") == []:
            candidates.append(variant.search_fields)
    if not candidates:
        raise GovernanceError(
            "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
            "the active catalog lacks an unstripped full projection",
        )
    canonical = {_canonical_json(dict(candidate)) for candidate in candidates}
    if len(canonical) != 1:
        raise GovernanceError(
            "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
            "the active catalog has ambiguous full projections",
        )
    return dict(candidates[0])


def _stage_target_projection_namespace(
    vault_root: Path,
    *,
    active_snapshot: schema_v4.ActivePolicySnapshot,
    target_policy: policy_module.Policy,
    ready_at: int,
) -> _StagedTargetProjection:
    try:
        active_evidence = projection_store.namespace_evidence_from_snapshot(
            active_snapshot
        )
        active_manifest, active_items = projection_store.load_projection_catalog(
            vault_root,
            key=active_evidence.manifest.namespace_key,
            expected_rows_digest=active_evidence.manifest.rows_digest,
        )
        projection_store.bind_active_projection_namespace(
            active_snapshot,
            manifest=active_manifest,
            items=active_items,
        )
        if active_evidence.required_measurement_roots:
            raise GovernanceError(
                "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
                "the active model/graph lanes require a prepared measurement rebuild",
            )
        target_memberships: list[tuple[str, ...]] = []
        for item in active_items:
            snapshot = reserved_paths.read_generic_bytes(
                vault_root,
                item.item_identity,
            )
            if not __import__("hmac").compare_digest(
                hashlib.sha256(snapshot.data).hexdigest(),
                item.content_hash,
            ):
                raise GovernanceError(
                    "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
                    "the live corpus no longer matches the active catalog",
                )
            if Path(item.item_identity).suffix.casefold() == ".md":
                page = find_corpus.parse_page(
                    vault_root / item.item_identity,
                    snapshot.mtime,
                    vault_root,
                    content=snapshot.data,
                    resolved_relative=item.item_identity,
                )
                if page is None:
                    raise GovernanceError(
                        "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
                        "the active Markdown catalog item cannot be classified",
                    )
                scope_ids = membership.evaluate(page, target_policy)
            else:
                scope_ids = membership.evaluate_path_only(
                    vault_root,
                    item.item_identity,
                    target_policy,
                ).require_classified()
            target_memberships.append(tuple(sorted(scope_ids)))

        membership_changed = any(
            target_scope_ids != item.scope_ids
            for item, target_scope_ids in zip(
                active_items,
                target_memberships,
                strict=True,
            )
        )
        target_catalog_generation = active_snapshot.active.catalog_generation + int(
            membership_changed
        )
        key = projections.ProjectionNamespaceKey(
            policy_fingerprint=target_policy.fingerprint,
            projector_schema_version=active_snapshot.active.projector_schema_version,
            catalog_generation=target_catalog_generation,
        )
        target_items = tuple(
            projection_store.ProjectionItemVariants(
                item_identity=item.item_identity,
                content_hash=item.content_hash,
                scope_ids=target_scope_ids,
                variants=projections.enumerate_projection_variants(
                    item_identity=item.item_identity,
                    content_hash=item.content_hash,
                    scope_ids=target_scope_ids,
                    policy=target_policy,
                    projector_schema_version=key.projector_schema_version,
                    full_search_fields=_full_search_fields(item),
                ),
            )
            for item, target_scope_ids in zip(
                active_items,
                target_memberships,
                strict=True,
            )
        )
        target_catalog_descriptor = projection_store.catalog_descriptor_bytes(
            key,
            target_items,
        )
        if not membership_changed and not __import__("hmac").compare_digest(
            target_catalog_descriptor,
            active_snapshot.catalog_descriptor,
        ):
            raise GovernanceError(
                "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
                "the prepared projection catalog does not match the reviewed catalog",
            )
        target_catalog = (
            schema_v4.CatalogGenerationSeed(
                catalog_generation=target_catalog_generation,
                descriptor=target_catalog_descriptor,
                artifact_count=len(target_items),
                created_at=ready_at,
            )
            if membership_changed
            else None
        )
        target_manifest = projection_store.stage_variant_store(
            vault_root,
            key=key,
            items=target_items,
        )
        evidence = projection_store.projection_namespace_evidence_bytes(target_manifest)
        connection = store.open_authorization_session_connection(vault_root)
        try:
            existing_namespace = connection.execute(
                "SELECT namespace_id, evidence, ready_at "
                "FROM governance_projection_namespaces "
                "WHERE policy_fingerprint=? AND projector_schema_version=? "
                "AND catalog_generation=?",
                (
                    key.policy_fingerprint,
                    key.projector_schema_version,
                    key.catalog_generation,
                ),
            ).fetchone()
        finally:
            connection.close()
        if existing_namespace is not None:
            if (
                existing_namespace[0] != key.namespace_id
                or bytes(existing_namespace[1]) != evidence
            ):
                raise GovernanceError(
                    "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
                    "the reusable projection namespace does not verify",
                )
            ready_at = int(existing_namespace[2])
    except GovernanceError:
        raise
    except (
        membership.MembershipUnresolved,
        projection_store.ProjectionStoreError,
        projections.ProjectionError,
        reserved_paths.ReservedPathLeafError,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        raise GovernanceError(
            "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
            "the target authorization-projection namespace is unavailable",
        ) from None
    return _StagedTargetProjection(
        catalog=target_catalog,
        namespace={
            "namespace_id": key.namespace_id,
            "projector_schema_version": key.projector_schema_version,
            "catalog_generation": key.catalog_generation,
            "projection_rows_digest": target_manifest.rows_digest,
            "evidence": base64.b64encode(evidence).decode("ascii"),
            "ready_at": ready_at,
        },
        predecessor_items=active_items,
        items=target_items,
    )


def _v4_dependent_grant_transitions(
    vault_root: Path,
    *,
    current_policy: policy_module.Policy,
    target_policy: policy_module.Policy,
    predecessor_items: tuple[projection_store.ProjectionItemVariants, ...],
    target_items: tuple[projection_store.ProjectionItemVariants, ...],
) -> tuple[schema_v4.DependentGrantTransition, ...]:
    connection = store.open_authorization_session_connection(vault_root)
    try:
        rows = connection.execute(
            "SELECT grant_id, paths, fingerprints, scope_ids, membership_manifest, "
            "policy_fingerprint, prepared_event_id FROM governance_session_grants "
            "WHERE status='active' ORDER BY grant_id"
        ).fetchall()
    except sqlite3.Error:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "dependent grant authority cannot be reviewed exactly",
        ) from None
    finally:
        connection.close()
    predecessor_by_path = {item.item_identity: item for item in predecessor_items}
    target_by_path = {item.item_identity: item for item in target_items}
    transitions: list[schema_v4.DependentGrantTransition] = []
    for row in rows:
        try:
            grant_id = str(row[0])
            paths = json.loads(str(row[1]))
            fingerprints = json.loads(str(row[2]))
            scope_ids = json.loads(str(row[3]))
            expected_manifest = str(row[4])
            stored_policy_fingerprint = str(row[5])
            reviewed_membership = authorization_session_authority._load_membership(
                expected_manifest
            )
            if (
                not grant_id
                or not isinstance(paths, list)
                or not all(isinstance(path, str) and path for path in paths)
                or not isinstance(fingerprints, list)
                or not all(isinstance(value, str) for value in fingerprints)
                or len(paths) != len(fingerprints)
                or not isinstance(scope_ids, list)
                or not scope_ids
                or not all(isinstance(scope_id, str) and scope_id for scope_id in scope_ids)
                or scope_ids != sorted(set(scope_ids))
                or tuple(paths) != tuple(item.path for item in reviewed_membership)
                or tuple(fingerprints)
                != tuple(item.fingerprint for item in reviewed_membership)
                or any(
                    path not in predecessor_by_path
                    for path in paths
                )
                or not set(scope_ids).issubset(
                    {
                        scope_id
                        for item in reviewed_membership
                        for scope_id in item.scope_ids
                    }
                )
                or stored_policy_fingerprint != current_policy.fingerprint
                or row[6] is not None
            ):
                raise ValueError
            target_membership = [
                {
                    "path": path,
                    "fingerprint": target_by_path[path].content_hash,
                    "scope_ids": list(target_by_path[path].scope_ids),
                }
                for path in paths
                if path in target_by_path
            ]
            target_manifest = _canonical_json(target_membership)
        except (
            GovernanceError,
            authorization_session_lifecycle.AuthorizationSessionUnavailable,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "dependent grant authority cannot be reviewed exactly",
            ) from None
        transitions.append(
            schema_v4.DependentGrantTransition(
                grant_id=grant_id,
                expected_policy_fingerprint=stored_policy_fingerprint,
                expected_membership_manifest=expected_manifest,
                target_status=(
                    "expired"
                    if target_manifest != expected_manifest
                    else "review"
                ),
                target_policy_fingerprint=target_policy.fingerprint,
                target_membership_manifest=target_manifest,
            )
        )
    return tuple(transitions)


def _direction_with_dependent_grants(
    policy_direction: str,
    transitions: tuple[schema_v4.DependentGrantTransition, ...],
) -> str:
    """Classify the complete policy/grant transition conservatively.

    Every dependent transition removes an active widening grant by moving it to
    ``review`` or ``expired``.  That cannot make a pointwise policy narrowing more
    permissive.  If policy widens anywhere, grant removal can make the combined
    lattice incomparable, which belongs to the widening/unknown release class.
    """

    if policy_direction not in {"narrowing", "widening"} or any(
        transition.target_status not in {"review", "expired"}
        for transition in transitions
    ):
        return "widening"
    return policy_direction


def _dependent_grant_value(
    transitions: tuple[schema_v4.DependentGrantTransition, ...],
) -> list[dict[str, str]]:
    return [
        {
            "grant_id": transition.grant_id,
            "expected_policy_fingerprint": transition.expected_policy_fingerprint,
            "expected_membership_manifest": transition.expected_membership_manifest,
            "target_status": transition.target_status,
            "target_policy_fingerprint": transition.target_policy_fingerprint,
            "target_membership_manifest": transition.target_membership_manifest,
        }
        for transition in transitions
    ]


def _catalog_seed_value(
    catalog: schema_v4.CatalogGenerationSeed | None,
) -> dict[str, Any] | None:
    if catalog is None:
        return None
    return {
        "catalog_generation": catalog.catalog_generation,
        "descriptor": base64.b64encode(catalog.descriptor).decode("ascii"),
        "artifact_count": catalog.artifact_count,
        "created_at": catalog.created_at,
    }


def _v4_proposal_authority_binding(
    *,
    proposal_id: str,
    created_at: float,
    active_snapshot: schema_v4.ActivePolicySnapshot,
    prospective: policy_module.ProspectiveCompile,
    staged_projection: _StagedTargetProjection,
    membership_manifest: list[dict[str, Any]],
    transition_direction: str,
    dependent_grants: tuple[schema_v4.DependentGrantTransition, ...],
) -> dict[str, Any]:
    namespace = dict(staged_projection.namespace)
    catalog = staged_projection.catalog
    target = {
        "source_documents": [
            {
                "path": relative,
                "bytes": base64.b64encode(content).decode("ascii"),
            }
            for relative, content in prospective.target_documents
        ],
        "source_fingerprint": prospective.policy.fingerprint,
        "policy_fingerprint": prospective.policy.fingerprint,
        "compiled_policy": base64.b64encode(
            policy_module.canonical_compiled_bytes(prospective.policy)
        ).decode("ascii"),
        "compiler_schema_version": 1,
        "catalog": _catalog_seed_value(catalog),
        "projection_rows_digest": namespace.pop("projection_rows_digest"),
        "projection_namespace": namespace,
    }
    binding = {
        "schema": _V4_POLICY_PROPOSAL_SCHEMA,
        "transition_direction": transition_direction,
        "reviewed_active_tuple": _active_tuple_value(active_snapshot.active),
        "authoring_snapshot": _snapshot_value(prospective.snapshot),
        "membership_manifest": membership_manifest,
        "dependent_grants": _dependent_grant_value(dependent_grants),
        "target": target,
    }
    generation_id, authoring_event_id, receipt_event_id = (
        _policy_publication_identities(
            proposal_id=proposal_id,
            created_at=created_at,
            review_digest=_digest(binding),
        )
    )
    target.update(
        generation_id=generation_id,
        authoring_event_id=authoring_event_id,
        receipt_event_id=receipt_event_id,
    )
    return binding


def _require_authorization_session(
    value: RequestPrincipal | None, supplied: Any
) -> tuple[RequestPrincipal, str]:
    who = _principal(value)
    if who.verified_authorization_session is not None:
        context = _verified_session_context(who, supplied)
        return who, context.session_id
    handle = str(supplied or "").strip()
    if (
        not who.resolved
        or not handle
        or not who.authorization_session_id
        or not __import__("hmac").compare_digest(handle, who.authorization_session_id)
    ):
        raise GovernanceError(
            "AUTHORIZATION_SESSION_REQUIRED",
            "an explicit authorization session bound to this principal is required",
        )
    return who, handle


def _authorize_operation(
    vault_root: Path,
    selection: OperationVariant,
    kwargs: Mapping[str, Any],
) -> None:
    """Apply the registry's coarse authorization before handler-specific bounds."""
    if selection.authorization == "owner":
        _require_owner(kwargs.get("principal"))
    elif selection.authorization in {"self_session", "token_session"}:
        who = _principal(kwargs.get("principal"))
        if who.verified_authorization_session is None:
            if (
                store.authorization_session_schema_version(vault_root)
                != store.SCHEMA_USER_VERSION
            ):
                raise GovernanceError(
                    "AUTHORIZATION_SESSION_REQUIRED",
                    "a verified authorization session is required",
                )
        _require_authorization_session(
            kwargs.get("principal"), kwargs.get("authorization_session")
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _content_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"


def _canonical_documents(documents: Any) -> dict[str, str]:
    if not isinstance(documents, Mapping) or not documents:
        raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "documents must be a mapping")
    canonical: dict[str, str] = {}
    for raw_path, raw_content in sorted(documents.items(), key=lambda item: str(item[0])):
        rel = str(raw_path).replace("\\", "/").strip("/")
        parts = Path(rel).parts
        if (
            Path(rel).is_absolute()
            or not parts
            or parts[0] not in {"scopes", "rules", "grants"}
            or any(part in {"", ".", ".."} for part in parts)
            or not rel.endswith(".yaml")
        ):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "invalid policy path")
        if not isinstance(raw_content, str):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "policy YAML must be text")
        try:
            parsed = yaml.safe_load(raw_content)
        except yaml.YAMLError as exc:
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "invalid policy YAML") from exc
        if not isinstance(parsed, dict):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "policy YAML must be a mapping")
        canonical[rel] = raw_content.rstrip() + "\n"
    return canonical


def _affected_scope_ids(
    current: policy_module.Policy,
    prospective: policy_module.Policy,
    document_paths: set[str],
) -> frozenset[str]:
    affected: set[str] = set()
    for candidate in (current, prospective):
        for scope in candidate.scopes.values():
            if scope.source in document_paths:
                affected.add(scope.id)
        for document in (*candidate.rules, *candidate.grants):
            if document.source in document_paths:
                affected.update(document.scope_ids)
    return frozenset(affected)


def _memberships_for_path(
    vault_root: Path, path: Path, candidate: policy_module.Policy
) -> frozenset[str]:
    rel = path.relative_to(vault_root).as_posix()
    if path.suffix.casefold() != ".md":
        try:
            return membership.evaluate_path_only(
                vault_root, rel, candidate
            ).require_classified()
        except membership.MembershipUnresolved as exc:
            raise GovernanceError(
                "MEMBERSHIP_UNRESOLVED", "cannot resolve exact non-Markdown membership"
            ) from exc
    try:
        page = find_corpus.parse_page(path, path.stat().st_mtime, vault_root)
        return membership.evaluate(page, candidate)
    except (OSError, UnicodeError, membership.MembershipUnresolved) as exc:
        raise GovernanceError(
            "MEMBERSHIP_UNRESOLVED", f"cannot resolve exact membership for {rel!r}"
        ) from exc


def _is_operational_membership_path(vault_root: Path, candidate: Path) -> bool:
    """Whether a current internal-state owner, rather than content, owns a path."""
    from .. import claims, reserved_paths, voice_profiles

    logical = reserved_paths.classify_logical(
        candidate.relative_to(vault_root).as_posix()
    )
    if logical.disposition is reserved_paths.PathDisposition.RESERVED:
        return True

    sidecars = (
        index_paths.sidecar_path(vault_root),
        index_paths.clip_sidecar_path(vault_root),
        index_paths.governance_sidecar_path(vault_root),
        claims.sidecar_path(vault_root),
        memory_refs.sidecar_path(vault_root),
        lexstore.lexical_path(vault_root),
        epistemic_graph.sidecar_path(vault_root),
        deferred_index.store_path(vault_root),
        media_jobs.job_store_path(vault_root),
    )
    for sidecar in sidecars:
        if candidate in (
            sidecar,
            sidecar.with_name(f"{sidecar.name}-wal"),
            sidecar.with_name(f"{sidecar.name}-shm"),
            sidecar.with_name(f"{sidecar.name}-journal"),
        ):
            return True

    if candidate in (
        media_jobs.worker_lock_path(vault_root),
        voice_profiles.voice_profiles_path(vault_root),
        review_state.state_path(vault_root),
        graph_sync.checkpoint_path(vault_root),
        graph_sync.floor_path(vault_root),
    ):
        return True
    review_state_path = review_state.state_path(vault_root)
    if (
        candidate.parent == review_state_path.parent
        and _REVIEW_STATE_TEMP_RE.fullmatch(candidate.name) is not None
    ):
        return True
    lexical_path = lexstore.lexical_path(vault_root)
    if (
        candidate.parent == lexical_path.parent
        and _LEXICAL_REBUILD_TEMP_RE.fullmatch(candidate.name) is not None
    ):
        return True
    receipt_root = graph_sync.graph_commit_receipt_path(vault_root, "0" * 24).parent
    graph_sidecar_parent = epistemic_graph.sidecar_path(vault_root).parent
    return candidate.is_relative_to(receipt_root) or (
        candidate.parent == graph_sidecar_parent
        and _GRAPH_REBUILD_SIDECAR_RE.fullmatch(candidate.name) is not None
    )


def _membership_manifest(
    vault_root: Path,
    current: policy_module.Policy,
    prospective: policy_module.Policy,
    document_paths: set[str],
) -> list[dict[str, str]]:
    affected = _affected_scope_ids(current, prospective, document_paths)
    rows: dict[str, str] = {}
    if affected:
        for candidate in sorted((vault_root / kb_dirname()).rglob("*")):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(vault_root).as_posix()
            if policy_module.is_governance_path(rel):
                continue
            if _is_operational_membership_path(vault_root, candidate):
                continue
            union = _memberships_for_path(
                vault_root, candidate, current
            ) | _memberships_for_path(vault_root, candidate, prospective)
            if union & affected:
                rows[rel] = _content_hash(candidate)

    # Release proposals bind concrete bridge/dependency identities, not scope
    # membership.  Include both their declared paths and every live page that
    # carries one of those IDs so a byte edit, rename, deletion, or newly
    # ambiguous duplicate invalidates the reviewed proposal before commit.
    releases = [
        release
        for candidate in (current, prospective)
        for release in candidate.release_grants
        if release.source in document_paths
    ]
    expected_paths = {
        path
        for release in releases
        for path in (release.path, *(dependency.path for dependency in release.bridge_of))
    }
    wanted_ids = {
        identity
        for release in releases
        for ref in (release.ref, *(dependency.ref for dependency in release.bridge_of))
        if (identity := memory_refs.parse_memory_ref(ref)) is not None
    }
    for rel in expected_paths:
        rows[rel] = _content_hash(vault_root / rel)
    if wanted_ids:
        for candidate in sorted(vault_root.rglob("*.md")):
            if not candidate.is_file():
                continue
            try:
                parsed = find_corpus.parse_page(
                    candidate,
                    candidate.stat().st_mtime,
                    vault_root,
                )
            except OSError:
                parsed = None
            if parsed is None:
                continue
            identity = memory_refs.normalize_id(parsed.frontmatter.get("exomem_id"))
            if identity in wanted_ids:
                rows[parsed.rel_path] = _content_hash(candidate)
    return [
        {"path": rel, "content_hash": rows[rel]}
        for rel in sorted(rows)
    ]


def _resolved_membership_manifest(
    vault_root: Path, candidate: policy_module.Policy, paths: list[str] | tuple[str, ...]
) -> list[dict[str, Any]]:
    return [
        {
            "path": rel,
            "scope_ids": sorted(
                _memberships_for_path(vault_root, vault_root / rel, candidate)
            ),
        }
        for rel in paths
    ]


def _compared_audiences(
    current: policy_module.Policy, prospective: policy_module.Policy
) -> list[str]:
    """Every audience a transition can move, including the unnamed default.

    Enumerating rules and grants alone compares only audiences some document
    names — which is exactly the set a change to the DEFAULT cannot appear in.
    Removing a scope's `default_deny` then moves an unnamed audience from 0 to
    6 while every named audience sits still, and the review reports a
    narrowing. `UNNAMED_AUDIENCE_PROBE` stands in for that audience; no
    document can name it and no credential can mint it, so it always resolves
    through the default and never matches a rule or grant.

    It also guarantees a non-empty audience set, so an empty lattice can no
    longer be mistaken for "nothing to compare".
    """
    return sorted(
        {
            document.audience
            for policy in (current, prospective)
            for document in (*policy.rules, *policy.grants)
        }
        | {UNNAMED_AUDIENCE_PROBE}
    )


def _proposal_analysis(
    vault_root: Path,
    current: policy_module.Policy,
    prospective: policy_module.Policy,
    manifest: list[dict[str, str]],
) -> tuple[
    dict[str, int], list[str], str, list[str], bool, int | None, int | None
]:
    """Evaluate the concrete affected membership, never caller hint paths."""
    audiences = _compared_audiences(current, prospective)
    purposes = sorted(
        {rule.purpose for policy in (current, prospective) for rule in policy.rules if rule.purpose}
    )
    purposes = [None, *purposes]
    before: dict[str, tuple[int, str]] = {}
    after: dict[str, tuple[int, str]] = {}
    rule_ids: set[str] = set()
    # `target_ceiling` remains the highest level the proposal leaves an
    # AUTHORED audience at. The default has its own owner-visible ceiling so the
    # rotation path is explicit without changing that compatibility field.
    named_after: list[int] = []
    unnamed_after: list[int] = []
    all_open = True
    for row in manifest:
        rel = str(row["path"])
        try:
            current_scopes = _memberships_for_path(vault_root, vault_root / rel, current)
            prospective_scopes = _memberships_for_path(vault_root, vault_root / rel, prospective)
        except GovernanceError:
            return (
                {"narrowed": 0, "widened": 0, "unchanged": 0},
                [],
                "widening",
                [],
                False,
                None,
                None,
            )
        for audience in audiences:
            for purpose in purposes:
                key = f"{audience}:{purpose or '-'}:{rel}"
                old = decisions.decide(current_scopes, audience=audience, purpose=purpose, policy=current)
                new = decisions.decide(prospective_scopes, audience=audience, purpose=purpose, policy=prospective)
                before[key] = _decision_direction_state(old)
                after[key] = _decision_direction_state(new)
                if audience != UNNAMED_AUDIENCE_PROBE:
                    named_after.append(new.level)
                else:
                    unnamed_after.append(new.level)
                rule_ids.update(old.rule_ids)
                rule_ids.update(new.rule_ids)
                rule_ids.update(
                    grant.id for grant in (*current.grants, *prospective.grants)
                    if grant.audience == audience and bool(set(grant.scope_ids) & (set(current_scopes) | set(prospective_scopes)))
                )
                all_open = all_open and old.level == policy_module.DISCLOSURE_MAX and new.level == policy_module.DISCLOSURE_MAX and old.release_reason is None and new.release_reason is None
    consequences = {
        "narrowed": sum(after[key][0] < before[key][0] for key in before),
        "widened": sum(
            after[key][0] > before[key][0]
            or (after[key][0] == before[key][0] and after[key][1] != before[key][1])
            for key in before
        ),
        "unchanged": sum(after[key] == before[key] for key in before),
    }
    direction = classify_transition_direction(before, after)
    if current.release_grants != prospective.release_grants:
        direction = "widening"
    samples = [str(row["path"]) for row in manifest[:5]] if all_open else []
    return (
        consequences,
        samples,
        direction,
        sorted(rule_ids),
        all_open,
        max(named_after, default=None),
        max(unnamed_after, default=None),
    )


def _purpose_direction(
    vault_root: Path, *, audience: str, before_purpose: str | None, after_purpose: str
) -> str:
    policy = policy_module.load(vault_root)
    if policy.blocked:
        return "widening"
    conn = store.open_readonly_connection(vault_root)
    if conn is not None:
        try:
            active = conn.execute(
                "SELECT 1 FROM governance_session_grants WHERE audience=? AND status='active' LIMIT 1",
                (audience,),
            ).fetchone()
        finally:
            conn.close()
        if active is not None:
            return "widening"
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    try:
        for candidate in sorted((vault_root / kb_dirname()).rglob("*")):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(vault_root).as_posix()
            if policy_module.is_governance_path(rel):
                continue
            if _is_operational_membership_path(vault_root, candidate):
                continue
            scopes = _memberships_for_path(vault_root, candidate, policy)
            before[rel] = decisions.decide(
                scopes, audience=audience, purpose=before_purpose, policy=policy
            ).level
            after[rel] = decisions.decide(
                scopes, audience=audience, purpose=after_purpose, policy=policy
            ).level
    except GovernanceError:
        return "widening"
    return classify_transition_direction(before, after)


def _proposal(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    _require_owner(kwargs.get("principal"))
    documents = _canonical_documents(kwargs.get("documents"))
    raw_patterns = kwargs.get("selector_paths") or []
    if not isinstance(raw_patterns, list) or not all(
        isinstance(pattern, str) and pattern for pattern in raw_patterns
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL", "selector_paths must be a list of strings"
        )
    patterns = sorted(set(raw_patterns))
    now = float(kwargs.get("now", time.time()))
    schema_version = store.authorization_session_schema_version(vault_root)
    active_snapshot = (
        _v4_active_policy_snapshot(vault_root, now=int(now))
        if schema_version == schema_v4.SCHEMA_USER_VERSION
        else None
    )
    current_policy = (
        active_snapshot.policy
        if active_snapshot is not None
        else policy_module.load(vault_root)
    )
    if current_policy.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    semantic_operation = kwargs.get("_semantic_operation")
    prospective_compile = policy_module.compile_prospective(
        vault_root,
        documents,
        _replace_document_set=semantic_operation in {"suspend", "resume", "undo"},
    )
    if prospective_compile is None:
        raise GovernanceError(
            "GOVERNANCE_AUTHORING_UNSTABLE",
            "the policy workspace changed or could not be acquired safely",
        )
    prospective = prospective_compile.policy
    if prospective.blocked:
        raise GovernanceError(
            "INVALID_GOVERNANCE_POLICY",
            _canonical_json(list(prospective.findings)),
        )
    manifest = _membership_manifest(
        vault_root, current_policy, prospective, set(documents)
    )
    expires_at = now + max(1, int(kwargs.get("ttl_seconds", DEFAULT_PROPOSAL_TTL_SECONDS)))
    intent = str(kwargs.get("intent") or "").strip()
    if not intent:
        raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "intent is required")
    target_ceiling = int(kwargs.get("target_ceiling", policy_module.DISCLOSURE_MAX))
    if not policy_module.DISCLOSURE_MIN <= target_ceiling <= policy_module.DISCLOSURE_MAX:
        raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "target ceiling is invalid")
    (
        consequences,
        samples,
        direction,
        overlaps,
        all_open,
        derived_ceiling,
        unnamed_audience_ceiling,
    ) = _proposal_analysis(vault_root, current_policy, prospective, manifest)
    hint_diagnostics: list[str] = []
    if patterns:
        hint_diagnostics.append("selector_paths are compatibility hints; concrete membership is authoritative")
    if derived_ceiling is None or target_ceiling != derived_ceiling:
        hint_diagnostics.append("target_ceiling is a compatibility hint, not an authorization fact")
    proposal_id = uuid.uuid4().hex
    payload = {
        "interpretation": intent,
        "documents": documents,
        "duration": kwargs.get("duration"),
    }
    if semantic_operation is not None:
        if semantic_operation not in {"commit", "suspend", "resume", "undo"}:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "semantic policy operation is invalid",
            )
        payload["semantic_operation"] = semantic_operation
    undo_source_generation_id = kwargs.get("_undo_source_generation_id")
    if undo_source_generation_id is not None:
        if semantic_operation != "undo" or not isinstance(
            undo_source_generation_id, str
        ) or not undo_source_generation_id:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "undo source generation is invalid",
            )
        payload["undo_source_generation_id"] = undo_source_generation_id
    semantic_rule_ids = kwargs.get("_semantic_rule_ids")
    if semantic_rule_ids is not None:
        if (
            semantic_operation not in {"suspend", "resume"}
            or not isinstance(semantic_rule_ids, list)
            or not semantic_rule_ids
            or not all(isinstance(rule_id, str) and rule_id for rule_id in semantic_rule_ids)
            or semantic_rule_ids != sorted(set(semantic_rule_ids))
        ):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "semantic policy rule set is invalid",
            )
        payload["semantic_rule_ids"] = semantic_rule_ids
    semantic_request_digest = kwargs.get("_semantic_request_digest")
    if semantic_request_digest is not None:
        if (
            semantic_operation not in {"suspend", "resume", "undo"}
            or not isinstance(semantic_request_digest, str)
            or _SHA256_RE.fullmatch(semantic_request_digest) is None
        ):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "semantic policy request identity is invalid",
            )
        payload["semantic_request_digest"] = semantic_request_digest
    if active_snapshot is not None:
        if current_policy.fingerprint != active_snapshot.active.policy_fingerprint:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "the active governance policy does not match its tuple",
            )
        staged_projection = _stage_target_projection_namespace(
            vault_root,
            active_snapshot=active_snapshot,
            target_policy=prospective,
            ready_at=int(now),
        )
        dependent_grants = _v4_dependent_grant_transitions(
            vault_root,
            current_policy=active_snapshot.policy,
            target_policy=prospective,
            predecessor_items=staged_projection.predecessor_items,
            target_items=staged_projection.items,
        )
        direction = _direction_with_dependent_grants(direction, dependent_grants)
        payload["authority_binding"] = _v4_proposal_authority_binding(
            proposal_id=proposal_id,
            created_at=now,
            active_snapshot=active_snapshot,
            prospective=prospective_compile,
            staged_projection=staged_projection,
            membership_manifest=manifest,
            transition_direction=direction,
            dependent_grants=dependent_grants,
        )
    conn = store.open_connection(vault_root)
    try:
        conn.execute(
            "INSERT INTO governance_proposals "
            "(proposal_id, created_at, expires_at, proposal_json, fingerprint_at_propose, "
            "membership_manifest, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (
                proposal_id,
                now,
                expires_at,
                _canonical_json(payload),
                current_policy.fingerprint,
                _canonical_json(manifest),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "interpretation": intent,
        "canonical_yaml": documents,
        "membership_preview": {"count": len(manifest), "samples": samples},
        "consequences": {
            **consequences,
            "target_ceiling": derived_ceiling,
            "unnamed_audience_ceiling": unnamed_audience_ceiling,
            "direction": direction,
        },
        "overlaps": overlaps,
        "hint_diagnostics": hint_diagnostics,
        "duration": kwargs.get("duration"),
        "reversal": "undo",
        "proposal_id": proposal_id,
        "expires_at": expires_at,
    }


def _decision_direction_state(decision: decisions.Decision) -> tuple[int, str]:
    return (
        decision.level,
        _digest(
            {
                "options": decision.options,
                "notice": decision.notice,
                "bridge_abstraction": decision.bridge_abstraction,
                "bridge": decision.bridge,
                "release_reason": decision.release_reason,
                "release_grant_id": decision.release_grant_id,
                "release_strip": list(decision.release_strip),
                "release_dependency_digest": decision.release_dependency_digest,
            }
        ),
    )


def classify_transition_direction(
    before: Mapping[str, object] | None, after: Mapping[str, object] | None
) -> str:
    """Only a complete pointwise proof can classify a transition narrowing."""
    if before is None or after is None or set(before) != set(after):
        return "widening"

    def no_more_permissive(prior: object, target: object) -> bool:
        if isinstance(prior, int) and isinstance(target, int):
            return target <= prior
        if (
            isinstance(prior, tuple)
            and isinstance(target, tuple)
            and len(prior) == len(target) == 2
            and isinstance(prior[0], int)
            and isinstance(target[0], int)
            and isinstance(prior[1], str)
            and isinstance(target[1], str)
        ):
            return target[0] < prior[0] or (
                target[0] == prior[0] and target[1] == prior[1]
            )
        return False

    return (
        "narrowing"
        if all(no_more_permissive(before[key], after[key]) for key in before)
        else "widening"
    )


def _component(
    kind: str, key: str, value: Mapping[str, Any], *, status: str
) -> dict[str, Any]:
    normalized = dict(value)
    if (
        kind in {"token", "grant", "dependent_grant", "purpose", "proposal"}
        and normalized.get("status") != "absent"
        and "projection_version" not in normalized
    ):
        normalized = authorization_row(**normalized)
    return {
        "component_kind": kind,
        "component_key": key,
        "value_json": _canonical_json(normalized),
        "value_hash": _digest(normalized),
        "status": status,
    }


def _composite(phase: str, components: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "kind": item["component_kind"],
            "key": item["component_key"],
            "value_hash": item["value_hash"],
            "status": item["status"],
        }
        for item in sorted(
            components,
            key=lambda row: (row["component_kind"], row["component_key"]),
        )
    ]
    return _digest({"domain": f"governance-composite/{phase}/v1", "components": normalized})


def _archive_value(rel: str, prior_bytes: bytes | None, prior_hash: str) -> dict[str, str]:
    return {
        "path_hash": hashlib.sha256(rel.encode()).hexdigest(),
        "prior_hash": prior_hash,
        "bytes_hash": (
            "absent" if prior_bytes is None else hashlib.sha256(prior_bytes).hexdigest()
        ),
    }


def _commit_event_id(
    proposal_id: str, attempt_no: int, attempt_nonce: str, prepared_digest: str
) -> str:
    return receipts.critical_event_id(
        {
            "operation": "governance_policy_commit",
            "proposal_id": proposal_id,
            "attempt": attempt_no,
            "attempt_nonce": attempt_nonce,
            "prepared": prepared_digest,
        }
    )


def _marker_path(vault_root: Path) -> Path:
    return policy_target(policy_module.governance_root(vault_root), PENDING_MARKER)


def _durable_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _durable_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _clear_policy_caches(vault_root: Path) -> None:
    policy_module._CACHE.pop(str(policy_module.governance_root(vault_root)), None)
    membership.clear_memo()
    try:
        from . import egress

        egress.clear_decision_memo()
    except ImportError:  # pragma: no cover - package is complete in production
        pass


def _insert_components(
    conn: sqlite3.Connection,
    event_id: str,
    phases: Mapping[str, list[dict[str, Any]]],
) -> None:
    for phase, rows in phases.items():
        for ordinal, row in enumerate(
            sorted(rows, key=lambda item: (item["component_kind"], item["component_key"]))
        ):
            conn.execute(
                "INSERT OR IGNORE INTO governance_operation_components "
                "(event_id, phase, ordinal, component_kind, component_key, value_json, "
                "value_hash, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    phase,
                    ordinal,
                    row["component_kind"],
                    row["component_key"],
                    row["value_json"],
                    row["value_hash"],
                    row["status"],
                ),
            )


def _create_journal(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    operation: str,
    who: RequestPrincipal,
    authorization_session: str | None,
    direction: str,
    phases: Mapping[str, list[dict[str, Any]]],
    child_ids: list[str],
    marker_required: bool = False,
    proposal_id: str | None = None,
    phase: str = "pending",
    now: float,
) -> dict[str, str]:
    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    affected_ids = sorted(
        {
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for rows in phases.values()
            for row in rows
        }
    )
    conn.execute(
        "INSERT INTO governance_operation_journals "
        "(event_id, operation, causation_id, authorization_session, principal_id, phase, "
        "direction, prior_digest, prepared_digest, final_digest, affected_ids, "
        "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
        "marker_required, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            event_id,
            operation,
            event_id,
            authorization_session,
            who.audience_id,
            phase,
            direction,
            digests["prior"],
            digests["prepared"],
            digests["final"],
            _canonical_json(affected_ids),
            _canonical_json(child_ids),
            _canonical_json([f"{child}:committed" for child in child_ids]),
            proposal_id,
            1 if marker_required else 0,
            now,
            now,
        ),
    )
    _insert_components(conn, event_id, phases)
    return digests


def _arm_journal(vault_root: Path, event_id: str, *, now: float) -> None:
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE governance_operation_journals SET phase='pending', updated_at=? "
            "WHERE event_id=? AND phase='allocating'",
            (now, event_id),
        )
        if cursor.rowcount != 1:
            raise GovernanceError("GOVERNANCE_BLOCKED", "control journal could not arm")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _standing_grant_relative_path(vault_root: Path, raw_grant_id: Any) -> tuple[str, str]:
    """Validate a canonical grant id and prove its target is one direct child."""
    if not policy_module.is_valid_document_id(raw_grant_id):
        raise GovernanceError(
            "INVALID_STANDING_GRANT_ID",
            "grant_id must be a canonical 26-character Crockford-base32 ULID",
        )
    grant_id = str(raw_grant_id)
    try:
        governance_root = policy_module.governance_root(vault_root).resolve(strict=False)
        target = policy_target(
            policy_module.governance_root(vault_root), f"grants/{grant_id}.yaml"
        )
        grants_root = target.parent
    except (OSError, RuntimeError) as exc:
        raise GovernanceError(
            "INVALID_STANDING_GRANT_ID", "standing grant target cannot be resolved safely"
        ) from exc
    if (
        grants_root.parent != governance_root
        or target.name != f"{grant_id}.yaml"
    ):
        raise GovernanceError(
            "INVALID_STANDING_GRANT_ID",
            "standing grant target must be a direct child of _Governance/grants",
        )
    return grant_id, f"grants/{grant_id}.yaml"


def _standing_grant(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    grant_id, rel = _standing_grant_relative_path(vault_root, kwargs.get("grant_id"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    raw_scope_ids = kwargs.get("scope_ids")
    audience = str(kwargs.get("audience") or "").strip()
    ceiling = kwargs.get("ceiling")
    if (
        not isinstance(raw_scope_ids, list)
        or not raw_scope_ids
        or not all(isinstance(value, str) and value for value in raw_scope_ids)
        or not audience
        or not isinstance(ceiling, int)
        or isinstance(ceiling, bool)
        or not policy_module.DISCLOSURE_MIN <= ceiling <= policy_module.DISCLOSURE_MAX
    ):
        raise GovernanceError("INVALID_STANDING_GRANT", "grant fields are invalid")
    current = policy_module.load(vault_root)
    if current.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    if grant_id in {grant.id for grant in current.grants}:
        raise GovernanceError("GRANT_EXISTS", "standing grant already exists")
    if not set(raw_scope_ids) <= set(current.scopes):
        raise GovernanceError("SCOPE_UNKNOWN", "one or more scopes do not exist")
    document = yaml.safe_dump(
        {
            "governance_version": 1,
            "id": grant_id,
            "scope_ids": sorted(set(raw_scope_ids)),
            "audience": audience,
            "ceiling": ceiling,
        },
        sort_keys=False,
        allow_unicode=True,
    )
    prospective = _prospective_policy(vault_root, {rel: document})
    if prospective.blocked:
        raise GovernanceError(
            "INVALID_STANDING_GRANT", _canonical_json(list(prospective.findings))
        )
    transition = kwargs["_selection"]
    result = _yaml_transition(
        vault_root,
        transition=transition,
        who=who,
        documents={rel: document},
        direction=_effective_transition_direction(vault_root, {rel: document}),
        now=float(kwargs.get("now", time.time())),
    )
    return {**result, "grant_id": grant_id}


def _standing_revoke(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    grant_id, _rel = _standing_grant_relative_path(vault_root, kwargs.get("grant_id"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    current = policy_module.load(vault_root)
    match = next((grant for grant in current.grants if grant.id == grant_id), None)
    if match is None:
        raise GovernanceError("GRANT_UNKNOWN", "standing grant does not exist")
    return _yaml_transition(
        vault_root,
        transition=kwargs["_selection"],
        who=who,
        documents={match.source: None},
        direction=_effective_transition_direction(vault_root, {match.source: None}),
        now=float(kwargs.get("now", time.time())),
    )


def _token_projection(row: tuple[Any, ...], *, consumed_at: Any, status: str, prepared_event_id: Any) -> dict[str, Any]:
    return authorization_row(
        audience=str(row[0]),
        max_level=int(row[1]),
        fingerprints=str(row[2]),
        paths=str(row[3]),
        expires_at=float(row[4]),
        minted_at=float(row[5]),
        consumed_at=consumed_at,
        authorization_session=str(row[7]),
        purpose=row[8],
        org_ceiling=int(row[9]),
        status=status,
        prepared_event_id=prepared_event_id,
    )


def _grant_projection(
    *,
    authorization_session: str,
    audience: str,
    purpose: str | None,
    ceiling: int,
    paths: str,
    fingerprints: str,
    token_jti: str,
    status: str,
    prepared_event_id: str | None,
    created_at: float,
    expires_at: float,
    revoked_at: float | None,
    membership_manifest: str,
    policy_fingerprint: str,
) -> dict[str, Any]:
    return authorization_row(
        authorization_session=authorization_session,
        audience=audience,
        purpose=purpose,
        ceiling=ceiling,
        paths=paths,
        fingerprints=fingerprints,
        token_jti=token_jti,
        status=status,
        prepared_event_id=prepared_event_id,
        created_at=created_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
        membership_manifest=membership_manifest,
        policy_fingerprint=policy_fingerprint,
    )


def _grant(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    if _principal(kwargs.get("principal")).verified_authorization_session is not None:
        return _grant_v4(vault_root, **kwargs)
    selection = kwargs["_selection"]
    who, authorization_session = _require_authorization_session(
        kwargs.get("principal"), kwargs.get("authorization_session")
    )
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    token = str(kwargs.get("token") or "")
    now = float(kwargs.get("now", time.time()))
    purpose = kwargs.get("purpose")
    if purpose is None:
        purpose = who.purpose
    if purpose is None:
        purpose = store.active_session_purpose(
            vault_root,
            audience=who.audience_id,
            authorization_session=authorization_session,
            now=now,
        )
    try:
        claim = tokens_module.verify(
            vault_root,
            token,
            audience=who.audience_id,
            authorization_session=authorization_session,
            purpose=purpose,
            now=int(now),
        )
        tokens_module._check_content(vault_root, claim)
    except tokens_module.WithholdTokenError as exc:
        raise GovernanceError(exc.code, exc.reason) from exc
    duration = max(1, int(kwargs.get("duration_seconds", 3600)))
    expires_at = min(float(claim.expires_at), now + duration)
    bound_policy = policy_module.load(vault_root)
    if bound_policy.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    membership_manifest = _resolved_membership_manifest(
        vault_root, bound_policy, claim.paths
    )

    conn = store.open_connection(vault_root)
    try:
        token_row = conn.execute(
            "SELECT audience, max_level, fingerprints, paths, expires_at, minted_at, consumed_at, "
            "authorization_session, purpose, org_ceiling, status, prepared_event_id "
            "FROM withhold_tokens WHERE jti=?",
            (claim.jti,),
        ).fetchone()
    finally:
        conn.close()
    if token_row is None:
        raise GovernanceError("TOKEN_UNKNOWN", "no such escalation token")
    if token_row[6] is not None:
        raise GovernanceError("TOKEN_CONSUMED", "this escalation was already used")
    if token_row[11] is not None:
        raise GovernanceError("TOKEN_RESERVED", "this escalation has an open operation")
    nonce = uuid.uuid4().hex
    causation_id = receipts.critical_event_id(
        {
            "operation": "governance_session_grant",
            "jti": claim.jti,
            "authorization_session": authorization_session,
            "nonce": nonce,
        }
    )
    grant_id = hashlib.sha256(f"grant:{causation_id}:{claim.jti}".encode()).hexdigest()
    child_ids = [
        receipts.critical_event_id(
            {"causation_id": causation_id, "intent": f"child-{index}"}
        )
        for index, _receipt in enumerate(selection.child_receipts, start=1)
    ]
    grant_paths = _canonical_json(list(claim.paths))
    grant_fingerprints = _canonical_json(list(claim.fingerprints))
    grant_membership = _canonical_json(membership_manifest)
    grant_prepared = _grant_projection(
        authorization_session=authorization_session, audience=who.audience_id,
        purpose=purpose, ceiling=claim.max_level, paths=grant_paths,
        fingerprints=grant_fingerprints, token_jti=claim.jti, status="prepared",
        prepared_event_id=causation_id, created_at=now, expires_at=expires_at,
        revoked_at=None, membership_manifest=grant_membership,
        policy_fingerprint=bound_policy.fingerprint,
    )
    phases = {
        "prior": [
            _component(
                "token",
                claim.jti,
                _token_projection(token_row, consumed_at=None, status="active", prepared_event_id=None),
                status="active",
            ),
            _component("grant", grant_id, {"status": "absent"}, status="absent"),
        ],
        "prepared": [
            _component(
                "token",
                claim.jti,
                _token_projection(token_row, consumed_at=now, status="prepared", prepared_event_id=causation_id),
                status="prepared",
            ),
            _component(
                "grant",
                grant_id,
                grant_prepared,
                status="prepared",
            ),
        ],
        "final": [
            _component(
                "token",
                claim.jti,
                _token_projection(token_row, consumed_at=now, status="consumed", prepared_event_id=None),
                status="consumed",
            ),
            _component(
                "grant",
                grant_id,
                {**grant_prepared, "status": "active", "prepared_event_id": None},
                status="active",
            ),
        ],
    }
    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    operations = selection.child_receipts
    affected = sorted(
        hashlib.sha256(
            f"{component['component_kind']}:{component['component_key']}".encode()
        ).hexdigest()
        for component in phases["prepared"]
    )
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        token_row = conn.execute(
            "SELECT consumed_at, prepared_event_id FROM withhold_tokens WHERE jti=?",
            (claim.jti,),
        ).fetchone()
        if token_row is None or token_row[0] is not None or token_row[1] is not None:
            raise GovernanceError("TOKEN_CONSUMED", "this escalation is no longer available")
        persisted = _create_journal(
            conn,
            event_id=causation_id,
            operation=selection.journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction="widening",
            phases=phases,
            child_ids=child_ids,
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    for index, (child_id, child_operation) in enumerate(
        zip(child_ids, operations, strict=True), start=1
    ):
        receipts.begin_event(
            vault_root,
            operation=child_operation,
            prior=digests["prior"],
            prepared=digests["prepared"],
            target=digests["final"],
            affected_ids=affected,
            event_id=child_id,
            parent_causation_id=causation_id,
            intent_id=f"child-{index}",
        )
        if kwargs.get("crash_at") == f"after_child_intent:{index}":
            raise GovernanceCrash(f"after_child_intent:{index}")
    _arm_journal(vault_root, causation_id, now=now)

    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE withhold_tokens SET consumed_at=?, status='prepared', prepared_event_id=? "
            "WHERE jti=? AND consumed_at IS NULL AND prepared_event_id IS NULL",
            (now, causation_id, claim.jti),
        )
        if cursor.rowcount != 1:
            raise GovernanceError("TOKEN_CONSUMED", "this escalation was already used")
        conn.execute(
            "INSERT INTO governance_session_grants "
            "(grant_id, authorization_session, audience, purpose, ceiling, paths, fingerprints, "
            "token_jti, status, prepared_event_id, created_at, expires_at, "
            "membership_manifest, policy_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?)",
            (
                grant_id,
                authorization_session,
                who.audience_id,
                purpose,
                claim.max_level,
                grant_paths,
                grant_fingerprints,
                claim.jti,
                causation_id,
                now,
                expires_at,
                grant_membership,
                bound_policy.fingerprint,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if kwargs.get("crash_at") == "after_compound_state":
        raise GovernanceCrash("after_compound_state")
    for index, child_id in enumerate(child_ids, start=1):
        receipts.commit_event(vault_root, child_id, outcome="prepared")
        if kwargs.get("crash_at") == f"after_child_terminal:{index}":
            raise GovernanceCrash(f"after_child_terminal:{index}")
    _activate_event(vault_root, causation_id, remove_marker=False, now=now)
    return {
        "status": "committed",
        "causation_id": causation_id,
        "grant_id": grant_id,
    }


def _v4_authority_inputs(
    vault_root: Path,
    kwargs: Mapping[str, Any],
) -> tuple[
    RequestPrincipal,
    authorization_session_lifecycle.AuthorizationSessionContext,
    authorization_custody.AuthorizationCustody,
    sqlite3.Connection,
    int,
]:
    who = _principal(kwargs.get("principal"))
    context = _verified_session_context(who, kwargs.get("authorization_session"))
    raw_now = kwargs.get("now", int(time.time()))
    if isinstance(raw_now, bool) or not isinstance(raw_now, (int, float)) or raw_now <= 0:
        raise GovernanceError("AUTHORIZATION_SESSION_UNAVAILABLE", "authorization session is unavailable")
    now = int(raw_now)
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(vault_root, now=now)
        if (
            custody.keyring.cell_id != context.cell_id
            or custody.keyring.logical_vault_id != context.logical_vault_id
            or custody.keyring.keyring_id != context.keyring_id
        ):
            raise authorization_session_lifecycle.AuthorizationSessionUnavailable
        connection = store.open_authorization_session_connection(vault_root)
        return who, context, custody, connection, now
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
    ):
        if connection is not None:
            connection.close()
        raise GovernanceError(
            "AUTHORIZATION_SESSION_UNAVAILABLE",
            "authorization session is unavailable",
        ) from None


def _inspect_v4_token(
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    context: authorization_session_lifecycle.AuthorizationSessionContext,
    token: object,
    audience: str,
    purpose: str | None,
    now: int,
) -> tuple[authorization_session_authority.EscalationReview, bytes]:
    for verifier in custody.keyring.accepted_keys:
        try:
            review = authorization_session_authority.inspect_escalation_token(
                connection,
                token=token,
                context=context,
                signing_key=verifier.key,
                audience=audience,
                purpose=purpose,
                now=now,
            )
        except authorization_session_lifecycle.AuthorizationSessionUnavailable:
            continue
        return review, verifier.key
    raise GovernanceError("AUTHORIZATION_SESSION_UNAVAILABLE", "authorization session is unavailable")


def _v4_token_projection(
    row: tuple[Any, ...],
    *,
    status: str,
    prepared_event_id: str | None,
    consumed_at: int | None,
) -> dict[str, Any]:
    return authorization_row(
        authorization_session_id=str(row[0]),
        principal_id=str(row[1]),
        issuer_family=str(row[2]),
        audience=str(row[3]),
        max_level=int(row[4]),
        fingerprints=str(row[5]),
        paths=str(row[6]),
        scope_ids=str(row[7]),
        purpose=row[8],
        org_ceiling=int(row[9]),
        status=status,
        prepared_event_id=prepared_event_id,
        expires_at=int(row[12]),
        minted_at=int(row[13]),
        consumed_at=consumed_at,
    )


def _v4_grant_projection(
    grant: authorization_session_authority.SessionGrant,
    *,
    status: str,
    prepared_event_id: str | None,
) -> dict[str, Any]:
    membership_manifest = _canonical_json(
        [
            {
                "fingerprint": item.fingerprint,
                "path": item.path,
                "scope_ids": list(item.scope_ids),
            }
            for item in grant.membership
        ]
    )
    return authorization_row(
        authorization_session_id=grant.authorization_session_id,
        principal_id=grant.principal_id,
        issuer_family=grant.issuer_family,
        audience=grant.audience,
        purpose=grant.purpose,
        ceiling=grant.ceiling,
        paths=_canonical_json(list(grant.paths)),
        fingerprints=_canonical_json(list(grant.fingerprints)),
        scope_ids=_canonical_json(list(grant.scope_ids)),
        membership_manifest=membership_manifest,
        policy_fingerprint=grant.policy_fingerprint,
        token_jti=grant.token_jti,
        status=status,
        prepared_event_id=prepared_event_id,
        created_at=grant.created_at,
        expires_at=grant.expires_at,
        revoked_at=None,
    )


def _grant_v4(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    selection = kwargs["_selection"]
    who, context, custody, connection, now = _v4_authority_inputs(vault_root, kwargs)
    try:
        purpose = kwargs.get("purpose")
        if purpose is None:
            purpose = who.purpose
        if purpose is None:
            purpose = authorization_session_authority.active_session_purpose(
                connection,
                context=context,
                audience=who.audience_id,
                now=now,
            )
        review, signing_key = _inspect_v4_token(
            connection,
            custody=custody,
            context=context,
            token=kwargs.get("token"),
            audience=who.audience_id,
            purpose=purpose,
            now=now,
        )
        bound_policy = policy_module.load(vault_root)
        if bound_policy.empty or bound_policy.blocked:
            raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
        membership_rows = _resolved_membership_manifest(
            vault_root,
            bound_policy,
            review.paths,
        )
        fingerprints = dict(zip(review.paths, review.fingerprints, strict=True))
        current_membership = tuple(
            authorization_session_authority.SessionMembership(
                path=str(row["path"]),
                fingerprint=_content_hash(vault_root / str(row["path"])),
                scope_ids=tuple(str(scope) for scope in row["scope_ids"]),
            )
            for row in membership_rows
        )
        if any(
            row.fingerprint != fingerprints.get(row.path)
            for row in current_membership
        ):
            raise GovernanceError("AUTHORIZATION_SESSION_UNAVAILABLE", "authorization session is unavailable")
        duration = max(1, int(kwargs.get("duration_seconds", 3600)))
        grant_expires_at = min(review.expires_at, now + duration)
        grant = authorization_session_authority.review_escalation_redemption(
            connection,
            token=kwargs.get("token"),
            context=context,
            signing_key=signing_key,
            audience=who.audience_id,
            purpose=purpose,
            membership=current_membership,
            policy_fingerprint=bound_policy.fingerprint,
            now=now,
            grant_expires_at=grant_expires_at,
        )
        token_row = connection.execute(
            "SELECT authorization_session_id, principal_id, issuer_family, audience, "
            "max_level, fingerprints, paths, scope_ids, purpose, org_ceiling, status, "
            "prepared_event_id, expires_at, minted_at, consumed_at FROM withhold_tokens "
            "WHERE jti=?",
            (grant.token_jti,),
        ).fetchone()
        if token_row is None:
            raise GovernanceError(
                "AUTHORIZATION_SESSION_UNAVAILABLE",
                "authorization session is unavailable",
            )
        nonce = uuid.uuid4().hex
        causation_id = receipts.critical_event_id(
            {
                "operation": "governance_session_grant",
                "jti": grant.token_jti,
                "authorization_session": context.session_id,
                "nonce": nonce,
            }
        )
        child_ids = [
            receipts.critical_event_id(
                {"causation_id": causation_id, "intent": f"child-{index}"}
            )
            for index, _receipt in enumerate(selection.child_receipts, start=1)
        ]
        prepared_grant = _v4_grant_projection(
            grant,
            status="prepared",
            prepared_event_id=causation_id,
        )
        phases = {
            "prior": [
                _component(
                    "token",
                    grant.token_jti,
                    _v4_token_projection(
                        token_row,
                        status="active",
                        prepared_event_id=None,
                        consumed_at=None,
                    ),
                    status="active",
                ),
                _component("grant", grant.grant_id, {"status": "absent"}, status="absent"),
            ],
            "prepared": [
                _component(
                    "token",
                    grant.token_jti,
                    _v4_token_projection(
                        token_row,
                        status="prepared",
                        prepared_event_id=causation_id,
                        consumed_at=now,
                    ),
                    status="prepared",
                ),
                _component("grant", grant.grant_id, prepared_grant, status="prepared"),
            ],
            "final": [
                _component(
                    "token",
                    grant.token_jti,
                    _v4_token_projection(
                        token_row,
                        status="consumed",
                        prepared_event_id=None,
                        consumed_at=now,
                    ),
                    status="consumed",
                ),
                _component(
                    "grant",
                    grant.grant_id,
                    {**prepared_grant, "status": "active", "prepared_event_id": None},
                    status="active",
                ),
            ],
        }
        digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
        affected = sorted(
            hashlib.sha256(
                f"{component['component_kind']}:{component['component_key']}".encode()
            ).hexdigest()
            for component in phases["prepared"]
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            persisted = _create_journal(
                connection,
                event_id=causation_id,
                operation=selection.journal_operation,
                who=who,
                authorization_session=context.session_id,
                direction="widening",
                phases=phases,
                child_ids=child_ids,
                phase="allocating",
                now=now,
            )
            if persisted != digests:
                raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        for index, (child_id, child_operation) in enumerate(
            zip(child_ids, selection.child_receipts, strict=True), start=1
        ):
            receipts.begin_event(
                vault_root,
                operation=child_operation,
                prior=digests["prior"],
                prepared=digests["prepared"],
                target=digests["final"],
                affected_ids=affected,
                event_id=child_id,
                parent_causation_id=causation_id,
                intent_id=f"child-{index}",
            )
            if kwargs.get("crash_at") == f"after_child_intent:{index}":
                raise GovernanceCrash(f"after_child_intent:{index}")
        _arm_journal(vault_root, causation_id, now=now)
        authorization_session_authority.prepare_escalation_redemption(
            connection,
            token=kwargs.get("token"),
            context=context,
            signing_key=signing_key,
            audience=who.audience_id,
            purpose=purpose,
            membership=current_membership,
            policy_fingerprint=bound_policy.fingerprint,
            now=now,
            grant_expires_at=grant_expires_at,
            prepared_event_id=causation_id,
            expected_grant=grant,
        )
        if kwargs.get("crash_at") == "after_compound_state":
            raise GovernanceCrash("after_compound_state")
        for index, child_id in enumerate(child_ids, start=1):
            receipts.commit_event(vault_root, child_id, outcome="prepared")
            if kwargs.get("crash_at") == f"after_child_terminal:{index}":
                raise GovernanceCrash(f"after_child_terminal:{index}")
        _activate_event(vault_root, causation_id, remove_marker=False, now=now)
        return {
            "status": "committed",
            "causation_id": causation_id,
            "grant_id": grant.grant_id,
        }
    except authorization_session_lifecycle.AuthorizationSessionUnavailable:
        raise GovernanceError(
            "AUTHORIZATION_SESSION_UNAVAILABLE",
            "authorization session is unavailable",
        ) from None
    finally:
        connection.close()


def _single_sidecar_transition(
    vault_root: Path,
    *,
    operation: str,
    who: RequestPrincipal,
    authorization_session: str,
    phases: Mapping[str, list[dict[str, Any]]],
    prepare: Any,
    direction: str,
    now: float,
) -> str:
    event_id = receipts.critical_event_id(
        {
            "operation": operation,
            "authorization_session": authorization_session,
            "nonce": uuid.uuid4().hex,
            "prepared": _composite("prepared", phases["prepared"]),
        }
    )
    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    transition = operation_variant(operation)
    journal_operation = transition.journal_operation
    if journal_operation is None:
        raise GovernanceError("GOVERNANCE_BLOCKED", "operation has no journal identity")
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=transition.receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=sorted({
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for row in phases["prepared"]
        }),
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    prepare(event_id)
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return event_id


def _declare(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    if _principal(kwargs.get("principal")).verified_authorization_session is not None:
        return _declare_v4(vault_root, **kwargs)
    who, authorization_session = _require_authorization_session(
        kwargs.get("principal"), kwargs.get("authorization_session")
    )
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    purpose = str(kwargs.get("purpose") or "").strip()
    if not purpose or len(purpose) > 256:
        raise GovernanceError("INVALID_PURPOSE", "purpose is required")
    now = float(kwargs.get("now", time.time()))
    expires_at = now + max(1, int(kwargs.get("duration_seconds", 3600)))
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT authorization_session, principal_id, purpose, status, prepared_event_id, "
            "created_at, expires_at "
            "FROM governance_session_purpose "
            "WHERE authorization_session=?",
            (authorization_session,),
        ).fetchone()
    finally:
        conn.close()
    prior_value = (
        {"status": "absent"}
        if row is None
        else authorization_row(
            authorization_session=str(row[0]), principal_id=str(row[1]),
            purpose=str(row[2]), status=str(row[3]), prepared_event_id=row[4],
            created_at=float(row[5]), expires_at=float(row[6]),
        )
    )
    prior_purpose = (
        str(row[2])
        if row is not None and str(row[3]) == "active" and float(row[6]) >= now
        else None
    )
    direction = _purpose_direction(
        vault_root,
        audience=who.audience_id,
        before_purpose=prior_purpose,
        after_purpose=purpose,
    )
    phases = {
        "prior": [_component("purpose", authorization_session, prior_value, status="prior")],
        "prepared": [
            _component(
                "purpose",
                authorization_session,
                authorization_row(
                    authorization_session=authorization_session, principal_id=who.audience_id,
                    purpose=purpose, status="prepared", prepared_event_id="EVENT",
                    created_at=now, expires_at=expires_at,
                ),
                status="prepared",
            )
        ],
        "final": [
            _component(
                "purpose",
                authorization_session,
                authorization_row(
                    authorization_session=authorization_session, principal_id=who.audience_id,
                    purpose=purpose, status="active", prepared_event_id=None,
                    created_at=now, expires_at=expires_at,
                ),
                status="active",
            )
        ],
    }
    # Event ids are preallocated before final component rows; substitute the
    # stable id once, then persist the immutable component set.
    event_id = receipts.critical_event_id(
        {
            "operation": "declare",
            "authorization_session": authorization_session,
            "purpose": purpose,
            "nonce": uuid.uuid4().hex,
        }
    )
    phases["prepared"] = [
        _component(
            "purpose",
            authorization_session,
            authorization_row(
                authorization_session=authorization_session, principal_id=who.audience_id,
                purpose=purpose, status="prepared", prepared_event_id=event_id,
                created_at=now, expires_at=expires_at,
            ),
            status="prepared",
        )
    ]

    def prepare(prepared_event: str) -> None:
        conn = store.open_connection(vault_root)
        try:
            conn.execute(
                "INSERT INTO governance_session_purpose_staging "
                "(event_id, authorization_session, principal_id, purpose, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET authorization_session=excluded.authorization_session, "
                "principal_id=excluded.principal_id, purpose=excluded.purpose, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at",
                (
                    prepared_event,
                    authorization_session,
                    who.audience_id,
                    purpose,
                    now,
                    expires_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    # Inline the generic transition so the preallocated event id is retained.
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=kwargs["_selection"].journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=kwargs["_selection"].receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=sorted({
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for row in phases["prepared"]
        }),
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    prepare(event_id)
    if kwargs.get("crash_at") == "after_purpose_prepare":
        raise GovernanceCrash("after_purpose_prepare")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return {
        "status": "committed",
        "event_id": event_id,
        "purpose": purpose,
        "direction": direction,
    }


def _declare_v4(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who, context, _custody, connection, now = _v4_authority_inputs(vault_root, kwargs)
    purpose = str(kwargs.get("purpose") or "").strip()
    if not purpose or len(purpose) > 256:
        connection.close()
        raise GovernanceError("INVALID_PURPOSE", "purpose is required")
    expires_at = min(
        context.expires_at,
        now + max(1, int(kwargs.get("duration_seconds", 3600))),
    )
    try:
        prior = authorization_session_authority.active_session_purpose(
            connection,
            context=context,
            audience=who.audience_id,
            now=now,
        )
        direction = _purpose_direction(
            vault_root,
            audience=who.audience_id,
            before_purpose=prior,
            after_purpose=purpose,
        )
        authorization_session_authority.declare_purpose(
            connection,
            context=context,
            audience=who.audience_id,
            purpose=purpose,
            now=now,
            expires_at=expires_at,
        )
        return {
            "status": "committed",
            "purpose": purpose,
            "direction": direction,
        }
    except authorization_session_lifecycle.AuthorizationSessionUnavailable:
        raise GovernanceError(
            "AUTHORIZATION_SESSION_UNAVAILABLE",
            "authorization session is unavailable",
        ) from None
    finally:
        connection.close()


def _revoke(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    selection = kwargs["_selection"]
    if kwargs.get("scope") != "session":
        raise GovernanceError("INVALID_REVOKE_SCOPE", "scope must be session or standing")
    if _principal(kwargs.get("principal")).verified_authorization_session is not None:
        return _revoke_v4(vault_root, **kwargs)
    who, authorization_session = _require_authorization_session(
        kwargs.get("principal"), kwargs.get("authorization_session")
    )
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    now = float(kwargs.get("now", time.time()))
    conn = store.open_connection(vault_root)
    try:
        rows = conn.execute(
            "SELECT grant_id, authorization_session, audience, purpose, ceiling, paths, fingerprints, "
            "token_jti, status, prepared_event_id, created_at, expires_at, revoked_at, "
            "membership_manifest, policy_fingerprint FROM governance_session_grants WHERE authorization_session=? "
            "AND audience=? AND status='active' ORDER BY grant_id",
            (authorization_session, who.audience_id),
        ).fetchall()
    finally:
        conn.close()
    grant_values = {
        str(row[0]): _grant_projection(
            authorization_session=str(row[1]), audience=str(row[2]), purpose=row[3], ceiling=int(row[4]),
            paths=str(row[5]), fingerprints=str(row[6]), token_jti=str(row[7]), status=str(row[8]),
            prepared_event_id=row[9], created_at=float(row[10]), expires_at=float(row[11]),
            revoked_at=row[12], membership_manifest=str(row[13]), policy_fingerprint=str(row[14]),
        ) for row in rows
    }
    grant_ids = sorted(grant_values)
    if not grant_ids:
        return {"status": "committed", "revoked": 0}
    event_id = receipts.critical_event_id(
        {"operation": "revoke", "grants": grant_ids, "nonce": uuid.uuid4().hex}
    )
    phases = {
        "prior": [
            _component(
                "grant",
                grant_id,
                grant_values[grant_id],
                status="active",
            )
            for grant_id in grant_ids
        ],
        "prepared": [
            _component(
                "grant",
                grant_id,
                {**grant_values[grant_id], "status": "prepared_revoke", "prepared_event_id": event_id},
                status="prepared_revoke",
            )
            for grant_id in grant_ids
        ],
        "final": [
            _component(
                "grant",
                grant_id,
                {**grant_values[grant_id], "status": "revoked", "prepared_event_id": None, "revoked_at": now},
                status="revoked",
            )
            for grant_id in grant_ids
        ],
    }
    digests = {phase: _composite(phase, values) for phase, values in phases.items()}
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=selection.journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction="narrowing",
            phases=phases,
            child_ids=[event_id],
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=selection.receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=sorted({
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for row in phases["prepared"]
        }),
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    conn = store.open_connection(vault_root)
    try:
        conn.execute(
            "UPDATE governance_session_grants SET status='prepared_revoke', prepared_event_id=? "
            "WHERE authorization_session=? AND audience=? AND status='active'",
            (event_id, authorization_session, who.audience_id),
        )
        conn.commit()
    finally:
        conn.close()
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return {"status": "committed", "event_id": event_id, "revoked": len(grant_ids)}


def _revoke_v4(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who, context, _custody, connection, now = _v4_authority_inputs(vault_root, kwargs)
    try:
        revoked = authorization_session_authority.revoke_session_grants(
            connection,
            context=context,
            audience=who.audience_id,
            now=now,
        )
        return {"status": "committed", "revoked": revoked}
    except authorization_session_lifecycle.AuthorizationSessionUnavailable:
        raise GovernanceError(
            "AUTHORIZATION_SESSION_UNAVAILABLE",
            "authorization session is unavailable",
        ) from None
    finally:
        connection.close()


def _effective_transition_direction(
    vault_root: Path, documents: Mapping[str, str | None]
) -> str:
    """Classify a YAML transition from its complete effective decision lattice."""
    current = policy_module.load(vault_root)
    if current.blocked:
        return "widening"
    try:
        prospective = _prospective_policy(vault_root, documents)
    except (GovernanceError, OSError, TypeError, ValueError):
        return "widening"
    if prospective.blocked or current.release_grants != prospective.release_grants:
        return "widening"
    try:
        manifest = _membership_manifest(vault_root, current, prospective, set(documents))
        audiences = _compared_audiences(current, prospective)
        purposes = [
            None,
            *sorted(
                {
                    rule.purpose
                    for policy in (current, prospective)
                    for rule in policy.rules
                    if rule.purpose is not None
                }
            ),
        ]
        before: dict[str, tuple[int, str]] = {}
        after: dict[str, tuple[int, str]] = {}
        for row in manifest:
            rel = str(row["path"])
            current_scopes = _memberships_for_path(vault_root, vault_root / rel, current)
            prospective_scopes = _memberships_for_path(
                vault_root, vault_root / rel, prospective
            )
            for audience in audiences:
                for purpose in purposes:
                    key = f"{audience}:{purpose or '-'}:{rel}"
                    before[key] = _decision_direction_state(
                        decisions.decide(
                            current_scopes,
                            audience=audience,
                            purpose=purpose,
                            policy=current,
                        )
                    )
                    after[key] = _decision_direction_state(
                        decisions.decide(
                            prospective_scopes,
                            audience=audience,
                            purpose=purpose,
                            policy=prospective,
                        )
                    )
    except (GovernanceError, OSError, TypeError, ValueError):
        return "widening"
    return classify_transition_direction(before, after)


def _release_commit_reservation(
    vault_root: Path,
    *,
    proposal_id: str,
    prior_attempt: int,
    attempt_no: int,
    attempt_nonce: str,
    event_id: str,
) -> None:
    """Compensate a reservation when no commit journal can own it."""
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE governance_proposals SET attempt_no=?, attempt_nonce=NULL, "
            "reserved_event_id=NULL WHERE proposal_id=? AND status='pending' "
            "AND attempt_no=? AND attempt_nonce=? AND reserved_event_id=?",
            (
                prior_attempt,
                proposal_id,
                attempt_no,
                attempt_nonce,
                event_id,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _proposal_guard_value(fingerprint: str, manifest: str) -> dict[str, str]:
    try:
        parsed_manifest = json.loads(manifest)
    except (TypeError, ValueError) as exc:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL", "membership manifest is malformed"
        ) from exc
    if _canonical_json(parsed_manifest) != manifest:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL", "membership manifest is not canonical"
        )
    return {
        "fingerprint_at_propose": fingerprint,
        "membership_manifest_digest": _digest(parsed_manifest),
    }


def _prepare_commit_attempt(
    vault_root: Path,
    proposal_id: str,
    who: RequestPrincipal,
    *,
    direction: str,
    now: float,
    crash_at: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, str], list[str]]:
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id, created_at, spent_at "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
    proposal_json, fingerprint, manifest, status, expires_at, prior_attempt, reserved, created_at, spent_at = row
    payload = _validate_proposal_values(
        vault_root,
        proposal_json=str(proposal_json),
        fingerprint=str(fingerprint),
        manifest=str(manifest),
        status=str(status),
        expires_at=float(expires_at),
        now=now,
    )
    if reserved:
        raise GovernanceError("PROPOSAL_RESERVED", "proposal has an open attempt")
    documents = dict(payload["documents"])
    attempt_no = int(prior_attempt) + 1
    attempt_nonce = uuid.uuid4().hex
    root = policy_module.governance_root(vault_root)
    _marker_path(vault_root)
    targets = {rel: policy_target(root, rel) for rel in documents}
    prior_hashes = {rel: _content_hash(targets[rel]) for rel in documents}
    prior_bytes_by_rel: dict[str, bytes | None] = {}
    for rel in documents:
        try:
            prior_bytes_by_rel[rel] = targets[rel].read_bytes()
        except FileNotFoundError:
            prior_bytes_by_rel[rel] = None
    target_hashes = {
        rel: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for rel, content in documents.items()
    }
    prior_components = [
        _component("yaml", rel, {"hash": prior_hashes[rel]}, status="prior")
        for rel in documents
    ]
    prepared_components = [
        _component("yaml", rel, {"hash": target_hashes[rel]}, status="prepared")
        for rel in documents
    ]
    final_components = [
        _component("yaml", rel, {"hash": target_hashes[rel]}, status="active")
        for rel in documents
    ]
    archive_ids = {
        rel: hashlib.sha256(f"{attempt_nonce}:{rel}".encode()).hexdigest()
        for rel in documents
    }
    for rel in documents:
        archive_component = _component(
            "archive",
            archive_ids[rel],
            _archive_value(rel, prior_bytes_by_rel[rel], prior_hashes[rel]),
            status="archived",
        )
        prior_components.append(archive_component)
        prepared_components.append(archive_component)
        final_components.append(archive_component)
    reserved_proposal = authorization_row(
        proposal_json=str(proposal_json), fingerprint_at_propose=str(fingerprint),
        membership_manifest=str(manifest), status="pending", expires_at=float(expires_at),
        attempt_no=attempt_no, attempt_nonce=attempt_nonce, reserved_event_id="SELF_EVENT",
        created_at=float(created_at), spent_at=None,
    )
    final_proposal = {**reserved_proposal, "status": "spent", "reserved_event_id": None, "spent_at": now}
    prior_components.append(_component("proposal", proposal_id, reserved_proposal, status="pending"))
    prepared_components.append(_component("proposal", proposal_id, reserved_proposal, status="pending"))
    final_components.append(_component("proposal", proposal_id, final_proposal, status="spent"))
    proposal_guard = _proposal_guard_value(str(fingerprint), str(manifest))
    prepared_components.append(
        _component("proposal_guard", proposal_id, proposal_guard, status="prepared")
    )
    final_components.append(
        _component("proposal_guard", proposal_id, proposal_guard, status="active")
    )
    phases = {
        "prior": prior_components,
        "prepared": prepared_components,
        "final": final_components,
    }
    digests = {name: _composite(name, values) for name, values in phases.items()}
    event_id = _commit_event_id(
        proposal_id, attempt_no, attempt_nonce, digests["prepared"]
    )
    affected = sorted(
        hashlib.sha256(
            f"{component['component_kind']}:{component['component_key']}".encode()
        ).hexdigest()
        for component in prepared_components
    )
    transition = operation_variant("commit")
    journal_operation = transition.journal_operation
    if journal_operation is None:
        raise GovernanceError("GOVERNANCE_BLOCKED", "operation has no journal identity")
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if live is None:
            raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
        live_payload = _validate_proposal_values(
            vault_root,
            proposal_json=str(live[0]),
            fingerprint=str(live[1]),
            manifest=str(live[2]),
            status=str(live[3]),
            expires_at=float(live[4]),
            now=now,
        )
        if live[6] or int(live[5]) != int(prior_attempt) or live_payload != payload:
            raise GovernanceError("PROPOSAL_RESERVED", "proposal changed during commit")
        reserved_row = conn.execute(
            "UPDATE governance_proposals SET attempt_no=?, attempt_nonce=?, "
            "reserved_event_id=? WHERE proposal_id=? AND status='pending' "
            "AND attempt_no=? AND attempt_nonce IS NULL AND reserved_event_id IS NULL",
            (
                attempt_no,
                attempt_nonce,
                event_id,
                proposal_id,
                prior_attempt,
            ),
        )
        if reserved_row.rowcount != 1:
            raise GovernanceError("PROPOSAL_RESERVED", "proposal changed during commit")
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=journal_operation,
            who=who,
            authorization_session=None,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            marker_required=transition.yaml_marker,
            proposal_id=proposal_id,
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        for rel in sorted(documents):
            conn.execute(
                "INSERT OR IGNORE INTO governance_policy_archives "
                "(archive_id, event_id, path, prior_bytes, prior_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    archive_ids[rel],
                    event_id,
                    rel,
                    prior_bytes_by_rel[rel],
                    prior_hashes[rel],
                    now,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if crash_at == "after_reservation":
        raise GovernanceCrash("after_reservation")

    try:
        receipts.begin_event(
            vault_root,
            operation=transition.receipt_event,
            prior=digests["prior"],
            prepared=digests["prepared"],
            target=digests["final"],
            affected_ids=affected,
            event_id=event_id,
        )
    except BaseException:
        raise
    if crash_at == "after_intent_before_journal":
        raise GovernanceCrash("after_intent_before_journal")

    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, attempt_nonce, reserved_event_id "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if (
            live is None
            or str(live[0]) != str(proposal_json)
            or str(live[1]) != str(fingerprint)
            or str(live[2]) != str(manifest)
            or str(live[3]) != "pending"
            or float(live[4]) != float(expires_at)
            or int(live[5]) != attempt_no
            or str(live[6]) != attempt_nonce
            or str(live[7]) != event_id
        ):
            raise GovernanceError("PROPOSAL_RESERVED", "commit reservation changed")
        child_ids = [event_id]
        conn.execute(
            "INSERT OR IGNORE INTO governance_operation_journals "
            "(event_id, operation, causation_id, authorization_session, principal_id, phase, "
            "direction, prior_digest, prepared_digest, final_digest, affected_ids, "
            "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
            "marker_required, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                transition.journal_operation,
                event_id,
                who.audience_id,
                direction,
                digests["prior"],
                digests["prepared"],
                digests["final"],
                _canonical_json(affected),
                _canonical_json(child_ids),
                _canonical_json([f"{event_id}:committed"]),
                proposal_id,
                attempt_no,
                1 if transition.yaml_marker else 0,
                now,
                now,
            ),
        )
        _insert_components(conn, event_id, phases)
        for rel in sorted(documents):
            conn.execute(
                "INSERT OR IGNORE INTO governance_policy_archives "
                "(archive_id, event_id, path, prior_bytes, prior_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    archive_ids[rel],
                    event_id,
                    rel,
                    prior_bytes_by_rel[rel],
                    prior_hashes[rel],
                    now,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        try:
            receipts.abort_event(vault_root, event_id, outcome="reservation_refused")
        except receipts.ReceiptError:
            pass
        _release_commit_reservation(
            vault_root,
            proposal_id=proposal_id,
            prior_attempt=int(prior_attempt),
            attempt_no=attempt_no,
            attempt_nonce=attempt_nonce,
            event_id=event_id,
        )
        raise
    finally:
        conn.close()
    try:
        _validate_proposal_drift(vault_root, proposal_id, now=now)
    except GovernanceError as exc:
        if exc.code != "STALE_GOVERNANCE_POLICY":
            raise
        reconciliation = reconcile_governance_operations(vault_root)
        conn = store.open_connection(vault_root)
        try:
            cleaned = conn.execute(
                "SELECT phase FROM governance_operation_journals WHERE event_id=?",
                (event_id,),
            ).fetchone() == ("aborted",)
            released = conn.execute(
                "SELECT reserved_event_id, attempt_nonce FROM governance_proposals "
                "WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone() == (None, None)
        finally:
            conn.close()
        if reconciliation["blocked"] or not cleaned or not released:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED", "stale commit intent could not be cleaned exactly"
            ) from exc
        raise
    _arm_journal(vault_root, event_id, now=now)
    return event_id, payload, digests, affected


def _proposal_matches_exact_prior(
    vault_root: Path,
    *,
    proposal_json: str,
    fingerprint: str,
    manifest: str,
) -> bool:
    """Classify an orphan reservation only when its exact prior still holds."""
    try:
        payload = json.loads(proposal_json)
        documents = dict(payload["documents"])
        current_policy = policy_module.load(vault_root)
        if current_policy.blocked:
            return False
        prospective = _prospective_policy(vault_root, documents)
        if prospective.blocked:
            return False
        current_manifest = _membership_manifest(
            vault_root, current_policy, prospective, set(documents)
        )
        return (
            current_policy.fingerprint == fingerprint
            and _canonical_json(current_manifest) == manifest
        )
    except (GovernanceError, KeyError, TypeError, ValueError):
        return False


def _validate_proposal_values(
    vault_root: Path,
    *,
    proposal_json: str,
    fingerprint: str,
    manifest: str,
    status: str,
    expires_at: float,
    now: float,
) -> dict[str, Any]:
    if status == "spent":
        raise GovernanceError("PROPOSAL_SPENT", "proposal was already activated")
    if expires_at < now:
        raise GovernanceError("PROPOSAL_EXPIRED", "proposal expired")
    payload = json.loads(proposal_json)
    current_policy = policy_module.load(vault_root)
    if current_policy.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    documents = dict(payload["documents"])
    try:
        prospective = _prospective_policy(vault_root, documents)
    except GovernanceError as exc:
        if exc.code != "GOVERNANCE_AUTHORING_UNSTABLE":
            raise
        raise GovernanceError(
            "INVALID_GOVERNANCE_TARGET",
            "the policy workspace cannot be acquired through stable regular-file identities",
        ) from exc
    if prospective.blocked:
        raise GovernanceError(
            "INVALID_GOVERNANCE_POLICY", _canonical_json(list(prospective.findings))
        )
    current_manifest = _membership_manifest(
        vault_root, current_policy, prospective, set(documents)
    )
    if current_policy.fingerprint != fingerprint or _canonical_json(current_manifest) != manifest:
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY", "policy or exact affected membership changed"
        )
    return payload


def _validate_proposal_drift(vault_root: Path, proposal_id: str, *, now: float) -> None:
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, expires_at "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
    _validate_proposal_values(
        vault_root,
        proposal_json=str(row[0]),
        fingerprint=str(row[1]),
        manifest=str(row[2]),
        status=str(row[3]),
        expires_at=float(row[4]),
        now=now,
    )


def _decoded_bound_documents(value: Any) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(value, list):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "bound policy source documents are malformed",
        )
    documents: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "bytes"}:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "bound policy source documents are malformed",
            )
        relative = item["path"]
        encoded = item["bytes"]
        if (
            not isinstance(relative, str)
            or not isinstance(encoded, str)
            or relative in seen
        ):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "bound policy source documents are malformed",
            )
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "bound policy source documents are malformed",
            ) from None
        if base64.b64encode(content).decode("ascii") != encoded:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "bound policy source documents are non-canonical",
            )
        seen.add(relative)
        documents.append((relative, content))
    ordered = tuple(sorted(documents))
    if tuple(documents) != ordered:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "bound policy source documents are not ordered",
        )
    return ordered


def _v4_mirror_relative_path(relative: str) -> bool:
    path = Path(relative)
    return (
        not path.is_absolute()
        and len(path.parts) == 2
        and path.parts[0] in {"scopes", "rules", "grants"}
        and path.parts[1] not in {"", ".", ".."}
        and path.parts[1].endswith(".yaml")
        and relative == path.as_posix()
    )


def _decoded_stable_identity(value: object, *, kind: str) -> held_fs.StableIdentity:
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "kind",
        "link_count",
    }:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring identity is invalid",
        )
    integer_fields = ("device", "inode", "link_count")
    if (
        any(
            isinstance(value[field], bool)
            or not isinstance(value[field], int)
            or value[field] < 0
            for field in integer_fields
        )
        or value["link_count"] < 1
        or value["kind"] != kind
        or (kind == "file" and value["link_count"] != 1)
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring identity is invalid",
        )
    return held_fs.StableIdentity(
        device=value["device"],
        inode=value["inode"],
        kind=value["kind"],
        link_count=value["link_count"],
    )


def _decoded_v4_authoring_snapshot(value: object) -> policy_module.AuthoringSnapshot:
    expected_fields = {
        "documents",
        "source_fingerprint",
        "conflict_set_digest",
        "guard_generation",
        "file_identities",
        "directory_identities",
        "governance_root_identity",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring snapshot is invalid",
        )
    documents = _decoded_bound_documents(value["documents"])
    document_map = dict(documents)
    if any(not _v4_mirror_relative_path(relative) for relative in document_map):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring path is invalid",
        )
    raw_identities = value["file_identities"]
    if not isinstance(raw_identities, list):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring identities are invalid",
        )
    identities: list[policy_module.AuthoringFileIdentity] = []
    seen: set[str] = set()
    for item in raw_identities:
        if not isinstance(item, dict) or set(item) != {"path", "identity", "sha256"}:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored governance authoring identities are invalid",
            )
        relative = item["path"]
        digest = item["sha256"]
        if (
            not isinstance(relative, str)
            or relative in seen
            or relative not in document_map
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or hashlib.sha256(document_map[relative]).hexdigest() != digest
        ):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored governance authoring identities are invalid",
            )
        seen.add(relative)
        identities.append(
            policy_module.AuthoringFileIdentity(
                path=relative,
                identity=_decoded_stable_identity(item["identity"], kind="file"),
                sha256=digest,
            )
        )
    if tuple(item.path for item in identities) != tuple(sorted(document_map)):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring identities are incomplete",
        )
    raw_directories = value["directory_identities"]
    if not isinstance(raw_directories, list):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring directories are invalid",
        )
    directories: list[tuple[str, held_fs.StableIdentity]] = []
    seen_directories: set[str] = set()
    for item in raw_directories:
        if not isinstance(item, dict) or set(item) != {"path", "identity"}:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored governance authoring directories are invalid",
            )
        relative = item["path"]
        path = Path(relative) if isinstance(relative, str) else None
        if (
            path is None
            or path.is_absolute()
            or relative != path.as_posix()
            or any(part in {"", ".", ".."} for part in path.parts)
            or relative in seen_directories
        ):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored governance authoring directories are invalid",
            )
        seen_directories.add(relative)
        directories.append(
            (
                relative,
                _decoded_stable_identity(item["identity"], kind="directory"),
            )
        )
    if tuple(relative for relative, _ in directories) != tuple(
        sorted(seen_directories)
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring directories are not ordered",
        )
    root_value = value["governance_root_identity"]
    root_identity = (
        None
        if root_value is None
        else _decoded_stable_identity(root_value, kind="directory")
    )
    if (root_identity is None) != (not documents):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring root is invalid",
        )
    source_fingerprint = value["source_fingerprint"]
    conflict_digest = value["conflict_set_digest"]
    guard_generation = value["guard_generation"]
    compiled = policy_module.compile_documents(document_map)
    if (
        not isinstance(source_fingerprint, str)
        or _SHA256_RE.fullmatch(source_fingerprint) is None
        or source_fingerprint != compiled.fingerprint
        or not isinstance(conflict_digest, str)
        or _SHA256_RE.fullmatch(conflict_digest) is None
        or not isinstance(guard_generation, str)
        or not guard_generation
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authoring snapshot is invalid",
        )
    return policy_module.AuthoringSnapshot(
        documents=documents,
        source_fingerprint=source_fingerprint,
        conflict_set_digest=conflict_digest,
        guard_generation=guard_generation,
        file_identities=tuple(identities),
        directory_identities=tuple(directories),
        governance_root_identity=root_identity,
    )


def _reviewed_active_state(value: object) -> schema_v4.VerifiedActiveGovernanceState:
    expected_keys = {
        "logical_vault_id",
        "activation_store_id",
        "activation_epoch",
        "activation_state_digest",
        "policy_generation_id",
        "policy_fingerprint",
        "projector_schema_version",
        "catalog_generation",
        "projection_namespace_id",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "reviewed active tuple is malformed",
        )
    text_fields = (
        "logical_vault_id",
        "activation_store_id",
        "policy_generation_id",
        "projection_namespace_id",
    )
    digest_fields = ("activation_state_digest", "policy_fingerprint")
    integer_fields = (
        "activation_epoch",
        "projector_schema_version",
        "catalog_generation",
    )
    if (
        any(not isinstance(value[field], str) or not value[field] for field in text_fields)
        or any(
            not isinstance(value[field], str)
            or _SHA256_RE.fullmatch(value[field]) is None
            for field in digest_fields
        )
        or any(
            isinstance(value[field], bool)
            or not isinstance(value[field], int)
            or value[field] <= 0
            for field in integer_fields
        )
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "reviewed active tuple is malformed",
        )
    return schema_v4.VerifiedActiveGovernanceState(**value)


def _decoded_dependent_grants(
    value: object,
) -> tuple[schema_v4.DependentGrantTransition, ...]:
    if not isinstance(value, list):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored dependent grant manifest is malformed",
        )
    expected_keys = {
        "grant_id",
        "expected_policy_fingerprint",
        "expected_membership_manifest",
        "target_status",
        "target_policy_fingerprint",
        "target_membership_manifest",
    }
    transitions: list[schema_v4.DependentGrantTransition] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored dependent grant manifest is malformed",
            )
        transitions.append(schema_v4.DependentGrantTransition(**item))
    ordered = tuple(sorted(transitions, key=lambda transition: transition.grant_id))
    if tuple(transitions) != ordered or _dependent_grant_value(ordered) != value:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored dependent grant manifest is malformed",
        )
    return ordered


def _decode_v4_proposal_binding(
    vault_root: Path,
    *,
    proposal_id: str,
    proposal_json: str,
    membership_manifest: str,
    created_at: float,
    verify_projection_store: bool = True,
) -> _DecodedV4PolicyProposal:
    try:
        payload = json.loads(proposal_json)
    except (TypeError, json.JSONDecodeError):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance proposal is malformed",
        ) from None
    if not isinstance(payload, dict) or _canonical_json(payload) != proposal_json:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance proposal is not canonical",
        )
    binding = payload.get("authority_binding")
    schema = binding.get("schema") if isinstance(binding, dict) else None
    expected_binding_keys = {
            "schema",
            "transition_direction",
            "reviewed_active_tuple",
            "authoring_snapshot",
            "membership_manifest",
            "target",
    }
    if schema == _V4_POLICY_PROPOSAL_SCHEMA:
        expected_binding_keys.add("dependent_grants")
    if (
        not isinstance(binding, dict)
        or set(binding) != expected_binding_keys
        or schema not in {_V4_POLICY_PROPOSAL_SCHEMA_V3, _V4_POLICY_PROPOSAL_SCHEMA}
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance authority binding is invalid",
        )
    try:
        parsed_manifest = json.loads(membership_manifest)
    except (TypeError, json.JSONDecodeError):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance membership is malformed",
        ) from None
    if (
        _canonical_json(parsed_manifest) != membership_manifest
        or binding["membership_manifest"] != parsed_manifest
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance membership does not match its authority binding",
        )
    expected = _reviewed_active_state(binding["reviewed_active_tuple"])
    direction = binding["transition_direction"]
    if direction not in {"narrowing", "widening"}:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance transition direction is invalid",
        )
    dependent_grants = (
        None
        if schema == _V4_POLICY_PROPOSAL_SCHEMA_V3
        else _decoded_dependent_grants(binding["dependent_grants"])
    )

    target = binding["target"]
    expected_target_keys = {
        "source_documents",
        "generation_id",
        "authoring_event_id",
        "receipt_event_id",
        "source_fingerprint",
        "policy_fingerprint",
        "compiled_policy",
        "compiler_schema_version",
        "projection_rows_digest",
        "projection_namespace",
    }
    if schema == _V4_POLICY_PROPOSAL_SCHEMA:
        expected_target_keys.add("catalog")
    if not isinstance(target, dict) or set(target) != expected_target_keys:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance target is invalid",
        )
    target_documents = _decoded_bound_documents(target["source_documents"])
    if any(not _v4_mirror_relative_path(relative) for relative, _ in target_documents):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance target path is invalid",
        )
    compiled = policy_module.compile_documents(dict(target_documents))
    try:
        compiled_bytes = base64.b64decode(target["compiled_policy"], validate=True)
    except (TypeError, ValueError):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored compiled policy is malformed",
        ) from None
    if (
        compiled.empty
        or compiled.blocked
        or target["source_fingerprint"] != compiled.fingerprint
        or target["policy_fingerprint"] != compiled.fingerprint
        or target["compiler_schema_version"] != 1
        or compiled_bytes != policy_module.canonical_compiled_bytes(compiled)
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored compiled policy does not verify",
        )
    target_without_identities = {
        key_name: value
        for key_name, value in target.items()
        if key_name not in {"generation_id", "authoring_event_id", "receipt_event_id"}
    }
    review_binding = {
        key_name: (target_without_identities if key_name == "target" else value)
        for key_name, value in binding.items()
    }
    expected_identities = _policy_publication_identities(
        proposal_id=proposal_id,
        created_at=created_at,
        review_digest=_digest(review_binding),
    )
    if (
        (target["generation_id"], target["authoring_event_id"], target["receipt_event_id"])
        != expected_identities
        or _ULID_RE.fullmatch(target["generation_id"]) is None
        or _SHA256_RE.fullmatch(target["authoring_event_id"]) is None
        or _SHA256_RE.fullmatch(target["receipt_event_id"]) is None
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance publication identities do not verify",
        )
    catalog_value = target.get("catalog")
    catalog: schema_v4.CatalogGenerationSeed | None
    if catalog_value is None:
        catalog = None
        target_catalog_generation = expected.catalog_generation
    else:
        if not isinstance(catalog_value, dict) or set(catalog_value) != {
            "catalog_generation",
            "descriptor",
            "artifact_count",
            "created_at",
        }:
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored target catalog is invalid",
            )
        try:
            descriptor = base64.b64decode(
                catalog_value["descriptor"],
                validate=True,
            )
        except (TypeError, ValueError):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored target catalog is invalid",
            ) from None
        target_catalog_generation = catalog_value["catalog_generation"]
        if (
            isinstance(target_catalog_generation, bool)
            or not isinstance(target_catalog_generation, int)
            or target_catalog_generation != expected.catalog_generation + 1
            or isinstance(catalog_value["artifact_count"], bool)
            or not isinstance(catalog_value["artifact_count"], int)
            or catalog_value["artifact_count"] < 0
            or isinstance(catalog_value["created_at"], bool)
            or not isinstance(catalog_value["created_at"], int)
            or catalog_value["created_at"] < 0
        ):
            raise GovernanceError(
                "INVALID_GOVERNANCE_PROPOSAL",
                "stored target catalog is invalid",
            )
        catalog = schema_v4.CatalogGenerationSeed(
            catalog_generation=target_catalog_generation,
            descriptor=descriptor,
            artifact_count=catalog_value["artifact_count"],
            created_at=catalog_value["created_at"],
        )

    namespace = target["projection_namespace"]
    if not isinstance(namespace, dict) or set(namespace) != {
        "namespace_id",
        "projector_schema_version",
        "catalog_generation",
        "evidence",
        "ready_at",
    }:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored projection namespace is invalid",
        )
    if (
        not isinstance(target["projection_rows_digest"], str)
        or _SHA256_RE.fullmatch(target["projection_rows_digest"]) is None
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored projection row digest is invalid",
        )
    try:
        key = projections.ProjectionNamespaceKey(
            policy_fingerprint=compiled.fingerprint,
            projector_schema_version=namespace["projector_schema_version"],
            catalog_generation=namespace["catalog_generation"],
        )
        stored_evidence = base64.b64decode(namespace["evidence"], validate=True)
        if verify_projection_store:
            manifest, target_items = projection_store.load_projection_catalog(
                vault_root,
                key=key,
                expected_rows_digest=target["projection_rows_digest"],
            )
            expected_evidence = projection_store.projection_namespace_evidence_bytes(
                manifest
            )
        else:
            target_items = ()
            expected_evidence = stored_evidence
    except (
        projection_store.ProjectionStoreError,
        projections.ProjectionError,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        raise GovernanceError(
            "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
            "the reviewed target projection namespace does not verify",
        ) from None
    if (
        namespace["namespace_id"] != key.namespace_id
        or namespace["catalog_generation"] != target_catalog_generation
        or namespace["projector_schema_version"]
        != expected.projector_schema_version
        or stored_evidence != expected_evidence
    ):
        raise GovernanceError(
            "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
            "the reviewed target projection namespace does not verify",
        )
    if verify_projection_store:
        target_catalog_descriptor = projection_store.catalog_descriptor_bytes(
            key,
            target_items,
        )
        if catalog is not None:
            if (
                catalog.artifact_count != len(target_items)
                or not __import__("hmac").compare_digest(
                    catalog.descriptor,
                    target_catalog_descriptor,
                )
            ):
                raise GovernanceError(
                    "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
                    "the reviewed target catalog does not verify",
                )
        else:
            connection = store.open_authorization_session_connection(vault_root)
            try:
                catalog_row = connection.execute(
                    "SELECT descriptor FROM catalog_generation_descriptors "
                    "WHERE catalog_generation=?",
                    (expected.catalog_generation,),
                ).fetchone()
            finally:
                connection.close()
            if catalog_row is None or not __import__("hmac").compare_digest(
                bytes(catalog_row[0]),
                target_catalog_descriptor,
            ):
                raise GovernanceError(
                    "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
                    "the reviewed catalog reuse does not verify",
                )
    snapshot = _decoded_v4_authoring_snapshot(binding["authoring_snapshot"])
    return _DecodedV4PolicyProposal(
        payload=payload,
        expected=expected,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id=target["generation_id"],
            source_documents=target_documents,
            source_fingerprint=target["source_fingerprint"],
            conflict_digest=snapshot.conflict_set_digest,
            compiled_policy=compiled_bytes,
            policy_fingerprint=target["policy_fingerprint"],
            compiler_schema_version=target["compiler_schema_version"],
            projector_schema_version=namespace["projector_schema_version"],
            predecessor_generation_id=expected.policy_generation_id,
            authoring_event_id=target["authoring_event_id"],
            receipt_event_id=target["receipt_event_id"],
            created_at=int(created_at),
        ),
        catalog=catalog,
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id=namespace["namespace_id"],
            evidence=stored_evidence,
            ready_at=namespace["ready_at"],
        ),
        direction=direction,
        authoring_snapshot=snapshot,
        authoring_snapshot_value=binding["authoring_snapshot"],
        dependent_grants=dependent_grants,
    )


def _validate_v4_proposal_binding(
    vault_root: Path,
    *,
    proposal_id: str,
    proposal_json: str,
    membership_manifest: str,
    created_at: float,
    now: int,
) -> _ValidatedV4PolicyProposal:
    decoded = _decode_v4_proposal_binding(
        vault_root,
        proposal_id=proposal_id,
        proposal_json=proposal_json,
        membership_manifest=membership_manifest,
        created_at=created_at,
    )
    custody, active_snapshot = _v4_active_authority_snapshot(vault_root, now=now)
    if decoded.expected != active_snapshot.active:
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed active policy tuple changed",
        )
    documents = decoded.payload.get("documents")
    if not isinstance(documents, dict):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL",
            "stored governance documents are malformed",
        )
    prospective = policy_module.compile_prospective(
        vault_root,
        documents,
        _replace_document_set=decoded.payload.get("semantic_operation")
        in {"suspend", "resume", "undo"},
    )
    if prospective is None:
        raise GovernanceError(
            "GOVERNANCE_AUTHORING_UNSTABLE",
            "the policy workspace changed or could not be acquired safely",
        )
    binding = decoded.payload["authority_binding"]
    if _snapshot_value(decoded.authoring_snapshot) != _snapshot_value(
        prospective.snapshot
    ):
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed policy workspace changed",
        )
    if decoded.policy.source_documents != prospective.target_documents:
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed immutable policy target changed",
        )
    staged_projection = _stage_target_projection_namespace(
        vault_root,
        active_snapshot=active_snapshot,
        target_policy=prospective.policy,
        ready_at=decoded.namespace.ready_at,
    )
    staged_namespace = dict(staged_projection.namespace)
    staged_rows_digest = staged_namespace.pop("projection_rows_digest")
    target_binding = binding["target"]
    if (
        _catalog_seed_value(staged_projection.catalog) != target_binding.get("catalog")
        or staged_namespace != target_binding["projection_namespace"]
        or staged_rows_digest != target_binding["projection_rows_digest"]
    ):
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed policy catalog target changed",
        )
    current_manifest = _membership_manifest(
        vault_root,
        active_snapshot.policy,
        prospective.policy,
        set(documents),
    )
    if current_manifest != binding["membership_manifest"]:
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed affected membership changed",
        )
    recomputed_direction = _proposal_analysis(
        vault_root,
        active_snapshot.policy,
        prospective.policy,
        current_manifest,
    )[2]
    if recomputed_direction != decoded.direction:
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed transition direction changed",
        )
    current_dependent_grants = _v4_dependent_grant_transitions(
        vault_root,
        current_policy=active_snapshot.policy,
        target_policy=prospective.policy,
        predecessor_items=staged_projection.predecessor_items,
        target_items=staged_projection.items,
    )
    if (
        decoded.dependent_grants is None
        and current_dependent_grants
    ) or (
        decoded.dependent_grants is not None
        and current_dependent_grants != decoded.dependent_grants
    ):
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed dependent grant manifest changed",
        )
    return _ValidatedV4PolicyProposal(decoded, custody)


def _control_matches_active(
    control: authorization_custody.AuthorizationControlRecord,
    active: schema_v4.VerifiedActiveGovernanceState,
) -> bool:
    return (
        control.governance_enrolled
        and control.logical_vault_id == active.logical_vault_id
        and control.activation_store_id == active.activation_store_id
        and control.activation_epoch == active.activation_epoch
        and control.activation_state_digest == active.activation_state_digest
    )


def _committed_v4_policy_target(
    connection: sqlite3.Connection,
    decoded: _DecodedV4PolicyProposal,
) -> schema_v4.VerifiedActiveGovernanceState | None:
    target_catalog_generation = (
        decoded.expected.catalog_generation
        if decoded.catalog is None
        else decoded.catalog.catalog_generation
    )
    row = connection.execute(
        "SELECT publication_kind, predecessor_activation_state_digest, "
        "target_activation_state_digest, policy_generation_id, policy_fingerprint, "
        "projector_schema_version, catalog_generation, activation_epoch, status "
        "FROM governance_tuple_publications WHERE event_id=?",
        (decoded.policy.receipt_event_id,),
    ).fetchone()
    if row is None:
        return None
    target = schema_v4.load_active_tuple_pointer(connection)
    if (
        tuple(row)
        != (
            "policy",
            decoded.expected.activation_state_digest,
            target.activation_state_digest,
            decoded.policy.generation_id,
            decoded.policy.policy_fingerprint,
            decoded.policy.projector_schema_version,
            target_catalog_generation,
            decoded.expected.activation_epoch + 1,
            "committed",
        )
        or target.logical_vault_id != decoded.expected.logical_vault_id
        or target.activation_store_id != decoded.expected.activation_store_id
        or target.activation_epoch != decoded.expected.activation_epoch + 1
        or target.policy_generation_id != decoded.policy.generation_id
        or target.policy_fingerprint != decoded.policy.policy_fingerprint
        or target.projector_schema_version != decoded.policy.projector_schema_version
        or target.catalog_generation != target_catalog_generation
        or target.projection_namespace_id != decoded.namespace.namespace_id
    ):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "committed policy publication does not match the reviewed proposal",
        )
    active_count = connection.execute(
        "SELECT COUNT(*) FROM governance_session_grants WHERE status='active'"
    ).fetchone()
    if decoded.dependent_grants is not None:
        committed_grants = connection.execute(
            "SELECT grant_id, status, policy_fingerprint, membership_manifest, "
            "prepared_event_id FROM governance_session_grants "
            "WHERE grant_id IN ("
            + ",".join("?" for _ in decoded.dependent_grants)
            + ") ORDER BY grant_id",
            tuple(transition.grant_id for transition in decoded.dependent_grants),
        ).fetchall() if decoded.dependent_grants else []
        expected_grants = [
            (
                transition.grant_id,
                transition.target_status,
                transition.target_policy_fingerprint,
                transition.target_membership_manifest,
                None,
            )
            for transition in decoded.dependent_grants
        ]
        if committed_grants != expected_grants or active_count != (0,):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "committed dependent grants do not match the reviewed proposal",
            )
    elif active_count != (0,):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "legacy policy publication did not bind the active dependent grants",
        )
    return target


def _verify_recorded_v4_policy_terminal(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
) -> None:
    """Verify one historical committed terminal without reopening live authority.

    Exact request retries may arrive after a later publication or projection
    GC.  Their response is fixed by the retained proposal plus committed
    publication/receipt/mirror evidence; replay must not require the historical
    tuple to still be active.
    """

    target_catalog_generation = (
        decoded.expected.catalog_generation
        if decoded.catalog is None
        else decoded.catalog.catalog_generation
    )
    connection = store.open_authorization_session_connection(vault_root)
    try:
        publication = connection.execute(
            "SELECT predecessor_activation_state_digest, target_activation_state_digest, "
            "policy_generation_id, policy_fingerprint, projector_schema_version, "
            "catalog_generation, activation_epoch, status FROM governance_tuple_publications "
            "WHERE event_id=? AND publication_kind='policy'",
            (decoded.policy.receipt_event_id,),
        ).fetchone()
        if (
            publication is None
            or publication[0] != decoded.expected.activation_state_digest
            or not isinstance(publication[1], str)
            or _SHA256_RE.fullmatch(publication[1]) is None
            or tuple(publication[2:])
            != (
                decoded.policy.generation_id,
                decoded.policy.policy_fingerprint,
                decoded.policy.projector_schema_version,
                target_catalog_generation,
                decoded.expected.activation_epoch + 1,
                "committed",
            )
            or schema_v4.load_policy_generation(
                connection,
                decoded.policy.generation_id,
            )
            != decoded.policy
        ):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "historical policy publication terminal does not verify",
            )
        namespace = connection.execute(
            "SELECT namespace_id, evidence, ready_at FROM governance_projection_namespaces "
            "WHERE policy_fingerprint=? AND projector_schema_version=? "
            "AND catalog_generation=?",
            (
                decoded.policy.policy_fingerprint,
                decoded.policy.projector_schema_version,
                target_catalog_generation,
            ),
        ).fetchone()
        if namespace != (
            decoded.namespace.namespace_id,
            decoded.namespace.evidence,
            decoded.namespace.ready_at,
        ):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "historical policy namespace terminal does not verify",
            )
        if decoded.catalog is not None:
            catalog = connection.execute(
                "SELECT descriptor, artifact_count, created_at "
                "FROM catalog_generation_descriptors WHERE catalog_generation=?",
                (target_catalog_generation,),
            ).fetchone()
            if catalog != (
                decoded.catalog.descriptor,
                decoded.catalog.artifact_count,
                decoded.catalog.created_at,
            ):
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "historical policy catalog terminal does not verify",
                )
    except GovernanceError:
        raise
    except (schema_v4.SchemaV4Error, sqlite3.Error, TypeError, ValueError):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "historical policy publication terminal does not verify",
        ) from None
    finally:
        connection.close()
    if _v4_policy_publication_receipt_terminal(vault_root, decoded) != "committed":
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "historical policy publication receipt does not verify",
        )


def _recover_v4_policy_publication(
    vault_root: Path,
    *,
    connection: sqlite3.Connection,
    decoded: _DecodedV4PolicyProposal,
    now: int,
) -> schema_v4.VerifiedActiveGovernanceState | None:
    try:
        target = _committed_v4_policy_target(connection, decoded)
        if target is None:
            return None
        custody = authorization_custody.load_authorization_custody(
            vault_root,
            now=now,
        )
        if _control_matches_active(custody.control, target):
            verified = schema_v4.load_active_state(
                connection,
                expected_logical_vault_id=target.logical_vault_id,
                expected_activation_store_id=target.activation_store_id,
                expected_activation_epoch=target.activation_epoch,
                expected_activation_state_digest=target.activation_state_digest,
            )
            if verified != target:
                raise schema_v4.SchemaV4Error(
                    "active tuple changed during policy recovery"
                )
            return target
        if not _control_matches_active(custody.control, decoded.expected):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "external activation authority names neither reviewed policy state",
            )
        recovered = schema_v4.recover_registry_acknowledgement(
            connection,
            expected=decoded.expected,
            acknowledge_registry=lambda active: (
                authorization_custody.acknowledge_activation_tuple(
                    vault_root,
                    expected_control=custody.control,
                    target=active,
                    now=now,
                )
            ),
        )
        if recovered.active != target:
            raise schema_v4.SchemaV4Error(
                "registry recovery selected an unexpected policy tuple"
            )
        return target
    except GovernanceError:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        schema_v4.SchemaV4Error,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the committed policy tuple needs exact registry recovery",
        ) from None


def _spend_v4_policy_proposal(
    vault_root: Path,
    *,
    proposal_id: str,
    proposal_json: str,
    membership_manifest: str,
    spent_at: int,
) -> None:
    connection = store.open_authorization_session_connection(vault_root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE governance_proposals SET status='spent', spent_at=?, "
            "reserved_event_id=NULL, attempt_nonce=NULL "
            "WHERE proposal_id=? AND proposal_json=? AND membership_manifest=? "
            "AND status='pending'",
            (
                spent_at,
                proposal_id,
                proposal_json,
                membership_manifest,
            ),
        )
        if updated.rowcount != 1:
            current = connection.execute(
                "SELECT proposal_json, membership_manifest, status "
                "FROM governance_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if current != (proposal_json, membership_manifest, "spent"):
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "committed policy proposal state cannot be finalized exactly",
                )
        connection.commit()
    except GovernanceError:
        connection.rollback()
        raise
    except (OSError, sqlite3.Error, store.UnsupportedGovernanceSchema):
        connection.rollback()
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "committed policy proposal state cannot be finalized exactly",
        ) from None
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _reserve_v4_policy_proposal(
    vault_root: Path,
    *,
    proposal_id: str,
    proposal_json: str,
    membership_manifest: str,
    receipt_event_id: str,
) -> None:
    connection = store.open_authorization_session_connection(vault_root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE governance_proposals SET reserved_event_id=?, expires_at=MAX(expires_at, ?) "
            "WHERE proposal_id=? AND proposal_json=? AND membership_manifest=? "
            "AND status='pending' AND (reserved_event_id IS NULL OR reserved_event_id=?)",
            (
                receipt_event_id,
                _V4_POLICY_RECOVERY_EXPIRES_AT,
                proposal_id,
                proposal_json,
                membership_manifest,
                receipt_event_id,
            ),
        )
        if updated.rowcount != 1:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "policy publication recovery reservation changed",
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _expire_v4_policy_proposal(
    vault_root: Path,
    *,
    proposal_id: str,
    expired_at: int,
) -> None:
    connection = store.open_authorization_session_connection(vault_root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE governance_proposals SET status='expired', spent_at=?, "
            "reserved_event_id=NULL, attempt_nonce=NULL WHERE proposal_id=? "
            "AND status='pending'",
            (expired_at, proposal_id),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _v4_policy_publication_receipt_digests(
    decoded: _DecodedV4PolicyProposal,
) -> tuple[str, str, str, list[str]]:
    dependent_grants = decoded.dependent_grants or ()
    prior = _digest(
        {
            "schema": _V4_POLICY_PUBLICATION_RECEIPT_SCHEMA,
            "active_tuple": _active_tuple_value(decoded.expected),
            "dependent_grants": [
                {
                    "grant_id": transition.grant_id,
                    "policy_fingerprint": transition.expected_policy_fingerprint,
                    "membership_manifest": transition.expected_membership_manifest,
                    "status": "active",
                }
                for transition in dependent_grants
            ],
        }
    )
    prepared = _digest(
        {
            "schema": _V4_POLICY_PUBLICATION_RECEIPT_SCHEMA,
            "generation_id": decoded.policy.generation_id,
            "policy_fingerprint": decoded.policy.policy_fingerprint,
            "namespace_id": decoded.namespace.namespace_id,
            "dependent_grants": _dependent_grant_value(dependent_grants),
        }
    )
    target = _digest(
        {
            "schema": _V4_POLICY_PUBLICATION_RECEIPT_SCHEMA,
            "authority_binding": decoded.payload["authority_binding"],
        }
    )
    affected = [
        hashlib.sha256(
            f"policy_generation:{decoded.policy.generation_id}".encode()
        ).hexdigest(),
        *(
            hashlib.sha256(f"dependent_grant:{transition.grant_id}".encode()).hexdigest()
            for transition in dependent_grants
        ),
    ]
    return prior, prepared, target, sorted(affected)


def _v4_policy_publication_receipt_terminal(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
) -> str | None:
    prior, prepared, target, affected = _v4_policy_publication_receipt_digests(
        decoded
    )
    try:
        records = receipts.event_records(vault_root)
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy publication receipt evidence cannot be verified",
        ) from None
    intents = [
        record
        for record in records
        if record.get("event_id") == decoded.policy.receipt_event_id
        and record.get("phase") == "intent"
    ]
    terminals = [
        record
        for record in records
        if record.get("causation_id") == decoded.policy.receipt_event_id
        and record.get("phase") in {"committed", "aborted"}
    ]
    if not intents and not terminals:
        return None
    if len(intents) != 1 or any(
        intents[0].get(field) != expected
        for field, expected in {
            "event_type": "critical",
            "operation": _V4_POLICY_PUBLICATION_RECEIPT_OPERATION,
            "prior": prior,
            "prepared": prepared,
            "target": target,
            "affected_ids": affected,
        }.items()
    ):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy publication receipt intent is contradictory",
        )
    if not terminals:
        return "pending"
    if len(terminals) != 1:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy publication receipt terminal is contradictory",
        )
    phase = terminals[0].get("phase")
    outcome = terminals[0].get("outcome")
    if phase == "committed" and outcome == "committed":
        return "committed"
    if phase == "aborted" and isinstance(outcome, str) and outcome:
        return "aborted"
    raise GovernanceError(
        "GOVERNANCE_BLOCKED",
        "policy publication receipt terminal is contradictory",
    )


def _begin_v4_policy_publication_receipt(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
) -> None:
    terminal = _v4_policy_publication_receipt_terminal(vault_root, decoded)
    if terminal == "committed":
        return
    if terminal == "aborted":
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed policy publication was already refused",
        )
    if terminal == "pending":
        return
    prior, prepared, target, affected = _v4_policy_publication_receipt_digests(
        decoded
    )
    try:
        receipts.begin_event(
            vault_root,
            operation=_V4_POLICY_PUBLICATION_RECEIPT_OPERATION,
            prior=prior,
            prepared=prepared,
            target=target,
            affected_ids=affected,
            event_id=decoded.policy.receipt_event_id,
        )
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy publication receipt intent could not be recorded",
        ) from None


def _commit_v4_policy_publication_receipt(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
) -> None:
    terminal = _v4_policy_publication_receipt_terminal(vault_root, decoded)
    if terminal == "committed":
        return
    if terminal == "aborted":
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "committed policy publication has an aborted receipt",
        )
    if terminal is None:
        _begin_v4_policy_publication_receipt(vault_root, decoded)
    try:
        receipts.commit_event(
            vault_root,
            decoded.policy.receipt_event_id,
            outcome="committed",
        )
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy publication receipt terminal could not be recorded",
        ) from None


def _abort_v4_policy_publication_receipt(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
) -> None:
    terminal = _v4_policy_publication_receipt_terminal(vault_root, decoded)
    if terminal in {None, "aborted"}:
        return
    if terminal == "committed":
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "committed policy publication cannot be expired as stale",
        )
    try:
        receipts.abort_event(
            vault_root,
            decoded.policy.receipt_event_id,
            outcome="stale_predecessor",
        )
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "stale policy publication receipt could not be closed",
        ) from None


def _v4_workspace_mirror_barrier(_phase: str, _path: str | None = None) -> None:
    """Deterministic test seam around the non-authoritative workspace mirror."""


def _v4_workspace_mirror_event_id(decoded: _DecodedV4PolicyProposal) -> str:
    return receipts.critical_event_id(
        {
            "schema": _V4_POLICY_MIRROR_SCHEMA,
            "policy_receipt_event_id": decoded.policy.receipt_event_id,
            "policy_generation_id": decoded.policy.generation_id,
        }
    )


def _v4_workspace_mirror_terminal(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
) -> str | None:
    event_id = _v4_workspace_mirror_event_id(decoded)
    prior, prepared, target, affected = _v4_workspace_mirror_receipt_digests(decoded)
    try:
        event_records = receipts.event_records(vault_root)
        intents = [
            record
            for record in event_records
            if record.get("event_id") == event_id and record.get("phase") == "intent"
        ]
        terminals = [
            record
            for record in event_records
            if record.get("causation_id") == event_id
            and record.get("phase") in {"committed", "aborted"}
        ]
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror evidence cannot be verified",
        ) from None
    if not intents and not terminals:
        return None
    if len(intents) != 1 or any(
        intents[0].get(field) != expected
        for field, expected in {
            "event_type": "critical",
            "operation": _V4_POLICY_MIRROR_OPERATION,
            "prior": prior,
            "prepared": prepared,
            "target": target,
            "affected_ids": affected,
            "parent_causation_id": decoded.policy.receipt_event_id,
        }.items()
    ):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror intent evidence is contradictory",
        )
    if not terminals:
        return None
    if len(terminals) != 1 or terminals[0].get("phase") != "committed":
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror evidence is contradictory",
        )
    outcome = terminals[0].get("outcome")
    if outcome not in _V4_POLICY_MIRROR_OUTCOMES:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror evidence has an unknown outcome",
        )
    return str(outcome)


def _v4_workspace_mirror_receipt_digests(
    decoded: _DecodedV4PolicyProposal,
) -> tuple[str, str, str, list[str]]:
    reviewed = decoded.authoring_snapshot
    prior = _digest(
        {
            "schema": _V4_POLICY_MIRROR_SCHEMA,
            "policy_generation_id": decoded.expected.policy_generation_id,
            "source_fingerprint": reviewed.source_fingerprint,
            # Receipt identity is the exact reviewed representation.  Older
            # v4 proposals recorded physical directory link counts; decoding
            # may normalize those for live identity comparison, but recovery
            # must never rewrite their already-durable receipt preimage.
            "authoring_snapshot": decoded.authoring_snapshot_value,
        }
    )
    prepared = _digest(
        {
            "schema": _V4_POLICY_MIRROR_SCHEMA,
            "policy_generation_id": decoded.policy.generation_id,
            "policy_receipt_event_id": decoded.policy.receipt_event_id,
            "source_fingerprint": decoded.policy.source_fingerprint,
        }
    )
    target = _digest(
        {
            "schema": _V4_POLICY_MIRROR_SCHEMA,
            "policy_generation_id": decoded.policy.generation_id,
            "source_fingerprint": decoded.policy.source_fingerprint,
            "source_documents": [
                {
                    "path_digest": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for relative, content in decoded.policy.source_documents
            ],
        }
    )
    affected = [
        hashlib.sha256(
            _canonical_json(
                sorted(
                    set(dict(reviewed.documents))
                    | set(dict(decoded.policy.source_documents))
                )
            ).encode("utf-8")
        ).hexdigest()
    ]
    return prior, prepared, target, affected


def _apply_v4_workspace_mirror(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
    *,
    crash_at: object,
) -> str:
    if decoded.payload.get("semantic_operation") in {"suspend", "resume", "undo"}:
        connection = store.open_authorization_session_connection(vault_root)
        try:
            predecessor = schema_v4.load_policy_generation(
                connection,
                decoded.expected.policy_generation_id,
            )
        except (schema_v4.SchemaV4Error, sqlite3.Error):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "semantic policy mirror predecessor cannot be verified",
            ) from None
        finally:
            connection.close()
        if predecessor.source_documents != decoded.authoring_snapshot.documents:
            return "diverged"

    effect_index = 0

    def barrier(phase: str, relative: str) -> None:
        nonlocal effect_index
        _v4_workspace_mirror_barrier(phase, relative)
        if phase != "after_write":
            return
        effect_index += 1
        if crash_at in {
            "v4_after_mirror_write",
            f"v4_after_mirror_write:{effect_index}",
        }:
            raise GovernanceCrash(str(crash_at))

    return policy_module.mirror_authoring_workspace(
        vault_root,
        reviewed=decoded.authoring_snapshot,
        target_documents=decoded.policy.source_documents,
        barrier=barrier,
    )


def _mirror_v4_policy_workspace(
    vault_root: Path,
    decoded: _DecodedV4PolicyProposal,
    *,
    crash_at: object,
) -> str:
    event_id = _v4_workspace_mirror_event_id(decoded)
    existing = _v4_workspace_mirror_terminal(vault_root, decoded)
    if existing is not None:
        return existing
    prior, prepared, target, affected = _v4_workspace_mirror_receipt_digests(decoded)
    try:
        receipts.begin_event(
            vault_root,
            operation=_V4_POLICY_MIRROR_OPERATION,
            prior=prior,
            prepared=prepared,
            target=target,
            affected_ids=affected,
            event_id=event_id,
            parent_causation_id=decoded.policy.receipt_event_id,
        )
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror intent could not be recorded",
        ) from None
    _v4_workspace_mirror_barrier("after_intent")
    if crash_at == "v4_after_mirror_intent":
        raise GovernanceCrash("v4_after_mirror_intent")
    outcome = _apply_v4_workspace_mirror(
        vault_root,
        decoded,
        crash_at=crash_at,
    )
    if crash_at == "v4_after_mirror_effect":
        raise GovernanceCrash("v4_after_mirror_effect")
    if outcome == "pending":
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the committed policy tuple is active but its workspace mirror needs retry",
        )
    try:
        receipts.commit_event(vault_root, event_id, outcome=outcome)
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror outcome could not be recorded",
        ) from None
    if crash_at == "v4_after_mirror_terminal":
        raise GovernanceCrash("v4_after_mirror_terminal")
    return outcome


def _v4_commit_terminal(
    *,
    proposal_id: str,
    decoded: _DecodedV4PolicyProposal,
    mirror_status: str,
) -> dict[str, Any]:
    return {
        "status": "committed",
        "event_id": decoded.policy.receipt_event_id,
        "proposal_id": proposal_id,
        "direction": decoded.direction,
        "mirror_status": mirror_status,
    }


def _commit(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    proposal_id = str(kwargs.get("proposal_id") or "")
    if not proposal_id:
        raise GovernanceError("PROPOSAL_UNKNOWN", "proposal_id is required")
    now = float(kwargs.get("now", time.time()))
    if store.authorization_session_schema_version(vault_root) == schema_v4.SCHEMA_USER_VERSION:
        connection = store.open_authorization_session_connection(vault_root)
        try:
            row = connection.execute(
                "SELECT proposal_json, membership_manifest, status, expires_at, created_at, "
                "reserved_event_id "
                "FROM governance_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
            status = str(row[2])
            if status not in {"pending", "spent"}:
                raise GovernanceError("PROPOSAL_EXPIRED", "proposal is not active")
            proposal_json = str(row[0])
            manifest_json = str(row[1])
            created_at = float(row[4])
            decoded = _decode_v4_proposal_binding(
                vault_root,
                proposal_id=proposal_id,
                proposal_json=proposal_json,
                membership_manifest=manifest_json,
                created_at=created_at,
            )
            recovered = _recover_v4_policy_publication(
                vault_root,
                connection=connection,
                decoded=decoded,
                now=int(now),
            )
        finally:
            connection.close()
        if status == "spent":
            if recovered is None:
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "spent policy proposal has no exact committed publication",
                )
            _commit_v4_policy_publication_receipt(vault_root, decoded)
            mirror_status = _mirror_v4_policy_workspace(
                vault_root,
                decoded,
                crash_at=kwargs.get("crash_at"),
            )
            return _v4_commit_terminal(
                proposal_id=proposal_id,
                decoded=decoded,
                mirror_status=mirror_status,
            )
        if recovered is not None:
            _reserve_v4_policy_proposal(
                vault_root,
                proposal_id=proposal_id,
                proposal_json=proposal_json,
                membership_manifest=manifest_json,
                receipt_event_id=decoded.policy.receipt_event_id,
            )
            _commit_v4_policy_publication_receipt(vault_root, decoded)
            _spend_v4_policy_proposal(
                vault_root,
                proposal_id=proposal_id,
                proposal_json=proposal_json,
                membership_manifest=manifest_json,
                spent_at=int(now),
            )
            mirror_status = _mirror_v4_policy_workspace(
                vault_root,
                decoded,
                crash_at=kwargs.get("crash_at"),
            )
            return _v4_commit_terminal(
                proposal_id=proposal_id,
                decoded=decoded,
                mirror_status=mirror_status,
            )
        reserved_event_id = row[5]
        if reserved_event_id not in {None, decoded.policy.receipt_event_id}:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "policy publication recovery reservation is contradictory",
            )
        if float(row[3]) < now and reserved_event_id is None:
            raise GovernanceError("PROPOSAL_EXPIRED", "proposal is not active")
        validated = _validate_v4_proposal_binding(
            vault_root,
            proposal_id=proposal_id,
            proposal_json=proposal_json,
            membership_manifest=manifest_json,
            created_at=created_at,
            now=int(now),
        )
        _reserve_v4_policy_proposal(
            vault_root,
            proposal_id=proposal_id,
            proposal_json=proposal_json,
            membership_manifest=manifest_json,
            receipt_event_id=validated.decoded.policy.receipt_event_id,
        )
        _begin_v4_policy_publication_receipt(
            vault_root,
            validated.decoded,
        )
        if kwargs.get("crash_at") == "v4_after_policy_receipt_intent":
            raise GovernanceCrash("v4_after_policy_receipt_intent")
        connection = store.open_authorization_session_connection(vault_root)
        try:
            try:
                schema_v4.publish_policy_generation(
                    connection,
                    expected=validated.decoded.expected,
                    policy=validated.decoded.policy,
                    catalog=validated.decoded.catalog,
                    namespace=validated.decoded.namespace,
                    # A pre-v4 proposal did not bind grants. Passing an exact
                    # empty tuple makes the transaction prove there are none;
                    # it must never silently preserve unreviewed active rows.
                    dependent_grants=validated.decoded.dependent_grants or (),
                    activated_at=int(now),
                    acknowledge_registry=lambda active: (
                        authorization_custody.acknowledge_activation_tuple(
                            vault_root,
                            expected_control=validated.custody.control,
                            target=active,
                            now=int(now),
                        )
                    ),
                )
            except schema_v4.ActiveTupleStale:
                recovered = _recover_v4_policy_publication(
                    vault_root,
                    connection=connection,
                    decoded=validated.decoded,
                    now=int(now),
                )
                if recovered is None:
                    _abort_v4_policy_publication_receipt(
                        vault_root,
                        validated.decoded,
                    )
                    _expire_v4_policy_proposal(
                        vault_root,
                        proposal_id=proposal_id,
                        expired_at=int(now),
                    )
                    raise GovernanceError(
                        "STALE_GOVERNANCE_POLICY",
                        "the reviewed active policy tuple changed",
                    ) from None
            except authorization_custody.AuthorizationCustodyUnavailable:
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "the committed policy tuple needs exact registry recovery",
                ) from None
            except (schema_v4.SchemaV4Error, OSError, sqlite3.Error):
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "the reviewed policy tuple could not be published exactly",
                ) from None
        finally:
            connection.close()
        if kwargs.get("crash_at") == "v4_after_registry_ack":
            raise GovernanceCrash("v4_after_registry_ack")
        _commit_v4_policy_publication_receipt(vault_root, validated.decoded)
        if kwargs.get("crash_at") == "v4_after_policy_receipt_terminal":
            raise GovernanceCrash("v4_after_policy_receipt_terminal")
        _spend_v4_policy_proposal(
            vault_root,
            proposal_id=proposal_id,
            proposal_json=proposal_json,
            membership_manifest=manifest_json,
            spent_at=int(now),
        )
        mirror_status = _mirror_v4_policy_workspace(
            vault_root,
            validated.decoded,
            crash_at=kwargs.get("crash_at"),
        )
        return _v4_commit_terminal(
            proposal_id=proposal_id,
            decoded=validated.decoded,
            mirror_status=mirror_status,
        )
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    _validate_proposal_drift(vault_root, proposal_id, now=now)
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
    proposed_documents = json.loads(str(row[0]))["documents"]
    direction = _effective_transition_direction(vault_root, proposed_documents)
    event_id, payload, digests, affected = _prepare_commit_attempt(
        vault_root,
        proposal_id,
        who,
        direction=direction,
        now=now,
        crash_at=kwargs.get("crash_at"),
    )
    transition = operation_variant("commit")
    if kwargs.get("crash_at") == "after_intent":
        raise GovernanceCrash("after_intent")

    if transition.yaml_marker:
        marker = _marker_path(vault_root)
        _durable_json(
            marker,
            {
                "protocol_version": 1,
                "phase": "pending",
                "schema_version": store.SCHEMA_USER_VERSION,
                "event_id": event_id,
                "operation": transition.journal_operation,
                "prior": digests["prior"],
                "prepared": digests["prepared"],
                "final": digests["final"],
                "affected_paths": sorted(payload["documents"]),
                "affected_ids": sorted(affected),
            },
        )
        if kwargs.get("crash_at") == "after_marker":
            raise GovernanceCrash("after_marker")
    root = policy_module.governance_root(vault_root)
    for index, (rel, content) in enumerate(sorted(payload["documents"].items()), start=1):
        _durable_bytes(policy_target(root, rel), content.encode("utf-8"))
        if kwargs.get("crash_at") in {"after_target_write", f"after_target_write:{index}"}:
            raise GovernanceCrash("after_target_write")
    if kwargs.get("crash_at") == "after_prepare":
        raise GovernanceCrash("after_prepare")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    if kwargs.get("crash_at") == "after_terminal":
        raise GovernanceCrash("after_terminal")
    if kwargs.get("crash_at") == "after_marker_removal":
        if transition.yaml_marker:
            _remove_marker(vault_root, event_id)
        raise GovernanceCrash("after_marker_removal")
    _activate_event(
        vault_root, event_id, remove_marker=transition.yaml_marker, now=now
    )
    return {
        "status": "committed",
        "event_id": event_id,
        "proposal_id": proposal_id,
        "direction": direction,
    }


def _backfill_plan(vault_root: Path, value: object) -> companion_backfill.BackfillPlan:
    try:
        return companion_backfill.plan(vault_root, value)
    except companion_backfill.CompanionBackfillError as error:
        raise GovernanceError(error.code, error.reason) from error


def _backfill_payload(
    plan: companion_backfill.BackfillPlan,
) -> dict[str, Any]:
    return {
        "kind": "companion-backfill/v1",
        "input": plan.normalized_input,
        "descriptor": plan.descriptor,
        "identities": list(plan.identities),
        "prior": plan.prior_value,
        "target": plan.target_value,
    }


def _backfill_preview(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    _require_owner(kwargs.get("principal"))
    store.require_authoring_schema(
        vault_root,
        supported_versions=(store.SCHEMA_USER_VERSION, schema_v4.SCHEMA_USER_VERSION),
    )
    plan = _backfill_plan(vault_root, kwargs.get("companion_input"))
    payload = _backfill_payload(plan)
    proposal_id = uuid.uuid4().hex
    now = float(kwargs.get("now", time.time()))
    expires_at = now + max(
        1, int(kwargs.get("ttl_seconds", DEFAULT_PROPOSAL_TTL_SECONDS))
    )
    payload_json = _canonical_json(payload)
    conn = store.open_connection(vault_root)
    try:
        conn.execute(
            "INSERT INTO governance_proposals "
            "(proposal_id, created_at, expires_at, proposal_json, fingerprint_at_propose, "
            "membership_manifest, status) VALUES (?, ?, ?, ?, ?, '[]', 'pending')",
            (proposal_id, now, expires_at, payload_json, _digest(payload)),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "preview",
        "proposal_id": proposal_id,
        "expires_at": expires_at,
        "descriptor": plan.descriptor,
        "identities": list(plan.identities),
    }


def _backfill_proposal(vault_root: Path, proposal_id: str) -> tuple[Any, ...]:
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id, created_at, spent_at "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such companion backfill proposal")
    return tuple(row)


def _validated_backfill_payload(
    row: tuple[Any, ...], supplied: object
) -> dict[str, Any]:
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError) as error:
        raise GovernanceError(
            "INVALID_COMPANION_BACKFILL", "stored backfill proposal is malformed"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "companion-backfill/v1"
        or _canonical_json(payload) != str(row[0])
        or _digest(payload) != str(row[1])
        or str(row[2]) != "[]"
        or set(payload) != {
            "kind",
            "input",
            "descriptor",
            "identities",
            "prior",
            "target",
        }
    ):
        raise GovernanceError(
            "INVALID_COMPANION_BACKFILL", "stored backfill proposal is invalid"
        )
    try:
        supplied_json = _canonical_json(supplied)
    except (TypeError, ValueError) as error:
        raise GovernanceError(
            "INVALID_COMPANION_BACKFILL", "companion_input must be exact JSON"
        ) from error
    if supplied_json != _canonical_json(payload["input"]):
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "reviewed companion input changed"
        )
    return payload


def _backfill_terminal(
    vault_root: Path, proposal_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    conn = store.open_connection(vault_root)
    try:
        journal = conn.execute(
            "SELECT event_id, phase FROM governance_operation_journals "
            "WHERE operation='commit_backfill_companion' AND proposal_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if journal is None or str(journal[1]) != "closed":
            raise GovernanceError(
                "GOVERNANCE_BLOCKED", "committed companion backfill is incomplete"
            )
        current = _actual_backfill_value(
            vault_root, str(payload["input"]["expected_companion_path"])
        )
        if current != payload["target"]:
            raise GovernanceError(
                "STALE_COMPANION_BACKFILL", "committed companion bytes changed"
            )
    finally:
        conn.close()
    return {
        "status": "committed",
        "event_id": str(journal[0]),
        "proposal_id": proposal_id,
        "direction": "widening",
    }


def _actual_backfill_value(vault_root: Path, companion_path: str) -> dict[str, Any]:
    try:
        snapshot = reserved_paths.read_generic_bytes(vault_root, companion_path)
    except reserved_paths.ReservedPathLeafError as error:
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "companion snapshot is unavailable"
        ) from error
    return {
        "path_hash": hashlib.sha256(companion_path.encode("utf-8")).hexdigest(),
        "sha256": hashlib.sha256(snapshot.data).hexdigest(),
        "size": len(snapshot.data),
    }


def _backfill_commit(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    proposal_id = str(kwargs.get("proposal_id") or "")
    if not proposal_id:
        raise GovernanceError("PROPOSAL_UNKNOWN", "proposal_id is required")
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs repair")
    store.require_authoring_schema(
        vault_root,
        supported_versions=(store.SCHEMA_USER_VERSION, schema_v4.SCHEMA_USER_VERSION),
    )
    now = float(kwargs.get("now", time.time()))
    row = _backfill_proposal(vault_root, proposal_id)
    payload = _validated_backfill_payload(row, kwargs.get("companion_input"))
    status = str(row[3])
    if status == "spent":
        return _backfill_terminal(vault_root, proposal_id, payload)
    if status != "pending" or float(row[4]) < now:
        raise GovernanceError("PROPOSAL_EXPIRED", "backfill proposal is not active")
    if row[6] is not None:
        raise GovernanceError("PROPOSAL_RESERVED", "backfill proposal has an open attempt")
    plan = _backfill_plan(vault_root, kwargs.get("companion_input"))
    if _backfill_payload(plan) != payload:
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "reviewed companion snapshots changed"
        )
    try:
        target_source = plan.target_bytes.decode("utf-8")
        expected_before_hash = hashlib.sha256(plan.prior_bytes).hexdigest()

        def graph_replacement_provider() -> tuple[
            catalog_publication.GraphMeasurementReplacement, ...
        ]:
            before_corpus = semantic_contract.build_corpus_context(vault_root)
            write = vault.PlannedWrite(
                path=vault_root / plan.companion_path,
                content=target_source,
                expected_hash=expected_before_hash,
            )
            return graph_producer.replacements_for_planned_markdown(
                vault_root,
                before_corpus=before_corpus,
                writes=(write,),
            )

        catalog_target = catalog_publication.prepare_markdown_upsert(
            vault_root,
            path=plan.companion_path,
            source=target_source,
            expected_before_hash=expected_before_hash,
            graph_replacement_provider=graph_replacement_provider,
            now=int(now),
        )
        catalog_values = (
            None
            if catalog_target is None
            else catalog_publication.catalog_component_values(catalog_target)
        )
    except (UnicodeDecodeError, catalog_publication.CatalogPublicationError) as error:
        raise GovernanceError(
            "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
            str(error),
        ) from error

    prior_attempt = int(row[5])
    attempt_no = prior_attempt + 1
    attempt_nonce = uuid.uuid4().hex
    reserved_proposal = authorization_row(
        proposal_json=str(row[0]),
        fingerprint_at_propose=str(row[1]),
        membership_manifest=str(row[2]),
        status="pending",
        expires_at=float(row[4]),
        attempt_no=attempt_no,
        attempt_nonce=attempt_nonce,
        reserved_event_id="SELF_EVENT",
        created_at=float(row[7]),
        spent_at=None,
    )
    final_proposal = {
        **reserved_proposal,
        "status": "spent",
        "reserved_event_id": None,
        "spent_at": now,
    }
    phases = {
        "prior": [
            _component("companion", plan.companion_path, plan.prior_value, status="prior"),
            _component("proposal", proposal_id, reserved_proposal, status="pending"),
        ],
        "prepared": [
            _component(
                "companion", plan.companion_path, plan.target_value, status="prepared"
            ),
            _component("proposal", proposal_id, reserved_proposal, status="pending"),
        ],
        "final": [
            _component("companion", plan.companion_path, plan.target_value, status="active"),
            _component("proposal", proposal_id, final_proposal, status="spent"),
        ],
    }
    if catalog_values is not None:
        catalog_prior, catalog_final = catalog_values
        phases["prior"].append(
            _component("catalog", "active", catalog_prior, status="prior")
        )
        phases["prepared"].append(
            _component("catalog", "active", catalog_final, status="prepared")
        )
        phases["final"].append(
            _component("catalog", "active", catalog_final, status="active")
        )
    digests = {phase: _composite(phase, values) for phase, values in phases.items()}
    event_id = receipts.critical_event_id(
        {
            "operation": "governance_companion_backfill",
            "proposal_id": proposal_id,
            "attempt": attempt_no,
            "attempt_nonce": attempt_nonce,
            "prepared": digests["prepared"],
        }
    )
    affected = sorted(
        {
            hashlib.sha256(
                f"{item['component_kind']}:{item['component_key']}".encode()
            ).hexdigest()
            for item in phases["prepared"]
        }
    )
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if (
            live is None
            or tuple(live[:5]) != tuple(row[:5])
            or int(live[5]) != prior_attempt
            or live[6] is not None
        ):
            raise GovernanceError("PROPOSAL_RESERVED", "backfill proposal changed")
        updated = conn.execute(
            "UPDATE governance_proposals SET attempt_no=?, attempt_nonce=?, "
            "reserved_event_id=? WHERE proposal_id=? AND status='pending' "
            "AND attempt_no=? AND attempt_nonce IS NULL AND reserved_event_id IS NULL",
            (attempt_no, attempt_nonce, event_id, proposal_id, prior_attempt),
        )
        if updated.rowcount != 1:
            raise GovernanceError("PROPOSAL_RESERVED", "backfill proposal changed")
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation="commit_backfill_companion",
            who=who,
            authorization_session=None,
            direction="widening",
            phases=phases,
            child_ids=[event_id],
            proposal_id=proposal_id,
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "backfill digest changed")
        conn.execute(
            "UPDATE governance_operation_journals SET attempt_no=? WHERE event_id=?",
            (attempt_no, event_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if kwargs.get("crash_at") == "after_reservation":
        raise GovernanceCrash("after_reservation")

    receipts.begin_event(
        vault_root,
        operation="governance_companion_backfill",
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=affected,
        event_id=event_id,
    )
    if kwargs.get("crash_at") == "after_intent":
        raise GovernanceCrash("after_intent")
    try:
        confirmed = _backfill_plan(vault_root, kwargs.get("companion_input"))
        if _backfill_payload(confirmed) != payload:
            raise GovernanceError(
                "STALE_COMPANION_BACKFILL", "companion changed after receipt intent"
            )
    except GovernanceError:
        reconcile_governance_operations(vault_root)
        raise
    _arm_journal(vault_root, event_id, now=now)
    if kwargs.get("crash_at") == "after_arming":
        raise GovernanceCrash("after_arming")
    try:
        reserved_paths.publish_generic_bytes(
            vault_root,
            plan.companion_path,
            plan.target_bytes,
            expected_identity=plan.companion_identity,
            expected_sha256=hashlib.sha256(plan.prior_bytes).hexdigest(),
        )
    except reserved_paths.ReservedPathLeafError as error:
        reconcile_governance_operations(vault_root)
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "companion changed during publication"
        ) from error
    if kwargs.get("crash_at") == "after_publish":
        raise GovernanceCrash("after_publish")
    if catalog_target is not None:
        try:
            catalog_publication.publish_markdown_batch(catalog_target)
        except catalog_publication.CatalogPublicationError as error:
            raise GovernanceError(
                "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                str(error),
            ) from error
        if kwargs.get("crash_at") == "after_catalog":
            raise GovernanceCrash("after_catalog")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    if kwargs.get("crash_at") == "after_terminal":
        raise GovernanceCrash("after_terminal")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return {
        "status": "committed",
        "event_id": event_id,
        "proposal_id": proposal_id,
        "direction": "widening",
    }


def _durable_remove(path: Path) -> None:
    if path.exists():
        path.unlink()
        _fsync_directory(path.parent)


def _yaml_transition(
    vault_root: Path,
    *,
    transition: OperationVariant,
    who: RequestPrincipal,
    documents: Mapping[str, str | None],
    direction: str,
    dependent_targets: Mapping[str, str] | None = None,
    source_event_id: str | None = None,
    crash_at: str | None = None,
    now: float,
) -> dict[str, Any]:
    """Apply a pre-resolved YAML/sidecar target without semantic replay."""
    operation = transition.journal_operation
    if operation is None:
        raise GovernanceError("GOVERNANCE_BLOCKED", "operation has no journal identity")
    if not documents:
        raise GovernanceError("NO_GOVERNANCE_CHANGE", "no policy documents would change")
    root = policy_module.governance_root(vault_root)
    _marker_path(vault_root)
    targets = {rel: policy_target(root, rel) for rel in documents}
    prior_hashes = {rel: _content_hash(targets[rel]) for rel in documents}
    prior_bytes_by_rel: dict[str, bytes | None] = {}
    for rel in documents:
        try:
            prior_bytes_by_rel[rel] = targets[rel].read_bytes()
        except FileNotFoundError:
            prior_bytes_by_rel[rel] = None
    target_hashes = {
        rel: (
            "absent"
            if content is None
            else hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        for rel, content in documents.items()
    }
    event_id = receipts.critical_event_id(
        {
            "operation": operation,
            "target_hashes": target_hashes,
            "nonce": uuid.uuid4().hex,
        }
    )
    phases: dict[str, list[dict[str, Any]]] = {
        "prior": [
            _component("yaml", rel, {"hash": prior_hashes[rel]}, status="prior")
            for rel in documents
        ],
        "prepared": [
            _component("yaml", rel, {"hash": target_hashes[rel]}, status="prepared")
            for rel in documents
        ],
        "final": [
            _component("yaml", rel, {"hash": target_hashes[rel]}, status="active")
            for rel in documents
        ],
    }
    archive_ids = {
        rel: hashlib.sha256(f"{event_id}:{rel}".encode()).hexdigest()
        for rel in documents
    }
    for rel in documents:
        archive_component = _component(
            "archive",
            archive_ids[rel],
            _archive_value(rel, prior_bytes_by_rel[rel], prior_hashes[rel]),
            status="archived",
        )
        for phase in phases.values():
            phase.append(archive_component)
    dependent_targets = dict(dependent_targets or {})
    if dependent_targets:
        conn = store.open_connection(vault_root)
        try:
            rows = {
                str(row[0]): row[1:]
                for row in conn.execute(
                    "SELECT grant_id, authorization_session, audience, purpose, ceiling, paths, "
                    "fingerprints, token_jti, status, prepared_event_id, created_at, expires_at, "
                    "revoked_at, membership_manifest, policy_fingerprint FROM "
                    "governance_session_grants WHERE grant_id IN "
                    f"({','.join('?' for _ in dependent_targets)})",
                    tuple(sorted(dependent_targets)),
                )
            }
        finally:
            conn.close()
        if set(rows) != set(dependent_targets):
            raise GovernanceError("STALE_DEPENDENT_GRANT", "a dependent grant disappeared")
        for grant_id in sorted(dependent_targets):
            row = rows[grant_id]
            prior = _grant_projection(
                authorization_session=str(row[0]), audience=str(row[1]), purpose=row[2],
                ceiling=int(row[3]), paths=str(row[4]), fingerprints=str(row[5]),
                token_jti=str(row[6]), status=str(row[7]), prepared_event_id=row[8],
                created_at=float(row[9]), expires_at=float(row[10]), revoked_at=row[11],
                membership_manifest=str(row[12]), policy_fingerprint=str(row[13]),
            )
            phases["prior"].append(
                _component(
                    "dependent_grant",
                    grant_id,
                    prior,
                    status=str(row[7]),
                )
            )
            phases["prepared"].append(
                _component(
                    "dependent_grant",
                    grant_id,
                    {**prior, "status": "prepared_undo", "prepared_event_id": event_id},
                    status="prepared_undo",
                )
            )
            phases["final"].append(
                _component(
                    "dependent_grant",
                    grant_id,
                    {**prior, "status": dependent_targets[grant_id], "prepared_event_id": None},
                    status=dependent_targets[grant_id],
                )
            )

    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    affected_ids = sorted(
        hashlib.sha256(f"{row['component_kind']}:{row['component_key']}".encode()).hexdigest()
        for row in phases["prepared"]
    )
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=operation,
            who=who,
            authorization_session=None,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            marker_required=transition.yaml_marker,
            proposal_id=(f"undo:{source_event_id}" if source_event_id else None),
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        for rel in sorted(documents):
            conn.execute(
                "INSERT INTO governance_policy_archives "
                "(archive_id, event_id, path, prior_bytes, prior_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    archive_ids[rel],
                    event_id,
                    rel,
                    prior_bytes_by_rel[rel],
                    prior_hashes[rel],
                    now,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=transition.receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=affected_ids,
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    if crash_at == "after_intent":
        raise GovernanceCrash("after_intent")
    if transition.yaml_marker:
        _durable_json(
            _marker_path(vault_root),
            {
                "protocol_version": 1,
                "phase": "pending",
                "schema_version": store.SCHEMA_USER_VERSION,
                "event_id": event_id,
                "operation": operation,
                "prior": digests["prior"],
                "prepared": digests["prepared"],
                "final": digests["final"],
                "affected_paths": sorted(documents),
                "affected_ids": affected_ids,
            },
        )
        if crash_at == "after_marker":
            raise GovernanceCrash("after_marker")
    for index, (rel, content) in enumerate(sorted(documents.items()), start=1):
        target = policy_target(root, rel)
        if content is None:
            _durable_remove(target)
        else:
            _durable_bytes(target, content.encode("utf-8"))
        if crash_at in {"after_target_write", f"after_target_write:{index}"}:
            raise GovernanceCrash("after_target_write")
    if dependent_targets:
        conn = store.open_connection(vault_root)
        try:
            conn.execute(
                "UPDATE governance_session_grants SET status='prepared_undo', "
                "prepared_event_id=? WHERE grant_id IN "
                f"({','.join('?' for _ in dependent_targets)})",
                (event_id, *sorted(dependent_targets)),
            )
            conn.commit()
        finally:
            conn.close()
    if crash_at == "after_prepare":
        raise GovernanceCrash("after_prepare")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    if crash_at == "after_terminal":
        raise GovernanceCrash("after_terminal")
    _activate_event(
        vault_root, event_id, remove_marker=transition.yaml_marker, now=now
    )
    return {
        "status": "committed",
        "event_id": event_id,
        "operation": operation,
        "direction": direction,
    }


def _resume_v4_semantic_policy_operation(
    vault_root: Path,
    *,
    operation: str,
    rule_ids: list[str] | None,
    request_digest: str | None,
    who: RequestPrincipal,
    crash_at: object,
    now: float,
) -> dict[str, Any] | None:
    connection = store.open_authorization_session_connection(vault_root)
    try:
        rows = connection.execute(
            "SELECT proposal_id, proposal_json, membership_manifest, status, created_at "
            "FROM governance_proposals WHERE status IN ('pending','spent') "
            "ORDER BY created_at, proposal_id"
        ).fetchall()
    finally:
        connection.close()
    incomplete: list[tuple[str, str, _DecodedV4PolicyProposal]] = []
    historical_mirrors: list[tuple[str, str, _DecodedV4PolicyProposal]] = []
    response_replays: list[tuple[str, str, _DecodedV4PolicyProposal]] = []
    for proposal_id, proposal_json, membership_manifest, status, created_at in rows:
        try:
            payload = json.loads(str(proposal_json))
        except json.JSONDecodeError:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "semantic policy recovery found a malformed proposal",
            ) from None
        stored_operation = payload.get("semantic_operation")
        if stored_operation is None:
            continue
        if stored_operation not in {"suspend", "resume", "undo"}:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "semantic policy recovery found an invalid operation",
            )
        stored_rule_ids = payload.get("semantic_rule_ids")
        if stored_operation in {"suspend", "resume"}:
            if (
                not isinstance(stored_rule_ids, list)
                or not stored_rule_ids
                or not all(
                    isinstance(rule_id, str) and rule_id
                    for rule_id in stored_rule_ids
                )
                or stored_rule_ids != sorted(set(stored_rule_ids))
            ):
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "semantic policy recovery found an invalid rule set",
                )
        elif stored_rule_ids is not None:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "semantic policy recovery found an invalid undo rule set",
            )
        stored_request_digest = payload.get("semantic_request_digest")
        if stored_request_digest is not None and (
            not isinstance(stored_request_digest, str)
            or _SHA256_RE.fullmatch(stored_request_digest) is None
        ):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "semantic policy recovery found an invalid request identity",
            )
        decoded = _decode_v4_proposal_binding(
            vault_root,
            proposal_id=str(proposal_id),
            proposal_json=str(proposal_json),
            membership_manifest=str(membership_manifest),
            created_at=float(created_at),
            verify_projection_store=status == "pending",
        )
        mirror_terminal = (
            _v4_workspace_mirror_terminal(vault_root, decoded)
            if status == "spent"
            else None
        )
        candidate = (str(proposal_id), str(stored_operation), decoded)
        same_request = (
            stored_operation == operation
            and (stored_operation == "undo" or stored_rule_ids == rule_ids)
            and request_digest is not None
            and stored_request_digest == request_digest
        )
        if status == "pending":
            incomplete.append(candidate)
        elif mirror_terminal is None:
            historical_mirrors.append(candidate)
        elif same_request:
            response_replays.append(candidate)
    if historical_mirrors:
        if len(historical_mirrors) != 1 or incomplete:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "semantic policy recovery is ambiguous",
            )
        _proposal_id, stored_operation, decoded = historical_mirrors[0]
        _verify_recorded_v4_policy_terminal(vault_root, decoded)
        mirror_status = _mirror_v4_policy_workspace(
            vault_root,
            decoded,
            crash_at=crash_at,
        )
        return {
            "status": "committed",
            "event_id": decoded.policy.receipt_event_id,
            "operation": stored_operation,
            "direction": decoded.direction,
            "mirror_status": mirror_status,
        }
    if not incomplete and response_replays:
        if len(response_replays) != 1:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "semantic policy recovery is ambiguous",
            )
        _proposal_id, stored_operation, decoded = response_replays[0]
        _verify_recorded_v4_policy_terminal(vault_root, decoded)
        return {
            "status": "committed",
            "event_id": decoded.policy.receipt_event_id,
            "operation": stored_operation,
            "direction": decoded.direction,
            "mirror_status": _v4_workspace_mirror_terminal(vault_root, decoded),
        }
    candidates = incomplete
    if not candidates:
        return None
    if len(candidates) != 1:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "semantic policy recovery is ambiguous",
        )
    try:
        committed = _commit(
            vault_root,
            principal=who,
            proposal_id=candidates[0][0],
            crash_at=crash_at,
            now=now,
        )
    except GovernanceError as exc:
        if exc.code != "STALE_GOVERNANCE_POLICY":
            raise
        _abort_v4_policy_publication_receipt(vault_root, candidates[0][2])
        _expire_v4_policy_proposal(
            vault_root,
            proposal_id=candidates[0][0],
            expired_at=int(now),
        )
        raise
    return {
        "status": committed["status"],
        "event_id": committed["event_id"],
        "operation": candidates[0][1],
        "direction": committed["direction"],
        "mirror_status": committed["mirror_status"],
    }


def _active_semantic_request_digest() -> str | None:
    from .. import writer_lease

    request_id = writer_lease.active_mutation_request_id()
    if request_id is None:
        return None
    return hashlib.sha256(
        b"exomem.govern-memory-semantic-request.v1\0" + request_id.encode("utf-8")
    ).hexdigest()


def _toggle_rules(vault_root: Path, operation: str, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    raw_ids = kwargs.get("rule_ids")
    if not isinstance(raw_ids, list) or not raw_ids or not all(
        isinstance(rule_id, str) and rule_id for rule_id in raw_ids
    ):
        raise GovernanceError("INVALID_RULE_SET", "rule_ids must be a non-empty list")
    rule_ids = set(raw_ids)
    now = float(kwargs.get("now", time.time()))
    schema_version = store.authorization_session_schema_version(vault_root)
    if schema_version == schema_v4.SCHEMA_USER_VERSION:
        request_digest = _active_semantic_request_digest()
        recovered = _resume_v4_semantic_policy_operation(
            vault_root,
            operation=operation,
            rule_ids=sorted(rule_ids),
            request_digest=request_digest,
            who=who,
            crash_at=kwargs.get("crash_at"),
            now=now,
        )
        if recovered is not None:
            return recovered
    active_snapshot = (
        _v4_active_policy_snapshot(vault_root, now=int(now))
        if schema_version == schema_v4.SCHEMA_USER_VERSION
        else None
    )
    if active_snapshot is None:
        store.require_authoring_schema(vault_root)
    current = (
        active_snapshot.policy
        if active_snapshot is not None
        else policy_module.load(vault_root)
    )
    selected = [rule for rule in current.rules if rule.id in rule_ids]
    if {rule.id for rule in selected} != rule_ids:
        raise GovernanceError("RULE_UNKNOWN", "one or more rules do not exist")
    documents: dict[str, str | None] = {}
    active_documents = (
        dict(active_snapshot.source_documents)
        if active_snapshot is not None
        else None
    )
    if active_documents is not None:
        try:
            documents.update(
                {
                    relative: content.decode("utf-8")
                    for relative, content in active_documents.items()
                }
            )
        except UnicodeDecodeError:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "active policy source bytes are not valid UTF-8 policy documents",
            ) from None
    for source in sorted({rule.source for rule in selected}):
        if active_documents is None:
            source_text = (
                policy_module.governance_root(vault_root) / source
            ).read_text(encoding="utf-8")
        else:
            source_bytes = active_documents.get(source)
            if source_bytes is None:
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "the active policy generation is missing a rule source",
                )
            source_text = source_bytes.decode("utf-8")
        data = yaml.safe_load(source_text)
        if not isinstance(data, dict):
            raise GovernanceError("INVALID_POLICY_DOCUMENT", f"{source} is not a mapping")
        options = data.get("options")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise GovernanceError("INVALID_POLICY_DOCUMENT", f"{source}:options is invalid")
        rule_id = str(data.get("id") or "")
        if rule_id in rule_ids:
            if operation == "suspend":
                options["suspended"] = True
            else:
                options.pop("suspended", None)
        if options:
            data["options"] = options
        else:
            data.pop("options", None)
        documents[source] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if active_snapshot is not None:
        proposed = _proposal(
            vault_root,
            principal=who,
            documents=documents,
            selector_paths=[],
            intent=f"{operation} reviewed governance rules",
            target_ceiling=policy_module.DISCLOSURE_MAX,
            _semantic_operation=operation,
            _semantic_rule_ids=sorted(rule_ids),
            _semantic_request_digest=request_digest,
            now=now,
        )
        committed = _commit(
            vault_root,
            principal=who,
            proposal_id=proposed["proposal_id"],
            crash_at=kwargs.get("crash_at"),
            now=now,
        )
        return {
            "status": committed["status"],
            "event_id": committed["event_id"],
            "operation": operation,
            "direction": committed["direction"],
            "mirror_status": committed["mirror_status"],
        }
    direction = _effective_transition_direction(vault_root, documents)
    return _yaml_transition(
        vault_root,
        transition=operation_variant(operation),
        who=who,
        documents=documents,
        direction=direction,
        crash_at=kwargs.get("crash_at"),
        now=now,
    )


def _v4_generation_operations(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str | None]]:
    operations: dict[str, tuple[str, str | None]] = {}
    try:
        rows = connection.execute(
            "SELECT proposal_json FROM governance_proposals "
            "WHERE status='spent' ORDER BY proposal_id"
        ).fetchall()
        for row in rows:
            payload = json.loads(str(row[0]))
            binding = payload.get("authority_binding")
            target = binding.get("target") if isinstance(binding, dict) else None
            generation_id = (
                target.get("generation_id") if isinstance(target, dict) else None
            )
            if not isinstance(generation_id, str) or not generation_id:
                continue
            operation = payload.get("semantic_operation", "commit")
            source = payload.get("undo_source_generation_id")
            if operation not in {"commit", "suspend", "resume", "undo"} or (
                operation == "undo" and (not isinstance(source, str) or not source)
            ):
                raise ValueError
            value = (operation, source if isinstance(source, str) else None)
            if generation_id in operations and operations[generation_id] != value:
                raise ValueError
            operations[generation_id] = value
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy generation history cannot be interpreted exactly",
        ) from None
    return operations


def _v4_undo_generations(
    connection: sqlite3.Connection,
    *,
    active_generation_id: str,
) -> tuple[str, schema_v4.PolicyGenerationSeed]:
    operations = _v4_generation_operations(connection)
    undone = {
        source
        for operation, source in operations.values()
        if operation == "undo" and source is not None
    }
    candidate_id: str | None = active_generation_id
    seen: set[str] = set()
    while candidate_id is not None:
        if candidate_id in seen:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "policy generation history contains a cycle",
            )
        seen.add(candidate_id)
        try:
            candidate = schema_v4.load_policy_generation(connection, candidate_id)
        except schema_v4.SchemaV4Error:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "policy generation history cannot be verified",
            ) from None
        operation = operations.get(candidate_id, ("migration", None))[0]
        if (
            operation != "undo"
            and candidate_id not in undone
            and candidate.predecessor_generation_id is not None
        ):
            try:
                target = schema_v4.load_policy_generation(
                    connection,
                    candidate.predecessor_generation_id,
                )
            except schema_v4.SchemaV4Error:
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED",
                    "undo target generation cannot be verified",
                ) from None
            return candidate_id, target
        candidate_id = candidate.predecessor_generation_id
    raise GovernanceError("NOTHING_TO_UNDO", "no policy generation is available to undo")


def _undo(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    now = float(kwargs.get("now", time.time()))
    if store.authorization_session_schema_version(vault_root) == schema_v4.SCHEMA_USER_VERSION:
        request_digest = _active_semantic_request_digest()
        recovered = _resume_v4_semantic_policy_operation(
            vault_root,
            operation="undo",
            rule_ids=None,
            request_digest=request_digest,
            who=who,
            crash_at=kwargs.get("crash_at"),
            now=now,
        )
        if recovered is not None:
            return recovered
        active_snapshot = _v4_active_policy_snapshot(vault_root, now=int(now))
        connection = store.open_authorization_session_connection(vault_root)
        try:
            source_generation_id, target_generation = _v4_undo_generations(
                connection,
                active_generation_id=active_snapshot.active.policy_generation_id,
            )
        finally:
            connection.close()
        try:
            documents = {
                relative: content.decode("utf-8")
                for relative, content in target_generation.source_documents
            }
        except UnicodeDecodeError:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "undo target source bytes are not valid UTF-8 policy documents",
            ) from None
        proposed = _proposal(
            vault_root,
            principal=who,
            documents=documents,
            selector_paths=[],
            intent="undo the last reviewed governance policy change",
            target_ceiling=policy_module.DISCLOSURE_MAX,
            _semantic_operation="undo",
            _undo_source_generation_id=source_generation_id,
            _semantic_request_digest=request_digest,
            now=now,
        )
        committed = _commit(
            vault_root,
            principal=who,
            proposal_id=proposed["proposal_id"],
            crash_at=kwargs.get("crash_at"),
            now=now,
        )
        return {
            "status": committed["status"],
            "event_id": committed["event_id"],
            "operation": "undo",
            "direction": committed["direction"],
            "mirror_status": committed["mirror_status"],
        }
    store.require_authoring_schema(vault_root)
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT event_id FROM governance_operation_journals j "
            "WHERE phase='closed' AND operation IN "
            "('commit','suspend','resume','standing_grant','standing_revoke') "
            "AND NOT EXISTS (SELECT 1 FROM governance_operation_journals u "
            "WHERE u.operation='undo' AND u.proposal_id='undo:' || j.event_id "
            "AND u.phase='closed') ORDER BY updated_at DESC, event_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise GovernanceError("NOTHING_TO_UNDO", "no archived policy change is available")
        source_event_id = str(row[0])
        archive_rows = conn.execute(
            "SELECT path, prior_bytes FROM governance_policy_archives "
            "WHERE event_id=? ORDER BY path",
            (source_event_id,),
        ).fetchall()
        grant_rows = conn.execute(
            "SELECT grant_id, paths, fingerprints, membership_manifest "
            "FROM governance_session_grants "
            "WHERE status='active' ORDER BY grant_id"
        ).fetchall()
    finally:
        conn.close()
    if not archive_rows:
        raise GovernanceError("ARCHIVE_MISSING", "the prior policy archive is unavailable")
    documents = {
        str(path): (None if prior_bytes is None else bytes(prior_bytes).decode("utf-8"))
        for path, prior_bytes in archive_rows
    }
    restored_policy = _prospective_policy(vault_root, documents)
    if restored_policy.blocked:
        raise GovernanceError("ARCHIVE_INVALID", "the restored policy does not compile")
    dependent_targets: dict[str, str] = {}
    for grant_id, raw_paths, raw_fingerprints, raw_membership in grant_rows:
        paths = [str(path) for path in json.loads(str(raw_paths))]
        fingerprints = [str(value) for value in json.loads(str(raw_fingerprints))]
        current = [_content_hash(vault_root / path) for path in paths]
        restored_membership = _resolved_membership_manifest(
            vault_root, restored_policy, paths
        )
        dependent_targets[str(grant_id)] = (
            "active"
            if current == fingerprints
            and restored_membership == json.loads(str(raw_membership))
            else "expired"
        )
    direction = _effective_transition_direction(vault_root, documents)
    return _yaml_transition(
        vault_root,
        transition=operation_variant("undo"),
        who=who,
        documents=documents,
        direction=direction,
        dependent_targets=dependent_targets,
        source_event_id=source_event_id,
        crash_at=kwargs.get("crash_at"),
        now=now,
    )


from .recovery import (  # noqa: E402 - imported after circular protocol helpers exist
    _activate_event,
    _remove_marker,
    reconcile_governance_operations,
)


def _selected_variant(
    operation: str, _spec: OperationSpec, kwargs: Mapping[str, Any]
) -> OperationVariant:
    if operation == "session":
        action = kwargs.get("session_action")
        if action not in _SESSION_ACTIONS:
            raise GovernanceError(
                "INVALID_AUTHORIZATION_SESSION_ACTION",
                "session_action must be open, status, rotate, or close",
            )
        return select_operation(operation)
    if kwargs.get("session_action") is not None:
        raise GovernanceError(
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
            "session_action is valid only when operation is session",
        )
    if operation == "backfill_companion":
        action = kwargs.get("backfill_action")
        if action not in {"preview", "commit"}:
            raise GovernanceError(
                "INVALID_COMPANION_BACKFILL",
                "backfill_action must be preview or commit",
            )
        return select_operation(operation, "commit" if action == "commit" else None)
    scope = kwargs.get("scope")
    if operation == "grant" and scope not in (None, "session", "standing"):
        raise GovernanceError(
            "INVALID_GRANT_SCOPE", "scope must be omitted, session, or standing"
        )
    return select_operation(operation, scope)


def _validate_session_arguments(kwargs: Mapping[str, Any]) -> None:
    action = kwargs.get("session_action")
    if action not in _SESSION_ACTIONS:
        raise GovernanceError(
            "INVALID_AUTHORIZATION_SESSION_ACTION",
            "session_action must be open, status, rotate, or close",
        )
    if set(kwargs) - _SESSION_ARGUMENTS:
        raise GovernanceError(
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
            "session action received unsupported arguments",
        )
    ttl = kwargs.get("ttl_seconds")
    if action in {"open", "rotate"}:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or not 1 <= ttl <= authorization_session_lifecycle.MAX_SESSION_TTL_SECONDS
        ):
            raise GovernanceError(
                "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
                "open and rotate require a bounded ttl_seconds",
            )
    elif ttl is not None:
        raise GovernanceError(
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
            "status and close forbid ttl_seconds",
        )
    if action == "open" and kwargs.get("authorization_session") is not None:
        raise GovernanceError(
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
            "session open forbids an existing authorization-session echo",
        )
    supplied_now = kwargs.get("now")
    if supplied_now is not None and (
        isinstance(supplied_now, bool)
        or not isinstance(supplied_now, int)
        or supplied_now <= 0
    ):
        raise GovernanceError(
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
            "session time must be a positive integer",
        )


def _trusted_session_principal(value: RequestPrincipal | None) -> RequestPrincipal:
    who = _principal(value)
    if (
        not who.resolved
        or _bounded_session_identity(who.audience_id) is None
        or _bounded_session_identity(who.issuer_family) is None
    ):
        raise GovernanceError(
            "AUTHORIZATION_SESSION_REQUIRED",
            "trusted principal and issuer context are required",
        )
    return who


def _bounded_session_identity(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value if len(encoded) <= 512 else None


def _verified_session_context(
    who: RequestPrincipal,
    supplied_echo: object,
) -> authorization_session_lifecycle.AuthorizationSessionContext:
    context = who.verified_authorization_session
    if (
        not isinstance(
            context,
            authorization_session_lifecycle.AuthorizationSessionContext,
        )
        or context.principal_id != who.audience_id
        or context.issuer_family != who.issuer_family
    ):
        raise GovernanceError(
            "AUTHORIZATION_SESSION_REQUIRED",
            "a verified authorization session is required",
        )
    for echo in (who.authorization_session_id, supplied_echo):
        if echo is None:
            continue
        if (
            _bounded_session_identity(echo) is None
            or not __import__("hmac").compare_digest(echo, context.session_id)
        ):
            raise GovernanceError(
                "AUTHORIZATION_SESSION_REQUIRED",
                "legacy authorization-session echo does not match verified context",
            )
    return context


def _session_status_response(
    context: authorization_session_lifecycle.AuthorizationSessionContext,
) -> dict[str, Any]:
    return {
        "status": "active",
        "credential_generation": context.credential_generation,
        "expires_at": datetime.fromtimestamp(context.expires_at, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def _session(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    action = str(kwargs["session_action"])
    who = _trusted_session_principal(kwargs.get("principal"))
    now = int(kwargs.get("now", int(time.time())))
    context: authorization_session_lifecycle.AuthorizationSessionContext | None = None
    if action == "open":
        if (
            who.verified_authorization_session is not None
            or who.authorization_session_id is not None
        ):
            raise GovernanceError(
                "AUTHORIZATION_SESSION_CREDENTIAL_FORBIDDEN",
                "session open forbids existing authorization-session authority",
            )
    else:
        context = _verified_session_context(
            who,
            kwargs.get("authorization_session"),
        )

    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(vault_root, now=now)
        connection = store.open_authorization_session_connection(vault_root)
        if action == "open":
            issuance = authorization_session_lifecycle.open_session(
                connection,
                custody=custody,
                principal_id=who.audience_id,
                issuer_family=who.issuer_family,
                now=now,
                ttl_seconds=int(kwargs["ttl_seconds"]),
            )
            return issuance.response()
        if context is None:  # pragma: no cover - guarded above
            raise authorization_session_lifecycle.AuthorizationSessionUnavailable
        if action == "status":
            current = authorization_session_lifecycle.status_verified_session(
                connection,
                custody=custody,
                context=context,
                now=now,
            )
            return _session_status_response(current)
        if action == "rotate":
            issuance = authorization_session_lifecycle.rotate_verified_session(
                connection,
                custody=custody,
                context=context,
                now=now,
                ttl_seconds=int(kwargs["ttl_seconds"]),
            )
            return issuance.response()
        authorization_session_lifecycle.close_verified_session(
            connection,
            custody=custody,
            context=context,
            now=now,
        )
        return {"status": "closed"}
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
    ):
        raise GovernanceError(
            "AUTHORIZATION_SESSION_UNAVAILABLE",
            "authorization session is unavailable",
        ) from None
    finally:
        if connection is not None:
            connection.close()


def _inspect(vault_root: Path, operation: str, **kwargs: Any) -> dict[str, Any]:
    from .inspection import InspectionError, inspect_operation

    try:
        return inspect_operation(vault_root, operation, **kwargs)
    except InspectionError as exc:
        raise GovernanceError(exc.code, exc.reason) from exc


def _not_implemented(_vault_root: Path, operation: str, **_kwargs: Any) -> dict[str, Any]:
    raise GovernanceError("GOVERNANCE_OPERATION_UNAVAILABLE", f"{operation} is not implemented")


_HANDLER_STRATEGIES: Mapping[str, Any] = MappingProxyType(
    {
        "backfill_companion_commit": _backfill_commit,
        "backfill_companion_preview": _backfill_preview,
        "inspect": _inspect,
        "proposal": _proposal,
        "commit": _commit,
        "grant_session": _grant,
        "grant_standing": _standing_grant,
        "revoke_session": _revoke,
        "revoke_standing": _standing_revoke,
        "session": _session,
        "toggle_rules": _toggle_rules,
        "undo": _undo,
        "declare": _declare,
    }
)
if frozenset(_HANDLER_STRATEGIES) != HANDLER_STRATEGY_KEYS:
    raise RuntimeError("governance handler strategies do not cover registry keys")


def op_govern_memory(vault_root: Path, operation: str, **kwargs: Any) -> dict[str, Any]:
    """Inspect or author opt-in confidential governance policy."""
    # This lookup is deliberately the first executable action.  Unknown input
    # must not create a sidecar, policy directory, receipt, or marker.
    spec = OPERATION_SPECS.get(operation)
    if spec is None:
        raise GovernanceError(
            "UNKNOWN_GOVERNANCE_OPERATION", f"unsupported operation {operation!r}"
        )
    if not reserved_paths.owner_authorized("governance-tree"):
        raise GovernanceError(
            "GOVERNANCE_AUTHORITY_REQUIRED",
            "governance operations require dispatcher-issued owner authority",
        )
    root = Path(vault_root)
    selection = _selected_variant(operation, spec, kwargs)
    if operation == "session":
        _validate_session_arguments(kwargs)
    if not spec.read_only:
        _authorize_operation(root, selection, kwargs)
        # One registry-driven gate for every authoring operation, rather than
        # thirteen `.blocked` sites. Prospective policy paths independently
        # re-probe the conflict set around their held-handle byte acquisition;
        # this fast gate also covers authoring variants that do not compile.
        if operation != "session" and policy_module.has_conflict_copy(root):
            raise GovernanceError(
                "GOVERNANCE_CONFLICTED",
                "a synchronisation conflict copy is present under _Governance/; "
                "resolve it before authoring policy",
            )
    handler = _HANDLER_STRATEGIES.get(selection.handler_key)
    if not callable(handler):
        raise GovernanceError(
            "GOVERNANCE_OPERATION_UNAVAILABLE", f"{operation} has no handler"
        )
    result = handler(root, operation=operation, _selection=selection, **kwargs)
    if not spec.read_only:
        from .. import writer_lease

        writer_lease.mark_active_mutation_committed()
    return result
