"""Publish canonical Markdown changes through the exact-v4 catalog tuple.

This is the content-side dual of policy publication.  It prepares a complete
immutable successor namespace while the predecessor is still active, then the
caller writes the already-reviewed canonical bytes and advances the catalog by
one full-tuple CAS before returning success.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import find_corpus
from . import (
    authorization_custody,
    membership,
    projection_store,
    projections,
    receipts,
    schema_v4,
    store,
)


class CatalogPublicationError(RuntimeError):
    """A content change cannot safely advance the active v4 catalog."""


@dataclass(frozen=True, slots=True)
class PreparedMarkdownCatalogPublication:
    """One complete lexical-only catalog successor awaiting canonical bytes."""

    vault_root: Path
    expected: schema_v4.VerifiedActiveGovernanceState
    expected_control: authorization_custody.AuthorizationControlRecord
    target_key: projections.ProjectionNamespaceKey
    target_items: tuple[projection_store.ProjectionItemVariants, ...]
    catalog_descriptor: bytes
    activated_at: int


def _custody_configured() -> bool:
    return any(
        os.environ.get(name, "").strip()
        for name in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
        )
    )


def _relative_markdown_path(vault_root: Path, path: str) -> str:
    value = PurePosixPath(str(path).replace("\\", "/")).as_posix().lstrip("/")
    candidate = PurePosixPath(value)
    if (
        not value
        or candidate.is_absolute()
        or candidate.suffix.casefold() != ".md"
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise CatalogPublicationError("catalog publication requires a safe Markdown path")
    try:
        (vault_root / value).resolve().relative_to(vault_root.resolve())
    except (OSError, ValueError):
        raise CatalogPublicationError("catalog publication path is outside the vault") from None
    return value


def _search_fields(page: find_corpus.ParsedPage) -> dict[str, str]:
    fields = {"title": page.title}
    for name, value in (
        ("body", page.body),
        ("status", page.frontmatter.get("status")),
        ("type", page.page_type),
        ("updated", page.updated),
    ):
        if value:
            fields[name] = str(value)
    return fields


def _control_matches_active(
    control: authorization_custody.AuthorizationControlRecord,
    active: schema_v4.VerifiedActiveGovernanceState,
) -> bool:
    return (
        control.logical_vault_id == active.logical_vault_id
        and control.activation_store_id == active.activation_store_id
        and control.activation_epoch == active.activation_epoch
        and control.activation_state_digest == active.activation_state_digest
    )


def _recover_catalog_acknowledgement(
    vault_root: Path,
    connection: sqlite3.Connection,
    *,
    custody: authorization_custody.AuthorizationCustody,
    now: int,
) -> authorization_custody.AuthorizationCustody:
    """Finish only the exact receipt-proven catalog successor, if present."""

    control = custody.control
    target = schema_v4.load_active_tuple_pointer(connection)
    if _control_matches_active(control, target):
        return custody
    if (
        control.activation_store_id is None
        or control.activation_epoch is None
        or control.activation_state_digest is None
        or target.logical_vault_id != control.logical_vault_id
        or target.activation_store_id != control.activation_store_id
        or target.activation_epoch != control.activation_epoch + 1
        or target.catalog_generation < 1
    ):
        raise CatalogPublicationError(
            "external governance authority does not name a recoverable catalog predecessor"
        )
    publication = connection.execute(
        "SELECT publication_kind, predecessor_activation_state_digest, "
        "policy_generation_id, policy_fingerprint, projector_schema_version, "
        "catalog_generation, activation_epoch, status "
        "FROM governance_tuple_publications WHERE target_activation_state_digest=?",
        (target.activation_state_digest,),
    ).fetchone()
    predecessor_catalog = target.catalog_generation - 1
    predecessor_namespace = connection.execute(
        "SELECT namespace_id FROM governance_projection_namespaces "
        "WHERE policy_fingerprint=? AND projector_schema_version=? "
        "AND catalog_generation=?",
        (
            target.policy_fingerprint,
            target.projector_schema_version,
            predecessor_catalog,
        ),
    ).fetchone()
    if publication != (
        "catalog",
        control.activation_state_digest,
        target.policy_generation_id,
        target.policy_fingerprint,
        target.projector_schema_version,
        target.catalog_generation,
        target.activation_epoch,
        "committed",
    ) or predecessor_namespace is None:
        raise CatalogPublicationError(
            "the committed catalog successor is not receipt-proven"
        )
    expected = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=target.logical_vault_id,
        activation_store_id=target.activation_store_id,
        activation_epoch=control.activation_epoch,
        activation_state_digest=control.activation_state_digest,
        policy_generation_id=target.policy_generation_id,
        policy_fingerprint=target.policy_fingerprint,
        projector_schema_version=target.projector_schema_version,
        catalog_generation=predecessor_catalog,
        projection_namespace_id=str(predecessor_namespace[0]),
    )
    schema_v4.recover_registry_acknowledgement(
        connection,
        expected=expected,
        acknowledge_registry=lambda active: (
            authorization_custody.acknowledge_activation_tuple(
                vault_root,
                expected_control=control,
                target=active,
                now=now,
            )
        ),
    )
    recovered = authorization_custody.load_authorization_custody(
        vault_root, now=now
    )
    if not _control_matches_active(recovered.control, target):
        raise CatalogPublicationError(
            "the committed catalog acknowledgement did not verify"
        )
    return recovered


def _replacement_item(
    *,
    vault_root: Path,
    path: str,
    source: str,
    snapshot: schema_v4.ActivePolicySnapshot,
    target_key: projections.ProjectionNamespaceKey,
) -> projection_store.ProjectionItemVariants:
    content = source.encode("utf-8")
    parsed = find_corpus.parse_page(
        vault_root / path,
        0.0,
        vault_root,
        content=content,
        resolved_relative=path,
    )
    if parsed is None:
        raise CatalogPublicationError("catalog publication cannot parse target Markdown")
    content_hash = hashlib.sha256(content).hexdigest()
    try:
        scope_ids = tuple(
            sorted(
                membership.evaluate_snapshot(
                    parsed,
                    snapshot.policy,
                    content_hash=content_hash,
                )
            )
        )
        variants = projections.enumerate_projection_variants(
            item_identity=path,
            content_hash=content_hash,
            scope_ids=scope_ids,
            policy=snapshot.policy,
            projector_schema_version=target_key.projector_schema_version,
            full_search_fields=_search_fields(parsed),
        )
        return projection_store.ProjectionItemVariants(
            item_identity=path,
            content_hash=content_hash,
            scope_ids=scope_ids,
            variants=variants,
        )
    except (membership.MembershipUnresolved, projections.ProjectionError) as error:
        raise CatalogPublicationError(
            "catalog publication cannot classify the target Markdown"
        ) from error


def prepare_markdown_upsert(
    vault_root: Path,
    *,
    path: str,
    source: str,
    expected_before_hash: str | None,
    now: int | None = None,
) -> PreparedMarkdownCatalogPublication | None:
    """Prepare one complete catalog successor, or return ``None`` for v3/open.

    The current slice intentionally supports lexical-only active namespaces.
    Model and graph measurement roots are content-bound, so reusing them after
    an edit would be unsafe; those deployments refuse before canonical bytes
    change until their rebuild publisher is connected.
    """

    root = Path(vault_root)
    relative = _relative_markdown_path(root, path)
    connection: sqlite3.Connection | None = None
    try:
        connection = store.open_active_governance_read_connection(root)
    except store.UnsupportedGovernanceSchema:
        if _custody_configured():
            try:
                custody = authorization_custody.load_authorization_custody(
                    root, now=int(time.time()) if now is None else now
                )
            except (
                authorization_custody.AuthorizationCustodyUnavailable,
                OSError,
                RuntimeError,
                ValueError,
            ):
                raise CatalogPublicationError(
                    "governance enrollment cannot be verified for content publication"
                ) from None
            if custody.control.governance_enrolled:
                raise CatalogPublicationError(
                    "the enrolled governance activation store is unavailable"
                ) from None
        return None
    except (FileNotFoundError, OSError, sqlite3.Error, RuntimeError, ValueError):
        raise CatalogPublicationError(
            "the governance activation store cannot be opened safely"
        ) from None

    activated_at = int(time.time()) if now is None else now
    try:
        custody = authorization_custody.load_authorization_custody(
            root, now=activated_at
        )
        custody = _recover_catalog_acknowledgement(
            root,
            connection,
            custody=custody,
            now=activated_at,
        )
        control = custody.control
        if (
            not control.governance_enrolled
            or control.activation_store_id is None
            or control.activation_epoch is None
            or control.activation_state_digest is None
        ):
            raise CatalogPublicationError(
                "exact governance enrollment is required for v4 content publication"
            )
        connection.execute("BEGIN")
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=control.activation_store_id,
            expected_activation_epoch=control.activation_epoch,
            expected_activation_state_digest=control.activation_state_digest,
        )
        connection.commit()
        evidence = projection_store.namespace_evidence_from_snapshot(snapshot)
        if evidence.required_measurement_roots:
            raise CatalogPublicationError(
                "content publication requires rebuilt model and graph measurements"
            )
        manifest, active_items = projection_store.load_projection_catalog(
            root,
            key=evidence.manifest.namespace_key,
            expected_rows_digest=evidence.manifest.rows_digest,
        )
        verified = projection_store.bind_active_projection_namespace(
            snapshot,
            manifest=manifest,
            items=active_items,
        )
    except CatalogPublicationError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        membership.MembershipUnresolved,
        projection_store.ProjectionStoreError,
        projections.ProjectionError,
        schema_v4.SchemaV4Error,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ):
        if connection.in_transaction:
            connection.rollback()
        raise CatalogPublicationError(
            "the active governance catalog cannot be verified"
        ) from None
    finally:
        connection.close()

    by_identity = {item.item_identity: item for item in verified.items}
    predecessor = by_identity.get(relative)
    if expected_before_hash is None:
        if predecessor is not None:
            raise CatalogPublicationError("catalog creation target already exists")
    elif predecessor is None or predecessor.content_hash != expected_before_hash:
        raise CatalogPublicationError(
            "catalog content identity no longer matches the reviewed predecessor"
        )

    target_key = projections.ProjectionNamespaceKey(
        policy_fingerprint=snapshot.active.policy_fingerprint,
        projector_schema_version=snapshot.active.projector_schema_version,
        catalog_generation=snapshot.active.catalog_generation + 1,
    )
    by_identity[relative] = _replacement_item(
        vault_root=root,
        path=relative,
        source=source,
        snapshot=snapshot,
        target_key=target_key,
    )
    target_items = tuple(by_identity.values())
    try:
        descriptor = projection_store.catalog_descriptor_bytes(
            target_key, target_items
        )
    except projections.ProjectionError:
        raise CatalogPublicationError(
            "the target governance catalog cannot be prepared"
        ) from None
    return PreparedMarkdownCatalogPublication(
        vault_root=root,
        expected=snapshot.active,
        expected_control=control,
        target_key=target_key,
        target_items=target_items,
        catalog_descriptor=descriptor,
        activated_at=activated_at,
    )


def publish_markdown_upsert(
    prepared: PreparedMarkdownCatalogPublication | None,
) -> schema_v4.TuplePublicationResult | None:
    """Advance one prepared v4 catalog after canonical bytes have committed."""

    if prepared is None:
        return None
    connection: sqlite3.Connection | None = None
    try:
        target_manifest = projection_store.stage_variant_store(
            prepared.vault_root,
            key=prepared.target_key,
            items=prepared.target_items,
        )
        namespace = schema_v4.ProjectionNamespaceSeed(
            namespace_id=prepared.target_key.namespace_id,
            evidence=projection_store.projection_namespace_evidence_bytes(
                target_manifest
            ),
            ready_at=prepared.activated_at,
        )
        catalog = schema_v4.CatalogGenerationSeed(
            catalog_generation=prepared.target_key.catalog_generation,
            descriptor=prepared.catalog_descriptor,
            artifact_count=len(prepared.target_items),
            created_at=prepared.activated_at,
        )
        receipt_event_id = receipts.critical_event_id(
            {
                "operation": "governance_catalog_markdown_upsert",
                "logical_vault_id": prepared.expected.logical_vault_id,
                "predecessor_activation_state_digest": (
                    prepared.expected.activation_state_digest
                ),
                "catalog_generation": prepared.target_key.catalog_generation,
                "catalog_descriptor_digest": hashlib.sha256(
                    prepared.catalog_descriptor
                ).hexdigest(),
                "namespace_id": prepared.target_key.namespace_id,
                "projection_rows_digest": target_manifest.rows_digest,
            }
        )
        connection = store.open_authorization_session_connection(
            prepared.vault_root
        )
        return schema_v4.publish_catalog_generation(
            connection,
            expected=prepared.expected,
            catalog=catalog,
            namespace=namespace,
            receipt_event_id=receipt_event_id,
            activated_at=prepared.activated_at,
            acknowledge_registry=lambda target: (
                authorization_custody.acknowledge_activation_tuple(
                    prepared.vault_root,
                    expected_control=prepared.expected_control,
                    target=target,
                    now=prepared.activated_at,
                )
            ),
        )
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        projection_store.ProjectionStoreError,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ) as error:
        if connection is not None and not connection.in_transaction:
            try:
                custody = authorization_custody.load_authorization_custody(
                    prepared.vault_root,
                    now=prepared.activated_at,
                )
                _recover_catalog_acknowledgement(
                    prepared.vault_root,
                    connection,
                    custody=custody,
                    now=prepared.activated_at,
                )
                target = schema_v4.load_active_tuple_pointer(connection)
                if (
                    target.logical_vault_id == prepared.expected.logical_vault_id
                    and target.activation_store_id
                    == prepared.expected.activation_store_id
                    and target.activation_epoch
                    == prepared.expected.activation_epoch + 1
                    and target.policy_generation_id
                    == prepared.expected.policy_generation_id
                    and target.policy_fingerprint
                    == prepared.expected.policy_fingerprint
                    and target.projector_schema_version
                    == prepared.expected.projector_schema_version
                    and target.catalog_generation
                    == prepared.target_key.catalog_generation
                    and target.projection_namespace_id
                    == prepared.target_key.namespace_id
                ):
                    return None
            except (
                authorization_custody.AuthorizationCustodyUnavailable,
                CatalogPublicationError,
                schema_v4.SchemaV4Error,
                OSError,
                sqlite3.Error,
                RuntimeError,
                ValueError,
            ):
                pass
        raise CatalogPublicationError(
            "the canonical bytes committed but the active catalog outcome is uncertain"
        ) from error
    finally:
        if connection is not None:
            connection.close()


__all__ = [
    "CatalogPublicationError",
    "PreparedMarkdownCatalogPublication",
    "prepare_markdown_upsert",
    "publish_markdown_upsert",
]
