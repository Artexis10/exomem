"""Prepare the exact inert target for an offline governance v3-to-v4 migration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import find_corpus, reserved_paths, state_migration, writer_lease
from ..kbdir import kb_dirname
from . import (
    authorization_custody,
    legacy_v3_placement,
    membership,
    policy,
    projection_store,
    projections,
    receipts,
    schema_v4,
    store,
)

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CATALOG_GENERATION = 1
_COMPILER_SCHEMA_VERSION = 1
_BACKUP_SCHEMA = "exomem.governance-v3-backup/v1"
_BACKUP_MAGIC = b"EXOMEM-GOVERNANCE-V3-BACKUP-V1\0"
_BACKUP_DOMAIN = b"exomem.governance-v3-backup.v1"
_RESTORE_OPERATION = "governance_schema_v3_backup_restore"
_RESTORE_PLAN_SCHEMA = "exomem.governance-v3-backup-restore-plan/v1"
_RESTORE_PLAN_DOMAIN = b"exomem.governance-v3-backup-restore-plan.v1"
_RESTORE_TARGET_DOMAIN = b"exomem.governance-v3-backup-restore-target.v1"
_MAX_BACKUP_BYTES = 512 * 1024 * 1024
_RECEIPT_TABLES = (
    "receipt_instance",
    "receipts_head",
    "receipt_secrets",
)
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "logical_vault_id",
        "activation_store_id",
        "activation_epoch",
        "activation_state_digest",
        "policy_generation_id",
        "policy_fingerprint",
        "projector_schema_version",
        "catalog_generation",
        "projection_namespace_id",
        "source_store_digest",
        "projection_rows_digest",
        "item_count",
        "plan_digest",
    }
)


class ForwardMigrationUnavailable(RuntimeError):
    """The exact v3 source cannot produce one reviewable migration target."""


class ForwardMigrationPlanMismatch(ForwardMigrationUnavailable):
    """The owner-confirmed plan is not the exact current migration plan."""


class ForwardMigrationRestoreUnavailable(ForwardMigrationUnavailable):
    """The private predecessor backup cannot be restored safely."""


class _ForwardMigrationCrash(RuntimeError):
    """Test-only crash seam that must cross the coordinator boundary."""


@dataclass(frozen=True, slots=True)
class ForwardMigrationPlan:
    """Private seed plus the content-free target an owner may review."""

    seed: schema_v4.MigrationSeed
    target: schema_v4.VerifiedActiveGovernanceState
    source_store_digest: str
    projection_rows_digest: str
    item_count: int
    plan_digest: str


@dataclass(frozen=True, slots=True)
class ForwardMigrationStageResult:
    """Content-free terminal for one inert immutable namespace publication."""

    plan_digest: str
    projection_namespace_id: str
    projection_rows_digest: str
    item_count: int


@dataclass(frozen=True, slots=True)
class ForwardMigrationBackup:
    """Verified private predecessor snapshot for one reviewed cutover."""

    plan_digest: str
    source_store_digest: str
    projection_rows_digest: str
    item_count: int
    backup_digest: str
    backup_reference: str
    target: schema_v4.VerifiedActiveGovernanceState
    source_documents: tuple[tuple[str, bytes], ...] = field(repr=False)
    catalog_descriptor: bytes = field(repr=False)
    projection_namespace_evidence: bytes = field(repr=False)
    serialized_v3: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ForwardMigrationResult:
    """Content-free terminal derived from the verified backup and active tuple."""

    schema_version: int
    target: schema_v4.VerifiedActiveGovernanceState
    plan_digest: str
    source_store_digest: str
    backup_reference: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ForwardMigrationRestoreResult:
    """Content-free terminal for one immediate predecessor-backup restore."""

    schema_version: int
    plan_digest: str
    source_store_digest: str
    backup_reference: str
    recovery_event_id: str
    recovery_plan_digest: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class _ForwardMigrationRestorePlan:
    backup: ForwardMigrationBackup
    control_digest: str
    keyring_digest: str
    membership_epoch: int
    membership_digest: str
    schema_fence_generation: int | None
    plan_digest: str
    target_digest: str
    event_id: str


def _framed_digest(domain: bytes, *parts: bytes) -> str:
    value = bytearray(domain)
    for part in parts:
        value.extend(len(part).to_bytes(8, "big"))
        value.extend(part)
    return hashlib.sha256(value).hexdigest()


def _deterministic_ulid(*, timestamp: int, entropy: bytes) -> str:
    if timestamp < 0 or timestamp * 1000 >= 1 << 48:
        raise ForwardMigrationUnavailable
    value = (timestamp * 1000 << 80) | int.from_bytes(
        hashlib.sha256(entropy).digest()[:10],
        "big",
    )
    return "".join(
        _CROCKFORD32[(value >> shift) & 31] for shift in range(125, -1, -5)
    )


def _search_fields(page: find_corpus.ParsedPage) -> dict[str, str]:
    fields = {"title": page.title}
    for name, value in (
        ("body", page.body),
        ("status", page.frontmatter.get("status")),
        ("type", page.page_type),
        ("updated", page.updated),
        ("media_type", page.frontmatter.get("media_type")),
        ("parent_media", page.frontmatter.get("parent_media")),
    ):
        if value:
            fields[name] = str(value)
    return fields


def _source_store_snapshot(vault_root: Path) -> tuple[bytes, str]:
    source = store.open_readonly_connection(vault_root)
    if source is None:
        raise ForwardMigrationUnavailable
    snapshot = sqlite3.connect(":memory:")
    try:
        schema_v4.require_exact_v3_connection(source)
        source.backup(snapshot)
        snapshot.execute("VACUUM")
        schema_v4.require_exact_v3_connection(snapshot)
        serialized = snapshot.serialize()
    except (AttributeError, OSError, RuntimeError, sqlite3.Error) as error:
        raise ForwardMigrationUnavailable from error
    finally:
        snapshot.close()
        source.close()
    return serialized, _framed_digest(
        b"exomem.governance-v3-snapshot.v1",
        serialized,
    )


def _source_store_digest(vault_root: Path) -> str:
    return _source_store_snapshot(vault_root)[1]


def _migration_identity(
    vault_root: Path,
    *,
    now: int,
) -> tuple[
    authorization_custody.StandaloneV3StagingResult,
    authorization_custody.AuthorizationControlRecord | None,
]:
    """Stage inert identity or recover the exact enrolled-v3 identity."""

    try:
        return authorization_custody.stage_standalone_v3_custody(
            vault_root,
            now=now,
        ), None
    except authorization_custody.AuthorizationCustodyUnavailable:
        custody = authorization_custody.load_authorization_custody(
            vault_root,
            now=now,
        )
        control = custody.control
        membership_record = custody.serving_membership
        active_key = custody.keyring.active_key
        if (
            not control.governance_enrolled
            or control.activation_epoch != 1
            or custody.local_replica_id is None
            or membership_record is None
            or membership_record.epoch != 1
            or not membership_record.replicas
            or any(
                replica.state != "DRAINING"
                or replica.schema_version != store.SCHEMA_USER_VERSION
                or not replica.issuance_stopped
                or not replica.no_in_flight
                for replica in membership_record.replicas
            )
        ):
            raise
        return (
            authorization_custody.StandaloneV3StagingResult(
                keyring_path=custody.keyring_path,
                keyring_id=custody.keyring.keyring_id,
                cell_id=custody.keyring.cell_id,
                logical_vault_id=custody.keyring.logical_vault_id,
                registry_attachment_id=control.registry_attachment_id,
                attachment_epoch=control.attachment_epoch,
                staged_at=active_key.not_before,
            ),
            control,
        )


def _catalog_items(
    vault_root: Path,
    *,
    compiled: policy.Policy,
    key: projections.ProjectionNamespaceKey,
) -> tuple[projection_store.ProjectionItemVariants, ...]:
    kb = vault_root / kb_dirname()
    if not kb.is_dir():
        raise ForwardMigrationUnavailable
    items: list[projection_store.ProjectionItemVariants] = []
    for path in sorted(find_corpus.walk_md(kb)):
        if path.name.casefold() in find_corpus.NAVIGATION_BASENAMES:
            continue
        try:
            relative = path.relative_to(vault_root).as_posix()
            snapshot = reserved_paths.read_generic_bytes(vault_root, relative)
            parsed = find_corpus.parse_page(
                path,
                snapshot.mtime,
                vault_root,
                content=snapshot.data,
                resolved_relative=relative,
            )
            if parsed is None:
                raise ForwardMigrationUnavailable
            content_hash = hashlib.sha256(snapshot.data).hexdigest()
            scope_ids = tuple(
                sorted(
                    membership.evaluate_snapshot(
                        parsed,
                        compiled,
                        content_hash=content_hash,
                    )
                )
            )
            variants = projections.enumerate_projection_variants(
                item_identity=relative,
                content_hash=content_hash,
                scope_ids=scope_ids,
                policy=compiled,
                projector_schema_version=key.projector_schema_version,
                full_search_fields=_search_fields(parsed),
            )
        except ForwardMigrationUnavailable:
            raise
        except (
            OSError,
            RuntimeError,
            UnicodeError,
            ValueError,
            membership.MembershipUnresolved,
            projections.ProjectionError,
            reserved_paths.ReservedPathLeafError,
        ) as error:
            raise ForwardMigrationUnavailable from error
        items.append(
            projection_store.ProjectionItemVariants(
                item_identity=relative,
                content_hash=content_hash,
                scope_ids=scope_ids,
                variants=variants,
            )
        )
    return tuple(items)


def _policy_seed(
    snapshot: policy.AuthoringSnapshot,
    compiled: policy.Policy,
    *,
    staged_at: int,
    keyring_id: str,
) -> schema_v4.PolicyGenerationSeed:
    generation_id = _deterministic_ulid(
        timestamp=staged_at,
        entropy=(
            b"exomem.initial-policy-generation.v1\0"
            + keyring_id.encode("ascii")
            + snapshot.source_fingerprint.encode("ascii")
            + snapshot.conflict_set_digest.encode("ascii")
        ),
    )
    identity = {
        "operation": "governance_schema_v3_to_v4",
        "generation_id": generation_id,
        "keyring_id": keyring_id,
        "source_fingerprint": snapshot.source_fingerprint,
        "conflict_digest": snapshot.conflict_set_digest,
        "staged_at": staged_at,
    }
    authoring_event_id = receipts.critical_event_id(
        {**identity, "phase": "initial-policy-reviewed"}
    )
    receipt_event_id = receipts.critical_event_id(
        {
            **identity,
            "phase": "initial-policy-publication",
            "authoring_event_id": authoring_event_id,
        }
    )
    return schema_v4.PolicyGenerationSeed(
        generation_id=generation_id,
        source_documents=snapshot.documents,
        source_fingerprint=snapshot.source_fingerprint,
        conflict_digest=snapshot.conflict_set_digest,
        compiled_policy=policy.canonical_compiled_bytes(compiled),
        policy_fingerprint=compiled.fingerprint,
        compiler_schema_version=_COMPILER_SCHEMA_VERSION,
        projector_schema_version=projections.PROJECTOR_SCHEMA_VERSION,
        predecessor_generation_id=None,
        authoring_event_id=authoring_event_id,
        receipt_event_id=receipt_event_id,
        created_at=staged_at,
    )


def _plan_value(
    target: schema_v4.VerifiedActiveGovernanceState,
    *,
    source_store_digest: str,
    projection_rows_digest: str,
    item_count: int,
) -> dict[str, str | int]:
    return {
        "schema_version": 3,
        "logical_vault_id": target.logical_vault_id,
        "activation_store_id": target.activation_store_id,
        "activation_epoch": target.activation_epoch,
        "activation_state_digest": target.activation_state_digest,
        "policy_generation_id": target.policy_generation_id,
        "policy_fingerprint": target.policy_fingerprint,
        "projector_schema_version": target.projector_schema_version,
        "catalog_generation": target.catalog_generation,
        "projection_namespace_id": target.projection_namespace_id,
        "source_store_digest": source_store_digest,
        "projection_rows_digest": projection_rows_digest,
        "item_count": item_count,
    }


def prepare_forward_migration(
    vault_root: Path,
    *,
    now: int,
) -> ForwardMigrationPlan:
    """Stage an immutable v4 target without enrolling or changing schema v3."""

    root = Path(vault_root)
    try:
        staged, enrolled_control = _migration_identity(root, now=now)
        before = policy.observe_authoring_snapshot(root)
        if before is None:
            raise ForwardMigrationUnavailable
        source_store_digest = _source_store_digest(root)
        compiled = policy.compile_documents(dict(before.documents))
        if compiled.empty or compiled.blocked:
            raise ForwardMigrationUnavailable
        policy_seed = _policy_seed(
            before,
            compiled,
            staged_at=staged.staged_at,
            keyring_id=staged.keyring_id,
        )
        key = projections.ProjectionNamespaceKey(
            policy_fingerprint=compiled.fingerprint,
            projector_schema_version=projections.PROJECTOR_SCHEMA_VERSION,
            catalog_generation=_CATALOG_GENERATION,
        )
        items = _catalog_items(root, compiled=compiled, key=key)
        descriptor = projection_store.catalog_descriptor_bytes(key, items)
        manifest = projection_store.preview_variant_store(key=key, items=items)
        evidence = projection_store.projection_namespace_evidence_bytes(manifest)
        after = policy.observe_authoring_snapshot(root)
        if (
            after != before
            or _catalog_items(root, compiled=compiled, key=key) != items
            or _source_store_digest(root) != source_store_digest
        ):
            raise ForwardMigrationUnavailable
        seed = schema_v4.MigrationSeed(
            activation_store_id=(
                "activation-store-"
                + _framed_digest(
                    b"exomem.initial-activation-store.v1",
                    staged.keyring_id.encode("ascii"),
                    staged.logical_vault_id.encode("ascii"),
                )[:32]
            ),
            logical_vault_id=staged.logical_vault_id,
            activation_epoch=1,
            policy=policy_seed,
            catalog=schema_v4.CatalogGenerationSeed(
                catalog_generation=_CATALOG_GENERATION,
                descriptor=descriptor,
                artifact_count=len(items),
                created_at=staged.staged_at,
            ),
            namespace=schema_v4.ProjectionNamespaceSeed(
                namespace_id=key.namespace_id,
                evidence=evidence,
                ready_at=staged.staged_at,
            ),
            migrated_at=staged.staged_at,
        )
        target = schema_v4.migration_target(seed)
        if enrolled_control is not None and (
            enrolled_control.logical_vault_id != target.logical_vault_id
            or enrolled_control.activation_store_id != target.activation_store_id
            or enrolled_control.activation_epoch != target.activation_epoch
            or enrolled_control.activation_state_digest
            != target.activation_state_digest
        ):
            raise ForwardMigrationPlanMismatch
        plan_value = _plan_value(
            target,
            source_store_digest=source_store_digest,
            projection_rows_digest=manifest.rows_digest,
            item_count=len(items),
        )
        plan_digest = _framed_digest(
            b"exomem.governance-schema-migration-plan.v1",
            projections.canonical_jcs(plan_value),
        )
    except ForwardMigrationUnavailable:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        projection_store.ProjectionStoreError,
        projections.ProjectionError,
        schema_v4.SchemaV4Error,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        raise ForwardMigrationUnavailable from error
    return ForwardMigrationPlan(
        seed=seed,
        target=target,
        source_store_digest=source_store_digest,
        projection_rows_digest=manifest.rows_digest,
        item_count=len(items),
        plan_digest=plan_digest,
    )


def plan_summary(plan: ForwardMigrationPlan) -> dict[str, object]:
    """Return the closed, content-free owner review surface."""

    if not isinstance(plan, ForwardMigrationPlan):
        raise ForwardMigrationUnavailable
    return {
        **_plan_value(
            plan.target,
            source_store_digest=plan.source_store_digest,
            projection_rows_digest=plan.projection_rows_digest,
            item_count=plan.item_count,
        ),
        "plan_digest": plan.plan_digest,
    }


def _stage_material(
    vault_root: Path,
    plan: ForwardMigrationPlan,
) -> tuple[
    projections.ProjectionNamespaceKey,
    tuple[projection_store.ProjectionItemVariants, ...],
    projection_store.VariantStoreManifest,
]:
    compiled = policy.compile_documents(dict(plan.seed.policy.source_documents))
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=plan.target.policy_fingerprint,
        projector_schema_version=plan.target.projector_schema_version,
        catalog_generation=plan.target.catalog_generation,
    )
    items = _catalog_items(vault_root, compiled=compiled, key=key)
    manifest = projection_store.preview_variant_store(key=key, items=items)
    if (
        compiled.fingerprint != plan.target.policy_fingerprint
        or projection_store.catalog_descriptor_bytes(key, items)
        != plan.seed.catalog.descriptor
        or projection_store.projection_namespace_evidence_bytes(manifest)
        != plan.seed.namespace.evidence
        or manifest.namespace_id != plan.target.projection_namespace_id
        or manifest.rows_digest != plan.projection_rows_digest
        or manifest.item_count != plan.item_count
    ):
        raise ForwardMigrationPlanMismatch
    return key, items, manifest


def stage_forward_migration(
    vault_root: Path,
    *,
    expected_plan_digest: str,
    now: int,
) -> ForwardMigrationStageResult:
    """Publish only the exact reviewed namespace while authority remains v3.

    The immutable namespace is inert until a later coordinator enrolls and commits
    its bound activation tuple.  A crash or late source drift can therefore leave
    only an unreferenced, content-addressed store that an exact retry may reuse.
    """

    expected = str(expected_plan_digest)
    if (
        len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise ForwardMigrationPlanMismatch
    root = Path(vault_root)
    plan = prepare_forward_migration(root, now=now)
    if not hmac.compare_digest(plan.plan_digest, expected):
        raise ForwardMigrationPlanMismatch
    try:
        key, items, manifest = _stage_material(root, plan)
        staged = projection_store.stage_variant_store(
            root,
            key=key,
            items=items,
        )
        verified = projection_store.verify_variant_store(
            root,
            key=key,
            expected_rows_digest=plan.projection_rows_digest,
        )
        current = prepare_forward_migration(root, now=now)
    except ForwardMigrationPlanMismatch:
        raise
    except (
        ForwardMigrationUnavailable,
        projection_store.ProjectionStoreError,
        projections.ProjectionError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        raise ForwardMigrationUnavailable from error
    if (
        staged != manifest
        or verified != manifest
        or not hmac.compare_digest(current.plan_digest, expected)
        or current != plan
    ):
        raise ForwardMigrationPlanMismatch
    return ForwardMigrationStageResult(
        plan_digest=plan.plan_digest,
        projection_namespace_id=manifest.namespace_id,
        projection_rows_digest=manifest.rows_digest,
        item_count=manifest.item_count,
    )


def _require_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ForwardMigrationUnavailable
    return value


def forward_migration_backup_path(
    vault_root: Path,
    *,
    plan_digest: str,
) -> Path:
    """Return the fixed external path for one plan-bound private backup."""

    digest = _require_digest(plan_digest)
    root = Path(vault_root)
    keyring_path = authorization_custody._configured_external_path(  # noqa: SLF001
        authorization_custody.KEYRING_FILE_ENV,
        root,
    )
    backup_path = keyring_path.with_name(f"governance-v3-backup-{digest}.bin")
    custody_paths = {
        keyring_path,
        authorization_custody._configured_external_path(  # noqa: SLF001
            authorization_custody.CONTROL_FILE_ENV,
            root,
        ),
        authorization_custody._configured_external_path(  # noqa: SLF001
            authorization_custody.MEMBERSHIP_FILE_ENV,
            root,
        ),
    }
    if backup_path in custody_paths or len(custody_paths) != 3:
        raise ForwardMigrationUnavailable
    return backup_path


def _backup_manifest(
    plan: ForwardMigrationPlan,
    *,
    serialized_v3: bytes,
) -> bytes:
    value = {
        "schema": _BACKUP_SCHEMA,
        "plan": {**_plan_value(
            plan.target,
            source_store_digest=plan.source_store_digest,
            projection_rows_digest=plan.projection_rows_digest,
            item_count=plan.item_count,
        ), "plan_digest": plan.plan_digest},
        "source_documents": [
            {
                "path": relative,
                "base64url": base64.urlsafe_b64encode(data).decode("ascii"),
            }
            for relative, data in plan.seed.policy.source_documents
        ],
        "catalog_descriptor_base64url": base64.urlsafe_b64encode(
            plan.seed.catalog.descriptor
        ).decode("ascii"),
        "projection_namespace_evidence_base64url": base64.urlsafe_b64encode(
            plan.seed.namespace.evidence
        ).decode("ascii"),
        "serialized_v3_size": len(serialized_v3),
        "serialized_v3_sha256": hashlib.sha256(serialized_v3).hexdigest(),
        "created_at": plan.seed.migrated_at,
    }
    return projections.canonical_jcs(value)


def _backup_bytes(
    plan: ForwardMigrationPlan,
    *,
    serialized_v3: bytes,
) -> bytes:
    manifest = _backup_manifest(plan, serialized_v3=serialized_v3)
    return b"".join(
        (
            _BACKUP_MAGIC,
            len(manifest).to_bytes(8, "big"),
            manifest,
            len(serialized_v3).to_bytes(8, "big"),
            serialized_v3,
        )
    )


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > _MAX_BACKUP_BYTES * 2:
        raise ForwardMigrationUnavailable
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise ForwardMigrationUnavailable from error
    if base64.urlsafe_b64encode(decoded).decode("ascii") != value:
        raise ForwardMigrationUnavailable
    return decoded


def _closed_json(raw: bytes) -> dict[str, object]:
    def closed(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ForwardMigrationUnavailable
            value[key] = item
        return value

    try:
        parsed = json.loads(raw, object_pairs_hook=closed)
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError) as error:
        raise ForwardMigrationUnavailable from error
    if not isinstance(parsed, dict) or projections.canonical_jcs(parsed) != raw:
        raise ForwardMigrationUnavailable
    return parsed


def _bounded_integer(value: object, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value < 1 << 63
    ):
        raise ForwardMigrationUnavailable
    return value


def _target_from_plan(value: dict[str, object]) -> schema_v4.VerifiedActiveGovernanceState:
    if set(value) != _PLAN_FIELDS or value.get("schema_version") != 3:
        raise ForwardMigrationUnavailable
    for name in (
        "activation_state_digest",
        "policy_fingerprint",
        "source_store_digest",
        "projection_rows_digest",
        "plan_digest",
    ):
        _require_digest(value.get(name))
    text_fields = (
        "logical_vault_id",
        "activation_store_id",
        "policy_generation_id",
        "projection_namespace_id",
    )
    if any(not isinstance(value.get(name), str) or not value[name] for name in text_fields):
        raise ForwardMigrationUnavailable
    target = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=str(value["logical_vault_id"]),
        activation_store_id=str(value["activation_store_id"]),
        activation_epoch=_bounded_integer(value["activation_epoch"], minimum=1),
        activation_state_digest=str(value["activation_state_digest"]),
        policy_generation_id=str(value["policy_generation_id"]),
        policy_fingerprint=str(value["policy_fingerprint"]),
        projector_schema_version=_bounded_integer(
            value["projector_schema_version"],
            minimum=1,
        ),
        catalog_generation=_bounded_integer(value["catalog_generation"], minimum=1),
        projection_namespace_id=str(value["projection_namespace_id"]),
    )
    _bounded_integer(value["item_count"])
    unsigned = {name: item for name, item in value.items() if name != "plan_digest"}
    computed_plan_digest = _framed_digest(
        b"exomem.governance-schema-migration-plan.v1",
        projections.canonical_jcs(unsigned),
    )
    if not hmac.compare_digest(str(value["plan_digest"]), computed_plan_digest):
        raise ForwardMigrationUnavailable
    return target


def verify_forward_migration_backup(
    vault_root: Path,
    *,
    expected_plan_digest: str,
) -> ForwardMigrationBackup:
    """Verify the private bundle, exact v3 payload, and reviewed target binding."""

    expected = _require_digest(expected_plan_digest)
    path = forward_migration_backup_path(vault_root, plan_digest=expected)
    try:
        loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
            path,
            maximum_bytes=_MAX_BACKUP_BYTES,
        ).data
        if not loaded.startswith(_BACKUP_MAGIC):
            raise ForwardMigrationUnavailable
        cursor = len(_BACKUP_MAGIC)
        manifest_size = int.from_bytes(loaded[cursor : cursor + 8], "big")
        cursor += 8
        if not 1 <= manifest_size <= _MAX_BACKUP_BYTES or cursor + manifest_size + 8 > len(loaded):
            raise ForwardMigrationUnavailable
        manifest_raw = loaded[cursor : cursor + manifest_size]
        cursor += manifest_size
        payload_size = int.from_bytes(loaded[cursor : cursor + 8], "big")
        cursor += 8
        serialized = loaded[cursor:]
        if payload_size != len(serialized) or not serialized:
            raise ForwardMigrationUnavailable
        manifest = _closed_json(manifest_raw)
        expected_fields = {
            "schema",
            "plan",
            "source_documents",
            "catalog_descriptor_base64url",
            "projection_namespace_evidence_base64url",
            "serialized_v3_size",
            "serialized_v3_sha256",
            "created_at",
        }
        if set(manifest) != expected_fields or manifest["schema"] != _BACKUP_SCHEMA:
            raise ForwardMigrationUnavailable
        plan_value = manifest["plan"]
        if not isinstance(plan_value, dict):
            raise ForwardMigrationUnavailable
        target = _target_from_plan(plan_value)
        if not hmac.compare_digest(str(plan_value["plan_digest"]), expected):
            raise ForwardMigrationUnavailable
        if (
            _bounded_integer(manifest["serialized_v3_size"], minimum=1)
            != len(serialized)
            or not hmac.compare_digest(
                _require_digest(manifest["serialized_v3_sha256"]),
                hashlib.sha256(serialized).hexdigest(),
            )
        ):
            raise ForwardMigrationUnavailable
        source_store_digest = _framed_digest(
            b"exomem.governance-v3-snapshot.v1",
            serialized,
        )
        if not hmac.compare_digest(
            str(plan_value["source_store_digest"]),
            source_store_digest,
        ):
            raise ForwardMigrationUnavailable
        snapshot = sqlite3.connect(":memory:")
        try:
            snapshot.deserialize(serialized)
            schema_v4.require_exact_v3_connection(snapshot)
        finally:
            snapshot.close()
        raw_documents = manifest["source_documents"]
        if not isinstance(raw_documents, list):
            raise ForwardMigrationUnavailable
        documents: list[tuple[str, bytes]] = []
        for record in raw_documents:
            if (
                not isinstance(record, dict)
                or set(record) != {"path", "base64url"}
                or not isinstance(record["path"], str)
                or not record["path"]
            ):
                raise ForwardMigrationUnavailable
            documents.append((record["path"], _decode_base64(record["base64url"])))
        source_documents = tuple(documents)
        if (
            tuple(sorted(source_documents)) != source_documents
            or len({relative for relative, _data in source_documents})
            != len(source_documents)
        ):
            raise ForwardMigrationUnavailable
        compiled = policy.compile_documents(dict(source_documents))
        if compiled.blocked or compiled.empty or compiled.fingerprint != target.policy_fingerprint:
            raise ForwardMigrationUnavailable
        catalog_descriptor = _decode_base64(
            manifest["catalog_descriptor_base64url"]
        )
        namespace_evidence = _decode_base64(
            manifest["projection_namespace_evidence_base64url"]
        )
        if not catalog_descriptor or not namespace_evidence:
            raise ForwardMigrationUnavailable
        _bounded_integer(manifest["created_at"], minimum=1)
    except ForwardMigrationUnavailable:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        schema_v4.SchemaV4Error,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ) as error:
        raise ForwardMigrationUnavailable from error
    backup_digest = _framed_digest(_BACKUP_DOMAIN, loaded)
    return ForwardMigrationBackup(
        plan_digest=expected,
        source_store_digest=source_store_digest,
        projection_rows_digest=str(plan_value["projection_rows_digest"]),
        item_count=_bounded_integer(plan_value["item_count"]),
        backup_digest=backup_digest,
        backup_reference=f"exomem-governance-v3-backup://sha256/{backup_digest}",
        target=target,
        source_documents=source_documents,
        catalog_descriptor=catalog_descriptor,
        projection_namespace_evidence=namespace_evidence,
        serialized_v3=serialized,
    )


def _forward_migration_barrier(point: str) -> None:
    """Crash-injection seam around the separately durable cutover effects."""

    del point


def _verify_active_target(
    vault_root: Path,
    backup: ForwardMigrationBackup,
) -> None:
    connection = store.open_active_governance_read_connection(vault_root)
    try:
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=backup.target.logical_vault_id,
            expected_activation_store_id=backup.target.activation_store_id,
            expected_activation_epoch=backup.target.activation_epoch,
            expected_activation_state_digest=backup.target.activation_state_digest,
        )
    finally:
        connection.close()
    if (
        snapshot.active != backup.target
        or snapshot.source_documents != backup.source_documents
        or snapshot.catalog_descriptor != backup.catalog_descriptor
        or snapshot.projection_namespace_evidence
        != backup.projection_namespace_evidence
    ):
        raise ForwardMigrationUnavailable


def _verify_live_source_material(
    vault_root: Path,
    backup: ForwardMigrationBackup,
) -> None:
    """Recheck exact workspace and catalog bytes before v4 may serve."""

    try:
        observed = policy.observe_authoring_snapshot(vault_root)
        compiled = policy.compile_documents(dict(backup.source_documents))
        if (
            observed is None
            or observed.documents != backup.source_documents
            or compiled.empty
            or compiled.blocked
            or compiled.fingerprint != backup.target.policy_fingerprint
        ):
            raise ForwardMigrationUnavailable
        key = projections.ProjectionNamespaceKey(
            policy_fingerprint=backup.target.policy_fingerprint,
            projector_schema_version=backup.target.projector_schema_version,
            catalog_generation=backup.target.catalog_generation,
        )
        items = _catalog_items(vault_root, compiled=compiled, key=key)
        manifest = projection_store.preview_variant_store(key=key, items=items)
        if (
            projection_store.catalog_descriptor_bytes(key, items)
            != backup.catalog_descriptor
            or projection_store.projection_namespace_evidence_bytes(manifest)
            != backup.projection_namespace_evidence
            or manifest.namespace_id != backup.target.projection_namespace_id
            or manifest.rows_digest != backup.projection_rows_digest
            or manifest.item_count != backup.item_count
        ):
            raise ForwardMigrationUnavailable
    except ForwardMigrationUnavailable:
        raise
    except (
        membership.MembershipUnresolved,
        projection_store.ProjectionStoreError,
        projections.ProjectionError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        raise ForwardMigrationUnavailable from error


def _require_backup_reference(value: object) -> str:
    prefix = "exomem-governance-v3-backup://sha256/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ForwardMigrationRestoreUnavailable
    _require_digest(value[len(prefix) :])
    return value


def _receipt_state(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, tuple[str, ...], tuple[tuple[object, ...], ...]], ...]:
    state: list[tuple[str, tuple[str, ...], tuple[tuple[object, ...], ...]]] = []
    for table in _RECEIPT_TABLES:
        columns = tuple(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if not columns:
            raise ForwardMigrationRestoreUnavailable
        rows = tuple(
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
        )
        state.append((table, columns, rows))
    return tuple(state)


def _replace_receipt_state(
    connection: sqlite3.Connection,
    state: tuple[tuple[str, tuple[str, ...], tuple[tuple[object, ...], ...]], ...],
) -> None:
    if tuple(table for table, _columns, _rows in state) != _RECEIPT_TABLES:
        raise ForwardMigrationRestoreUnavailable
    for table, columns, rows in state:
        current_columns = tuple(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if current_columns != columns:
            raise ForwardMigrationRestoreUnavailable
        connection.execute(f"DELETE FROM {table}")
        if rows:
            placeholders = ",".join("?" for _column in columns)
            connection.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})",
                rows,
            )
    connection.commit()


def _backup_receipt_state(
    backup: ForwardMigrationBackup,
) -> tuple[tuple[str, tuple[str, ...], tuple[tuple[object, ...], ...]], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(backup.serialized_v3)
        schema_v4.require_exact_v3_connection(connection)
        return _receipt_state(connection)
    finally:
        connection.close()


def _store_receipt_state(
    vault_root: Path,
    *,
    schema_version: int,
) -> tuple[tuple[str, tuple[str, ...], tuple[tuple[object, ...], ...]], ...]:
    if schema_version == schema_v4.SCHEMA_USER_VERSION:
        connection = store.open_active_governance_read_connection(vault_root)
    elif schema_version == store.SCHEMA_USER_VERSION:
        connection = store.open_readonly_connection(vault_root)
        if connection is None:
            raise ForwardMigrationRestoreUnavailable
    else:
        raise ForwardMigrationRestoreUnavailable
    try:
        return _receipt_state(connection)
    finally:
        connection.close()


def _require_drained_restore_custody(
    vault_root: Path,
    *,
    backup: ForwardMigrationBackup,
    now: int,
) -> authorization_custody.AuthorizationCustody:
    custody = authorization_custody.load_authorization_custody(vault_root, now=now)
    control = custody.control
    membership_record = custody.serving_membership
    if (
        not control.governance_enrolled
        or control.logical_vault_id != backup.target.logical_vault_id
        or control.activation_store_id != backup.target.activation_store_id
        or control.activation_epoch != backup.target.activation_epoch
        or control.activation_state_digest != backup.target.activation_state_digest
        or membership_record is None
        or membership_record.epoch != control.serving_membership_epoch
        or membership_record.record_digest != control.serving_membership_digest
        or not membership_record.replicas
        or any(
            replica.state != "DRAINING"
            or replica.schema_version != schema_v4.SCHEMA_USER_VERSION
            or not replica.issuance_stopped
            or not replica.no_in_flight
            for replica in membership_record.replicas
        )
    ):
        raise ForwardMigrationRestoreUnavailable
    return custody


def _restore_schema_fence_generation(*, schema_version: int) -> int | None:
    client = writer_lease.configured_schema_fence_operator_client()
    if client is None:
        return None
    current = client.schema_fence()
    if not current.governance_enrolled:
        raise ForwardMigrationRestoreUnavailable
    if current.schema_version == schema_v4.SCHEMA_USER_VERSION:
        if schema_version not in {
            store.SCHEMA_USER_VERSION,
            schema_v4.SCHEMA_USER_VERSION,
        }:
            raise ForwardMigrationRestoreUnavailable
        return current.generation
    if (
        schema_version == store.SCHEMA_USER_VERSION
        and current.schema_version == store.SCHEMA_USER_VERSION
        and current.generation > 0
    ):
        return current.generation - 1
    raise ForwardMigrationRestoreUnavailable


def _restore_plan(
    vault_root: Path,
    *,
    backup: ForwardMigrationBackup,
    custody: authorization_custody.AuthorizationCustody,
    schema_version: int,
) -> _ForwardMigrationRestorePlan:
    external = authorization_custody.load_external_custody(vault_root)
    membership_record = custody.serving_membership
    if membership_record is None:
        raise ForwardMigrationRestoreUnavailable
    control_digest = hashlib.sha256(external.control).hexdigest()
    keyring_digest = hashlib.sha256(external.keyring).hexdigest()
    fence_generation = _restore_schema_fence_generation(
        schema_version=schema_version,
    )
    value = {
        "schema": _RESTORE_PLAN_SCHEMA,
        "migration_plan_digest": backup.plan_digest,
        "backup_reference": backup.backup_reference,
        "source_store_digest": backup.source_store_digest,
        "logical_vault_id": backup.target.logical_vault_id,
        "activation_store_id": backup.target.activation_store_id,
        "activation_epoch": backup.target.activation_epoch,
        "activation_state_digest": backup.target.activation_state_digest,
        "control_digest": control_digest,
        "keyring_digest": keyring_digest,
        "membership_epoch": membership_record.epoch,
        "membership_digest": membership_record.record_digest,
        "schema_fence_generation": fence_generation,
    }
    plan_digest = _framed_digest(
        _RESTORE_PLAN_DOMAIN,
        projections.canonical_jcs(value),
    )
    target_digest = _framed_digest(
        _RESTORE_TARGET_DOMAIN,
        backup.source_store_digest.encode("ascii"),
        plan_digest.encode("ascii"),
    )
    identity = {
        "operation": _RESTORE_OPERATION,
        "prior": backup.target.activation_state_digest,
        "prepared": plan_digest,
        "target": target_digest,
        "affected_ids": sorted(
            (
                backup.target.activation_store_id,
                backup.target.logical_vault_id,
            )
        ),
    }
    return _ForwardMigrationRestorePlan(
        backup=backup,
        control_digest=control_digest,
        keyring_digest=keyring_digest,
        membership_epoch=membership_record.epoch,
        membership_digest=membership_record.record_digest,
        schema_fence_generation=fence_generation,
        plan_digest=plan_digest,
        target_digest=target_digest,
        event_id=receipts.critical_event_id(identity),
    )


def _restore_event_state(
    vault_root: Path,
    plan: _ForwardMigrationRestorePlan,
) -> str:
    records = receipts.event_records(vault_root)
    affected = sorted(
        (
            plan.backup.target.activation_store_id,
            plan.backup.target.logical_vault_id,
        )
    )
    intents = [
        item
        for item in records
        if item.get("event_type") == "critical"
        and item.get("phase") == "intent"
        and item.get("operation") == _RESTORE_OPERATION
    ]
    matching = [item for item in intents if item.get("event_id") == plan.event_id]
    if len(intents) != len(matching) or len(matching) > 1:
        raise ForwardMigrationRestoreUnavailable
    if not matching:
        return "absent"
    intent = matching[0]
    if (
        intent.get("prior") != plan.backup.target.activation_state_digest
        or intent.get("prepared") != plan.plan_digest
        or intent.get("target") != plan.target_digest
        or intent.get("affected_ids") != affected
    ):
        raise ForwardMigrationRestoreUnavailable
    terminals = [
        item
        for item in records
        if item.get("event_type") == "critical"
        and item.get("causation_id") == plan.event_id
        and item.get("phase") in {"committed", "aborted"}
    ]
    if len(terminals) > 1:
        raise ForwardMigrationRestoreUnavailable
    if terminals:
        terminal = terminals[0]
        if (
            terminal.get("phase") != "committed"
            or terminal.get("outcome") != "schema-v3-backup-restored"
            or terminal.get("instance_id") != intent.get("instance_id")
            or not isinstance(terminal.get("seq"), int)
            or terminal["seq"] <= intent.get("seq", 0)
        ):
            raise ForwardMigrationRestoreUnavailable
        return "committed"
    same_instance_successors = [
        item
        for item in records
        if item.get("instance_id") == intent.get("instance_id")
        and isinstance(item.get("seq"), int)
        and item["seq"] > intent.get("seq", 0)
    ]
    if same_instance_successors:
        raise ForwardMigrationRestoreUnavailable
    return "intent"


def _ensure_restore_intent(
    vault_root: Path,
    plan: _ForwardMigrationRestorePlan,
) -> str:
    state = _restore_event_state(vault_root, plan)
    if state != "absent":
        return state
    try:
        record = receipts.begin_event(
            vault_root,
            operation=_RESTORE_OPERATION,
            prior=plan.backup.target.activation_state_digest,
            prepared=plan.plan_digest,
            target=plan.target_digest,
            affected_ids=sorted(
                (
                    plan.backup.target.activation_store_id,
                    plan.backup.target.logical_vault_id,
                )
            ),
            event_id=plan.event_id,
        )
    except receipts.ReceiptError as error:
        raise ForwardMigrationRestoreUnavailable from error
    if record.get("event_id") != plan.event_id or record.get("phase") != "intent":
        raise ForwardMigrationRestoreUnavailable
    return "intent"


def _receipt_table(
    state: tuple[tuple[str, tuple[str, ...], tuple[tuple[object, ...], ...]], ...],
    name: str,
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    matches = [
        (columns, rows)
        for table, columns, rows in state
        if table == name
    ]
    if len(matches) != 1:
        raise ForwardMigrationRestoreUnavailable
    return matches[0]


def _require_restore_receipt_progress(
    vault_root: Path,
    plan: _ForwardMigrationRestorePlan,
    *,
    schema_version: int,
    event_state: str,
) -> None:
    if event_state not in {"intent", "committed"}:
        raise ForwardMigrationRestoreUnavailable
    backup_state = _backup_receipt_state(plan.backup)
    current_state = _store_receipt_state(
        vault_root,
        schema_version=schema_version,
    )
    if _receipt_table(current_state, "receipt_secrets") != _receipt_table(
        backup_state,
        "receipt_secrets",
    ):
        raise ForwardMigrationRestoreUnavailable

    records = receipts.event_records(vault_root)
    intents = [item for item in records if item.get("event_id") == plan.event_id]
    if len(intents) != 1:
        raise ForwardMigrationRestoreUnavailable
    intent = intents[0]
    endpoint = intent
    if event_state == "committed":
        terminals = [
            item for item in records if item.get("causation_id") == plan.event_id
        ]
        if len(terminals) != 1:
            raise ForwardMigrationRestoreUnavailable
        endpoint = terminals[0]
    instance_id = intent.get("instance_id")
    intent_seq = intent.get("seq")
    endpoint_seq = endpoint.get("seq")
    if (
        not isinstance(instance_id, str)
        or not isinstance(intent_seq, int)
        or isinstance(intent_seq, bool)
        or not isinstance(endpoint_seq, int)
        or isinstance(endpoint_seq, bool)
        or intent_seq < 1
        or endpoint_seq < intent_seq
    ):
        raise ForwardMigrationRestoreUnavailable

    backup_instance = _receipt_table(backup_state, "receipt_instance")
    current_instance = _receipt_table(current_state, "receipt_instance")
    if backup_instance[0] != current_instance[0]:
        raise ForwardMigrationRestoreUnavailable
    if backup_instance[1]:
        if current_instance != backup_instance:
            raise ForwardMigrationRestoreUnavailable
    elif current_instance[1] != ((1, instance_id),):
        raise ForwardMigrationRestoreUnavailable

    backup_columns, backup_rows = _receipt_table(backup_state, "receipts_head")
    current_columns, current_rows = _receipt_table(current_state, "receipts_head")
    required_columns = {
        "instance_id",
        "durable_seq",
        "durable_hash",
        "observed_seq",
        "observed_hash",
        "path",
        "byte_offset",
    }
    if (
        current_columns != backup_columns
        or set(current_columns) != required_columns
    ):
        raise ForwardMigrationRestoreUnavailable
    positions = {name: current_columns.index(name) for name in current_columns}
    backup_heads = {
        str(row[positions["instance_id"]]): row for row in backup_rows
    }
    current_heads = {
        str(row[positions["instance_id"]]): row for row in current_rows
    }
    if (
        len(backup_heads) != len(backup_rows)
        or len(current_heads) != len(current_rows)
        or set(current_heads) != set(backup_heads) | {instance_id}
        or instance_id not in current_heads
    ):
        raise ForwardMigrationRestoreUnavailable
    for current_instance, current_row in current_heads.items():
        if current_instance != instance_id and current_row != backup_heads[current_instance]:
            raise ForwardMigrationRestoreUnavailable
    prior_row = backup_heads.get(instance_id)
    current_row = current_heads[instance_id]
    if prior_row is None:
        prior_row = tuple(
            {
                "instance_id": instance_id,
                "durable_seq": 0,
                "durable_hash": receipts.GENESIS_HASH,
                "observed_seq": 0,
                "observed_hash": receipts.GENESIS_HASH,
                "path": "",
                "byte_offset": 0,
            }[column]
            for column in current_columns
        )
    if (
        prior_row[positions["durable_seq"]] != intent_seq - 1
        or prior_row[positions["observed_seq"]] != intent_seq - 1
        or prior_row[positions["durable_hash"]] != intent.get("prev")
        or prior_row[positions["observed_hash"]] != intent.get("prev")
        or current_row[positions["durable_seq"]] != endpoint_seq
        or current_row[positions["observed_seq"]] != endpoint_seq
        or current_row[positions["durable_hash"]] != endpoint.get("hash")
        or current_row[positions["observed_hash"]] != endpoint.get("hash")
        or not current_row[positions["path"]]
        or int(current_row[positions["byte_offset"]]) < 0
    ):
        raise ForwardMigrationRestoreUnavailable


def _require_restore_schema_fence(plan: _ForwardMigrationRestorePlan) -> None:
    client = writer_lease.configured_schema_fence_operator_client()
    if plan.schema_fence_generation is None:
        if client is not None:
            raise ForwardMigrationRestoreUnavailable
        return
    if client is None:
        raise ForwardMigrationRestoreUnavailable
    current = client.schema_fence()
    if (
        not current.governance_enrolled
        or current.schema_version != schema_v4.SCHEMA_USER_VERSION
        or current.generation != plan.schema_fence_generation
    ):
        raise ForwardMigrationRestoreUnavailable


def _complete_restore_schema_fence(plan: _ForwardMigrationRestorePlan) -> None:
    client = writer_lease.configured_schema_fence_operator_client()
    if plan.schema_fence_generation is None:
        if client is not None:
            raise ForwardMigrationRestoreUnavailable
        return
    if client is None:
        raise ForwardMigrationRestoreUnavailable
    current = client.schema_fence()
    if (
        current.governance_enrolled
        and current.schema_version == store.SCHEMA_USER_VERSION
        and current.generation == plan.schema_fence_generation + 1
    ):
        return
    if (
        not current.governance_enrolled
        or current.schema_version != schema_v4.SCHEMA_USER_VERSION
        or current.generation != plan.schema_fence_generation
    ):
        raise ForwardMigrationRestoreUnavailable
    advanced = client.transition_schema_fence(
        expected_generation=plan.schema_fence_generation,
        schema_version=store.SCHEMA_USER_VERSION,
    )
    if (
        not advanced.governance_enrolled
        or advanced.schema_version != store.SCHEMA_USER_VERSION
        or advanced.generation != plan.schema_fence_generation + 1
    ):
        raise ForwardMigrationRestoreUnavailable


def _restore_terminal_endpoint(
    vault_root: Path,
    plan: _ForwardMigrationRestorePlan,
) -> dict[str, object]:
    """Return the one exact committed endpoint that advances restore D0 to D1."""

    root = Path(vault_root)
    connection = store.open_readonly_connection(root)
    if connection is None:
        raise ForwardMigrationRestoreUnavailable
    try:
        schema_v4.require_exact_v3_connection(connection)
        instance = connection.execute(
            "SELECT instance_id FROM receipt_instance WHERE singleton=1"
        ).fetchone()
        if instance is None or not isinstance(instance[0], str):
            raise ForwardMigrationRestoreUnavailable
        instance_id = instance[0]
        head = connection.execute(
            "SELECT durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset "
            "FROM receipts_head WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if head is None:
            raise ForwardMigrationRestoreUnavailable
    except sqlite3.Error as error:
        raise ForwardMigrationRestoreUnavailable from error
    finally:
        connection.close()

    try:
        records, issues = receipts._chain_state(  # noqa: SLF001 - exact durable locator proof
            receipts._instance_dir(root, instance_id)  # noqa: SLF001 - active receipt authority
        )
    except receipts.ReceiptError as error:
        raise ForwardMigrationRestoreUnavailable from error
    if issues:
        raise ForwardMigrationRestoreUnavailable
    terminals = [
        record
        for record in records
        if record.get("causation_id") == plan.event_id
        and record.get("phase") == "committed"
        and record.get("outcome") == "schema-v3-backup-restored"
    ]
    if len(terminals) != 1:
        raise ForwardMigrationRestoreUnavailable
    terminal = terminals[0]
    endpoint = {
        "instance_id": instance_id,
        "seq": terminal.get("seq"),
        "hash": terminal.get("hash"),
        "path": receipts._relative_locator(  # noqa: SLF001 - receipt locator authority
            root,
            Path(str(terminal.get("_path", ""))),
        ),
        "byte_offset": terminal.get("_offset"),
    }
    if (
        not isinstance(endpoint["seq"], int)
        or isinstance(endpoint["seq"], bool)
        or endpoint["seq"] < 1
        or not isinstance(endpoint["hash"], str)
        or len(endpoint["hash"]) != 64
        or not isinstance(endpoint["path"], str)
        or not endpoint["path"]
        or not isinstance(endpoint["byte_offset"], int)
        or isinstance(endpoint["byte_offset"], bool)
        or endpoint["byte_offset"] < 0
        or tuple(head)
        != (
            endpoint["seq"],
            endpoint["hash"],
            endpoint["seq"],
            endpoint["hash"],
            endpoint["path"],
            endpoint["byte_offset"],
        )
    ):
        raise ForwardMigrationRestoreUnavailable
    return endpoint


def _publish_restore_legacy_d0(
    vault_root: Path,
    plan: _ForwardMigrationRestorePlan,
    *,
    d0_digest: str,
) -> None:
    try:
        legacy_v3_placement.publish_exact_v3_snapshot(
            vault_root,
            expected_digest=d0_digest,
            event_id=plan.event_id,
        )
    except legacy_v3_placement.LegacyV3PublicationUnavailable as error:
        raise ForwardMigrationRestoreUnavailable from error


def _require_restore_marker(
    marker: object,
    plan: _ForwardMigrationRestorePlan,
    *,
    backup_plan_digest: str,
    backup_reference: str,
    source_store_digest: str,
) -> dict[str, object]:
    if not isinstance(marker, dict) or marker.get("operation") != _RESTORE_OPERATION:
        raise ForwardMigrationRestoreUnavailable
    if (
        marker.get("event_id") != plan.event_id
        or marker.get("plan_digest") != plan.plan_digest
        or marker.get("target_digest") != plan.target_digest
        or marker.get("backup_plan_digest") != backup_plan_digest
        or marker.get("backup_reference") != backup_reference
        or marker.get("source_store_digest") != source_store_digest
        or marker.get("schema_fence_generation") != plan.schema_fence_generation
        or not isinstance(marker.get("timestamp"), int)
        or isinstance(marker["timestamp"], bool)
        or marker["timestamp"] < 1
        or not isinstance(marker.get("d0"), str)
        or len(marker["d0"]) != 64
    ):
        raise ForwardMigrationRestoreUnavailable
    return marker


def _restore_result_from_marker(
    marker: dict[str, object],
    *,
    backup_plan_digest: str,
    backup_reference: str,
) -> ForwardMigrationRestoreResult:
    event_id = marker.get("event_id")
    plan_digest = marker.get("plan_digest")
    source_store_digest = marker.get("source_store_digest")
    if (
        not isinstance(event_id, str)
        or not isinstance(plan_digest, str)
        or not isinstance(source_store_digest, str)
    ):
        raise ForwardMigrationRestoreUnavailable
    return ForwardMigrationRestoreResult(
        schema_version=store.SCHEMA_USER_VERSION,
        plan_digest=backup_plan_digest,
        source_store_digest=source_store_digest,
        backup_reference=backup_reference,
        recovery_event_id=event_id,
        recovery_plan_digest=plan_digest,
        replayed=True,
    )


def _require_postfence_restore_marker(
    marker: object,
    *,
    backup_plan_digest: str,
    backup_reference: str,
) -> dict[str, object]:
    if not isinstance(marker, dict) or (
        marker.get("operation") != _RESTORE_OPERATION
        or marker.get("backup_plan_digest") != backup_plan_digest
        or marker.get("backup_reference") != backup_reference
        or marker.get("phase") not in {"legacy-aligned", "complete"}
        or not isinstance(marker.get("d0"), str)
        or not isinstance(marker.get("d1"), str)
        or not isinstance(marker.get("source_store_digest"), str)
        or len(marker["source_store_digest"]) != 64
        or not isinstance(marker.get("schema_fence_generation"), (int, type(None)))
        or not isinstance(marker.get("terminal"), dict)
    ):
        raise ForwardMigrationRestoreUnavailable
    return marker


def _require_restore_postfence_external_d1(
    vault_root: Path,
    marker: dict[str, object],
) -> None:
    expected = marker.get("d1")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ForwardMigrationRestoreUnavailable
    connection = store.open_readonly_connection(vault_root)
    if connection is None:
        raise ForwardMigrationRestoreUnavailable
    try:
        schema_v4.require_exact_v3_connection(connection)
        if not hmac.compare_digest(
            store._v3_snapshot_digest(connection),  # noqa: SLF001 - canonical external D1 proof
            expected,
        ):
            raise ForwardMigrationRestoreUnavailable
    finally:
        connection.close()


def _restore_fence_is_sealed_metadata_only(marker: dict[str, object]) -> bool:
    generation = marker.get("schema_fence_generation")
    if generation is not None and (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ForwardMigrationRestoreUnavailable
    client = writer_lease.configured_schema_fence_operator_client()
    if client is None:
        return generation is None
    current = client.schema_fence()
    return (
        current.governance_enrolled
        and current.schema_version == store.SCHEMA_USER_VERSION
        and generation is not None
        and current.generation == generation + 1
    )


def _digest_part(digest: object, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _sqlite_value_bytes(value: object) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"f" + struct.pack(">d", value)
    if isinstance(value, str):
        return b"t" + value.encode("utf-8")
    if isinstance(value, bytes):
        return b"b" + value
    raise ForwardMigrationRestoreUnavailable


def _logical_store_digest(
    connection: sqlite3.Connection,
    *,
    exclude_receipt_rows: bool,
) -> str:
    """Hash the complete logical database, independent of SQLite page layout."""

    digest = hashlib.sha256(b"exomem.governance-logical-store.v1")
    _digest_part(
        digest,
        str(int(connection.execute("PRAGMA user_version").fetchone()[0])).encode(
            "ascii"
        ),
    )
    master = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        )
    )
    for row in master:
        _digest_part(
            digest,
            b"".join(
                len(encoded).to_bytes(8, "big") + encoded
                for encoded in (_sqlite_value_bytes(value) for value in row)
            ),
        )
    tables = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    for table in tables:
        _digest_part(digest, table.encode("utf-8"))
        if exclude_receipt_rows and table in _RECEIPT_TABLES:
            _digest_part(digest, b"receipt-rows-excluded")
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        rows = []
        for row in connection.execute(f"SELECT * FROM {quoted}"):
            rows.append(
                b"".join(
                    len(encoded).to_bytes(8, "big") + encoded
                    for encoded in (_sqlite_value_bytes(value) for value in row)
                )
            )
        for encoded in sorted(rows):
            _digest_part(digest, encoded)
    return digest.hexdigest()


def _initial_migration_seed(
    connection: sqlite3.Connection,
    backup: ForwardMigrationBackup,
) -> schema_v4.MigrationSeed:
    policy_row = connection.execute(
        "SELECT source_fingerprint, conflict_digest, compiled_policy, "
        "policy_fingerprint, compiler_schema_version, projector_schema_version, "
        "predecessor_generation_id, authoring_event_id, receipt_event_id, created_at "
        "FROM compiled_policy_generations WHERE generation_id=?",
        (backup.target.policy_generation_id,),
    ).fetchone()
    catalog_row = connection.execute(
        "SELECT descriptor, artifact_count, created_at "
        "FROM catalog_generation_descriptors WHERE catalog_generation=?",
        (backup.target.catalog_generation,),
    ).fetchone()
    namespace_row = connection.execute(
        "SELECT namespace_id, evidence, ready_at "
        "FROM governance_projection_namespaces WHERE policy_fingerprint=? "
        "AND projector_schema_version=? AND catalog_generation=?",
        (
            backup.target.policy_fingerprint,
            backup.target.projector_schema_version,
            backup.target.catalog_generation,
        ),
    ).fetchone()
    migration_row = connection.execute(
        "SELECT migrated_at FROM governance_schema_migrations "
        "WHERE migration_id='v3-to-v4'"
    ).fetchone()
    if any(row is None for row in (policy_row, catalog_row, namespace_row, migration_row)):
        raise ForwardMigrationRestoreUnavailable
    seed = schema_v4.MigrationSeed(
        activation_store_id=backup.target.activation_store_id,
        logical_vault_id=backup.target.logical_vault_id,
        activation_epoch=backup.target.activation_epoch,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id=backup.target.policy_generation_id,
            source_documents=backup.source_documents,
            source_fingerprint=str(policy_row[0]),
            conflict_digest=str(policy_row[1]),
            compiled_policy=bytes(policy_row[2]),
            policy_fingerprint=str(policy_row[3]),
            compiler_schema_version=int(policy_row[4]),
            projector_schema_version=int(policy_row[5]),
            predecessor_generation_id=(
                None if policy_row[6] is None else str(policy_row[6])
            ),
            authoring_event_id=str(policy_row[7]),
            receipt_event_id=str(policy_row[8]),
            created_at=int(policy_row[9]),
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=backup.target.catalog_generation,
            descriptor=bytes(catalog_row[0]),
            artifact_count=int(catalog_row[1]),
            created_at=int(catalog_row[2]),
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id=str(namespace_row[0]),
            evidence=bytes(namespace_row[1]),
            ready_at=int(namespace_row[2]),
        ),
        migrated_at=int(migration_row[0]),
    )
    if (
        schema_v4.migration_target(seed) != backup.target
        or seed.catalog.descriptor != backup.catalog_descriptor
        or seed.catalog.artifact_count != backup.item_count
        or seed.namespace.namespace_id != backup.target.projection_namespace_id
        or seed.namespace.evidence != backup.projection_namespace_evidence
    ):
        raise ForwardMigrationRestoreUnavailable
    return seed


def _require_pristine_v4_store(
    connection: sqlite3.Connection,
    backup: ForwardMigrationBackup,
) -> None:
    schema_v4.require_exact_v4_connection(connection)
    seed = _initial_migration_seed(connection, backup)
    expected = sqlite3.connect(":memory:")
    try:
        expected.deserialize(backup.serialized_v3)
        schema_v4.require_exact_v3_connection(expected)
        schema_v4.migrate_v3_connection(expected, seed)
        schema_v4.require_exact_v4_connection(expected)
        if not hmac.compare_digest(
            _logical_store_digest(
                connection,
                exclude_receipt_rows=True,
            ),
            _logical_store_digest(
                expected,
                exclude_receipt_rows=True,
            ),
        ):
            raise ForwardMigrationRestoreUnavailable
    finally:
        expected.close()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _replace_database_schema(
    destination: sqlite3.Connection,
    source: sqlite3.Connection,
) -> None:
    if not destination.in_transaction:
        raise ForwardMigrationRestoreUnavailable
    destination_objects = tuple(
        (str(row[0]), str(row[1]))
        for row in destination.execute(
            "SELECT type, name FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )
    for kind in ("trigger", "view", "index", "table"):
        for object_type, name in destination_objects:
            if object_type == kind:
                destination.execute(
                    f"DROP {kind.upper()} {_quoted_identifier(name)}"
                )

    source_objects = tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in source.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        )
    )
    for object_type, name, sql in source_objects:
        destination.execute(sql)
        if object_type != "table":
            continue
        quoted = _quoted_identifier(name)
        columns = tuple(source.execute(f"PRAGMA table_info({quoted})"))
        if not columns:
            raise ForwardMigrationRestoreUnavailable
        placeholders = ",".join("?" for _column in columns)
        rows = source.execute(f"SELECT * FROM {quoted}").fetchall()
        if rows:
            destination.executemany(
                f"INSERT INTO {quoted} VALUES ({placeholders})",
                rows,
            )
    destination.execute(
        f"PRAGMA user_version={int(source.execute('PRAGMA user_version').fetchone()[0])}"
    )


def _prepare_restore_destination(
    destination: sqlite3.Connection,
    backup: ForwardMigrationBackup,
) -> None:
    destination.execute("PRAGMA busy_timeout=0")
    destination.execute("PRAGMA synchronous=FULL")
    _require_pristine_v4_store(destination, backup)
    checkpoint = destination.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if (
        checkpoint is None
        or len(checkpoint) != 3
        or int(checkpoint[0]) != 0
        or int(checkpoint[1]) != int(checkpoint[2])
    ):
        raise ForwardMigrationRestoreUnavailable
    mode = destination.execute("PRAGMA journal_mode=DELETE").fetchone()
    if mode is None or str(mode[0]).casefold() != "delete":
        raise ForwardMigrationRestoreUnavailable
    destination.execute("BEGIN EXCLUSIVE")
    try:
        _require_pristine_v4_store(destination, backup)
    except BaseException:
        destination.rollback()
        raise


def _preflight_v4_restore_database(
    vault_root: Path,
    backup: ForwardMigrationBackup,
) -> None:
    root = Path(vault_root)
    path = store.sidecar_path(root)
    try:
        receipts._close_receipt_connections()  # noqa: SLF001
    except receipts.ReceiptError as error:
        raise ForwardMigrationRestoreUnavailable from error
    with reserved_paths._subsystem_authority_scope("governance.store"):
        with reserved_paths._sqlite_owner_target_scope(
            root,
            path,
            "governance-store",
            create=False,
        ) as retained_path:
            destination = sqlite3.connect(
                f"{retained_path.as_uri()}?mode=rw",
                uri=True,
            )
            try:
                reserved_paths._publish_sqlite_owner_family(
                    root,
                    path,
                    "governance-store",
                    destination,
                )
                _prepare_restore_destination(destination, backup)
                destination.rollback()
            finally:
                destination.close()


def _restore_v3_database(
    vault_root: Path,
    plan: _ForwardMigrationRestorePlan,
    *,
    prepare_marker: Callable[[str], None],
) -> str:
    root = Path(vault_root)
    path = store.sidecar_path(root)
    try:
        receipts._close_receipt_connections()  # noqa: SLF001
    except receipts.ReceiptError as error:
        raise ForwardMigrationRestoreUnavailable from error
    source = sqlite3.connect(":memory:")
    try:
        source.deserialize(plan.backup.serialized_v3)
        schema_v4.require_exact_v3_connection(source)
        with reserved_paths._subsystem_authority_scope("governance.store"):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                path,
                "governance-store",
                create=False,
            ) as retained_path:
                destination = sqlite3.connect(
                    f"{retained_path.as_uri()}?mode=rw",
                    uri=True,
                )
                try:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        path,
                        "governance-store",
                        destination,
                    )
                    _prepare_restore_destination(destination, plan.backup)
                    _replace_receipt_state(source, _receipt_state(destination))
                    schema_v4.require_exact_v3_connection(source)
                    _replace_database_schema(destination, source)
                    schema_v4.require_exact_v3_connection(destination)
                    d0_digest = store.canonical_uncommitted_v3_digest(destination)
                    prepare_marker(d0_digest)
                    destination.commit()
                    schema_v4.require_exact_v3_connection(destination)
                    if not hmac.compare_digest(
                        store._v3_snapshot_digest(destination),  # noqa: SLF001 - canonical post-commit proof
                        d0_digest,
                    ):
                        raise ForwardMigrationRestoreUnavailable
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        path,
                        "governance-store",
                        destination,
                    )
                    return d0_digest
                except BaseException:
                    if destination.in_transaction:
                        destination.rollback()
                    raise
                finally:
                    destination.close()
    finally:
        source.close()


def restore_forward_migration_backup(
    vault_root: Path,
    *,
    expected_plan_digest: str,
    expected_backup_reference: str,
    now: int,
) -> ForwardMigrationRestoreResult:
    """Restore the immediate pre-migration v3 backup under a drained v4 fence.

    This deliberately narrow rollback accepts only an unchanged active tuple,
    workspace/catalog, and receipt baseline.  Once later durable evidence exists,
    operators must use the v4-to-v3 downmigration that preserves the current
    generation instead of rewinding history.
    """

    root = Path(vault_root)
    try:
        moment = _bounded_integer(now, minimum=1)
        expected_plan = _require_digest(expected_plan_digest)
        expected_reference = _require_backup_reference(expected_backup_reference)
        with state_migration.governance_rollback_session(root) as session:
            marker = session.marker
            if marker is not None and marker.get("phase") == "complete":
                sealed = _require_postfence_restore_marker(
                    marker,
                    backup_plan_digest=expected_plan,
                    backup_reference=expected_reference,
                )
                _require_restore_postfence_external_d1(root, sealed)
                if not _restore_fence_is_sealed_metadata_only(sealed):
                    raise ForwardMigrationRestoreUnavailable
                return _restore_result_from_marker(
                    sealed,
                    backup_plan_digest=expected_plan,
                    backup_reference=expected_reference,
                )
            if (
                marker is not None
                and marker.get("phase") == "legacy-aligned"
                and _restore_fence_is_sealed_metadata_only(marker)
            ):
                sealed = _require_postfence_restore_marker(
                    marker,
                    backup_plan_digest=expected_plan,
                    backup_reference=expected_reference,
                )
                _require_restore_postfence_external_d1(root, sealed)
                session.seal_complete_metadata_only()
                _forward_migration_barrier("after_restore_complete_marker")
                return _restore_result_from_marker(
                    sealed,
                    backup_plan_digest=expected_plan,
                    backup_reference=expected_reference,
                )

            backup = verify_forward_migration_backup(
                root,
                expected_plan_digest=expected_plan,
            )
            if not hmac.compare_digest(backup.backup_reference, expected_reference):
                raise ForwardMigrationRestoreUnavailable

            with (
                receipts.exclusive_sequence(root),
                reserved_paths._identity_coordination_scope(
                    root,
                    identity_may_change=False,
                ),
            ):
                version = store.authorization_session_schema_version(root)
                if version not in {
                    store.SCHEMA_USER_VERSION,
                    schema_v4.SCHEMA_USER_VERSION,
                }:
                    raise ForwardMigrationRestoreUnavailable
                custody = _require_drained_restore_custody(
                    root,
                    backup=backup,
                    now=moment,
                )
                plan = _restore_plan(
                    root,
                    backup=backup,
                    custody=custody,
                    schema_version=version,
                )
                if marker is not None:
                    marker = _require_restore_marker(
                        marker,
                        plan,
                        backup_plan_digest=backup.plan_digest,
                        backup_reference=backup.backup_reference,
                        source_store_digest=backup.source_store_digest,
                    )
                event_state = _restore_event_state(root, plan)
                replayed = event_state != "absent"
                if event_state != "absent":
                    _require_restore_receipt_progress(
                        root,
                        plan,
                        schema_version=version,
                        event_state=event_state,
                    )
                if version == schema_v4.SCHEMA_USER_VERSION:
                    _verify_active_target(root, backup)
                    _verify_live_source_material(root, backup)
                    if event_state == "committed":
                        raise ForwardMigrationRestoreUnavailable
                    if event_state == "absent" and _store_receipt_state(
                        root,
                        schema_version=version,
                    ) != _backup_receipt_state(backup):
                        raise ForwardMigrationRestoreUnavailable
                    _preflight_v4_restore_database(root, backup)
                    event_state = _ensure_restore_intent(root, plan)
                    _require_restore_receipt_progress(
                        root,
                        plan,
                        schema_version=version,
                        event_state=event_state,
                    )
                    _forward_migration_barrier("after_restore_receipt_intent")
                    current_custody = _require_drained_restore_custody(
                        root,
                        backup=backup,
                        now=moment,
                    )
                    current_plan = _restore_plan(
                        root,
                        backup=backup,
                        custody=current_custody,
                        schema_version=version,
                    )
                    if current_plan != plan:
                        raise ForwardMigrationRestoreUnavailable
                    _verify_active_target(root, backup)
                    _verify_live_source_material(root, backup)
                    _require_restore_receipt_progress(
                        root,
                        plan,
                        schema_version=version,
                        event_state=event_state,
                    )
                    _require_restore_schema_fence(plan)

                    def prepare_marker(d0_digest: str) -> None:
                        nonlocal marker
                        if marker is None:
                            marker = session.begin_prepared(
                                operation=_RESTORE_OPERATION,
                                event_id=plan.event_id,
                                plan_digest=plan.plan_digest,
                                target_digest=plan.target_digest,
                                backup_reference=backup.backup_reference,
                                backup_plan_digest=backup.plan_digest,
                                source_store_digest=backup.source_store_digest,
                                schema_fence_generation=plan.schema_fence_generation,
                                timestamp=moment,
                                d0=d0_digest,
                            )
                            return
                        prepared = _require_restore_marker(
                            marker,
                            plan,
                            backup_plan_digest=backup.plan_digest,
                            backup_reference=backup.backup_reference,
                            source_store_digest=backup.source_store_digest,
                        )
                        if (
                            prepared.get("phase") != "prepared"
                            or not hmac.compare_digest(str(prepared["d0"]), d0_digest)
                        ):
                            raise ForwardMigrationRestoreUnavailable

                    d0_digest = _restore_v3_database(
                        root,
                        plan,
                        prepare_marker=prepare_marker,
                    )
                    _forward_migration_barrier("after_store_restore")
                else:
                    if marker is None or event_state not in {"intent", "committed"}:
                        raise ForwardMigrationRestoreUnavailable
                    _verify_live_source_material(root, backup)
                    _require_restore_receipt_progress(
                        root,
                        plan,
                        schema_version=version,
                        event_state=event_state,
                    )
                    d0_digest = str(marker["d0"])
                    if event_state == "intent" and not hmac.compare_digest(
                        legacy_v3_placement.exact_external_v3_digest(root),
                        d0_digest,
                    ):
                        raise ForwardMigrationRestoreUnavailable

                marker = _require_restore_marker(
                    marker,
                    plan,
                    backup_plan_digest=backup.plan_digest,
                    backup_reference=backup.backup_reference,
                    source_store_digest=backup.source_store_digest,
                )
                phase = marker.get("phase")
                if phase == "prepared":
                    if event_state != "committed":
                        _publish_restore_legacy_d0(root, plan, d0_digest=d0_digest)
                        _forward_migration_barrier("after_legacy_v3_publication")
                        try:
                            receipts.commit_event(
                                root,
                                plan.event_id,
                                outcome="schema-v3-backup-restored",
                            )
                        except receipts.ReceiptError as error:
                            raise ForwardMigrationRestoreUnavailable from error
                        _forward_migration_barrier("after_restore_terminal_durable")
                    d1_digest = legacy_v3_placement.prove_d1_against_legacy(
                        root,
                        event_id=plan.event_id,
                        d0_digest=d0_digest,
                        expected_outcome="schema-v3-backup-restored",
                    )
                    session.advance_receipt_committed(
                        d1_digest,
                        _restore_terminal_endpoint(root, plan),
                    )
                    marker = _require_restore_marker(
                        session.marker,
                        plan,
                        backup_plan_digest=backup.plan_digest,
                        backup_reference=backup.backup_reference,
                        source_store_digest=backup.source_store_digest,
                    )
                    _forward_migration_barrier("after_restore_receipt_commit")
                    phase = marker.get("phase")
                if phase == "receipt-committed":
                    expected_d1 = _require_digest(marker.get("d1"))
                    endpoint = _restore_terminal_endpoint(root, plan)
                    if marker.get("terminal") != endpoint:
                        raise ForwardMigrationRestoreUnavailable
                    d1_digest = legacy_v3_placement.align_legacy_to_d1(
                        root,
                        event_id=plan.event_id,
                        d0_digest=d0_digest,
                        expected_outcome="schema-v3-backup-restored",
                    )
                    if not hmac.compare_digest(expected_d1, d1_digest):
                        raise ForwardMigrationRestoreUnavailable
                    session.advance_legacy_aligned()
                    _forward_migration_barrier("after_restore_legacy_aligned")
                elif phase != "legacy-aligned":
                    raise ForwardMigrationRestoreUnavailable
                else:
                    expected_d1 = _require_digest(marker.get("d1"))
                    endpoint = _restore_terminal_endpoint(root, plan)
                    if marker.get("terminal") != endpoint:
                        raise ForwardMigrationRestoreUnavailable
                    d1_digest = legacy_v3_placement.align_legacy_to_d1(
                        root,
                        event_id=plan.event_id,
                        d0_digest=d0_digest,
                        expected_outcome="schema-v3-backup-restored",
                    )
                    if not hmac.compare_digest(expected_d1, d1_digest):
                        raise ForwardMigrationRestoreUnavailable
                _complete_restore_schema_fence(plan)
                _forward_migration_barrier("after_restore_schema_fence")
                session.seal_complete_metadata_only()
                _forward_migration_barrier("after_restore_complete_marker")
    except (_ForwardMigrationCrash, ForwardMigrationRestoreUnavailable):
        raise
    except (
        ForwardMigrationUnavailable,
        authorization_custody.AuthorizationCustodyUnavailable,
        receipts.ReceiptError,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        writer_lease.OpError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ) as error:
        raise ForwardMigrationRestoreUnavailable from error
    return ForwardMigrationRestoreResult(
        schema_version=store.SCHEMA_USER_VERSION,
        plan_digest=backup.plan_digest,
        source_store_digest=backup.source_store_digest,
        backup_reference=backup.backup_reference,
        recovery_event_id=plan.event_id,
        recovery_plan_digest=plan.plan_digest,
        replayed=replayed,
    )


def commit_forward_migration(
    vault_root: Path,
    *,
    expected_plan_digest: str,
    now: int,
) -> ForwardMigrationResult:
    """Commit the reviewed staged target after a verified private v3 backup."""

    expected = _require_digest(expected_plan_digest)
    root = Path(vault_root)
    version = store.authorization_session_schema_version(root)
    if version == schema_v4.SCHEMA_USER_VERSION:
        with reserved_paths._identity_coordination_scope(
            root,
            identity_may_change=False,
        ):
            backup = verify_forward_migration_backup(
                root,
                expected_plan_digest=expected,
            )
            _verify_active_target(root, backup)
            _verify_live_source_material(root, backup)
            authorization_custody.complete_standalone_v4_migration(
                root,
                target=backup.target,
                now=now,
            )
        return ForwardMigrationResult(
            schema_version=schema_v4.SCHEMA_USER_VERSION,
            target=backup.target,
            plan_digest=expected,
            source_store_digest=backup.source_store_digest,
            backup_reference=backup.backup_reference,
            replayed=True,
        )
    if version != store.SCHEMA_USER_VERSION:
        raise ForwardMigrationUnavailable

    try:
        with reserved_paths._identity_coordination_scope(
            root,
            identity_may_change=False,
        ):
            plan = prepare_forward_migration(root, now=now)
            if not hmac.compare_digest(plan.plan_digest, expected):
                raise ForwardMigrationPlanMismatch
            key, _items, manifest = _stage_material(root, plan)
            if projection_store.verify_variant_store(
                root,
                key=key,
                expected_rows_digest=plan.projection_rows_digest,
            ) != manifest:
                raise ForwardMigrationPlanMismatch
            serialized, source_digest = _source_store_snapshot(root)
            if not hmac.compare_digest(source_digest, plan.source_store_digest):
                raise ForwardMigrationPlanMismatch
            backup_bytes = _backup_bytes(plan, serialized_v3=serialized)
            backup_path = forward_migration_backup_path(root, plan_digest=expected)
            authorization_custody._publish_private_artifact(  # noqa: SLF001
                backup_path,
                backup_bytes,
                maximum_bytes=_MAX_BACKUP_BYTES,
            )
            backup = verify_forward_migration_backup(
                root,
                expected_plan_digest=expected,
            )
            if (
                backup.target != plan.target
                or backup.source_documents != plan.seed.policy.source_documents
                or backup.catalog_descriptor != plan.seed.catalog.descriptor
                or backup.projection_namespace_evidence != plan.seed.namespace.evidence
                or not hmac.compare_digest(
                    backup.source_store_digest,
                    plan.source_store_digest,
                )
            ):
                raise ForwardMigrationPlanMismatch
            _forward_migration_barrier("after_backup")
            current = prepare_forward_migration(root, now=now)
            if current != plan:
                raise ForwardMigrationPlanMismatch
            authorization_custody.enroll_standalone_v3_migration(
                root,
                target=plan.target,
                now=now,
            )
            _forward_migration_barrier("after_enrollment")
            active = store.migrate_enrolled_v3_store(
                root,
                seed=plan.seed,
                expected_source_store_digest=plan.source_store_digest,
                now=now,
                source_recheck=lambda: _verify_live_source_material(root, backup),
            )
    except (ForwardMigrationPlanMismatch, _ForwardMigrationCrash):
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        projection_store.ProjectionStoreError,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ) as error:
        raise ForwardMigrationUnavailable from error
    if active != plan.target:
        raise ForwardMigrationUnavailable
    return ForwardMigrationResult(
        schema_version=schema_v4.SCHEMA_USER_VERSION,
        target=active,
        plan_digest=expected,
        source_store_digest=backup.source_store_digest,
        backup_reference=backup.backup_reference,
        replayed=False,
    )


__all__ = [
    "ForwardMigrationBackup",
    "ForwardMigrationPlan",
    "ForwardMigrationPlanMismatch",
    "ForwardMigrationRestoreResult",
    "ForwardMigrationRestoreUnavailable",
    "ForwardMigrationResult",
    "ForwardMigrationStageResult",
    "ForwardMigrationUnavailable",
    "commit_forward_migration",
    "forward_migration_backup_path",
    "plan_summary",
    "prepare_forward_migration",
    "restore_forward_migration_backup",
    "stage_forward_migration",
    "verify_forward_migration_backup",
]
