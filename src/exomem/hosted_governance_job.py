"""Bounded target-image runner for provisioner-owned governance migration.

The provisioner proves stopped pods and binds the Job UID, image, fence and
PVC. This process verifies its fixed local binding and custody, emits a
request-bound terminal, and never publishes custody or opens admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import hosted_restore, hosted_runtime, mutation_lock
from .governance import authorization_custody, schema_migration, store

VAULT_ROOT = Path("/var/lib/exomem/vault")
STATE_ROOT = Path("/var/lib/exomem/state")
LOG_ROOT = Path("/var/lib/exomem/logs")
RUNTIME_UID = 10001
RUNTIME_GID = 10001
TERMINATION_LOG = Path("/dev/termination-log")
REQUEST_ENV = "EXOMEM_GOVERNANCE_MIGRATION_REQUEST"
MAX_REQUEST_BYTES = 8192
MAX_TERMINAL_BYTES = 4096
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]{0,511}@sha256:[0-9a-f]{64}\Z")
_REQUEST_FIELDS = frozenset(
    {
        "schemaVersion",
        "phase",
        "cellId",
        "vaultId",
        "replicaId",
        "operationId",
        "fenceGeneration",
        "pvcUid",
        "runtimeImage",
        "custodyRevision",
        "sourceStoreDigest",
        "planDigest",
    }
)


class HostedGovernanceJobError(RuntimeError):
    """Content-free refusal at the private migration Job boundary."""

    def __init__(self) -> None:
        super().__init__("HOSTED_GOVERNANCE_JOB_FAILED")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostedGovernanceJobError
        result[key] = value
    return result


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _request(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_REQUEST_BYTES:
        raise HostedGovernanceJobError
    value = json.loads(raw, object_pairs_hook=_closed_pairs)
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise HostedGovernanceJobError
    if (
        type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
        or value["phase"] not in ("inspect", "prepare", "commit")
        or type(value["fenceGeneration"]) is not int
        or not 1 <= value["fenceGeneration"] < 1 << 63
        or any(
            not isinstance(value[name], str) or _IDENTITY.fullmatch(value[name]) is None
            for name in ("cellId", "vaultId", "replicaId", "operationId", "pvcUid")
        )
        or not isinstance(value["runtimeImage"], str)
        or _IMAGE.fullmatch(value["runtimeImage"]) is None
        or not _digest(value["custodyRevision"])
        or (
            value["sourceStoreDigest"] is not None
            if value["phase"] == "inspect"
            else not _digest(value["sourceStoreDigest"])
        )
        or (
            not _digest(value["planDigest"])
            if value["phase"] == "commit"
            else value["planDigest"] is not None
        )
    ):
        raise HostedGovernanceJobError
    return value


def _binding(request: dict[str, Any]) -> hosted_runtime.HostedBindingV2:
    if not hosted_runtime.hosted_mode_enabled():
        raise HostedGovernanceJobError
    expected_env = {
        "EXOMEM_VAULT_PATH": VAULT_ROOT,
        "EXOMEM_HOSTED_STATE_ROOT": STATE_ROOT,
        "EXOMEM_STATE_ROOT": STATE_ROOT / "vault-state",
        "EXOMEM_WRITER_LEASE_STATE_DIR": STATE_ROOT,
        authorization_custody.KEYRING_FILE_ENV: authorization_custody.HOSTED_KEYRING_FILE,
        authorization_custody.CONTROL_FILE_ENV: authorization_custody.HOSTED_CONTROL_FILE,
        authorization_custody.MEMBERSHIP_FILE_ENV: authorization_custody.HOSTED_MEMBERSHIP_FILE,
    }
    if any(os.environ.get(name) != str(path) for name, path in expected_env.items()):
        raise HostedGovernanceJobError
    if os.environ.get(authorization_custody.REPLICA_ID_ENV) != request["replicaId"]:
        raise HostedGovernanceJobError
    binding = hosted_runtime.HostedBindingV2(
        cell_id=request["cellId"],
        vault_id=request["vaultId"],
        vault_root=VAULT_ROOT,
        state_root=STATE_ROOT,
        log_root=LOG_ROOT,
        runtime_uid=RUNTIME_UID,
        runtime_gid=RUNTIME_GID,
    )
    hosted_runtime.validate_hosted_binding_v2(binding)
    return binding


def _custody_revision() -> str:
    # Reuse the owner-only, bounded, no-follow loader; raw key bytes never leave
    # the process. Check the immutable expected mount before and after parsing.
    material = b"".join(
        authorization_custody._load_file(path).data
        for path in (
            authorization_custody.HOSTED_KEYRING_FILE,
            authorization_custody.HOSTED_CONTROL_FILE,
            authorization_custody.HOSTED_MEMBERSHIP_FILE,
        )
    )
    return hashlib.sha256(material).hexdigest()


def _custody(request: dict[str, Any], *, now: int) -> authorization_custody.AuthorizationCustody:
    if _custody_revision() != request["custodyRevision"]:
        raise HostedGovernanceJobError
    try:
        custody = authorization_custody.load_authorization_custody(VAULT_ROOT, now=now)
    except authorization_custody.AuthorizationCustodyUnavailable:
        if request["phase"] != "commit":
            raise
        custody = authorization_custody.load_hosted_migration_custody(VAULT_ROOT, now=now)
    record = custody.serving_membership
    if (
        custody.control.cell_id != request["cellId"]
        or custody.control.logical_vault_id != request["vaultId"]
        or custody.local_replica_id != request["replicaId"]
        or re.fullmatch(
            r"hosted-attachment-v1-[0-9a-f]{64}", custody.control.registry_attachment_id
        )
        is None
        or record is None
        or len(record.replicas) != 1
        or record.replicas[0].replica_id != request["replicaId"]
        or record.replicas[0].schema_version not in (3, 4)
        or record.replicas[0].state != "DRAINING"
        or not record.replicas[0].issuance_stopped
        or not record.replicas[0].no_in_flight
    ):
        raise HostedGovernanceJobError
    schema_migration._hosted_custody_digests(custody)
    if _custody_revision() != request["custodyRevision"]:
        raise HostedGovernanceJobError
    return custody


def _run(request: dict[str, Any], *, now: int) -> dict[str, Any]:
    custody = _custody(request, now=now)
    version = store.authorization_session_schema_version(VAULT_ROOT)
    phase = request["phase"]
    if version not in (3, 4) or (
        phase != "commit" and (version != 3 or custody.control.governance_enrolled)
    ):
        raise HostedGovernanceJobError
    record = custody.serving_membership
    assert record is not None  # authenticated by _custody
    result: dict[str, Any] = {
        "artifact": "exomem-hosted-governance-migration",
        "schemaVersion": 1,
        "phase": phase,
        "requestSha256": hashlib.sha256(_canonical(request)).hexdigest(),
        "custodyRevision": request["custodyRevision"],
        "actualSchema": version,
        "membershipSchema": record.replicas[0].schema_version,
        "governanceEnrolled": custody.control.governance_enrolled,
    }
    backup_root = STATE_ROOT / "governance-migration-backups"
    if phase == "inspect":
        result["sourceStoreDigest"] = schema_migration._source_store_digest(VAULT_ROOT)
    else:
        if phase == "prepare":
            if record.replicas[0].schema_version != 3:
                raise HostedGovernanceJobError
            plan = schema_migration.prepare_forward_migration(VAULT_ROOT, now=now)
            if plan.source_store_digest != request["sourceStoreDigest"]:
                raise HostedGovernanceJobError
            schema_migration.stage_forward_migration(
                VAULT_ROOT, expected_plan_digest=plan.plan_digest, now=now
            )
            _custody(request, now=now)
            parent = mutation_lock.retain_secure_directory(STATE_ROOT)
            try:
                try:
                    child = mutation_lock.retain_child_directory(parent, backup_root.name)
                except FileNotFoundError:
                    child = mutation_lock.create_retained_child_directory(parent, backup_root.name)
                child.close()
            finally:
                parent.close()
            backup = schema_migration.prepare_forward_migration_backup(
                VAULT_ROOT,
                expected_plan_digest=plan.plan_digest,
                now=now,
                backup_root=backup_root,
            )
        else:
            if not custody.control.governance_enrolled:
                raise HostedGovernanceJobError
            backup = schema_migration.verify_forward_migration_backup(
                VAULT_ROOT,
                expected_plan_digest=request["planDigest"],
                backup_root=backup_root,
            )
        if backup.source_store_digest != request["sourceStoreDigest"]:
            raise HostedGovernanceJobError
        target = backup.target
        result.update(
            planDigest=backup.plan_digest,
            sourceStoreDigest=backup.source_store_digest,
            backupReference=backup.backup_reference,
            activationStoreId=target.activation_store_id,
            activationEpoch=target.activation_epoch,
            activationStateDigest=target.activation_state_digest,
        )
        if phase == "prepare":
            result["backupDigest"] = backup.backup_digest
        else:
            commit = (
                schema_migration.commit_hosted_forward_migration
                if now >= custody.control.expires_at
                else schema_migration.commit_enrolled_forward_migration
            )
            committed = commit(
                VAULT_ROOT,
                expected_plan_digest=backup.plan_digest,
                now=now,
                backup_root=backup_root,
            )
            result.update(actualSchema=committed.schema_version, replayed=committed.replayed)
    if _custody(request, now=now) != custody:
        raise HostedGovernanceJobError
    return result


def execute(raw: bytes, *, now: int | None = None) -> dict[str, Any]:
    """Execute one closed request against the image's fixed local roots."""
    try:
        request = _request(raw)
        current = int(time.time()) if now is None else now
        if type(current) is not int or not 1 <= current < 1 << 63:
            raise HostedGovernanceJobError
        binding = _binding(request)
        if request["phase"] == "inspect":
            result = _run(request, now=current)
        else:
            with hosted_restore.acquire_hosted_lifetime_lock(STATE_ROOT, binding=binding):
                result = _run(request, now=current)
        if len(_canonical(result)) > MAX_TERMINAL_BYTES:
            raise HostedGovernanceJobError
        return result
    except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
        raise HostedGovernanceJobError from None


def main() -> int:
    """Emit a bounded Kubernetes terminal without logging inputs or exceptions."""
    try:
        result = execute(os.environ.get(REQUEST_ENV, "").encode("utf-8"))
        status = 0
    except Exception:  # noqa: BLE001 - content-free process boundary
        # This is the process boundary: even unexpected library failures must
        # not put credential-bearing exception text into Kubernetes pod logs.
        result = {"code": "HOSTED_GOVERNANCE_JOB_FAILED"}
        status = 1
    try:
        TERMINATION_LOG.write_bytes(_canonical(result))
    except OSError:
        return 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
