"""Fenced, inert first-consumer Job for fresh governed PVCs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .adapters import _require_annotations, _retryable_kubernetes_error
from .driver import DriverRetryable, LostAcknowledgement
from .governance_storage_init import (
    KubernetesGovernanceStorageInitAdapter,
    _mapping,
    _refuse,
    _retry,
    _string,
)
from .lifecycle import MetadataConflict, OpaqueProviderMetadata
from .repository import ClaimConflict, StaleFence


class KubernetesGovernanceStorageBindingAdapter(KubernetesGovernanceStorageInitAdapter):
    """Own only the signed fixed-slot binding Job, never the initializer."""

    def __init__(self, *, cell: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cell = cell

    @staticmethod
    def _labels(
        metadata: OpaqueProviderMetadata,
        *,
        template: bool = False,
        storage_init: bool = False,
    ) -> dict[str, str]:
        labels = KubernetesGovernanceStorageInitAdapter._labels(
            metadata, template=template, storage_init=True
        )
        labels["exomem.io/storage-binding"] = "true"
        return labels

    def _pod_spec(self, metadata: OpaqueProviderMetadata) -> dict[str, Any]:
        return {
            "runtimeClassName": "exomem-storage-init",
            "serviceAccountName": metadata.resource_name,
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [
                {
                    "name": "exomem",
                    "image": self._image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/true"],
                    "terminationMessagePath": "/dev/termination-log",
                    "terminationMessagePolicy": "File",
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
            "volumes": [
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": metadata.resource_name + "-data"},
                }
            ],
        }

    def _job_body(self, metadata: OpaqueProviderMetadata, recovery_envelope: str) -> dict[str, Any]:
        body = super()._job_body(metadata, recovery_envelope)
        body["apiVersion"] = "batch/v1"
        body["kind"] = "Job"
        body["metadata"]["annotations"]["exomem.io/job-purpose"] = "storage-binding"
        body["spec"]["backoffLimit"] = 0
        body["spec"]["activeDeadlineSeconds"] = 90
        body["spec"]["ttlSecondsAfterFinished"] = 60
        return body

    @staticmethod
    def _complete(job: dict[str, Any]) -> bool:
        status = _mapping(job.get("status", {}))
        failed = status.get("failed", 0)
        if type(failed) is not int or failed < 0:
            raise _refuse()
        conditions = status.get("conditions", [])
        if not isinstance(conditions, list):
            raise _refuse()
        failed_condition = any(
            _mapping(condition).get("type") == "Failed"
            and _mapping(condition).get("status") == "True"
            for condition in conditions
        )
        if not failed_condition:
            return KubernetesGovernanceStorageInitAdapter._complete(job)
        if any(
            _mapping(condition).get("type") == "Complete"
            and _mapping(condition).get("status") == "True"
            for condition in conditions
        ):
            raise _refuse()
        masked = {
            **job,
            "status": {
                **status,
                "failed": 0,
                "conditions": [
                    {**_mapping(condition), "status": "False"}
                    if _mapping(condition).get("type") == "Failed"
                    else condition
                    for condition in conditions
                ],
            },
        }
        KubernetesGovernanceStorageInitAdapter._complete(masked)
        return False

    async def _volume_state(self, metadata: OpaqueProviderMetadata, pvc_uid: str) -> str:
        uid, phase = await self._cell.authenticated_volume_state(metadata)
        if uid != pvc_uid:
            raise _refuse()
        return phase

    async def _slot_absent(self, metadata: OpaqueProviderMetadata) -> bool:
        if await self._job(metadata) is not None:
            return False
        # Namespace-wide, not a label selector: terminating and unlabelled PVC
        # users still reserve the fixed slot and cannot authorize initialization.
        return not await self._candidate_pods(metadata, None)

    async def _binding_pods(
        self, metadata: OpaqueProviderMetadata, uid: str | None
    ) -> list[dict[str, Any]]:
        pods = await self._candidate_pods(metadata, uid)
        name = self._name(metadata)
        return [
            pod
            for pod in pods
            if _mapping(pod["metadata"])["name"].startswith(name + "-")
            or _mapping(_mapping(pod["metadata"]).get("labels", {})).get(
                "exomem.io/storage-binding"
            )
            == "true"
            or any(
                _mapping(owner).get("name") == name
                or (uid is not None and _mapping(owner).get("uid") == uid)
                for owner in _mapping(pod["metadata"]).get("ownerReferences", [])
            )
        ]

    async def _prove_binding_pods(self, metadata: OpaqueProviderMetadata, uid: str) -> None:
        for pod in await self._binding_pods(metadata, uid):
            self._prove_pod(pod, metadata, uid)

    async def _other_slot_pods(
        self, metadata: OpaqueProviderMetadata, replacement_uid: str
    ) -> bool:
        for pod in await self._binding_pods(metadata, None):
            identity = _mapping(pod.get("metadata"))
            labels = _mapping(identity.get("labels", {}))
            owners = identity.get("ownerReferences", [])
            if labels.get("exomem.io/storage-binding") == "true" or not any(
                _mapping(owner).get("uid") == replacement_uid for owner in owners
            ):
                return True
        return False

    async def wait_for_original(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        pvc_uid: str | None = None,
        recovery_envelope: str,
        effect_guard: Callable[[], Awaitable[None]],
        replacement_metadata: OpaqueProviderMetadata | None = None,
        replacement_envelope: str | None = None,
    ) -> bool:
        """Observe the original binder without deleting across operations."""
        try:
            await effect_guard()
            job = await self._job(metadata)
            if job is None:
                pods = await self._binding_pods(metadata, None)
                if not pods:
                    return False
            else:
                identity = _mapping(job.get("metadata"))
                labels = _mapping(identity.get("labels", {}))
                annotations = _mapping(identity.get("annotations", {}))
                if (
                    annotations.get("exomem.io/job-purpose") != "storage-binding"
                    and labels.get("exomem.io/governance-migration") == "true"
                ):
                    if (
                        replacement_metadata is None
                        or replacement_envelope is None
                        or labels.get("app.kubernetes.io/name") != "exomem-governance-migration"
                        or labels.get("exomem.io/cell") != metadata.resource_name
                        or annotations.get("exomem.io/recovery-envelope") != replacement_envelope
                    ):
                        raise _refuse()
                    _require_annotations(annotations, replacement_metadata)
                    self._authenticate(
                        replacement_metadata,
                        envelope=replacement_envelope,
                        api_version="batch/v1",
                        kind="Job",
                        name=self._name(metadata),
                    )
                    # The current operation's migration coordinator proves this
                    # replacement Job. Only Pods from another slot execution
                    # can still be the retired original binder's dependents.
                    uid = _string(identity.get("uid"))
                    if await self._other_slot_pods(metadata, uid):
                        observed_uid = await self._cell.authenticated_volume_uid(metadata)
                        if pvc_uid is not None and observed_uid != pvc_uid:
                            raise _refuse()
                        return True
                    return False
            observed_uid = await self._cell.authenticated_volume_uid(metadata)
            if pvc_uid is not None and observed_uid != pvc_uid:
                raise _refuse()
            if job is None:
                return True
            uid, _, _ = self._prove_job(job, metadata, recovery_envelope, allow_deleting=True)
            await self._prove_binding_pods(metadata, uid)
            return True
        except (ClaimConflict, StaleFence, MetadataConflict, DriverRetryable):
            raise
        except Exception as error:  # noqa: BLE001 - provider payloads stay private
            if _retryable_kubernetes_error(error):
                raise _retry() from None
            raise _refuse() from None

    async def cleanup_late(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        pvc_uid: str,
        recovery_envelope: str,
        effect_guard: Callable[[], Awaitable[None]],
    ) -> bool:
        """Clean only this operation's binder; leave a later initializer intact."""
        try:
            await effect_guard()
            job = await self._job(metadata)
            if job is not None:
                annotations = _mapping(_mapping(job.get("metadata")).get("annotations"))
                if annotations.get("exomem.io/job-purpose") == "storage-binding":
                    await self.reconcile(
                        metadata,
                        pvc_uid=pvc_uid,
                        recovery_envelope=recovery_envelope,
                        effect_guard=effect_guard,
                    )
                    return False
                # The initializer is proved by its own adapter. Binder Pods
                # must nevertheless be gone before admitting that slot.
                return not await self._other_slot_pods(
                    metadata, _string(_mapping(job.get("metadata")).get("uid"))
                )
            return not await self._binding_pods(metadata, None)
        except (ClaimConflict, StaleFence, MetadataConflict, DriverRetryable):
            raise
        except Exception as error:  # noqa: BLE001 - provider payloads stay private
            if _retryable_kubernetes_error(error):
                raise _retry() from None
            raise _refuse() from None

    async def reconcile(
        self,
        metadata: OpaqueProviderMetadata,
        *,
        pvc_uid: str,
        recovery_envelope: str,
        effect_guard: Callable[[], Awaitable[None]],
    ) -> bool:
        """Return true only after Bound and fixed-slot/PVC Pod absence."""
        try:
            await effect_guard()
            phase = await self._volume_state(metadata, pvc_uid)
            job = await self._job(metadata)
            if job is None and phase == "Pending":
                if not await self._slot_absent(metadata):
                    raise _retry()
                await effect_guard()
                if await self._volume_state(metadata, pvc_uid) != "Pending":
                    return False
                if await self._job(metadata) is not None:
                    raise _retry()
                await effect_guard()
                await asyncio.to_thread(
                    self._batch.create_namespaced_job,
                    metadata.resource_name,
                    body=self._job_body(metadata, recovery_envelope),
                )
                return False
            if job is None:
                return await self._slot_absent(metadata)
            uid, rv, _complete = self._prove_job(
                job, metadata, recovery_envelope, allow_deleting=True
            )
            await self._prove_binding_pods(metadata, uid)
            if phase == "Pending":
                return False
            if _mapping(job["metadata"]).get("deletionTimestamp") is not None:
                return False
            await effect_guard()
            if await self._volume_state(metadata, pvc_uid) != "Bound":
                raise _retry()
            current = await self._job(metadata)
            if current is None:
                return await self._slot_absent(metadata)
            current_uid, current_rv, _ = self._prove_job(
                current, metadata, recovery_envelope, allow_deleting=False
            )
            if current_uid != uid:
                raise _refuse()
            if current_rv != rv:
                # The Job controller can publish status between these reads.
                # Re-prove a stable revision on the next pass before deleting;
                # a valid same-UID update is not a foreign successor.
                raise _retry()
            await self._prove_binding_pods(metadata, uid)
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
            except (LostAcknowledgement, OSError):
                pass
            except Exception as error:  # noqa: BLE001 - DELETE 404 races TTL cleanup
                if getattr(error, "status", None) != 404:
                    raise
            await effect_guard()
            return await self._slot_absent(metadata)
        except (ClaimConflict, StaleFence, MetadataConflict, DriverRetryable):
            raise
        except (LostAcknowledgement, OSError):
            raise _retry() from None
        except Exception as error:  # noqa: BLE001 - provider payloads stay private
            if _retryable_kubernetes_error(error):
                raise _retry() from None
            raise _refuse() from None
