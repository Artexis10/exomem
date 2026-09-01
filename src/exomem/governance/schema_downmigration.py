"""Receipt-first, replayable offline governance schema-v4 downmigration."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .. import held_fs, reserved_paths, state_migration, writer_lease
from . import authorization_custody, legacy_v3_placement, policy, receipts, schema_v4, store

_OPERATION: Final = "governance_schema_v4_downmigration"
_PLAN_SCHEMA: Final = "exomem.governance-downmigration-plan/v1"
_PLAN_DOMAIN: Final = b"exomem.governance-downmigration-plan.v1"
_TARGET_DOMAIN: Final = b"exomem.governance-v3-downmigration-target.v1"
_TERMINAL_DOMAIN: Final = b"exomem.governance-downmigration-terminal.v1"
_MAX_PLAN_BYTES: Final = 64 * 1024 * 1024
_ACTIVE_FIELDS: Final = frozenset(
    {
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
)
_TERMINAL_FIELDS: Final = frozenset(
    {
        "schema",
        "recovery_event_id",
        "recovery_plan_digest",
        "recovery_target_digest",
        "logical_vault_id",
        "activation_store_id",
        "activation_epoch",
        "activation_state_digest",
        "policy_generation_id",
        "policy_fingerprint",
        "projector_schema_version",
        "catalog_generation",
        "projection_namespace_id",
        "workspace_digest",
        "catalog_digest",
        "downmigrated_at",
        "closed_sessions",
        "expired_grants",
        "expired_purposes",
        "expired_tokens",
        "expired_proposals",
    }
)


class DownmigrationUnavailable(RuntimeError):
    """The offline coordinator cannot prove one exact safe downmigration."""


@dataclass(frozen=True, slots=True)
class OfflineDownmigrationResult:
    schema_version: int
    active: schema_v4.VerifiedActiveGovernanceState
    recovery_event_id: str
    recovery_plan_digest: str
    recovery_target_digest: str
    recovery_terminal_digest: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class _RecoveryPlan:
    active: schema_v4.VerifiedActiveGovernanceState
    reviewed_workspace: policy.AuthoringSnapshot
    source_documents: tuple[tuple[str, bytes], ...]
    catalog_descriptor: bytes
    workspace_digest: str
    catalog_digest: str
    control_digest: str
    keyring_digest: str
    membership_epoch: int
    membership_digest: str
    schema_fence_generation: int | None
    created_at: int
    value_json: str
    plan_digest: str
    target_digest: str
    event_id: str


@contextmanager
def _owned_store_connection(
    vault_root: Path,
    *,
    schema_version: int,
    writable: bool,
):
    root = Path(vault_root)
    path = store.sidecar_path(root)
    mode = "rw" if writable else "ro"
    with reserved_paths._subsystem_authority_scope("governance.store"):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=("governance-store",),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                path,
                "governance-store",
                create=False,
            ) as retained_path:
                connection = sqlite3.connect(
                    f"{retained_path.as_uri()}?mode={mode}",
                    uri=True,
                )
                try:
                    connection.execute("PRAGMA busy_timeout=0")
                    if writable:
                        connection.execute("PRAGMA synchronous=FULL")
                    else:
                        connection.execute("PRAGMA query_only=ON")
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version != schema_version:
                        raise DownmigrationUnavailable
                    if schema_version == schema_v4.SCHEMA_USER_VERSION:
                        schema_v4.require_exact_v4_connection(connection)
                    elif schema_version == store.SCHEMA_USER_VERSION:
                        schema_v4.require_exact_v3_connection(connection)
                    else:  # pragma: no cover - closed internal callers
                        raise DownmigrationUnavailable
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        path,
                        "governance-store",
                        connection,
                    )
                    yield connection
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        path,
                        "governance-store",
                        connection,
                    )
                finally:
                    connection.close()


def _downmigration_barrier(point: str) -> None:
    """Test seam around separately durable coordinator effects."""

    del point


def _frame(domain: bytes, *fields: bytes) -> bytes:
    value = bytearray(domain)
    value.append(0)
    for field in fields:
        if len(field) > (1 << 32) - 1:
            raise DownmigrationUnavailable
        value.extend(len(field).to_bytes(4, "big"))
        value.extend(field)
    return bytes(value)


def _framed_digest(domain: bytes, *fields: bytes) -> str:
    return hashlib.sha256(_frame(domain, *fields)).hexdigest()


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DownmigrationUnavailable
    return value


def _bounded_integer(value: object, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= (1 << 63) - 1
    ):
        raise DownmigrationUnavailable
    return value


def _sqlite_integer(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _bounded_integer(value, minimum=minimum)


def _bounded_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or unicodedata.normalize("NFC", value) != value
        or len(value.encode("utf-8")) > 4096
    ):
        raise DownmigrationUnavailable
    return value


def _identity_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= (1 << 128) - 1:
        raise DownmigrationUnavailable
    return value


def _identity_value(identity: held_fs.StableIdentity | None) -> dict[str, object] | None:
    if identity is None:
        return None
    return {
        "device": identity.device,
        "inode": identity.inode,
        "kind": identity.kind,
        # Directory link counts change when receipt infrastructure creates a
        # sibling. Held-directory equality deliberately binds identity, not
        # that mutable count; normalize the persisted recovery preimage too.
        "link_count": identity.link_count if identity.kind == "file" else 1,
    }


def _identity_from_value(value: object, *, kind: str) -> held_fs.StableIdentity:
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "kind",
        "link_count",
    }:
        raise DownmigrationUnavailable
    device = _identity_integer(value["device"])
    inode = _identity_integer(value["inode"])
    links = _bounded_integer(value["link_count"], minimum=1)
    if value["kind"] != kind or (kind == "file" and links != 1):
        raise DownmigrationUnavailable
    return held_fs.StableIdentity(device, inode, kind, links)


def _documents_value(
    documents: tuple[tuple[str, bytes], ...],
) -> list[dict[str, str]]:
    return [
        {"path": relative, "bytes": base64.b64encode(content).decode("ascii")}
        for relative, content in documents
    ]


def _documents_from_value(value: object) -> tuple[tuple[str, bytes], ...]:
    if not isinstance(value, list):
        raise DownmigrationUnavailable
    result: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "bytes"}:
            raise DownmigrationUnavailable
        relative = _bounded_text(item["path"])
        if relative in seen or not policy._authoring_snapshot_relative_path(relative):
            raise DownmigrationUnavailable
        encoded = item["bytes"]
        if not isinstance(encoded, str):
            raise DownmigrationUnavailable
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise DownmigrationUnavailable from None
        if base64.b64encode(content).decode("ascii") != encoded:
            raise DownmigrationUnavailable
        seen.add(relative)
        result.append((relative, content))
    ordered = tuple(sorted(result))
    try:
        encoded = schema_v4._source_document_bytes(ordered)
    except schema_v4.SchemaV4Error:
        raise DownmigrationUnavailable from None
    if tuple(result) != ordered or len(encoded) > _MAX_PLAN_BYTES:
        raise DownmigrationUnavailable
    return ordered


def _snapshot_value(snapshot: policy.AuthoringSnapshot) -> dict[str, object]:
    return {
        "documents": _documents_value(snapshot.documents),
        "source_fingerprint": snapshot.source_fingerprint,
        "conflict_set_digest": snapshot.conflict_set_digest,
        "guard_generation": snapshot.guard_generation,
        "file_identities": [
            {
                "path": item.path,
                "identity": _identity_value(item.identity),
                "sha256": item.sha256,
            }
            for item in snapshot.file_identities
        ],
        "directory_identities": [
            {"path": relative, "identity": _identity_value(identity)}
            for relative, identity in snapshot.directory_identities
        ],
        "governance_root_identity": _identity_value(snapshot.governance_root_identity),
    }


def _snapshot_from_value(value: object) -> policy.AuthoringSnapshot:
    if not isinstance(value, dict) or set(value) != {
        "documents",
        "source_fingerprint",
        "conflict_set_digest",
        "guard_generation",
        "file_identities",
        "directory_identities",
        "governance_root_identity",
    }:
        raise DownmigrationUnavailable
    documents = _documents_from_value(value["documents"])
    document_map = dict(documents)
    source_fingerprint = _digest(value["source_fingerprint"])
    if source_fingerprint != policy._document_fingerprint(document_map):
        raise DownmigrationUnavailable
    conflict_digest = _digest(value["conflict_set_digest"])
    if conflict_digest != policy._path_set_digest(
        b"exomem.governance-conflict-set.v1", ()
    ):
        raise DownmigrationUnavailable
    guard = value["guard_generation"]
    if guard != "":
        raise DownmigrationUnavailable

    raw_files = value["file_identities"]
    if not isinstance(raw_files, list):
        raise DownmigrationUnavailable
    files: list[policy.AuthoringFileIdentity] = []
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "identity", "sha256"}:
            raise DownmigrationUnavailable
        relative = _bounded_text(item["path"])
        digest = _digest(item["sha256"])
        content = document_map.get(relative)
        if content is None or hashlib.sha256(content).hexdigest() != digest:
            raise DownmigrationUnavailable
        files.append(
            policy.AuthoringFileIdentity(
                relative,
                _identity_from_value(item["identity"], kind="file"),
                digest,
            )
        )
    if tuple(item.path for item in files) != tuple(relative for relative, _ in documents):
        raise DownmigrationUnavailable

    raw_directories = value["directory_identities"]
    if not isinstance(raw_directories, list):
        raise DownmigrationUnavailable
    directories: list[tuple[str, held_fs.StableIdentity]] = []
    for item in raw_directories:
        if not isinstance(item, dict) or set(item) != {"path", "identity"}:
            raise DownmigrationUnavailable
        relative = _bounded_text(item["path"])
        if (
            not policy._authoring_snapshot_relative_path(relative)
            or relative in document_map
        ):
            raise DownmigrationUnavailable
        directories.append((relative, _identity_from_value(item["identity"], kind="directory")))
    if tuple(relative for relative, _ in directories) != tuple(
        sorted({relative for relative, _ in directories})
    ):
        raise DownmigrationUnavailable
    required_directories = {
        parent.as_posix()
        for relative in (*document_map, *(relative for relative, _ in directories))
        for parent in Path(relative).parents
        if parent.as_posix() != "."
    }
    if not required_directories <= {relative for relative, _ in directories}:
        raise DownmigrationUnavailable
    root_value = value["governance_root_identity"]
    root_identity = (
        None if root_value is None else _identity_from_value(root_value, kind="directory")
    )
    if root_identity is None and (documents or directories):
        raise DownmigrationUnavailable
    return policy.AuthoringSnapshot(
        documents=documents,
        source_fingerprint=source_fingerprint,
        conflict_set_digest=conflict_digest,
        guard_generation=guard,
        file_identities=tuple(files),
        directory_identities=tuple(directories),
        governance_root_identity=root_identity,
    )


def _active_value(active: schema_v4.VerifiedActiveGovernanceState) -> dict[str, object]:
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


def _active_from_value(value: object) -> schema_v4.VerifiedActiveGovernanceState:
    if not isinstance(value, dict) or set(value) != _ACTIVE_FIELDS:
        raise DownmigrationUnavailable
    return schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=_bounded_text(value["logical_vault_id"]),
        activation_store_id=_bounded_text(value["activation_store_id"]),
        activation_epoch=_bounded_integer(value["activation_epoch"], minimum=1),
        activation_state_digest=_digest(value["activation_state_digest"]),
        policy_generation_id=_bounded_text(value["policy_generation_id"]),
        policy_fingerprint=_digest(value["policy_fingerprint"]),
        projector_schema_version=_bounded_integer(value["projector_schema_version"], minimum=1),
        catalog_generation=_bounded_integer(value["catalog_generation"], minimum=1),
        projection_namespace_id=_bounded_text(value["projection_namespace_id"]),
    )


def _custody_digests(
    vault_root: Path,
) -> tuple[str, str]:
    external = authorization_custody.load_external_custody(vault_root)
    return hashlib.sha256(external.control).hexdigest(), hashlib.sha256(
        external.keyring
    ).hexdigest()


def _require_drained_custody(
    vault_root: Path,
    *,
    now: int,
) -> authorization_custody.AuthorizationCustody:
    try:
        custody = authorization_custody.load_authorization_custody(Path(vault_root), now=now)
        control = custody.control
        membership = custody.serving_membership
        if (
            not control.governance_enrolled
            or control.activation_store_id is None
            or control.activation_epoch is None
            or control.activation_state_digest is None
            or membership is None
            or membership.epoch != control.serving_membership_epoch
            or membership.record_digest != control.serving_membership_digest
            or not membership.replicas
            or any(
                item.state != "DRAINING"
                or not item.issuance_stopped
                or not item.no_in_flight
                or item.schema_version != schema_v4.SCHEMA_USER_VERSION
                for item in membership.replicas
            )
        ):
            raise DownmigrationUnavailable
        return custody
    except DownmigrationUnavailable:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise DownmigrationUnavailable from None


def _plan_from_parts(
    *,
    active_snapshot: schema_v4.ActivePolicySnapshot,
    reviewed_workspace: policy.AuthoringSnapshot,
    custody: authorization_custody.AuthorizationCustody,
    control_digest: str,
    keyring_digest: str,
    schema_fence_generation: int | None,
    created_at: int,
) -> _RecoveryPlan:
    if policy._immutable_companion_documents(
        dict(reviewed_workspace.documents)
    ) != policy._immutable_companion_documents(dict(active_snapshot.source_documents)):
        raise DownmigrationUnavailable
    workspace_digest = schema_v4.source_documents_digest(active_snapshot.source_documents)
    catalog_digest = schema_v4.catalog_rebuild_digest(active_snapshot.catalog_descriptor)
    membership = custody.serving_membership
    if membership is None:
        raise DownmigrationUnavailable
    value = {
        "schema": _PLAN_SCHEMA,
        "active": _active_value(active_snapshot.active),
        "reviewed_workspace": _snapshot_value(reviewed_workspace),
        "source_documents": _documents_value(active_snapshot.source_documents),
        "catalog_descriptor": base64.b64encode(active_snapshot.catalog_descriptor).decode("ascii"),
        "workspace_digest": workspace_digest,
        "catalog_digest": catalog_digest,
        "control_digest": control_digest,
        "keyring_digest": keyring_digest,
        "membership_epoch": membership.epoch,
        "membership_digest": membership.record_digest,
        "schema_fence_generation": schema_fence_generation,
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_PLAN_BYTES:
        raise DownmigrationUnavailable
    plan_digest = _framed_digest(_PLAN_DOMAIN, encoded)
    target_digest = _framed_digest(
        _TARGET_DOMAIN,
        plan_digest.encode("ascii"),
        b"3",
    )
    identity = {
        "operation": _OPERATION,
        "prior": active_snapshot.active.activation_state_digest,
        "prepared": plan_digest,
        "target": target_digest,
        "affected_ids": sorted(
            (
                active_snapshot.active.activation_store_id,
                active_snapshot.active.logical_vault_id,
            )
        ),
    }
    event_id = receipts.critical_event_id(identity)
    return _RecoveryPlan(
        active=active_snapshot.active,
        reviewed_workspace=reviewed_workspace,
        source_documents=active_snapshot.source_documents,
        catalog_descriptor=active_snapshot.catalog_descriptor,
        workspace_digest=workspace_digest,
        catalog_digest=catalog_digest,
        control_digest=control_digest,
        keyring_digest=keyring_digest,
        membership_epoch=membership.epoch,
        membership_digest=membership.record_digest,
        schema_fence_generation=schema_fence_generation,
        created_at=created_at,
        value_json=encoded.decode("utf-8"),
        plan_digest=plan_digest,
        target_digest=target_digest,
        event_id=event_id,
    )


def _plan_from_json(
    value_json: object,
    *,
    event_id: str,
    created_at: object,
) -> _RecoveryPlan:
    if not isinstance(value_json, str) or len(value_json.encode("utf-8")) > _MAX_PLAN_BYTES:
        raise DownmigrationUnavailable
    try:
        value = json.loads(value_json)
    except (UnicodeError, json.JSONDecodeError):
        raise DownmigrationUnavailable from None
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "active",
            "reviewed_workspace",
            "source_documents",
            "catalog_descriptor",
            "workspace_digest",
            "catalog_digest",
            "control_digest",
            "keyring_digest",
            "membership_epoch",
            "membership_digest",
            "schema_fence_generation",
        }
        or value["schema"] != _PLAN_SCHEMA
    ):
        raise DownmigrationUnavailable
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if canonical != value_json:
        raise DownmigrationUnavailable
    active = _active_from_value(value["active"])
    reviewed = _snapshot_from_value(value["reviewed_workspace"])
    source_documents = _documents_from_value(value["source_documents"])
    if policy._immutable_companion_documents(
        dict(reviewed.documents)
    ) != policy._immutable_companion_documents(dict(source_documents)):
        raise DownmigrationUnavailable
    encoded_descriptor = value["catalog_descriptor"]
    if not isinstance(encoded_descriptor, str):
        raise DownmigrationUnavailable
    try:
        descriptor = base64.b64decode(encoded_descriptor, validate=True)
    except (ValueError, TypeError):
        raise DownmigrationUnavailable from None
    if base64.b64encode(descriptor).decode("ascii") != encoded_descriptor:
        raise DownmigrationUnavailable
    workspace_digest = _digest(value["workspace_digest"])
    catalog_digest = _digest(value["catalog_digest"])
    if workspace_digest != schema_v4.source_documents_digest(
        source_documents
    ) or catalog_digest != schema_v4.catalog_rebuild_digest(descriptor):
        raise DownmigrationUnavailable
    raw_fence_generation = value["schema_fence_generation"]
    schema_fence_generation = (
        None if raw_fence_generation is None else _bounded_integer(raw_fence_generation, minimum=1)
    )
    plan_digest = _framed_digest(_PLAN_DOMAIN, value_json.encode("utf-8"))
    target_digest = _framed_digest(
        _TARGET_DOMAIN,
        plan_digest.encode("ascii"),
        b"3",
    )
    identity = {
        "operation": _OPERATION,
        "prior": active.activation_state_digest,
        "prepared": plan_digest,
        "target": target_digest,
        "affected_ids": sorted((active.activation_store_id, active.logical_vault_id)),
    }
    if receipts.critical_event_id(identity) != _digest(event_id):
        raise DownmigrationUnavailable
    return _RecoveryPlan(
        active=active,
        reviewed_workspace=reviewed,
        source_documents=source_documents,
        catalog_descriptor=descriptor,
        workspace_digest=workspace_digest,
        catalog_digest=catalog_digest,
        control_digest=_digest(value["control_digest"]),
        keyring_digest=_digest(value["keyring_digest"]),
        membership_epoch=_bounded_integer(value["membership_epoch"], minimum=1),
        membership_digest=_digest(value["membership_digest"]),
        schema_fence_generation=schema_fence_generation,
        created_at=_sqlite_integer(created_at, minimum=1),
        value_json=value_json,
        plan_digest=plan_digest,
        target_digest=target_digest,
        event_id=event_id,
    )


def _verify_plan_custody(
    plan: _RecoveryPlan,
    custody: authorization_custody.AuthorizationCustody,
    *,
    vault_root: Path,
) -> None:
    control = custody.control
    membership = custody.serving_membership
    control_digest, keyring_digest = _custody_digests(vault_root)
    if (
        membership is None
        or plan.membership_epoch != membership.epoch
        or plan.membership_digest != membership.record_digest
        or plan.control_digest != control_digest
        or plan.keyring_digest != keyring_digest
        or control.logical_vault_id != plan.active.logical_vault_id
        or control.activation_store_id != plan.active.activation_store_id
        or control.activation_epoch != plan.active.activation_epoch
        or control.activation_state_digest != plan.active.activation_state_digest
    ):
        raise DownmigrationUnavailable


def _require_plan_schema_fence(plan: _RecoveryPlan) -> None:
    try:
        client = writer_lease.configured_schema_fence_operator_client()
        if plan.schema_fence_generation is None:
            if client is not None:
                raise DownmigrationUnavailable
            return
        if client is None:
            raise DownmigrationUnavailable
        current = client.schema_fence()
    except writer_lease.OpError:
        raise DownmigrationUnavailable from None
    if (
        current.schema_version != schema_v4.SCHEMA_USER_VERSION
        or current.generation != plan.schema_fence_generation
    ):
        raise DownmigrationUnavailable


def _complete_plan_schema_fence(plan: _RecoveryPlan) -> None:
    try:
        client = writer_lease.configured_schema_fence_operator_client()
        if plan.schema_fence_generation is None:
            if client is not None:
                raise DownmigrationUnavailable
            return
        if client is None:
            raise DownmigrationUnavailable
        current = client.schema_fence()
        if (
            current.schema_version == store.SCHEMA_USER_VERSION
            and current.generation == plan.schema_fence_generation + 1
        ):
            return
        if (
            current.schema_version != schema_v4.SCHEMA_USER_VERSION
            or current.generation != plan.schema_fence_generation
        ):
            raise DownmigrationUnavailable
        advanced = client.transition_schema_fence(
            expected_generation=plan.schema_fence_generation,
            schema_version=store.SCHEMA_USER_VERSION,
        )
    except writer_lease.OpError:
        raise DownmigrationUnavailable from None
    if (
        advanced.schema_version != store.SCHEMA_USER_VERSION
        or advanced.generation != plan.schema_fence_generation + 1
    ):
        raise DownmigrationUnavailable


def _plan_schema_fence_is_complete(plan: _RecoveryPlan) -> bool:
    """Read the final fence classification without advancing or opening legacy state."""

    try:
        client = writer_lease.configured_schema_fence_operator_client()
        if plan.schema_fence_generation is None:
            return client is None
        if client is None:
            return False
        current = client.schema_fence()
    except writer_lease.OpError:
        raise DownmigrationUnavailable from None
    return (
        current.schema_version == store.SCHEMA_USER_VERSION
        and current.generation == plan.schema_fence_generation + 1
    )


def _ensure_receipt_intent(
    vault_root: Path,
    plan: _RecoveryPlan,
    *,
    must_exist: bool,
) -> None:
    records = receipts.event_records(vault_root)
    terminals = {
        item.get("causation_id")
        for item in records
        if item.get("event_type") == "critical" and item.get("phase") in {"committed", "aborted"}
    }
    intents = [
        item
        for item in records
        if item.get("event_type") == "critical"
        and item.get("phase") == "intent"
        and item.get("operation") == _OPERATION
        and item.get("event_id") not in terminals
    ]
    matching = [item for item in intents if item.get("event_id") == plan.event_id]
    if len(intents) != len(matching) or len(matching) > 1 or (must_exist and not matching):
        raise DownmigrationUnavailable
    try:
        record = receipts.begin_event(
            vault_root,
            operation=_OPERATION,
            prior=plan.active.activation_state_digest,
            prepared=plan.plan_digest,
            target=plan.target_digest,
            affected_ids=sorted((plan.active.activation_store_id, plan.active.logical_vault_id)),
            event_id=plan.event_id,
        )
    except receipts.ReceiptError:
        raise DownmigrationUnavailable from None
    if record.get("event_id") != plan.event_id or record.get("phase") != "intent":
        raise DownmigrationUnavailable


def _prepared_plan(connection: sqlite3.Connection) -> _RecoveryPlan | None:
    rows = connection.execute(
        "SELECT event_id, causation_id, authorization_session, principal_id, direction, "
        "prior_digest, prepared_digest, final_digest, affected_ids, "
        "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
        "marker_required, created_at, updated_at, blocked_reason "
        "FROM governance_operation_journals WHERE operation=? AND phase='pending' "
        "ORDER BY event_id",
        (_OPERATION,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise DownmigrationUnavailable
    (
        event_id,
        causation_id,
        authorization_session,
        principal_id,
        direction,
        prior,
        prepared,
        target,
        affected_ids,
        child_intents,
        child_terminals,
        proposal_id,
        attempt_no,
        marker_required,
        created_at,
        updated_at,
        blocked_reason,
    ) = rows[0]
    components = connection.execute(
        "SELECT phase, ordinal, component_kind, component_key, value_json, value_hash, "
        "status FROM governance_operation_components WHERE event_id=? ORDER BY phase, ordinal",
        (event_id,),
    ).fetchall()
    if len(components) != 1:
        raise DownmigrationUnavailable
    phase, ordinal, kind, key, value_json, value_hash, status = components[0]
    if (
        phase != "prepared"
        or ordinal != 0
        or kind != "schema-downmigration-plan"
        or key != event_id
        or status != "complete"
    ):
        raise DownmigrationUnavailable
    plan = _plan_from_json(
        value_json,
        event_id=event_id,
        created_at=created_at,
    )
    expected_affected = json.dumps(
        sorted((plan.active.activation_store_id, plan.active.logical_vault_id)),
        separators=(",", ":"),
    )
    if (
        value_hash != plan.plan_digest
        or causation_id != plan.event_id
        or authorization_session is not None
        or principal_id != "offline-schema-coordinator"
        or direction != "narrowing"
        or prior != plan.active.activation_state_digest
        or prepared != plan.plan_digest
        or target != plan.target_digest
        or affected_ids != expected_affected
        or child_intents != "[]"
        or child_terminals != "[]"
        or proposal_id is not None
        or attempt_no != 1
        or marker_required != 0
        or created_at != float(plan.created_at)
        or updated_at != float(plan.created_at)
        or blocked_reason is not None
    ):
        raise DownmigrationUnavailable
    return plan


def _stage_plan(vault_root: Path, plan: _RecoveryPlan) -> None:
    with _owned_store_connection(
        vault_root,
        schema_version=schema_v4.SCHEMA_USER_VERSION,
        writable=True,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            active = schema_v4.load_active_state(
                connection,
                expected_logical_vault_id=plan.active.logical_vault_id,
                expected_activation_store_id=plan.active.activation_store_id,
                expected_activation_epoch=plan.active.activation_epoch,
                expected_activation_state_digest=plan.active.activation_state_digest,
            )
            if active != plan.active:
                raise DownmigrationUnavailable
            current = _prepared_plan(connection)
            if current is not None:
                if current != plan:
                    raise DownmigrationUnavailable
                connection.rollback()
                return
            other = connection.execute(
                "SELECT 1 FROM governance_operation_journals "
                "WHERE phase IN ('allocating','pending') LIMIT 1"
            ).fetchone()
            collision = connection.execute(
                "SELECT 1 FROM governance_operation_journals WHERE event_id=?",
                (plan.event_id,),
            ).fetchone()
            if other is not None or collision is not None:
                raise DownmigrationUnavailable
            affected = json.dumps(
                sorted((plan.active.activation_store_id, plan.active.logical_vault_id)),
                separators=(",", ":"),
            )
            connection.execute(
                "INSERT INTO governance_operation_journals "
                "(event_id, operation, causation_id, authorization_session, principal_id, "
                "phase, direction, prior_digest, prepared_digest, final_digest, affected_ids, "
                "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
                "marker_required, created_at, updated_at, blocked_reason) VALUES "
                "(?, ?, ?, NULL, 'offline-schema-coordinator', 'pending', 'narrowing', ?, ?, ?, "
                "?, '[]', '[]', NULL, 1, 0, ?, ?, NULL)",
                (
                    plan.event_id,
                    _OPERATION,
                    plan.event_id,
                    plan.active.activation_state_digest,
                    plan.plan_digest,
                    plan.target_digest,
                    affected,
                    plan.created_at,
                    plan.created_at,
                ),
            )
            connection.execute(
                "INSERT INTO governance_operation_components "
                "(event_id, phase, ordinal, component_kind, component_key, value_json, "
                "value_hash, status) VALUES (?, 'prepared', 0, 'schema-downmigration-plan', "
                "?, ?, ?, 'complete')",
                (plan.event_id, plan.event_id, plan.value_json, plan.plan_digest),
            )
            connection.commit()
        except (sqlite3.Error, schema_v4.SchemaV4Error):
            if connection.in_transaction:
                connection.rollback()
            raise DownmigrationUnavailable from None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


def _load_or_prepare_plan(
    vault_root: Path,
    *,
    custody: authorization_custody.AuthorizationCustody,
    now: int,
) -> _RecoveryPlan:
    try:
        fence = writer_lease.require_configured_schema_fence(schema_v4.SCHEMA_USER_VERSION)
    except writer_lease.OpError:
        raise DownmigrationUnavailable from None
    with _owned_store_connection(
        vault_root,
        schema_version=schema_v4.SCHEMA_USER_VERSION,
        writable=False,
    ) as connection:
        existing = _prepared_plan(connection)
        if existing is not None:
            _verify_plan_custody(existing, custody, vault_root=vault_root)
            return existing
        control = custody.control
        connection.execute("BEGIN")
        active_snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=str(control.activation_store_id),
            expected_activation_epoch=int(control.activation_epoch),
            expected_activation_state_digest=str(control.activation_state_digest),
        )
        connection.commit()
    reviewed = policy.observe_authoring_snapshot(vault_root)
    if reviewed is None:
        raise DownmigrationUnavailable
    control_digest, keyring_digest = _custody_digests(vault_root)
    return _plan_from_parts(
        active_snapshot=active_snapshot,
        reviewed_workspace=reviewed,
        custody=custody,
        control_digest=control_digest,
        keyring_digest=keyring_digest,
        schema_fence_generation=None if fence is None else fence.generation,
        created_at=now,
    )


def _mirror_workspace(vault_root: Path, plan: _RecoveryPlan) -> None:
    def barrier(phase: str, relative: str) -> None:
        _downmigration_barrier(f"mirror:{phase}:{relative}")

    with reserved_paths._owner_authority_scope("govern_memory"):
        result = policy.mirror_authoring_workspace(
            vault_root,
            reviewed=plan.reviewed_workspace,
            target_documents=plan.source_documents,
            barrier=barrier,
        )
    if result != "complete":
        raise DownmigrationUnavailable
    observed = policy.observe_authoring_snapshot(vault_root)
    if (
        observed is None
        or observed.documents != plan.source_documents
        or schema_v4.source_documents_digest(observed.documents) != plan.workspace_digest
    ):
        raise DownmigrationUnavailable


def _commit_database(
    vault_root: Path,
    plan: _RecoveryPlan,
    *,
    marker_session: state_migration.GovernanceRollbackSession,
) -> tuple[str, str]:
    """Transform the one held v4 connection and bind its uncommitted D0.

    The manifest write is deliberately inside the retained SQLite transaction:
    a prepared marker therefore either describes the still-v4 source which
    must reproduce D0, or the exact committed v3 D0.  It never describes a
    separately cloned snapshot.
    """
    with _owned_store_connection(
        vault_root,
        schema_version=schema_v4.SCHEMA_USER_VERSION,
        writable=True,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = schema_v4.downmigrate_v4_connection_in_transaction(
                connection,
                expected=plan.active,
                expected_source_documents=plan.source_documents,
                expected_catalog_descriptor=plan.catalog_descriptor,
                verified_workspace_digest=plan.workspace_digest,
                verified_catalog_digest=plan.catalog_digest,
                recovery_event_id=plan.event_id,
                recovery_plan_digest=plan.plan_digest,
                recovery_target_digest=plan.target_digest,
                downmigrated_at=plan.created_at,
            )
            published_digest = store.canonical_uncommitted_v3_digest(connection)
            marker = marker_session.marker
            if marker is None:
                marker_session.begin_prepared(
                    operation=_OPERATION,
                    event_id=plan.event_id,
                    plan_digest=plan.plan_digest,
                    target_digest=plan.target_digest,
                    timestamp=plan.created_at,
                    d0=published_digest,
                    backup_reference=None,
                    backup_plan_digest=None,
                    source_store_digest=None,
                    schema_fence_generation=plan.schema_fence_generation,
                )
            else:
                _verify_prepared_marker(marker, plan, d0=published_digest)
            _downmigration_barrier("after_marker_prepare")
            connection.commit()
        except (sqlite3.Error, schema_v4.SchemaV4Error):
            if connection.in_transaction:
                connection.rollback()
            raise DownmigrationUnavailable from None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
    return result.recovery_terminal_digest, published_digest


def _terminal_plan(
    vault_root: Path,
    custody: authorization_custody.AuthorizationCustody | None,
) -> tuple[_RecoveryPlan, str]:
    with _owned_store_connection(
        vault_root,
        schema_version=store.SCHEMA_USER_VERSION,
        writable=False,
    ) as connection:
        rows = connection.execute(
            "SELECT event_id, causation_id, authorization_session, principal_id, direction, "
            "prior_digest, prepared_digest, final_digest, affected_ids, "
            "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
            "marker_required, created_at, blocked_reason FROM governance_operation_journals "
            "WHERE operation=? AND phase='closed' ORDER BY updated_at DESC",
            (_OPERATION,),
        ).fetchall()
        matches: list[tuple[_RecoveryPlan, str]] = []
        for row in rows:
            (
                event_id,
                causation_id,
                authorization_session,
                principal_id,
                direction,
                prior_digest,
                prepared_digest,
                final_digest,
                affected_ids,
                required_child_intents,
                required_child_terminals,
                proposal_id,
                attempt_no,
                marker_required,
                created_at,
                blocked_reason,
            ) = row
            prepared = connection.execute(
                "SELECT value_json, value_hash, status FROM governance_operation_components "
                "WHERE event_id=? AND phase='prepared' AND ordinal=0 "
                "AND component_kind='schema-downmigration-plan' AND component_key=?",
                (event_id, event_id),
            ).fetchone()
            terminal = connection.execute(
                "SELECT value_json, value_hash, status FROM governance_operation_components "
                "WHERE event_id=? AND phase='final' AND ordinal=0 "
                "AND component_kind='schema-downmigration-terminal' AND component_key=?",
                (event_id, event_id),
            ).fetchone()
            if prepared is None or terminal is None:
                continue
            components = connection.execute(
                "SELECT phase, ordinal, component_kind, component_key "
                "FROM governance_operation_components WHERE event_id=? "
                "ORDER BY phase, ordinal",
                (event_id,),
            ).fetchall()
            if components != [
                ("final", 0, "schema-downmigration-terminal", event_id),
                ("prepared", 0, "schema-downmigration-plan", event_id),
            ]:
                raise DownmigrationUnavailable
            plan = _plan_from_json(
                prepared[0],
                event_id=event_id,
                created_at=created_at,
            )
            if prepared[1:] != (plan.plan_digest, "complete"):
                raise DownmigrationUnavailable
            terminal_json = terminal[0]
            if not isinstance(terminal_json, str):
                raise DownmigrationUnavailable
            terminal_digest = _framed_digest(_TERMINAL_DOMAIN, terminal_json.encode("utf-8"))
            if terminal[1:] != (terminal_digest, "complete") or final_digest != terminal_digest:
                raise DownmigrationUnavailable
            try:
                value = json.loads(terminal_json)
            except json.JSONDecodeError:
                raise DownmigrationUnavailable from None
            if (
                not isinstance(value, dict)
                or set(value) != _TERMINAL_FIELDS
                or json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                != terminal_json
                or value.get("schema") != "exomem.governance-downmigration-terminal/v1"
                or value.get("recovery_event_id") != plan.event_id
                or value.get("recovery_plan_digest") != plan.plan_digest
                or value.get("recovery_target_digest") != plan.target_digest
                or value.get("workspace_digest") != plan.workspace_digest
                or value.get("catalog_digest") != plan.catalog_digest
                or any(
                    value.get(key) != expected
                    for key, expected in _active_value(plan.active).items()
                )
                or _bounded_integer(value.get("downmigrated_at"), minimum=1) < plan.created_at
                or any(
                    _bounded_integer(value.get(key)) < 0
                    for key in (
                        "closed_sessions",
                        "expired_grants",
                        "expired_purposes",
                        "expired_tokens",
                        "expired_proposals",
                    )
                )
            ):
                raise DownmigrationUnavailable
            expected_affected = json.dumps(
                sorted((plan.active.activation_store_id, plan.active.logical_vault_id)),
                separators=(",", ":"),
            )
            if (
                causation_id != plan.event_id
                or authorization_session is not None
                or principal_id != "offline-schema-coordinator"
                or direction != "narrowing"
                or prior_digest != plan.active.activation_state_digest
                or prepared_digest != plan.plan_digest
                or affected_ids != expected_affected
                or required_child_intents != "[]"
                or required_child_terminals != "[]"
                or proposal_id is not None
                or attempt_no != 1
                or marker_required != 0
                or blocked_reason is not None
            ):
                raise DownmigrationUnavailable
            if custody is None:
                matches.append((plan, terminal_digest))
                continue
            control = custody.control
            if (
                control.activation_store_id == plan.active.activation_store_id
                and control.activation_epoch == plan.active.activation_epoch
                and control.activation_state_digest == plan.active.activation_state_digest
                and control.logical_vault_id == plan.active.logical_vault_id
            ):
                matches.append((plan, terminal_digest))
        if len(matches) != 1:
            raise DownmigrationUnavailable
        return matches[0]


def _complete_receipt(vault_root: Path, plan: _RecoveryPlan) -> None:
    try:
        receipts.commit_event(
            vault_root,
            plan.event_id,
            outcome="schema-v3-restored",
        )
    except receipts.ReceiptError:
        raise DownmigrationUnavailable from None


def _publish_legacy_v3_snapshot(
    vault_root: Path,
    *,
    plan: _RecoveryPlan,
    expected_digest: str,
) -> None:
    try:
        legacy_v3_placement.publish_exact_v3_snapshot(
            vault_root,
            expected_digest=expected_digest,
            event_id=plan.event_id,
            barrier=lambda point: _downmigration_barrier(f"legacy:{point}"),
        )
    except legacy_v3_placement.LegacyV3PublicationUnavailable:
        raise DownmigrationUnavailable from None


def _verify_completed_state(
    vault_root: Path,
    plan: _RecoveryPlan,
    *,
    now: int,
) -> None:
    custody = _require_drained_custody(vault_root, now=now)
    _verify_plan_custody(plan, custody, vault_root=vault_root)
    observed = policy.observe_authoring_snapshot(vault_root)
    if (
        observed is None
        or observed.documents != plan.source_documents
        or schema_v4.source_documents_digest(observed.documents) != plan.workspace_digest
    ):
        raise DownmigrationUnavailable


def _marker_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DownmigrationUnavailable
    return value


def _verify_prepared_marker(
    marker: object,
    plan: _RecoveryPlan,
    *,
    d0: str | None = None,
) -> dict[str, object]:
    """Bind a v2 marker to exactly this one plan before any replay effect."""

    if not isinstance(marker, dict) or set(marker) != {
        "operation",
        "event_id",
        "phase",
        "plan_digest",
        "target_digest",
        "timestamp",
        "d0",
        "legacy_path",
        "stage_leaf",
        "backup_reference",
        "backup_plan_digest",
        "source_store_digest",
        "schema_fence_generation",
        "d1",
        "terminal",
    }:
        raise DownmigrationUnavailable
    if (
        marker.get("operation") != _OPERATION
        or marker.get("event_id") != plan.event_id
        or marker.get("plan_digest") != plan.plan_digest
        or marker.get("target_digest") != plan.target_digest
        or marker.get("timestamp") != plan.created_at
        or marker.get("legacy_path") != legacy_v3_placement.legacy_v3_path(Path(".")).as_posix()
        or marker.get("stage_leaf") != legacy_v3_placement.rollback_stage_leaf(plan.event_id)
        or marker.get("backup_reference") is not None
        or marker.get("backup_plan_digest") is not None
        or marker.get("source_store_digest") is not None
        or marker.get("schema_fence_generation") != plan.schema_fence_generation
    ):
        raise DownmigrationUnavailable
    marker_d0 = _marker_digest(marker.get("d0"))
    if d0 is not None and marker_d0 != _marker_digest(d0):
        raise DownmigrationUnavailable
    if marker.get("phase") not in {"prepared", "receipt-committed", "legacy-aligned", "complete"}:
        raise DownmigrationUnavailable
    return marker


def _terminal_endpoint(vault_root: Path, event_id: str) -> dict[str, object]:
    """Return the exact durable receipt endpoint already proved by D1 evidence."""

    try:
        with _owned_store_connection(
            vault_root,
            schema_version=store.SCHEMA_USER_VERSION,
            writable=False,
        ) as connection:
            instance = connection.execute(
                "SELECT instance_id FROM receipt_instance WHERE singleton=1"
            ).fetchone()
            if instance is None or not isinstance(instance[0], str):
                raise DownmigrationUnavailable
            instance_id = instance[0]
            head = connection.execute(
                "SELECT durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset "
                "FROM receipts_head WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if head is None:
                raise DownmigrationUnavailable
        records, issues = receipts._chain_state(  # noqa: SLF001 - exact durable locator proof
            receipts._instance_dir(Path(vault_root), instance_id)  # noqa: SLF001
        )
        if issues:
            raise DownmigrationUnavailable
        terminals = [
            record
            for record in records
            if record.get("causation_id") == event_id and record.get("phase") == "committed"
        ]
        if len(terminals) != 1:
            raise DownmigrationUnavailable
        terminal = terminals[0]
        endpoint = {
            "instance_id": instance_id,
            "seq": terminal.get("seq"),
            "hash": terminal.get("hash"),
            "path": receipts._relative_locator(  # noqa: SLF001 - shared locator authority
                Path(vault_root), Path(str(terminal.get("_path", "")))
            ),
            "byte_offset": terminal.get("_offset"),
        }
        if (
            not isinstance(endpoint["seq"], int)
            or endpoint["seq"] < 1
            or not isinstance(endpoint["hash"], str)
            or len(endpoint["hash"]) != 64
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
            raise DownmigrationUnavailable
        return endpoint
    except (OSError, sqlite3.Error, receipts.ReceiptError):
        raise DownmigrationUnavailable from None


def _verify_marker_endpoint(
    marker: dict[str, object], d1: str, endpoint: dict[str, object]
) -> None:
    if marker.get("d1") != _marker_digest(d1) or marker.get("terminal") != endpoint:
        raise DownmigrationUnavailable


def _verify_external_marker_d1(root: Path, marker: dict[str, object]) -> None:
    """Fence-era replay trusts neither a changed external store nor legacy bytes."""

    d1 = _marker_digest(marker.get("d1"))
    try:
        observed = legacy_v3_placement.exact_external_v3_digest(root)
    except legacy_v3_placement.LegacyV3PublicationUnavailable:
        raise DownmigrationUnavailable from None
    if observed != d1:
        raise DownmigrationUnavailable


def _marker_plan(
    root: Path, marker: object, *, custody: authorization_custody.AuthorizationCustody | None
) -> tuple[_RecoveryPlan, str, dict[str, object]]:
    plan, terminal_digest = _terminal_plan(root, custody)
    verified = _verify_prepared_marker(marker, plan)
    return plan, terminal_digest, verified


def _complete_pre_fence_v3(
    root: Path,
    *,
    session: state_migration.GovernanceRollbackSession,
    plan: _RecoveryPlan,
    marker: dict[str, object],
    now: int,
) -> None:
    """Advance v3 only through immutable D0, D1 and the final schema fence."""

    d0 = _marker_digest(marker.get("d0"))
    phase = marker["phase"]
    if phase == "prepared":
        try:
            external_digest = legacy_v3_placement.exact_external_v3_digest(root)
        except legacy_v3_placement.LegacyV3PublicationUnavailable:
            raise DownmigrationUnavailable from None
        if external_digest == d0:
            _verify_completed_state(root, plan, now=now)
            _publish_legacy_v3_snapshot(root, plan=plan, expected_digest=d0)
            _downmigration_barrier("after_legacy_v3_publication")
            _ensure_receipt_intent(root, plan, must_exist=True)
            _complete_receipt(root, plan)
            _downmigration_barrier("after_receipt_commit")
        d1 = _prove_d1(root, plan=plan, d0=d0)
        endpoint = _terminal_endpoint(root, plan.event_id)
        session.advance_receipt_committed(d1, endpoint)
        marker = _verify_prepared_marker(session.marker, plan)
        phase = marker["phase"]
    if phase == "receipt-committed":
        d1 = _prove_d1(root, plan=plan, d0=d0)
        endpoint = _terminal_endpoint(root, plan.event_id)
        _verify_marker_endpoint(marker, d1, endpoint)
        _align_d1(root, plan=plan, d0=d0, expected_d1=d1)
        session.advance_legacy_aligned()
        marker = _verify_prepared_marker(session.marker, plan)
        phase = marker["phase"]
    if phase != "legacy-aligned":
        raise DownmigrationUnavailable
    d1 = _marker_digest(marker.get("d1"))
    endpoint = _terminal_endpoint(root, plan.event_id)
    _verify_marker_endpoint(marker, d1, endpoint)
    _align_d1(root, plan=plan, d0=d0, expected_d1=d1)
    _complete_plan_schema_fence(plan)
    _downmigration_barrier("after_schema_fence")
    session.seal_complete_metadata_only()


def _prove_d1(root: Path, *, plan: _RecoveryPlan, d0: str) -> str:
    try:
        return legacy_v3_placement.prove_d1_against_legacy(
            root, event_id=plan.event_id, d0_digest=d0
        )
    except legacy_v3_placement.LegacyV3PublicationUnavailable:
        raise DownmigrationUnavailable from None


def _align_d1(root: Path, *, plan: _RecoveryPlan, d0: str, expected_d1: str) -> None:
    try:
        aligned = legacy_v3_placement.align_legacy_to_d1(root, event_id=plan.event_id, d0_digest=d0)
    except legacy_v3_placement.LegacyV3PublicationUnavailable:
        raise DownmigrationUnavailable from None
    if aligned != expected_d1:
        raise DownmigrationUnavailable


def _downmigrate_enrolled_v4_store_locked(
    vault_root: Path,
    *,
    now: int,
    marker_session: state_migration.GovernanceRollbackSession,
) -> OfflineDownmigrationResult:
    """Run or replay one explicit, drained, receipt-first v4-to-v3 rollback."""

    root = Path(vault_root)
    moment = _bounded_integer(now, minimum=1)
    marker = marker_session.marker
    version = store.authorization_session_schema_version(root)
    if marker is not None and marker.get("phase") in {"legacy-aligned", "complete"}:
        if version != store.SCHEMA_USER_VERSION:
            raise DownmigrationUnavailable
        plan, terminal_digest, verified_marker = _marker_plan(root, marker, custody=None)
        _verify_external_marker_d1(root, verified_marker)
        if verified_marker["phase"] == "legacy-aligned":
            if not _plan_schema_fence_is_complete(plan):
                # The old writer fence is still authoritative, so legacy proof
                # remains legal only in the pre-fence branch below.
                marker = verified_marker
            else:
                marker_session.seal_complete_metadata_only()
                return OfflineDownmigrationResult(
                    schema_version=3,
                    active=plan.active,
                    recovery_event_id=plan.event_id,
                    recovery_plan_digest=plan.plan_digest,
                    recovery_target_digest=plan.target_digest,
                    recovery_terminal_digest=terminal_digest,
                    replayed=True,
                )
        if verified_marker["phase"] == "complete":
            if not _plan_schema_fence_is_complete(plan):
                raise DownmigrationUnavailable
            return OfflineDownmigrationResult(
                schema_version=3,
                active=plan.active,
                recovery_event_id=plan.event_id,
                recovery_plan_digest=plan.plan_digest,
                recovery_target_digest=plan.target_digest,
                recovery_terminal_digest=terminal_digest,
                replayed=True,
            )
    if marker is not None and marker.get("phase") == "complete":
        # The earlier branch returns; keep this guard explicit if marker
        # validation above ever changes.
        raise DownmigrationUnavailable
    if version == store.SCHEMA_USER_VERSION:
        if marker is None:
            raise DownmigrationUnavailable
        custody = _require_drained_custody(root, now=moment)
        plan, terminal_digest, marker = _marker_plan(root, marker, custody=custody)
        _verify_plan_custody(plan, custody, vault_root=root)
        _complete_pre_fence_v3(
            root,
            session=marker_session,
            plan=plan,
            marker=marker,
            now=moment,
        )
        return OfflineDownmigrationResult(
            schema_version=3,
            active=plan.active,
            recovery_event_id=plan.event_id,
            recovery_plan_digest=plan.plan_digest,
            recovery_target_digest=plan.target_digest,
            recovery_terminal_digest=terminal_digest,
            replayed=True,
        )
    if version != schema_v4.SCHEMA_USER_VERSION:
        raise DownmigrationUnavailable
    custody = _require_drained_custody(root, now=moment)
    plan = _load_or_prepare_plan(root, custody=custody, now=moment)
    _verify_plan_custody(plan, custody, vault_root=root)
    if marker is not None:
        marker = _verify_prepared_marker(marker, plan)
        if marker.get("phase") != "prepared":
            raise DownmigrationUnavailable
    _ensure_receipt_intent(root, plan, must_exist=False)
    _downmigration_barrier("after_receipt_intent")
    _stage_plan(root, plan)
    _downmigration_barrier("after_plan_prepare")
    _mirror_workspace(root, plan)
    _downmigration_barrier("after_workspace_mirror")
    current_custody = _require_drained_custody(root, now=moment)
    _verify_plan_custody(plan, current_custody, vault_root=root)
    _require_plan_schema_fence(plan)
    terminal_digest, published_digest = _commit_database(root, plan, marker_session=marker_session)
    _downmigration_barrier("after_store_commit")
    try:
        if legacy_v3_placement.exact_external_v3_digest(root) != published_digest:
            raise DownmigrationUnavailable
    except legacy_v3_placement.LegacyV3PublicationUnavailable:
        raise DownmigrationUnavailable from None
    prepared = _verify_prepared_marker(marker_session.marker, plan, d0=published_digest)
    _complete_pre_fence_v3(
        root,
        session=marker_session,
        plan=plan,
        marker=prepared,
        now=moment,
    )
    return OfflineDownmigrationResult(
        schema_version=3,
        active=plan.active,
        recovery_event_id=plan.event_id,
        recovery_plan_digest=plan.plan_digest,
        recovery_target_digest=plan.target_digest,
        recovery_terminal_digest=terminal_digest,
        replayed=False,
    )


def downmigrate_enrolled_v4_store(
    vault_root: Path,
    *,
    now: int,
) -> OfflineDownmigrationResult:
    """Run rollback under the state marker then one reentrant receipt sequence."""
    root = Path(vault_root)
    try:
        with state_migration.governance_rollback_session(root) as marker_session:
            with receipts.exclusive_sequence(root):
                return _downmigrate_enrolled_v4_store_locked(
                    root,
                    now=now,
                    marker_session=marker_session,
                )
    except (
        state_migration.StateMigrationOfflineRequired,
        state_migration.StateMigrationManifestError,
        state_migration.StatePlacementConflict,
    ):
        raise DownmigrationUnavailable from None
    except (sqlite3.Error, schema_v4.SchemaV4Error, receipts.ReceiptError):
        raise DownmigrationUnavailable from None
