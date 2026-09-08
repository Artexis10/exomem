"""Fixed, custody-read-only Job boundary for the stopped-cell migration runner.

The caller owns authenticated operation/fence checks and durable checkpoints.
This adapter never enrolls custody, changes replicas, or opens admission.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kubernetes.client import ApiClient

from .conflict_reason import ConflictReason
from .lifecycle import MetadataConflict, OpaqueProviderMetadata

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]{0,511}@sha256:[0-9a-f]{64}\Z")
_BACKUP_PREFIX = "exomem-governance-v3-backup://sha256/"
_ROOT = "/var/lib/exomem"
_CUSTODY = "/run/exomem/authorization-session"
_PREFIX = "exomem.io/governance-migration-"


def _refuse() -> MetadataConflict:
    return MetadataConflict(
        "governance migration Job is unavailable",
        reason=ConflictReason.GOVERNANCE_MIGRATION_JOB_UNAVAILABLE,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, repr=False)
class MigrationJobRequest:
    metadata: OpaqueProviderMetadata
    vault_id: str
    pvc_uid: str
    runtime_image: str
    custody_revision: str
    phase: str
    source_store_digest: str | None = None
    plan_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.metadata, OpaqueProviderMetadata)
            or self.vault_id != self.metadata.tenant_id
            or self.phase not in ("inspect", "prepare", "commit")
            or type(self.metadata.fence_generation) is not int
            or any(
                not _matches(_IDENTITY, value)
                for value in (
                    self.metadata.subject_id,
                    self.vault_id,
                    self.metadata.operation_id,
                    self.pvc_uid,
                )
            )
            or not _matches(_IMAGE, self.runtime_image)
            or not _matches(_SHA256, self.custody_revision)
            or (
                self.source_store_digest is not None
                if self.phase == "inspect"
                else not _matches(_SHA256, self.source_store_digest)
            )
            or (
                not _matches(_SHA256, self.plan_digest)
                if self.phase == "commit"
                else self.plan_digest is not None
            )
            or len(_canonical(self.as_dict())) > 8192
        ):
            raise _refuse()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "phase": self.phase,
            "cellId": self.metadata.subject_id,
            "vaultId": self.vault_id,
            "replicaId": self.metadata.resource_name + "-0",
            "operationId": self.metadata.operation_id,
            "fenceGeneration": self.metadata.fence_generation,
            "pvcUid": self.pvc_uid,
            "runtimeImage": self.runtime_image,
            "custodyRevision": self.custody_revision,
            "sourceStoreDigest": self.source_store_digest,
            "planDigest": self.plan_digest,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical(self.as_dict())).hexdigest()


def _closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _refuse()
        result[key] = value
    return result


def parse_migration_terminal(request: MigrationJobRequest, raw: bytes) -> dict[str, Any]:
    """Accept only the phase's exact request-bound, content-free runner result."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 4096:
        raise _refuse()
    try:
        value = json.loads(raw, object_pairs_hook=_closed_pairs)
    except (ValueError, UnicodeError):
        raise _refuse() from None
    expected = {
        "artifact",
        "schemaVersion",
        "phase",
        "requestSha256",
        "custodyRevision",
        "actualSchema",
        "membershipSchema",
        "governanceEnrolled",
        "sourceStoreDigest",
    }
    if request.phase != "inspect":
        expected |= {
            "planDigest",
            "backupReference",
            "activationStoreId",
            "activationEpoch",
            "activationStateDigest",
        }
        expected.add("replayed" if request.phase == "commit" else "backupDigest")
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["artifact"] != "exomem-hosted-governance-migration"
        or type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
        or value["phase"] != request.phase
        or value["requestSha256"] != request.sha256
        or value["custodyRevision"] != request.custody_revision
        or type(value["actualSchema"]) is not int
        or value["actualSchema"] != (4 if request.phase == "commit" else 3)
        or type(value["membershipSchema"]) is not int
        or value["membershipSchema"] not in ((3,) if request.phase == "prepare" else (3, 4))
        or value["governanceEnrolled"] is not (request.phase == "commit")
        or not _matches(_SHA256, value["sourceStoreDigest"])
        or (
            request.source_store_digest is not None
            and value["sourceStoreDigest"] != request.source_store_digest
        )
    ):
        raise _refuse()
    if request.phase != "inspect":
        backup = value["backupReference"]
        if (
            not _matches(_SHA256, value["planDigest"])
            or (request.plan_digest is not None and value["planDigest"] != request.plan_digest)
            or not _matches(_IDENTITY, value["activationStoreId"])
            or type(value["activationEpoch"]) is not int
            or not 1 <= value["activationEpoch"] < 1 << 63
            or not _matches(_SHA256, value["activationStateDigest"])
            or not isinstance(backup, str)
            or not backup.startswith(_BACKUP_PREFIX)
            or not _matches(_SHA256, backup[len(_BACKUP_PREFIX) :])
            or (
                type(value["replayed"]) is not bool
                if request.phase == "commit"
                else value["backupDigest"] != backup[len(_BACKUP_PREFIX) :]
            )
        ):
            raise _refuse()
    return value


def build_governance_migration_job(
    request: MigrationJobRequest, *, recovery_envelope: str
) -> dict[str, Any]:
    """Build one singleton target-image Job in the signed fixed init slot."""
    if not isinstance(recovery_envelope, str) or not 1 <= len(recovery_envelope.encode()) <= 32768:
        raise _refuse()
    resource = request.metadata.resource_name
    annotations = {
        **request.metadata.kubernetes_annotations,
        "exomem.io/recovery-envelope": recovery_envelope,
        _PREFIX + "request": request.sha256,
        _PREFIX + "phase": request.phase,
        _PREFIX + "pvc": request.pvc_uid,
        _PREFIX + "image": request.runtime_image,
    }
    labels = {
        "app.kubernetes.io/name": "exomem-governance-migration",
        "exomem.io/cell": resource,
        "exomem.io/governance-migration": "true",
    }
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "capabilities": {"drop": ["ALL"]},
    }
    common = {
        "image": request.runtime_image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python"],
        "securityContext": security,
        "terminationMessagePath": "/dev/termination-log",
        "terminationMessagePolicy": "File",
    }
    environment = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_VAULT_PATH": _ROOT + "/vault",
        "EXOMEM_HOSTED_STATE_ROOT": _ROOT + "/state",
        "EXOMEM_STATE_ROOT": _ROOT + "/state/vault-state",
        "EXOMEM_WRITER_LEASE_STATE_DIR": _ROOT + "/state",
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": _CUSTODY + "/private/keyring.json",
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": _CUSTODY + "/private/control.json",
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": _CUSTODY + "/private/serving-membership.json",
        "EXOMEM_AUTH_SESSION_REPLICA_ID": resource + "-0",
        "EXOMEM_GOVERNANCE_MIGRATION_REQUEST": _canonical(request.as_dict()).decode(),
    }
    pod = {
        "serviceAccountName": resource,
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "initContainers": [
            {
                **common,
                "name": "authorization-session-custody",
                "args": ["-m", "exomem.governance.authorization_hosted_mount"],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "16Mi", "ephemeral-storage": "16Mi"},
                    "limits": {"cpu": "100m", "memory": "64Mi", "ephemeral-storage": "16Mi"},
                },
                "volumeMounts": [
                    {
                        "name": "authorization-session-source",
                        "mountPath": _CUSTODY + "-source",
                        "readOnly": True,
                    },
                    {"name": "authorization-session-custody", "mountPath": _CUSTODY},
                ],
            }
        ],
        "containers": [
            {
                **common,
                "name": "exomem",
                "args": ["-m", "exomem.hosted_governance_job"],
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "64Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "64Mi"},
                },
                "env": [{"name": name, "value": value} for name, value in environment.items()],
                "volumeMounts": [
                    *[
                        {
                            "name": "data",
                            "mountPath": _ROOT + "/" + path,
                            "subPath": path,
                            "readOnly": request.phase == "inspect" or path == "logs",
                        }
                        for path in ("vault", "state", "logs")
                    ],
                    {
                        "name": "authorization-session-custody",
                        "mountPath": _CUSTODY,
                        "readOnly": True,
                    },
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {
                "name": "data",
                "persistentVolumeClaim": {
                    "claimName": resource + "-data",
                    "readOnly": request.phase == "inspect",
                },
            },
            {
                "name": "authorization-session-source",
                "secret": {"secretName": "exomem-authorization-session", "defaultMode": 0o444},
            },
            {
                "name": "authorization-session-custody",
                "emptyDir": {"medium": "Memory", "sizeLimit": "256Ki"},
            },
            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
        ],
    }
    return copy.deepcopy(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": resource + "-init",
                "namespace": resource,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "backoffLimit": 0,
                "podReplacementPolicy": "Failed",
                "activeDeadlineSeconds": 600,
                "parallelism": 1,
                "completions": 1,
                "template": {
                    "metadata": {"labels": labels, "annotations": annotations},
                    "spec": pod,
                },
            },
        }
    )


@dataclass(frozen=True, slots=True, repr=False)
class MigrationJobEvidence:
    job_uid: str
    pod_uid: str
    _terminal: bytes = field(repr=False)

    @property
    def terminal(self) -> dict[str, Any]:
        return json.loads(self._terminal)


class KubernetesGovernanceMigrationAdapter:
    def __init__(
        self,
        *,
        core_v1: Any,
        apps_v1: Any,
        batch_v1: Any,
        sleep: Callable[[float], Any] = asyncio.sleep,
        poll_attempts: int = 150,
    ) -> None:
        self._core, self._apps, self._batch = core_v1, apps_v1, batch_v1
        self._sleep, self._poll_attempts = sleep, poll_attempts
        self._serializer = ApiClient()

    def _wire(self, value: Any) -> Any:
        return self._serializer.sanitize_for_serialization(value)

    async def _pause(self) -> None:
        result = self._sleep(1.0)
        if inspect.isawaitable(result):
            await result

    async def _read(self, request: MigrationJobRequest) -> dict[str, Any] | None:
        try:
            return self._wire(
                await asyncio.to_thread(
                    self._batch.read_namespaced_job,
                    request.metadata.resource_name + "-init",
                    request.metadata.resource_name,
                )
            )
        except Exception as error:
            if getattr(error, "status", None) == 404:
                return None
            raise

    async def _stopped(self, request: MigrationJobRequest) -> None:
        resource = request.metadata.resource_name
        pvc = self._wire(
            await asyncio.to_thread(
                self._core.read_namespaced_persistent_volume_claim, resource + "-data", resource
            )
        )
        identity = pvc["metadata"]
        annotations = identity.get("annotations", {})
        if (
            identity.get("uid") != request.pvc_uid
            or identity.get("deletionTimestamp")
            or pvc.get("status", {}).get("phase") != "Bound"
            or not pvc["spec"].get("volumeName")
            or any(
                annotations.get(key) != value
                for key, value in request.metadata.kubernetes_annotations.items()
                if key
                in (
                    "exomem.io/tenant-id",
                    "exomem.io/cell-id",
                    "exomem.io/tenant-digest",
                    "exomem.io/subject-digest",
                )
            )
        ):
            raise _refuse()
        try:
            runtime = self._wire(
                await asyncio.to_thread(self._apps.read_namespaced_stateful_set, resource, resource)
            )
        except Exception as error:
            if getattr(error, "status", None) != 404:
                raise
            runtime = {"spec": {"replicas": 0}}
        if type(runtime["spec"].get("replicas")) is not int or runtime["spec"]["replicas"] != 0:
            raise _refuse()
        pods = await asyncio.to_thread(
            self._core.list_namespaced_pod,
            resource,
            label_selector=f"app.kubernetes.io/name=exomem-cell,exomem.io/cell={resource},!exomem.io/storage-init,!exomem.io/vault-fingerprint",
        )
        if pods.items:
            raise _refuse()

    @staticmethod
    def _metadata(actual: dict[str, Any], expected: dict[str, Any]) -> None:
        if any(
            actual.get(field, {}).get(key) != value
            for field in ("labels", "annotations")
            for key, value in expected[field].items()
        ):
            raise _refuse()
        if any(
            key in actual.get("labels", {})
            for key in (
                "exomem.io/storage-init",
                "exomem.io/vault-fingerprint",
                "exomem.io/restore-candidate",
            )
        ):
            raise _refuse()

    @staticmethod
    def _pod_spec(
        actual: dict[str, Any], expected: dict[str, Any], *, scheduled: bool = False
    ) -> None:
        # Only API-server/controller defaults may supplement the closed spec.
        defaults: dict[str, Any] = {
            "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler",
            "terminationGracePeriodSeconds": 30,
            "enableServiceLinks": True,
            "serviceAccount": expected["serviceAccountName"],
            "preemptionPolicy": "PreemptLowerPriority",
            "priority": 0,
        }
        value = copy.deepcopy(actual)
        expected_value = copy.deepcopy(expected)
        # Go's omitempty drops readOnly:false from writable mounts/PVC
        # sources. Normalize only that observed API default, never true.
        for spec in (value, expected_value):
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                for mount in container.get("volumeMounts", []):
                    if mount.get("readOnly") is False:
                        mount.pop("readOnly")
            for volume in spec.get("volumes", []):
                claim = volume.get("persistentVolumeClaim", {})
                if claim.get("readOnly") is False:
                    claim.pop("readOnly")
        for key, default in defaults.items():
            if key in value:
                if value.pop(key) != default:
                    raise _refuse()
        if scheduled:
            if "nodeName" in value and not _matches(_IDENTITY, value.pop("nodeName")):
                raise _refuse()
            tolerations = value.pop("tolerations", [])
            allowed = [
                {
                    "key": "node.kubernetes.io/" + key,
                    "operator": "Exists",
                    "effect": "NoExecute",
                    "tolerationSeconds": 300,
                }
                for key in ("not-ready", "unreachable")
            ]
            if any(item not in allowed for item in tolerations) or len(tolerations) > 2:
                raise _refuse()
        if value != expected_value:
            raise _refuse()

    def _job(self, job: dict[str, Any], body: dict[str, Any], uid: str | None) -> str:
        meta = job["metadata"]
        self._metadata(meta, body["metadata"])
        observed = meta.get("uid")
        if (
            not _matches(_IDENTITY, observed)
            or (uid is not None and uid != observed)
            or not _matches(_IDENTITY, meta.get("resourceVersion"))
            or meta.get("deletionTimestamp")
            or meta.get("name") != body["metadata"]["name"]
            or meta.get("namespace") != body["metadata"]["namespace"]
        ):
            raise _refuse()
        spec = job["spec"]
        for key, value in body["spec"].items():
            if key != "template" and (type(spec.get(key)) is not type(value) or spec[key] != value):
                raise _refuse()
        defaults = {
            "completionMode": "NonIndexed",
            "suspend": False,
            "manualSelector": False,
        }
        if set(spec) - set(body["spec"]) - set(defaults) - {"selector"}:
            raise _refuse()
        if any(key in spec and spec[key] != default for key, default in defaults.items()):
            raise _refuse()
        self._metadata(spec["template"]["metadata"], body["spec"]["template"]["metadata"])
        self._pod_spec(spec["template"]["spec"], body["spec"]["template"]["spec"])
        return observed

    async def _result(
        self, request: MigrationJobRequest, body: dict[str, Any], job: dict[str, Any], uid: str
    ) -> MigrationJobEvidence | None:
        status = job.get("status", {})
        if status.get("failed", 0) != 0 or status.get("succeeded", 0) not in (0, 1):
            raise _refuse()
        if status.get("succeeded", 0) != 1:
            return None
        if status.get("active", 0) != 0:
            raise _refuse()
        pods = await asyncio.to_thread(
            self._core.list_namespaced_pod,
            request.metadata.resource_name,
            label_selector="job-name=" + body["metadata"]["name"],
        )
        if len(pods.items) != 1:
            raise _refuse()
        pod = self._wire(pods.items[0])
        meta = pod["metadata"]
        self._metadata(meta, body["spec"]["template"]["metadata"])
        owners = meta.get("ownerReferences", [])
        if (
            len(owners) != 1
            or any(
                owners[0].get(key) != value
                for key, value in {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": body["metadata"]["name"],
                    "uid": uid,
                    "controller": True,
                }.items()
            )
            or not _matches(_IDENTITY, meta.get("uid"))
            or meta.get("deletionTimestamp")
            or meta.get("namespace") != request.metadata.resource_name
            or not meta.get("name", "").startswith(body["metadata"]["name"] + "-")
        ):
            raise _refuse()
        self._pod_spec(pod["spec"], body["spec"]["template"]["spec"], scheduled=True)
        containers = pod.get("status", {}).get("containerStatuses", [])
        if (
            pod.get("status", {}).get("phase") != "Succeeded"
            or len(containers) != 1
            or containers[0].get("name") != "exomem"
        ):
            raise _refuse()
        terminated = containers[0].get("state", {}).get("terminated", {})
        if (
            type(terminated.get("exitCode")) is not int
            or terminated["exitCode"] != 0
            or not isinstance(terminated.get("message"), str)
        ):
            raise _refuse()
        terminal = parse_migration_terminal(request, terminated["message"].encode())
        return MigrationJobEvidence(uid, meta["uid"], _canonical(terminal))

    async def run(
        self, request: MigrationJobRequest, *, recovery_envelope: str
    ) -> MigrationJobEvidence:
        """Resume the exact request; never replace or delete a foreign fixed slot."""
        try:
            body = build_governance_migration_job(request, recovery_envelope=recovery_envelope)
            await self._stopped(request)
            uid: str | None = None
            for _ in range(self._poll_attempts):
                job = await self._read(request)
                if job is None:
                    if uid is not None:
                        raise _refuse()
                    await self._stopped(request)
                    # A create conflict is ambiguous; the caller can resume a
                    # later observation, but this attempt adopts no winner.
                    job = self._wire(
                        await asyncio.to_thread(
                            self._batch.create_namespaced_job, request.metadata.resource_name, body
                        )
                    )
                uid = self._job(job, body, uid)
                evidence = await self._result(request, body, job, uid)
                if evidence is not None:
                    await self._stopped(request)
                    latest = await self._read(request)
                    if latest is None:
                        raise _refuse()
                    self._job(latest, body, uid)
                    await asyncio.to_thread(
                        self._batch.delete_namespaced_job,
                        body["metadata"]["name"],
                        request.metadata.resource_name,
                        body={
                            "propagationPolicy": "Foreground",
                            "preconditions": {
                                "uid": uid,
                                "resourceVersion": latest["metadata"]["resourceVersion"],
                            },
                        },
                    )
                    for _ in range(self._poll_attempts):
                        remaining = await self._read(request)
                        if remaining is None:
                            pods = await asyncio.to_thread(
                                self._core.list_namespaced_pod,
                                request.metadata.resource_name,
                                label_selector="job-name=" + body["metadata"]["name"],
                            )
                            if not pods.items:
                                return evidence
                        elif remaining["metadata"].get("uid") != uid:
                            raise _refuse()
                        await self._pause()
                    raise _refuse()
                await self._pause()
            raise _refuse()
        except Exception:  # noqa: BLE001 - provider payloads must not escape this boundary
            raise _refuse() from None
