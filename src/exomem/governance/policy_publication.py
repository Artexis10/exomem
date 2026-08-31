"""Proposal-neutral exact publication of a reviewed schema-v4 policy tuple."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import find_corpus, reserved_paths
from . import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    membership,
    projection_store,
    projections,
    receipts,
    schema_v4,
    store,
)
from . import policy as policy_module
from .transaction import GovernanceError


@dataclass(frozen=True, slots=True)
class PolicyPublicationIdentity:
    """The immutable identity shared by publication receipts and SQLite state."""

    receipt_event_id: str
    policy_generation_id: str


@dataclass(frozen=True, slots=True)
class PreparedPolicyPublication:
    """One exact reviewed v4 tuple, independent of its proposal lifecycle."""

    identity: PolicyPublicationIdentity
    expected: schema_v4.VerifiedActiveGovernanceState
    policy: schema_v4.PolicyGenerationSeed
    catalog: schema_v4.CatalogGenerationSeed | None
    namespace: schema_v4.ProjectionNamespaceSeed
    dependent_grants: tuple[schema_v4.DependentGrantTransition, ...] | None


@dataclass(frozen=True, slots=True)
class PolicyPublicationClassification:
    """The monotonic SQLite/custody classification of one exact publication."""

    state: Literal["activated", "recovered", "stale"]
    active: schema_v4.VerifiedActiveGovernanceState | None


@dataclass(frozen=True, slots=True)
class AuthorityClassification:
    """Read-only relation between custody and one exact reviewed tuple."""

    state: Literal["prior", "tuple-committed", "active", "mixed"]
    active: schema_v4.VerifiedActiveGovernanceState | None


@dataclass(frozen=True, slots=True)
class CriticalReceipt:
    """Exact evidence for one receipt-first policy-publication effect."""

    event_id: str
    operation: str
    prior: str
    prepared: str
    target: str
    affected_ids: tuple[str, ...]
    parent_causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceMirror:
    """The exact receipt and terminal vocabulary of a non-authoritative mirror."""

    receipt: CriticalReceipt
    outcomes: frozenset[str]


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceMirror:
    """Exact source/target bytes behind one receipt-first workspace mirror."""

    publication: PreparedPolicyPublication
    mirror: WorkspaceMirror
    reviewed: policy_module.AuthoringSnapshot


@dataclass(frozen=True, slots=True)
class StagedTargetProjection:
    """One exact inert target namespace derived from the active catalog."""

    catalog: schema_v4.CatalogGenerationSeed | None
    namespace: dict[str, object]
    predecessor_items: tuple[projection_store.ProjectionItemVariants, ...]
    items: tuple[projection_store.ProjectionItemVariants, ...]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def load_active_authority_snapshot(
    vault_root: Path,
    *,
    now: int,
) -> tuple[
    authorization_custody.AuthorizationCustody,
    schema_v4.ActivePolicySnapshot,
]:
    """Load one custody-bound active v4 policy snapshot."""

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
            raise schema_v4.SchemaV4Error(
                "external activation authority is incomplete"
            )
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


def stage_target_projection_namespace(
    vault_root: Path,
    *,
    active_snapshot: schema_v4.ActivePolicySnapshot,
    target_policy: policy_module.Policy,
    ready_at: int,
) -> StagedTargetProjection:
    """Stage and verify the exact target projection namespace without activation."""

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
        target_catalog_generation = (
            active_snapshot.active.catalog_generation + int(membership_changed)
        )
        key = projections.ProjectionNamespaceKey(
            policy_fingerprint=target_policy.fingerprint,
            projector_schema_version=(
                active_snapshot.active.projector_schema_version
            ),
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
        evidence = projection_store.projection_namespace_evidence_bytes(
            target_manifest
        )
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
    return StagedTargetProjection(
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


def dependent_grant_transitions(
    vault_root: Path,
    *,
    current_policy: policy_module.Policy,
    target_policy: policy_module.Policy,
    predecessor_items: tuple[projection_store.ProjectionItemVariants, ...],
    target_items: tuple[projection_store.ProjectionItemVariants, ...],
) -> tuple[schema_v4.DependentGrantTransition, ...]:
    """Bind every active dependent grant to an exact non-active successor."""

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
    predecessor_by_path = {
        item.item_identity: item for item in predecessor_items
    }
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
            reviewed_membership = authorization_session_authority._load_membership(  # noqa: SLF001
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
                or not all(
                    isinstance(scope_id, str) and scope_id
                    for scope_id in scope_ids
                )
                or scope_ids != sorted(set(scope_ids))
                or tuple(paths)
                != tuple(item.path for item in reviewed_membership)
                or tuple(fingerprints)
                != tuple(item.fingerprint for item in reviewed_membership)
                or any(path not in predecessor_by_path for path in paths)
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


def prepare_destination_policy_publication(
    vault_root: Path,
    *,
    prospective: policy_module.ProspectiveCompile,
    document_edits: Mapping[str, str | None],
    generation_id: str,
    authoring_event_id: str,
    receipt_event_id: str,
    ready_at: int,
    now: int,
) -> PreparedPolicyPublication:
    """Prepare exact v4 seeds from one freshly reviewed destination policy."""

    _custody, active_snapshot = load_active_authority_snapshot(
        vault_root,
        now=now,
    )
    live = policy_module.compile_prospective(vault_root, dict(document_edits))
    if (
        not isinstance(prospective, policy_module.ProspectiveCompile)
        or live is None
        or live != prospective
        or prospective.policy.empty
        or prospective.policy.blocked
        or prospective.policy.conflicted
        or active_snapshot.active.policy_fingerprint
        != active_snapshot.policy.fingerprint
    ):
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed policy workspace changed",
        )
    staged = stage_target_projection_namespace(
        vault_root,
        active_snapshot=active_snapshot,
        target_policy=prospective.policy,
        ready_at=ready_at,
    )
    grants = dependent_grant_transitions(
        vault_root,
        current_policy=active_snapshot.policy,
        target_policy=prospective.policy,
        predecessor_items=staged.predecessor_items,
        target_items=staged.items,
    )
    namespace = staged.namespace
    try:
        namespace_seed = schema_v4.ProjectionNamespaceSeed(
            namespace_id=str(namespace["namespace_id"]),
            evidence=base64.b64decode(str(namespace["evidence"]), validate=True),
            ready_at=int(namespace["ready_at"]),
        )
    except (KeyError, TypeError, ValueError):
        raise GovernanceError(
            "GOVERNANCE_PROJECTION_REBUILD_REQUIRED",
            "the target authorization-projection namespace is unavailable",
        ) from None
    seed = schema_v4.PolicyGenerationSeed(
        generation_id=generation_id,
        source_documents=prospective.target_documents,
        source_fingerprint=prospective.policy.fingerprint,
        conflict_digest=prospective.snapshot.conflict_set_digest,
        compiled_policy=policy_module.canonical_compiled_bytes(
            prospective.policy
        ),
        policy_fingerprint=prospective.policy.fingerprint,
        compiler_schema_version=1,
        projector_schema_version=active_snapshot.active.projector_schema_version,
        predecessor_generation_id=active_snapshot.active.policy_generation_id,
        authoring_event_id=authoring_event_id,
        receipt_event_id=receipt_event_id,
        created_at=ready_at,
    )
    return prepare_policy_publication(
        expected=active_snapshot.active,
        policy=seed,
        catalog=staged.catalog,
        namespace=namespace_seed,
        dependent_grants=grants,
    )


def receipt_terminal(vault_root: Path, receipt: CriticalReceipt) -> str | None:
    """Classify one critical receipt without accepting substituted evidence."""

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
        if record.get("event_id") == receipt.event_id and record.get("phase") == "intent"
    ]
    terminals = [
        record
        for record in records
        if record.get("causation_id") == receipt.event_id
        and record.get("phase") in {"committed", "aborted"}
    ]
    if not intents and not terminals:
        return None
    expected = {
        "event_type": "critical",
        "operation": receipt.operation,
        "prior": receipt.prior,
        "prepared": receipt.prepared,
        "target": receipt.target,
        "affected_ids": list(receipt.affected_ids),
    }
    if receipt.parent_causation_id is not None:
        expected["parent_causation_id"] = receipt.parent_causation_id
    if len(intents) != 1 or any(
        intents[0].get(field) != value for field, value in expected.items()
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


def begin_receipt(vault_root: Path, receipt: CriticalReceipt) -> None:
    """Durably record the exact critical intent once."""

    terminal = receipt_terminal(vault_root, receipt)
    if terminal == "committed":
        return
    if terminal == "aborted":
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY",
            "the reviewed policy publication was already refused",
        )
    if terminal == "pending":
        return
    try:
        receipts.begin_event(
            vault_root,
            operation=receipt.operation,
            prior=receipt.prior,
            prepared=receipt.prepared,
            target=receipt.target,
            affected_ids=list(receipt.affected_ids),
            event_id=receipt.event_id,
            parent_causation_id=receipt.parent_causation_id,
        )
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy publication receipt intent could not be recorded",
        ) from None


def commit_receipt(vault_root: Path, receipt: CriticalReceipt) -> None:
    """Close an exact critical receipt only as committed."""

    terminal = receipt_terminal(vault_root, receipt)
    if terminal == "committed":
        return
    if terminal == "aborted":
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "committed policy publication has an aborted receipt",
        )
    if terminal is None:
        begin_receipt(vault_root, receipt)
    try:
        receipts.commit_event(vault_root, receipt.event_id, outcome="committed")
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy publication receipt terminal could not be recorded",
        ) from None


def abort_receipt(vault_root: Path, receipt: CriticalReceipt) -> None:
    """Close an in-flight exact receipt as a stale predecessor."""

    terminal = receipt_terminal(vault_root, receipt)
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
            receipt.event_id,
            outcome="stale_predecessor",
        )
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "stale policy publication receipt could not be closed",
        ) from None


def workspace_mirror_terminal(
    vault_root: Path,
    mirror: WorkspaceMirror,
) -> str | None:
    """Return only a receipt-proven mirror outcome from its closed vocabulary."""

    receipt = mirror.receipt
    try:
        records = receipts.event_records(vault_root)
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror evidence cannot be verified",
        ) from None
    intents = [
        record
        for record in records
        if record.get("event_id") == receipt.event_id and record.get("phase") == "intent"
    ]
    terminals = [
        record
        for record in records
        if record.get("causation_id") == receipt.event_id
        and record.get("phase") in {"committed", "aborted"}
    ]
    if not intents and not terminals:
        return None
    expected = {
        "event_type": "critical",
        "operation": receipt.operation,
        "prior": receipt.prior,
        "prepared": receipt.prepared,
        "target": receipt.target,
        "affected_ids": list(receipt.affected_ids),
    }
    if receipt.parent_causation_id is not None:
        expected["parent_causation_id"] = receipt.parent_causation_id
    if len(intents) != 1 or any(
        intents[0].get(field) != value for field, value in expected.items()
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
    if outcome not in mirror.outcomes:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror evidence has an unknown outcome",
        )
    return str(outcome)


def run_workspace_mirror(
    vault_root: Path,
    mirror: WorkspaceMirror,
    apply: Callable[[], str],
    *,
    after_intent: Callable[[], None] | None = None,
) -> str:
    """Run one receipt-first non-authoritative workspace mirror exactly once."""

    existing = workspace_mirror_terminal(vault_root, mirror)
    if existing is not None:
        return existing
    receipt = mirror.receipt
    try:
        receipts.begin_event(
            vault_root,
            operation=receipt.operation,
            prior=receipt.prior,
            prepared=receipt.prepared,
            target=receipt.target,
            affected_ids=list(receipt.affected_ids),
            event_id=receipt.event_id,
            parent_causation_id=receipt.parent_causation_id,
        )
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror intent could not be recorded",
        ) from None
    if after_intent is not None:
        after_intent()
    outcome = apply()
    if outcome not in mirror.outcomes:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the committed policy tuple is active but its workspace mirror needs retry",
        )
    try:
        receipts.commit_event(vault_root, receipt.event_id, outcome=outcome)
    except receipts.ReceiptError:
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "policy workspace mirror outcome could not be recorded",
        ) from None
    return outcome


def prepare_workspace_mirror(
    publication: PreparedPolicyPublication,
    *,
    mirror: WorkspaceMirror,
    reviewed: policy_module.AuthoringSnapshot,
) -> PreparedWorkspaceMirror:
    """Bind a mirror receipt to the exact reviewed and published document sets."""

    if (
        not isinstance(publication, PreparedPolicyPublication)
        or not isinstance(mirror, WorkspaceMirror)
        or not isinstance(reviewed, policy_module.AuthoringSnapshot)
    ):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the exact policy workspace mirror cannot be prepared",
        )
    compiled = policy_module.compile_documents(
        dict(publication.policy.source_documents)
    )
    if (
        compiled.empty
        or compiled.blocked
        or compiled.conflicted
        or compiled.fingerprint != publication.policy.policy_fingerprint
        or policy_module.canonical_compiled_bytes(compiled)
        != publication.policy.compiled_policy
        or mirror.receipt.parent_causation_id
        != publication.identity.receipt_event_id
    ):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the exact policy workspace mirror cannot be prepared",
        )
    return PreparedWorkspaceMirror(
        publication=publication,
        mirror=mirror,
        reviewed=reviewed,
    )


def run_prepared_workspace_mirror(
    vault_root: Path,
    prepared: PreparedWorkspaceMirror,
    *,
    barrier: Callable[[str, str], None] | None = None,
) -> str:
    """Mirror exact published bytes through the shared receipt-first primitive."""

    if not isinstance(prepared, PreparedWorkspaceMirror):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the exact policy workspace mirror is unavailable",
        )
    if not reserved_paths.owner_authorized("governance-tree"):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the exact policy workspace mirror lacks caller authority",
        )

    def apply() -> str:
        return policy_module.mirror_authoring_workspace(
            vault_root,
            reviewed=prepared.reviewed,
            target_documents=prepared.publication.policy.source_documents,
            barrier=barrier,
        )

    return run_workspace_mirror(
        vault_root,
        prepared.mirror,
        apply,
    )


def prepared_workspace_mirror_matches(
    vault_root: Path,
    prepared: PreparedWorkspaceMirror,
) -> bool:
    """Re-observe exact target bytes before trusting a completed mirror receipt."""

    if not isinstance(prepared, PreparedWorkspaceMirror):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "the exact policy workspace mirror is unavailable",
        )
    observed = policy_module.observe_authoring_snapshot(vault_root)
    return (
        observed is not None
        and observed.documents == prepared.publication.policy.source_documents
    )


def prepare_policy_publication(
    *,
    expected: schema_v4.VerifiedActiveGovernanceState,
    policy: schema_v4.PolicyGenerationSeed,
    catalog: schema_v4.CatalogGenerationSeed | None,
    namespace: schema_v4.ProjectionNamespaceSeed,
    dependent_grants: tuple[schema_v4.DependentGrantTransition, ...] | None,
) -> PreparedPolicyPublication:
    """Bind reviewed v4 seeds to their durable publication identity."""

    return PreparedPolicyPublication(
        identity=PolicyPublicationIdentity(
            receipt_event_id=policy.receipt_event_id,
            policy_generation_id=policy.generation_id,
        ),
        expected=expected,
        policy=policy,
        catalog=catalog,
        namespace=namespace,
        dependent_grants=dependent_grants,
    )


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


def _committed_target(
    connection: sqlite3.Connection,
    prepared: PreparedPolicyPublication,
) -> schema_v4.VerifiedActiveGovernanceState | None:
    target_catalog_generation = (
        prepared.expected.catalog_generation
        if prepared.catalog is None
        else prepared.catalog.catalog_generation
    )
    row = connection.execute(
        "SELECT publication_kind, predecessor_activation_state_digest, "
        "target_activation_state_digest, policy_generation_id, policy_fingerprint, "
        "projector_schema_version, catalog_generation, activation_epoch, status "
        "FROM governance_tuple_publications WHERE event_id=?",
        (prepared.identity.receipt_event_id,),
    ).fetchone()
    if row is None:
        return None
    target = schema_v4.load_active_tuple_pointer(connection)
    if (
        tuple(row)
        != (
            "policy",
            prepared.expected.activation_state_digest,
            target.activation_state_digest,
            prepared.identity.policy_generation_id,
            prepared.policy.policy_fingerprint,
            prepared.policy.projector_schema_version,
            target_catalog_generation,
            prepared.expected.activation_epoch + 1,
            "committed",
        )
        or target.logical_vault_id != prepared.expected.logical_vault_id
        or target.activation_store_id != prepared.expected.activation_store_id
        or target.activation_epoch != prepared.expected.activation_epoch + 1
        or target.policy_generation_id != prepared.identity.policy_generation_id
        or target.policy_fingerprint != prepared.policy.policy_fingerprint
        or target.projector_schema_version != prepared.policy.projector_schema_version
        or target.catalog_generation != target_catalog_generation
        or target.projection_namespace_id != prepared.namespace.namespace_id
    ):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED",
            "committed policy publication does not match the reviewed proposal",
        )
    active_count = connection.execute(
        "SELECT COUNT(*) FROM governance_session_grants WHERE status='active'"
    ).fetchone()
    if prepared.dependent_grants is not None:
        committed_grants = (
            connection.execute(
                "SELECT grant_id, status, policy_fingerprint, membership_manifest, "
                "prepared_event_id FROM governance_session_grants "
                "WHERE grant_id IN ("
                + ",".join("?" for _ in prepared.dependent_grants)
                + ") ORDER BY grant_id",
                tuple(transition.grant_id for transition in prepared.dependent_grants),
            ).fetchall()
            if prepared.dependent_grants
            else []
        )
        expected_grants = [
            (
                transition.grant_id,
                transition.target_status,
                transition.target_policy_fingerprint,
                transition.target_membership_manifest,
                None,
            )
            for transition in prepared.dependent_grants
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


def classify_authority(
    vault_root: Path,
    prepared: PreparedPolicyPublication,
    *,
    now: int,
) -> AuthorityClassification:
    """Classify SQLite/custody state without repairing either side."""

    connection = store.open_authorization_session_connection(vault_root)
    try:
        target = _committed_target(connection, prepared)
        if target is None:
            active = schema_v4.load_active_tuple_pointer(connection)
            if active != prepared.expected:
                return AuthorityClassification("mixed", active)
            custody = authorization_custody.load_authorization_custody(
                vault_root,
                now=now,
            )
            state: Literal["prior", "tuple-committed", "active", "mixed"] = (
                "prior"
                if _control_matches_active(custody.control, prepared.expected)
                else "mixed"
            )
            return AuthorityClassification(state, None)
        custody = authorization_custody.load_authorization_custody(vault_root, now=now)
        if _control_matches_active(custody.control, target):
            return AuthorityClassification("active", target)
        if _control_matches_active(custody.control, prepared.expected):
            return AuthorityClassification("tuple-committed", target)
        return AuthorityClassification("mixed", target)
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
    finally:
        connection.close()


def recover(
    vault_root: Path,
    *,
    connection: sqlite3.Connection,
    prepared: PreparedPolicyPublication,
    now: int,
) -> schema_v4.VerifiedActiveGovernanceState | None:
    """Recover custody acknowledgement only for this exact committed tuple."""

    try:
        target = _committed_target(connection, prepared)
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
        if not _control_matches_active(custody.control, prepared.expected):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED",
                "external activation authority names neither reviewed policy state",
            )
        recovered = schema_v4.recover_registry_acknowledgement(
            connection,
            expected=prepared.expected,
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


def activate_or_recover(
    vault_root: Path,
    prepared: PreparedPolicyPublication,
    *,
    now: int,
) -> PolicyPublicationClassification:
    """Atomically activate a reviewed tuple or classify its exact CAS winner."""

    connection = store.open_authorization_session_connection(vault_root)
    try:
        try:
            custody = authorization_custody.load_authorization_custody(
                vault_root,
                now=now,
            )
            published = schema_v4.publish_policy_generation(
                connection,
                expected=prepared.expected,
                policy=prepared.policy,
                catalog=prepared.catalog,
                namespace=prepared.namespace,
                # A pre-v4 proposal did not bind grants. Passing an exact
                # empty tuple makes the transaction prove there are none;
                # it must never silently preserve unreviewed active rows.
                dependent_grants=prepared.dependent_grants or (),
                activated_at=now,
                acknowledge_registry=lambda active: (
                    authorization_custody.acknowledge_activation_tuple(
                        vault_root,
                        expected_control=custody.control,
                        target=active,
                        now=now,
                    )
                ),
            )
            return PolicyPublicationClassification("activated", published.active)
        except schema_v4.ActiveTupleStale:
            recovered = recover(
                vault_root,
                connection=connection,
                prepared=prepared,
                now=now,
            )
            if recovered is not None:
                return PolicyPublicationClassification("recovered", recovered)
            return PolicyPublicationClassification("stale", None)
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
