"""Read and cleanup boundary for the fixed fresh-storage initializer Job."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from kubernetes.client import ApiClient

from .adapters import _retryable_kubernetes_error
from .conflict_reason import ConflictReason
from .driver import DriverRetryable, LostAcknowledgement
from .job_execution import metadata_matches, pod_spec_matches
from .lifecycle import MetadataConflict, OpaqueProviderMetadata
from .provider_identity import ProviderIdentityConflict, ProviderReference
from .repository import ClaimConflict, StaleFence

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]{0,511}@sha256:[0-9a-f]{64}\Z")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_PROTOCOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_CREDENTIAL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_INIT_REQUEST_KEYS = {
    "request_id",
    "operation_id",
    "cell_id",
    "vault_id",
    "vault_root",
    "state_root",
    "log_root",
    "expected_release",
    "expected_protocol",
    "runtime_uid",
    "runtime_gid",
    "active_credential_version",
}


def _refuse() -> MetadataConflict:
    return MetadataConflict(
        "governance storage initializer is unavailable",
        reason=ConflictReason.GOVERNANCE_MIGRATION_JOB_UNAVAILABLE,
    )


def _retry() -> DriverRetryable:
    return DriverRetryable("governance storage initializer is temporarily unavailable")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _refuse()
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise _refuse()
    return value


def _closed_values(
    value: object,
    expected: dict[str, str],
    allowed: dict[str, str] | None = None,
) -> bool:
    if value is None:
        actual: dict[str, Any] = {}
    elif isinstance(value, dict):
        actual = value
    else:
        return False
    optional = allowed or {}
    return (
        set(actual) <= set(expected) | set(optional)
        and all(actual.get(key) == expected_value for key, expected_value in expected.items())
        and all(actual[key] == optional[key] for key in actual if key not in expected)
    )


class KubernetesGovernanceStorageInitAdapter:
    """Prove and remove exactly the chart-created initializer; never submit it."""

    def __init__(
        self,
        *,
        core_v1: Any,
        batch_v1: Any,
        apps_v1: Any,
        identity_verifier: Any,
        runtime_image: str,
    ) -> None:
        if not isinstance(runtime_image, str) or _IMAGE.fullmatch(runtime_image) is None:
            raise _refuse()
        self._core, self._batch, self._apps = core_v1, batch_v1, apps_v1
        self._identity_verifier, self._image = identity_verifier, runtime_image
        self._serializer = ApiClient()

    @staticmethod
    def _name(metadata: OpaqueProviderMetadata) -> str:
        return metadata.resource_name + "-init"

    def _wire(self, value: Any) -> Any:
        return self._serializer.sanitize_for_serialization(value)

    async def _job(self, metadata: OpaqueProviderMetadata) -> dict[str, Any] | None:
        try:
            value = await asyncio.to_thread(
                self._batch.read_namespaced_job,
                self._name(metadata),
                metadata.resource_name,
            )
        except Exception as error:  # noqa: BLE001 - provider payloads must not escape
            if getattr(error, "status", None) == 404:
                return None
            raise
        return _mapping(self._wire(value))

    async def _config_map(self, metadata: OpaqueProviderMetadata) -> dict[str, Any]:
        value = await asyncio.to_thread(
            self._core.read_namespaced_config_map,
            metadata.resource_name + "-init-request",
            metadata.resource_name,
        )
        return _mapping(self._wire(value))

    def _authenticate(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        envelope: str,
        api_version: str,
        kind: str,
        name: str,
    ) -> None:
        if self._identity_verifier is None:
            raise _refuse()
        try:
            self._identity_verifier.authenticate(
                envelope,
                provider="kubernetes",
                provider_reference=ProviderReference.kubernetes(
                    provider="kubernetes",
                    api_version=api_version,
                    kind=kind,
                    namespace=metadata.resource_name,
                    name=name,
                ),
                tenant_id=metadata.tenant_id,
                cell_id=metadata.subject_id,
                operation_id=metadata.operation_id,
                fence_generation=metadata.fence_generation,
            )
        except ProviderIdentityConflict as error:
            raise _refuse() from error

    @staticmethod
    def _labels(
        metadata: OpaqueProviderMetadata,
        *,
        template: bool = False,
        storage_init: bool = False,
    ) -> dict[str, str]:
        labels = {
            "app.kubernetes.io/name": "exomem-cell",
            "exomem.io/cell": metadata.resource_name,
        }
        if not template:
            labels.update(
                {
                    "app.kubernetes.io/instance": metadata.resource_name,
                    "app.kubernetes.io/part-of": "exomem-hosted",
                }
            )
        if storage_init:
            labels["exomem.io/storage-init"] = "true"
        return labels

    def _pod_spec(self, metadata: OpaqueProviderMetadata) -> dict[str, Any]:
        resource = metadata.resource_name
        return {
            "runtimeClassName": "exomem-storage-init",
            "serviceAccountName": resource,
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [
                {
                    "name": "exomem",
                    "image": self._image,
                    "imagePullPolicy": "IfNotPresent",
                    "terminationMessagePath": "/dev/termination-log",
                    "terminationMessagePolicy": "File",
                    "args": [
                        "hosted",
                        "init",
                        "--contract-version",
                        "1",
                        "--request-file",
                        "/run/exomem/operator-requests/init.json",
                    ],
                    "env": [
                        {"name": "EXOMEM_LOG_DIR", "value": "/dev"},
                        {"name": "EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION", "value": "1"},
                    ],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {
                            "drop": ["ALL"],
                            "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
                        },
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/var/lib/exomem"},
                        {
                            "name": "credentials",
                            "mountPath": "/run/exomem/credentials",
                            "readOnly": True,
                        },
                        {
                            "name": "init-request",
                            "mountPath": "/run/exomem/operator-requests/init.json",
                            "subPath": "init.json",
                            "readOnly": True,
                        },
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": resource + "-data"},
                },
                {
                    "name": "credentials",
                    "secret": {"secretName": "exomem-cell-credentials", "defaultMode": 292},
                },
                {
                    "name": "init-request",
                    "configMap": {"name": resource + "-init-request", "defaultMode": 292},
                },
            ],
        }

    def _job_body(
        self,
        metadata: OpaqueProviderMetadata,
        recovery_envelope: str,
    ) -> dict[str, Any]:
        return {
            "metadata": {
                "name": self._name(metadata),
                "namespace": metadata.resource_name,
                "labels": self._labels(metadata, storage_init=True),
                "annotations": {
                    **metadata.kubernetes_annotations,
                    "exomem.io/recovery-envelope": recovery_envelope,
                },
            },
            "spec": {
                "backoffLimit": 1,
                "activeDeadlineSeconds": 120,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "metadata": {
                        "labels": self._labels(
                            metadata,
                            template=True,
                            storage_init=True,
                        ),
                        "annotations": {},
                    },
                    "spec": self._pod_spec(metadata),
                },
            },
        }

    async def _stopped(self, metadata: OpaqueProviderMetadata, pvc_uid: str) -> None:
        resource = metadata.resource_name
        pvc = _mapping(
            self._wire(
                await asyncio.to_thread(
                    self._core.read_namespaced_persistent_volume_claim,
                    resource + "-data",
                    resource,
                )
            )
        )
        identity = _mapping(pvc.get("metadata"))
        annotations = _mapping(identity.get("annotations"))
        spec = _mapping(pvc.get("spec"))
        status = _mapping(pvc.get("status"))
        expected = metadata.kubernetes_annotations
        if (
            identity.get("name") != resource + "-data"
            or identity.get("namespace") != resource
            or identity.get("uid") != pvc_uid
            or identity.get("deletionTimestamp") is not None
            or status.get("phase") != "Bound"
            or not isinstance(spec.get("volumeName"), str)
            or not spec["volumeName"]
            or any(
                annotations.get(key) != expected[key]
                for key in (
                    "exomem.io/tenant-id",
                    "exomem.io/cell-id",
                    "exomem.io/tenant-digest",
                    "exomem.io/subject-digest",
                )
            )
        ):
            raise _refuse()
        try:
            runtime = _mapping(
                self._wire(
                    await asyncio.to_thread(
                        self._apps.read_namespaced_stateful_set,
                        resource,
                        resource,
                    )
                )
            )
        except Exception as error:
            if getattr(error, "status", None) == 404:
                return
            raise
        runtime_identity = _mapping(runtime.get("metadata"))
        runtime_spec = _mapping(runtime.get("spec"))
        if (
            runtime_identity.get("name") != resource
            or runtime_identity.get("namespace") != resource
            or type(runtime_spec.get("replicas")) is not int
            or runtime_spec["replicas"] != 0
        ):
            raise _refuse()

    def _prove_config_map(
        self,
        value: dict[str, Any],
        metadata: OpaqueProviderMetadata,
        *,
        recovery_envelope: str,
        init_request: dict[str, Any],
    ) -> tuple[str, str]:
        resource = metadata.resource_name
        identity = _mapping(value.get("metadata"))
        annotations = _mapping(identity.get("annotations"))
        envelope = annotations.get("exomem.io/recovery-envelope")
        expected_labels = self._labels(metadata)
        expected_annotations = {
            **metadata.kubernetes_annotations,
            "exomem.io/recovery-envelope": recovery_envelope,
        }
        helm_labels = {"app.kubernetes.io/managed-by": "Helm"}
        helm_annotations = {
            "meta.helm.sh/release-name": resource,
            "meta.helm.sh/release-namespace": resource,
        }
        canonical_request = self._canonical_init_request(metadata, init_request)
        data = value.get("data")
        if (
            identity.get("name") != resource + "-init-request"
            or identity.get("namespace") != resource
            or not metadata_matches(
                identity,
                {
                    "labels": expected_labels,
                    "annotations": expected_annotations,
                },
            )
            or not _closed_values(identity.get("labels"), expected_labels, helm_labels)
            or not _closed_values(annotations, expected_annotations, helm_annotations)
            or envelope != recovery_envelope
            or not isinstance(data, dict)
            or set(data) != {"init.json"}
            or data["init.json"] != canonical_request
            or value.get("binaryData") not in (None, {})
        ):
            raise _refuse()
        uid = _string(identity.get("uid"))
        rv = _string(identity.get("resourceVersion"))
        self._authenticate(
            metadata,
            envelope=recovery_envelope,
            api_version="v1",
            kind="ConfigMap",
            name=resource + "-init-request",
        )
        return uid, rv

    @staticmethod
    def _canonical_init_request(
        metadata: OpaqueProviderMetadata,
        init_request: dict[str, Any],
    ) -> str:
        if not isinstance(init_request, dict) or set(init_request) != _INIT_REQUEST_KEYS:
            raise _refuse()
        request_id = init_request.get("request_id")
        try:
            parsed_request_id = uuid.UUID(request_id) if isinstance(request_id, str) else None
        except ValueError as error:
            raise _refuse() from error
        release = init_request.get("expected_release")
        if (
            parsed_request_id is None
            or parsed_request_id.version != 4
            or str(parsed_request_id) != request_id
            or init_request.get("operation_id") != metadata.operation_id
            or _OPERATION_ID.fullmatch(str(init_request["operation_id"])) is None
            or init_request.get("cell_id") != metadata.subject_id
            or _OPAQUE_ID.fullmatch(str(init_request["cell_id"])) is None
            or init_request.get("vault_id") != metadata.tenant_id
            or _OPAQUE_ID.fullmatch(str(init_request["vault_id"])) is None
            or init_request.get("vault_root") != "/var/lib/exomem/vault"
            or init_request.get("state_root") != "/var/lib/exomem/state"
            or init_request.get("log_root") != "/var/lib/exomem/logs"
            or not isinstance(release, str)
            or not 1 <= len(release.encode()) <= 64
            or not isinstance(init_request.get("expected_protocol"), str)
            or _PROTOCOL.fullmatch(init_request["expected_protocol"]) is None
            or any(
                type(init_request.get(key)) is not int
                or not 1 <= init_request[key] <= 2_147_483_647
                for key in ("runtime_uid", "runtime_gid")
            )
            or not isinstance(init_request.get("active_credential_version"), str)
            or _CREDENTIAL_VERSION.fullmatch(init_request["active_credential_version"]) is None
        ):
            raise _refuse()
        try:
            return json.dumps(
                init_request,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise _refuse() from error

    def _prove_job(
        self,
        job: dict[str, Any],
        metadata: OpaqueProviderMetadata,
        recovery_envelope: str,
        *,
        allow_deleting: bool,
    ) -> tuple[str, str, bool]:
        body = self._job_body(metadata, recovery_envelope)
        identity = _mapping(job.get("metadata"))
        annotations = _mapping(identity.get("annotations"))
        uid = _string(identity.get("uid"))
        rv = _string(identity.get("resourceVersion"))
        helm_labels = {"app.kubernetes.io/managed-by": "Helm"}
        helm_annotations = {
            "meta.helm.sh/release-name": metadata.resource_name,
            "meta.helm.sh/release-namespace": metadata.resource_name,
        }
        if (
            identity.get("name") != self._name(metadata)
            or identity.get("namespace") != metadata.resource_name
            or (identity.get("deletionTimestamp") is not None and not allow_deleting)
            or not metadata_matches(identity, body["metadata"])
            or not _closed_values(identity.get("labels"), body["metadata"]["labels"], helm_labels)
            or not _closed_values(annotations, body["metadata"]["annotations"], helm_annotations)
            or annotations.get("exomem.io/recovery-envelope") != recovery_envelope
        ):
            raise _refuse()
        self._authenticate(
            metadata,
            envelope=recovery_envelope,
            api_version="batch/v1",
            kind="Job",
            name=self._name(metadata),
        )
        spec = _mapping(job.get("spec"))
        for key, expected in body["spec"].items():
            if key != "template" and (
                type(spec.get(key)) is not type(expected) or spec[key] != expected
            ):
                raise _refuse()
        defaults = {
            "completionMode": "NonIndexed",
            "completions": 1,
            "parallelism": 1,
            "suspend": False,
            "manualSelector": False,
        }
        if set(spec) - set(body["spec"]) - set(defaults) - {"selector"}:
            raise _refuse()
        if any(key in spec and spec[key] != expected for key, expected in defaults.items()):
            raise _refuse()
        selector = spec.get("selector")
        if selector is not None and selector != {
            "matchLabels": {"batch.kubernetes.io/controller-uid": uid}
        }:
            raise _refuse()
        template = _mapping(spec.get("template"))
        template_identity = _mapping(template.get("metadata"))
        controller_labels = {
            "batch.kubernetes.io/controller-uid": uid,
            "batch.kubernetes.io/job-name": self._name(metadata),
            "controller-uid": uid,
            "job-name": self._name(metadata),
        }
        if (
            not metadata_matches(
                template_identity,
                body["spec"]["template"]["metadata"],
            )
            or not _closed_values(
                template_identity.get("labels"),
                body["spec"]["template"]["metadata"]["labels"],
                controller_labels,
            )
            or template_identity.get("annotations") not in (None, {})
            or not pod_spec_matches(
                _mapping(template.get("spec")),
                body["spec"]["template"]["spec"],
            )
        ):
            raise _refuse()
        return uid, rv, self._complete(job)

    @staticmethod
    def _complete(job: dict[str, Any]) -> bool:
        status = _mapping(job.get("status", {}))
        for key in ("failed", "succeeded", "active", "terminating"):
            value = status.get(key, 0)
            if type(value) is not int or value < 0:
                raise _refuse()
        conditions_value = status.get("conditions", [])
        if not isinstance(conditions_value, list):
            raise _refuse()
        conditions: dict[str, str] = {}
        for item in conditions_value:
            condition = _mapping(item)
            kind, state = condition.get("type"), condition.get("status")
            if (
                not isinstance(kind, str)
                or not isinstance(state, str)
                or state
                not in {
                    "True",
                    "False",
                    "Unknown",
                }
            ):
                raise _refuse()
            if kind in {"Complete", "Failed"}:
                if kind in conditions:
                    raise _refuse()
                conditions[kind] = state
        complete = conditions.get("Complete") == "True"
        if conditions.get("Failed") == "True":
            raise _refuse()
        if complete and (
            status.get("succeeded", 0) != 1
            or status.get("active", 0) != 0
            or status.get("terminating", 0) != 0
        ):
            raise _refuse()
        return complete

    async def _candidate_pods(
        self,
        metadata: OpaqueProviderMetadata,
        uid: str | None,
    ) -> list[dict[str, Any]]:
        observed = _mapping(
            self._wire(
                await asyncio.to_thread(
                    self._core.list_namespaced_pod,
                    metadata.resource_name,
                )
            )
        )
        page_metadata = _mapping(observed.get("metadata", {}))
        continuation = page_metadata.get("continue")
        remaining = page_metadata.get("remainingItemCount")
        if continuation is not None and not isinstance(continuation, str):
            raise _refuse()
        if remaining is not None and (type(remaining) is not int or remaining < 0):
            raise _refuse()
        if continuation or remaining:
            raise _retry()
        items = observed.get("items")
        if not isinstance(items, list):
            raise _refuse()
        resource = metadata.resource_name
        job_name = self._name(metadata)
        candidates: list[dict[str, Any]] = []
        for item in items:
            pod = _mapping(item)
            identity = _mapping(pod.get("metadata"))
            if identity.get("namespace") != resource:
                raise _refuse()
            name = _string(identity.get("name"))
            labels = _mapping(identity.get("labels", {}))
            owners = identity.get("ownerReferences", [])
            if not isinstance(owners, list):
                raise _refuse()
            owner_values = [_mapping(owner) for owner in owners]
            spec = _mapping(pod.get("spec"))
            volumes = spec.get("volumes", [])
            if not isinstance(volumes, list):
                raise _refuse()
            uses_pvc = False
            for volume_value in volumes:
                volume = _mapping(volume_value)
                claim = volume.get("persistentVolumeClaim")
                if claim is not None and _mapping(claim).get("claimName") == resource + "-data":
                    uses_pvc = True
            job_signal = (
                name.startswith(job_name + "-")
                or any(
                    labels.get(key) == job_name
                    for key in ("job-name", "batch.kubernetes.io/job-name")
                )
                or any(
                    owner.get("name") == job_name or (uid is not None and owner.get("uid") == uid)
                    for owner in owner_values
                )
            )
            runtime_signal = (
                name == resource + "-0"
                or any(
                    owner.get("kind") == "StatefulSet" and owner.get("name") == resource
                    for owner in owner_values
                )
                or (
                    labels.get("app.kubernetes.io/name") == "exomem-cell"
                    and labels.get("exomem.io/cell") == resource
                    and not job_signal
                )
            )
            if uses_pvc or job_signal or runtime_signal:
                candidates.append(pod)
        return candidates

    def _prove_pod(
        self,
        pod: dict[str, Any],
        metadata: OpaqueProviderMetadata,
        uid: str,
    ) -> tuple[bool, bool]:
        identity = _mapping(pod.get("metadata"))
        owners = identity.get("ownerReferences")
        if not isinstance(owners, list) or len(owners) != 1:
            raise _refuse()
        owner = _mapping(owners[0])
        expected_owner = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": self._name(metadata),
            "uid": uid,
            "controller": True,
        }
        if (
            set(owner)
            - {
                "apiVersion",
                "kind",
                "name",
                "uid",
                "controller",
                "blockOwnerDeletion",
            }
            or any(owner.get(key) != value for key, value in expected_owner.items())
            or ("blockOwnerDeletion" in owner and type(owner["blockOwnerDeletion"]) is not bool)
            or identity.get("namespace") != metadata.resource_name
            or not _string(identity.get("uid"))
        ):
            raise _refuse()
        if not identity["name"].startswith(self._name(metadata) + "-"):
            raise _refuse()
        expected_metadata = {
            "labels": self._labels(
                metadata,
                template=True,
                storage_init=True,
            ),
            "annotations": {},
        }
        controller_labels = {
            "batch.kubernetes.io/controller-uid": uid,
            "batch.kubernetes.io/job-name": self._name(metadata),
            "controller-uid": uid,
            "job-name": self._name(metadata),
        }
        if (
            not metadata_matches(identity, expected_metadata)
            or not _closed_values(
                identity.get("labels"), expected_metadata["labels"], controller_labels
            )
            or identity.get("annotations") not in (None, {})
            or not pod_spec_matches(
                _mapping(pod.get("spec")),
                self._pod_spec(metadata),
                scheduled=True,
            )
        ):
            raise _refuse()
        status = _mapping(pod.get("status", {}))
        phase = status.get("phase")
        if phase not in {"Succeeded", "Failed"}:
            return False, False
        containers = status.get("containerStatuses")
        if not isinstance(containers, list) or len(containers) != 1:
            raise _refuse()
        container = _mapping(containers[0])
        terminated = _mapping(_mapping(container.get("state")).get("terminated"))
        exit_code = terminated.get("exitCode")
        if container.get("name") != "exomem" or type(exit_code) is not int:
            raise _refuse()
        successful = phase == "Succeeded" and exit_code == 0
        failed = phase == "Failed" and exit_code != 0
        if not successful and not failed:
            raise _refuse()
        return True, successful

    async def _prove_pods(
        self,
        metadata: OpaqueProviderMetadata,
        uid: str,
        *,
        complete: bool,
    ) -> None:
        candidates = await self._candidate_pods(metadata, uid)
        terminal = successful = 0
        for pod in candidates:
            is_terminal, is_successful = self._prove_pod(pod, metadata, uid)
            terminal += int(is_terminal)
            successful += int(is_successful)
        if complete:
            if not candidates or terminal != len(candidates):
                raise _retry()
            if successful != 1:
                raise _refuse()

    async def _prove_absence(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        pvc_uid: str,
    ) -> bool:
        if await self._job(metadata) is not None:
            return False
        await self._stopped(metadata, pvc_uid)
        candidates = await self._candidate_pods(metadata, None)
        if not candidates:
            return True
        for pod in candidates:
            identity = _mapping(pod.get("metadata"))
            owners = identity.get("ownerReferences", [])
            if not isinstance(owners, list) or len(owners) != 1:
                raise _refuse()
            uid = _string(_mapping(owners[0]).get("uid"))
            terminal, _successful = self._prove_pod(pod, metadata, uid)
            if not terminal:
                raise _refuse()
        raise _retry()

    @staticmethod
    def _same(actual: tuple[str, str], expected: tuple[str, str]) -> None:
        if actual != expected:
            raise _refuse()

    async def completed(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        recovery_envelope: str,
        init_request_recovery_envelope: str,
        init_request: dict[str, Any],
        pvc_uid: str,
        effect_guard: Callable[[], Awaitable[None]],
    ) -> bool:
        try:
            await effect_guard()
            job = await self._job(metadata)
            if job is None:
                if await self._prove_absence(metadata, pvc_uid=pvc_uid):
                    return False
                raise _retry()
            uid, rv, complete = self._prove_job(
                job, metadata, recovery_envelope, allow_deleting=True
            )
            config_identity = self._prove_config_map(
                await self._config_map(metadata),
                metadata,
                recovery_envelope=init_request_recovery_envelope,
                init_request=init_request,
            )
            await self._stopped(metadata, pvc_uid)
            await self._prove_pods(metadata, uid, complete=complete)
            if not complete:
                return False
            await effect_guard()
            current = await self._job(metadata)
            if current is None:
                raise _retry()
            current_uid, current_rv, current_complete = self._prove_job(
                current, metadata, recovery_envelope, allow_deleting=True
            )
            self._same((current_uid, current_rv), (uid, rv))
            if not current_complete:
                raise _refuse()
            self._same(
                self._prove_config_map(
                    await self._config_map(metadata),
                    metadata,
                    recovery_envelope=init_request_recovery_envelope,
                    init_request=init_request,
                ),
                config_identity,
            )
            await self._stopped(metadata, pvc_uid)
            await self._prove_pods(metadata, uid, complete=True)
            return True
        except (ClaimConflict, StaleFence, MetadataConflict, DriverRetryable):
            raise
        except Exception as error:  # noqa: BLE001 - provider payloads must not escape
            if _retryable_kubernetes_error(error):
                raise _retry() from None
            raise _refuse() from None

    async def cleanup(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        recovery_envelope: str,
        init_request_recovery_envelope: str,
        init_request: dict[str, Any],
        pvc_uid: str,
        effect_guard: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await effect_guard()
            await self._stopped(metadata, pvc_uid)
            job = await self._job(metadata)
            if job is None:
                if await self._prove_absence(metadata, pvc_uid=pvc_uid):
                    return
                raise _retry()
            uid, rv, complete = self._prove_job(
                job, metadata, recovery_envelope, allow_deleting=True
            )
            if not complete:
                raise _refuse()
            config_identity = self._prove_config_map(
                await self._config_map(metadata),
                metadata,
                recovery_envelope=init_request_recovery_envelope,
                init_request=init_request,
            )
            await self._prove_pods(metadata, uid, complete=True)
            if _mapping(job["metadata"]).get("deletionTimestamp") is not None:
                await effect_guard()
                current = await self._job(metadata)
                if current is None and await self._prove_absence(metadata, pvc_uid=pvc_uid):
                    return
                if current is None:
                    raise _retry()
                current_uid, current_rv, current_complete = self._prove_job(
                    current, metadata, recovery_envelope, allow_deleting=True
                )
                self._same((current_uid, current_rv), (uid, rv))
                if not current_complete or not _mapping(current["metadata"]).get(
                    "deletionTimestamp"
                ):
                    raise _refuse()
                await self._prove_pods(metadata, uid, complete=True)
                raise _retry()
            await effect_guard()
            current = await self._job(metadata)
            if current is None:
                if await self._prove_absence(metadata, pvc_uid=pvc_uid):
                    return
                raise _retry()
            current_uid, current_rv, current_complete = self._prove_job(
                current, metadata, recovery_envelope, allow_deleting=False
            )
            self._same((current_uid, current_rv), (uid, rv))
            if not current_complete:
                raise _refuse()
            self._same(
                self._prove_config_map(
                    await self._config_map(metadata),
                    metadata,
                    recovery_envelope=init_request_recovery_envelope,
                    init_request=init_request,
                ),
                config_identity,
            )
            await self._stopped(metadata, pvc_uid)
            await self._prove_pods(metadata, uid, complete=True)
            await effect_guard()
            try:
                await asyncio.to_thread(
                    self._batch.delete_namespaced_job,
                    self._name(metadata),
                    metadata.resource_name,
                    body={
                        "propagationPolicy": "Foreground",
                        "preconditions": {"uid": uid, "resourceVersion": rv},
                    },
                )
            except (ClaimConflict, StaleFence):
                raise
            except (LostAcknowledgement, OSError):
                pass
            except Exception as error:  # noqa: BLE001 - provider payloads must not escape
                if getattr(error, "status", None) != 404 and not _retryable_kubernetes_error(error):
                    raise _refuse() from None
            await effect_guard()
            if not await self._prove_absence(metadata, pvc_uid=pvc_uid):
                raise _retry()
        except (ClaimConflict, StaleFence, MetadataConflict, DriverRetryable):
            raise
        except Exception as error:  # noqa: BLE001 - provider payloads must not escape
            if _retryable_kubernetes_error(error):
                raise _retry() from None
            raise _refuse() from None
