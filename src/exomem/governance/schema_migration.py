"""Prepare the exact inert target for an offline governance v3-to-v4 migration."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
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


class ForwardMigrationUnavailable(RuntimeError):
    """The exact v3 source cannot produce one reviewable migration target."""


class ForwardMigrationPlanMismatch(ForwardMigrationUnavailable):
    """The owner-confirmed plan is not the exact current migration plan."""


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


def _source_store_digest(vault_root: Path) -> str:
    source = store.open_readonly_connection(vault_root)
    if source is None:
        raise ForwardMigrationUnavailable
    snapshot = sqlite3.connect(":memory:")
    try:
        schema_v4.require_exact_v3_connection(source)
        source.backup(snapshot)
        schema_v4.require_exact_v3_connection(snapshot)
        serialized = snapshot.serialize()
    except (AttributeError, OSError, RuntimeError, sqlite3.Error) as error:
        raise ForwardMigrationUnavailable from error
    finally:
        snapshot.close()
        source.close()
    return _framed_digest(b"exomem.governance-v3-snapshot.v1", serialized)


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
        staged = authorization_custody.stage_standalone_v3_custody(root, now=now)
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


__all__ = [
    "ForwardMigrationPlan",
    "ForwardMigrationPlanMismatch",
    "ForwardMigrationStageResult",
    "ForwardMigrationUnavailable",
    "plan_summary",
    "prepare_forward_migration",
    "stage_forward_migration",
]
