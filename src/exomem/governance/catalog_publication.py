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
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import find_corpus, reserved_paths, vault
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


class CatalogCommitError(CatalogPublicationError):
    """A product write was blocked before bytes or became uncertain after them."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class MarkdownCatalogMutation:
    """One intended canonical Markdown successor and its exact predecessor."""

    path: str
    source: str
    expected_before_hash: str | None


@dataclass(frozen=True, slots=True)
class CatalogRemoval:
    """One intended catalog membership removal and its exact predecessor."""

    path: str
    expected_before_hash: str | None


@dataclass(frozen=True, slots=True)
class PreparedMarkdownCatalogPublication:
    """One complete lexical-only catalog successor awaiting canonical bytes."""

    vault_root: Path
    expected: schema_v4.VerifiedActiveGovernanceState
    expected_control: authorization_custody.AuthorizationControlRecord
    target_key: projections.ProjectionNamespaceKey
    target_items: tuple[projection_store.ProjectionItemVariants, ...]
    target_manifest: projection_store.VariantStoreManifest
    expected_catalog_descriptor: bytes
    catalog_descriptor: bytes
    catalog_seed: schema_v4.CatalogGenerationSeed
    namespace_seed: schema_v4.ProjectionNamespaceSeed
    target_publication: schema_v4.TuplePublicationResult
    activated_at: int
    mutation_count: int


def _catalog_component_value(
    active: schema_v4.VerifiedActiveGovernanceState,
    *,
    catalog_descriptor: bytes,
) -> dict[str, object]:
    return {
        "status": "active",
        "logical_vault_id": active.logical_vault_id,
        "activation_store_id": active.activation_store_id,
        "activation_epoch": active.activation_epoch,
        "activation_state_digest": active.activation_state_digest,
        "policy_generation_id": active.policy_generation_id,
        "policy_fingerprint": active.policy_fingerprint,
        "projector_schema_version": active.projector_schema_version,
        "catalog_generation": active.catalog_generation,
        "projection_namespace_id": active.projection_namespace_id,
        "catalog_descriptor_sha256": hashlib.sha256(catalog_descriptor).hexdigest(),
    }


def catalog_component_values(
    prepared: PreparedMarkdownCatalogPublication,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return the exact journal values before and after one catalog publication."""

    if not isinstance(prepared, PreparedMarkdownCatalogPublication):
        raise CatalogPublicationError("catalog publication target is invalid")
    expected = _catalog_component_value(
        prepared.expected,
        catalog_descriptor=prepared.expected_catalog_descriptor,
    )
    target = _catalog_component_value(
        prepared.target_publication.active,
        catalog_descriptor=prepared.catalog_descriptor,
    )
    return expected, target


def current_catalog_component_value(
    vault_root: Path,
    *,
    now: int | None = None,
) -> dict[str, object]:
    """Load one externally acknowledged active catalog value for recovery."""

    root = Path(vault_root)
    moment = int(time.time()) if now is None else now
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(root, now=moment)
        control = custody.control
        if (
            not control.governance_enrolled
            or control.activation_store_id is None
            or control.activation_epoch is None
            or control.activation_state_digest is None
        ):
            raise CatalogPublicationError("exact governance enrollment is unavailable")
        connection = store.open_authorization_session_connection(root)
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=control.activation_store_id,
            expected_activation_epoch=control.activation_epoch,
            expected_activation_state_digest=control.activation_state_digest,
        )
        return _catalog_component_value(
            snapshot.active,
            catalog_descriptor=snapshot.catalog_descriptor,
        )
    except CatalogPublicationError:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ):
        raise CatalogPublicationError(
            "the active catalog component cannot be verified"
        ) from None
    finally:
        if connection is not None:
            connection.close()


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


def _is_catalog_markdown_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    if (
        candidate.name.casefold().endswith(".md")
        and ".sync-conflict-" not in candidate.name
        and not any(
            part in find_corpus.EXCLUDED_DIR_NAMES
            or part.startswith(find_corpus.EXCLUDED_DIR_PREFIXES)
            for part in candidate.parts[:-1]
        )
        and not reserved_paths.classify_logical(path).blocked
    ):
        return True
    return False


def mutation_from_planned_write(
    vault_root: Path,
    write: vault.PlannedWrite,
) -> MarkdownCatalogMutation | None:
    """Bind one canonical Markdown write to the same predecessor as its file CAS."""

    root = Path(vault_root).absolute()
    try:
        relative = write.path.absolute().relative_to(root).as_posix()
    except (AttributeError, ValueError):
        raise CatalogPublicationError(
            "catalog publication write is outside the vault"
        ) from None
    if PurePosixPath(relative).suffix.casefold() != ".md":
        return None
    relative = _relative_markdown_path(root, relative)
    if not _is_catalog_markdown_path(relative):
        return None

    expected = write.expected_hash
    expected_missing = expected == vault.MISSING_CONTENT_HASH
    if expected_missing:
        expected = None
    guard = write.guard
    if guard is not None:
        if guard.target != relative:
            raise CatalogPublicationError(
                "catalog publication guard does not bind its Markdown target"
            )
        if guard.leaf_policy == "absent":
            guarded_expected = None
        elif guard.leaf_policy == "content":
            guarded_expected = guard.expected_content_hash
        else:
            guarded_expected = expected
        if expected is not None and guarded_expected != expected:
            raise CatalogPublicationError(
                "catalog publication predecessor bindings disagree"
            )
        expected = guarded_expected
    elif expected is None and not write.create_only and not expected_missing:
        raise CatalogPublicationError(
            "catalog Markdown mutation lacks an exact predecessor binding"
        )
    if expected is not None and (
        type(expected) is not str
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise CatalogPublicationError(
            "catalog publication predecessor hash is invalid"
        )
    return MarkdownCatalogMutation(relative, write.content, expected)


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


def _normalize_markdown_mutations(
    vault_root: Path,
    mutations: tuple[MarkdownCatalogMutation, ...],
) -> tuple[tuple[str, MarkdownCatalogMutation], ...]:
    if type(mutations) is not tuple:
        raise CatalogPublicationError(
            "catalog publication requires a finite Markdown mutation batch"
        )
    normalized: list[tuple[str, MarkdownCatalogMutation]] = []
    aliases: set[str] = set()
    for mutation in mutations:
        if type(mutation) is not MarkdownCatalogMutation or type(mutation.source) is not str:
            raise CatalogPublicationError("catalog publication mutation is invalid")
        relative = _relative_markdown_path(vault_root, mutation.path)
        if not _is_catalog_markdown_path(relative):
            raise CatalogPublicationError(
                "catalog publication mutation is not canonical Markdown"
            )
        alias = unicodedata.normalize("NFC", relative).casefold()
        if alias in aliases:
            raise CatalogPublicationError(
                "catalog publication Markdown targets collide"
            )
        aliases.add(alias)
        normalized.append((relative, mutation))
    return tuple(normalized)


def _normalize_catalog_removals(
    vault_root: Path,
    removals: tuple[CatalogRemoval, ...],
) -> tuple[tuple[str, CatalogRemoval], ...]:
    if type(removals) is not tuple:
        raise CatalogPublicationError(
            "catalog publication requires a finite removal batch"
        )
    normalized: list[tuple[str, CatalogRemoval]] = []
    aliases: set[str] = set()
    for removal in removals:
        if type(removal) is not CatalogRemoval:
            raise CatalogPublicationError("catalog publication removal is invalid")
        candidate = PurePosixPath(str(removal.path).replace("\\", "/"))
        if candidate.suffix.casefold() != ".md":
            raise CatalogPublicationError(
                "non-Markdown content publication is not available"
            )
        relative = _relative_markdown_path(vault_root, removal.path)
        if not _is_catalog_markdown_path(relative):
            continue
        expected = removal.expected_before_hash
        if (
            type(expected) is not str
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise CatalogPublicationError(
                "catalog removal lacks an exact predecessor binding"
            )
        alias = unicodedata.normalize("NFC", relative).casefold()
        if alias in aliases:
            raise CatalogPublicationError("catalog publication removal targets collide")
        aliases.add(alias)
        normalized.append((relative, removal))
    return tuple(normalized)


def _validate_catalog_content_paths(content_paths: tuple[str, ...]) -> None:
    if type(content_paths) is not tuple:
        raise CatalogPublicationError(
            "catalog publication requires a finite content-path batch"
        )
    aliases: set[str] = set()
    for path in content_paths:
        if type(path) is not str:
            raise CatalogPublicationError("catalog publication content path is invalid")
        candidate = PurePosixPath(path.replace("\\", "/"))
        if candidate.suffix.casefold() != ".md":
            raise CatalogPublicationError(
                "non-Markdown content publication is not available"
            )
        alias = unicodedata.normalize("NFC", candidate.as_posix()).casefold()
        if alias in aliases:
            raise CatalogPublicationError("catalog publication content paths collide")
        aliases.add(alias)


def _prepare_markdown_batch(
    vault_root: Path,
    *,
    normalized: tuple[tuple[str, MarkdownCatalogMutation], ...] | None,
    planned_writes: tuple[vault.PlannedWrite, ...] | None,
    removals: tuple[CatalogRemoval, ...] = (),
    content_paths: tuple[str, ...] = (),
    now: int | None = None,
    activated_at: int | None = None,
) -> PreparedMarkdownCatalogPublication | None:
    """Prepare one complete batch successor, or return ``None`` for v3/open.

    The current slice intentionally supports lexical-only active namespaces.
    Model and graph measurement roots are content-bound, so reusing them after
    an edit would be unsafe; those deployments refuse before canonical bytes
    change until their rebuild publisher is connected.
    """

    root = Path(vault_root)
    if (normalized is None) == (planned_writes is None):
        raise CatalogPublicationError("catalog publication batch input is invalid")
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

    moment = int(time.time()) if now is None else now
    publication_time = moment if activated_at is None else activated_at
    try:
        custody = authorization_custody.load_authorization_custody(
            root, now=moment
        )
        custody = _recover_catalog_acknowledgement(
            root,
            connection,
            custody=custody,
            now=moment,
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

    _validate_catalog_content_paths(content_paths)
    if normalized is None:
        assert planned_writes is not None
        if type(planned_writes) is not tuple:
            raise CatalogPublicationError(
                "catalog publication requires a finite planned-write batch"
            )
        if not planned_writes and not removals:
            raise CatalogPublicationError(
                "catalog publication requires a non-empty planned-write batch"
            )
        mutations = tuple(
            mutation
            for write in planned_writes
            if (mutation := mutation_from_planned_write(root, write)) is not None
        )
        normalized = _normalize_markdown_mutations(root, mutations)
    normalized_removals = _normalize_catalog_removals(root, removals)
    membership_aliases = [
        unicodedata.normalize("NFC", relative).casefold()
        for relative, _mutation in (*normalized_removals, *normalized)
    ]
    if len(membership_aliases) != len(set(membership_aliases)):
        raise CatalogPublicationError("catalog membership targets collide")
    if not normalized and not normalized_removals:
        return None

    by_identity = {item.item_identity: item for item in verified.items}
    target_key = projections.ProjectionNamespaceKey(
        policy_fingerprint=snapshot.active.policy_fingerprint,
        projector_schema_version=snapshot.active.projector_schema_version,
        catalog_generation=snapshot.active.catalog_generation + 1,
    )
    for relative, removal in normalized_removals:
        predecessor = by_identity.get(relative)
        if (
            predecessor is None
            or predecessor.content_hash != removal.expected_before_hash
        ):
            raise CatalogPublicationError(
                "catalog removal no longer matches the reviewed predecessor"
            )
        del by_identity[relative]
    for relative, mutation in normalized:
        predecessor = by_identity.get(relative)
        if mutation.expected_before_hash is None:
            if predecessor is not None:
                raise CatalogPublicationError("catalog creation target already exists")
        elif (
            predecessor is None
            or predecessor.content_hash != mutation.expected_before_hash
        ):
            raise CatalogPublicationError(
                "catalog content identity no longer matches the reviewed predecessor"
            )
        by_identity[relative] = _replacement_item(
            vault_root=root,
            path=relative,
            source=mutation.source,
            snapshot=snapshot,
            target_key=target_key,
        )
    target_items = tuple(by_identity.values())
    try:
        descriptor = projection_store.catalog_descriptor_bytes(
            target_key, target_items
        )
        target_manifest = projection_store.preview_variant_store(
            key=target_key,
            items=target_items,
        )
        namespace_seed = schema_v4.ProjectionNamespaceSeed(
            namespace_id=target_key.namespace_id,
            evidence=projection_store.projection_namespace_evidence_bytes(
                target_manifest
            ),
            ready_at=publication_time,
        )
        catalog_seed = schema_v4.CatalogGenerationSeed(
            catalog_generation=target_key.catalog_generation,
            descriptor=descriptor,
            artifact_count=len(target_items),
            created_at=publication_time,
        )
        connection = store.open_authorization_session_connection(root)
        try:
            target_publication = schema_v4.preview_catalog_generation(
                connection,
                expected=snapshot.active,
                catalog=catalog_seed,
                namespace=namespace_seed,
                activated_at=publication_time,
            )
        finally:
            connection.close()
    except (
        projection_store.ProjectionStoreError,
        projections.ProjectionError,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ):
        raise CatalogPublicationError(
            "the target governance catalog cannot be prepared"
        ) from None
    return PreparedMarkdownCatalogPublication(
        vault_root=root,
        expected=snapshot.active,
        expected_control=control,
        target_key=target_key,
        target_items=target_items,
        target_manifest=target_manifest,
        expected_catalog_descriptor=snapshot.catalog_descriptor,
        catalog_descriptor=descriptor,
        catalog_seed=catalog_seed,
        namespace_seed=namespace_seed,
        target_publication=target_publication,
        activated_at=publication_time,
        mutation_count=len(normalized) + len(normalized_removals),
    )


def prepare_markdown_batch(
    vault_root: Path,
    *,
    mutations: tuple[MarkdownCatalogMutation, ...],
    now: int | None = None,
    activated_at: int | None = None,
) -> PreparedMarkdownCatalogPublication | None:
    """Prepare an explicit canonical Markdown mutation batch."""

    if type(mutations) is not tuple or not mutations:
        raise CatalogPublicationError(
            "catalog publication requires a finite Markdown mutation batch"
        )
    root = Path(vault_root)
    return _prepare_markdown_batch(
        root,
        normalized=_normalize_markdown_mutations(root, mutations),
        planned_writes=None,
        removals=(),
        now=now,
        activated_at=activated_at,
    )


def prepare_planned_markdown_batch(
    vault_root: Path,
    *,
    writes: tuple[vault.PlannedWrite, ...],
    now: int | None = None,
) -> PreparedMarkdownCatalogPublication | None:
    """Prepare catalog rows lazily so v3/open planned writes remain unchanged."""

    return _prepare_markdown_batch(
        vault_root,
        normalized=None,
        planned_writes=writes,
        removals=(),
        now=now,
    )


def prepare_catalog_membership_batch(
    vault_root: Path,
    *,
    writes: tuple[vault.PlannedWrite, ...] = (),
    removals: tuple[CatalogRemoval, ...] = (),
    content_paths: tuple[str, ...] = (),
    now: int | None = None,
) -> PreparedMarkdownCatalogPublication | None:
    """Prepare lazy write/removal membership changes for a product mutation.

    Open and schema-v3 vaults retain their existing behavior. Exact-v4 vaults
    validate every removal only after enrollment is established, so unsupported
    content kinds fail before the caller mutates canonical bytes.
    """

    return _prepare_markdown_batch(
        vault_root,
        normalized=None,
        planned_writes=writes,
        removals=removals,
        content_paths=content_paths,
        now=now,
    )


def prepare_markdown_upsert(
    vault_root: Path,
    *,
    path: str,
    source: str,
    expected_before_hash: str | None,
    now: int | None = None,
    activated_at: int | None = None,
) -> PreparedMarkdownCatalogPublication | None:
    """Compatibility wrapper for a one-item Markdown catalog batch."""

    return prepare_markdown_batch(
        vault_root,
        mutations=(MarkdownCatalogMutation(path, source, expected_before_hash),),
        now=now,
        activated_at=activated_at,
    )


def publish_markdown_batch(
    prepared: PreparedMarkdownCatalogPublication | None,
) -> schema_v4.TuplePublicationResult | None:
    """Advance one prepared v4 batch after canonical bytes have committed."""

    if prepared is None:
        return None
    connection: sqlite3.Connection | None = None
    try:
        target_manifest = projection_store.stage_variant_store(
            prepared.vault_root,
            key=prepared.target_key,
            items=prepared.target_items,
        )
        if target_manifest != prepared.target_manifest:
            raise CatalogPublicationError(
                "the staged governance catalog does not match its reviewed target"
            )
        receipt_event_id = receipts.critical_event_id(
            {
                "operation": "governance_catalog_markdown_batch",
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
                "mutation_count": prepared.mutation_count,
            }
        )
        connection = store.open_authorization_session_connection(
            prepared.vault_root
        )
        result = schema_v4.publish_catalog_generation(
            connection,
            expected=prepared.expected,
            catalog=prepared.catalog_seed,
            namespace=prepared.namespace_seed,
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
        if result != prepared.target_publication:
            raise CatalogPublicationError(
                "the committed governance catalog does not match its reviewed target"
            )
        return result
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
                if target == prepared.target_publication.active:
                    return prepared.target_publication
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


def publish_markdown_upsert(
    prepared: PreparedMarkdownCatalogPublication | None,
) -> schema_v4.TuplePublicationResult | None:
    """Compatibility wrapper for publishing a one-item prepared batch."""

    return publish_markdown_batch(prepared)


__all__ = [
    "CatalogCommitError",
    "CatalogPublicationError",
    "CatalogRemoval",
    "MarkdownCatalogMutation",
    "PreparedMarkdownCatalogPublication",
    "catalog_component_values",
    "current_catalog_component_value",
    "mutation_from_planned_write",
    "prepare_markdown_batch",
    "prepare_catalog_membership_batch",
    "prepare_planned_markdown_batch",
    "prepare_markdown_upsert",
    "publish_markdown_batch",
    "publish_markdown_upsert",
]
