"""Prepare the exact inert target for an offline governance v3-to-v4 migration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import find_corpus, reserved_paths
from ..kbdir import kb_dirname
from . import (
    authorization_custody,
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
_MAX_BACKUP_BYTES = 512 * 1024 * 1024
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
    "ForwardMigrationResult",
    "ForwardMigrationStageResult",
    "ForwardMigrationUnavailable",
    "commit_forward_migration",
    "forward_migration_backup_path",
    "plan_summary",
    "prepare_forward_migration",
    "stage_forward_migration",
    "verify_forward_migration_backup",
]
