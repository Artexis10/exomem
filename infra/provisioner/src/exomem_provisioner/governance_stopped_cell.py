"""Read-only physical stopped-cell proof for migration-only target recovery."""

from __future__ import annotations

import asyncio
from typing import Any

from kubernetes.client import ApiClient

from .adapters import _retryable_kubernetes_error
from .conflict_reason import ConflictReason
from .driver import DriverRetryable
from .lifecycle import MetadataConflict, OpaqueProviderMetadata
from .repository import ClaimConflict, StaleFence


def _refuse() -> MetadataConflict:
    return MetadataConflict(
        "governance migration Job is unavailable",
        reason=ConflictReason.GOVERNANCE_MIGRATION_JOB_UNAVAILABLE,
    )


def _retry() -> DriverRetryable:
    return DriverRetryable("governance migration Job is temporarily unavailable")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _refuse()
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _refuse()
    return value


def _pvc_is_bound(value: object, *, metadata: OpaqueProviderMetadata, pvc_uid: str) -> None:
    resource = metadata.resource_name
    pvc = _mapping(value)
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
        or not _string(spec.get("volumeName"))
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


def _runtime_is_stopped(value: object, *, resource: str) -> None:
    runtime = _mapping(value)
    identity = _mapping(runtime.get("metadata"))
    spec = _mapping(runtime.get("spec"))
    replicas = spec.get("replicas")
    if (
        identity.get("name") != resource
        or identity.get("namespace") != resource
        or type(replicas) is not int
        or replicas != 0
    ):
        raise _refuse()


def _pod_uses_runtime_or_pvc(value: object, *, resource: str) -> bool:
    pod = _mapping(value)
    identity = _mapping(pod.get("metadata"))
    if identity.get("namespace") != resource:
        raise _refuse()
    name = _string(identity.get("name"))
    _string(identity.get("uid"))

    labels_value = identity.get("labels")
    if labels_value is None:
        labels: dict[str, Any] = {}
    else:
        labels = _mapping(labels_value)
        if any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in labels.items()
        ):
            raise _refuse()

    owners_value = identity.get("ownerReferences")
    if owners_value is None:
        owners: list[object] = []
    elif isinstance(owners_value, list):
        owners = owners_value
    else:
        raise _refuse()
    for owner in owners:
        owner_identity = _mapping(owner)
        _string(owner_identity.get("name"))

    spec = _mapping(pod.get("spec"))
    volumes_value = spec.get("volumes")
    if volumes_value is None:
        volumes: list[object] = []
    elif isinstance(volumes_value, list):
        volumes = volumes_value
    else:
        raise _refuse()
    for volume in volumes:
        source = _mapping(volume)
        claim = source.get("persistentVolumeClaim")
        if claim is None:
            continue
        claim_identity = _mapping(claim)
        if _string(claim_identity.get("claimName")) == resource + "-data":
            return True

    return (
        name == resource + "-0"
        or any(owner["name"] == resource for owner in owners if isinstance(owner, dict))
        or (
            labels.get("app.kubernetes.io/name") == "exomem-cell"
            and labels.get("exomem.io/cell") == resource
        )
    )


async def verify_stopped_cell(
    core_v1: Any,
    apps_v1: Any,
    *,
    metadata: OpaqueProviderMetadata,
    pvc_uid: str,
) -> None:
    """Prove that a bound cell volume has no workload before target recovery."""
    try:
        if (
            not isinstance(metadata, OpaqueProviderMetadata)
            or not isinstance(pvc_uid, str)
            or not pvc_uid
        ):
            raise _refuse()
        resource = metadata.resource_name
        serializer = ApiClient()
        pvc = serializer.sanitize_for_serialization(
            await asyncio.to_thread(
                core_v1.read_namespaced_persistent_volume_claim,
                resource + "-data",
                resource,
            )
        )
        _pvc_is_bound(pvc, metadata=metadata, pvc_uid=pvc_uid)
        try:
            runtime = serializer.sanitize_for_serialization(
                await asyncio.to_thread(apps_v1.read_namespaced_stateful_set, resource, resource)
            )
        except Exception as error:
            if getattr(error, "status", None) != 404:
                raise
        else:
            _runtime_is_stopped(runtime, resource=resource)

        page = serializer.sanitize_for_serialization(
            await asyncio.to_thread(core_v1.list_namespaced_pod, resource)
        )
        page_value = _mapping(page)
        page_metadata = _mapping(page_value.get("metadata"))
        continuation = page_metadata.get("continue")
        if continuation is not None and not isinstance(continuation, str):
            raise _refuse()
        remaining = page_metadata.get("remainingItemCount")
        if remaining is not None and (type(remaining) is not int or remaining < 0):
            raise _refuse()
        if continuation or remaining:
            raise _retry()
        items = page_value.get("items")
        if not isinstance(items, list):
            raise _refuse()
        if any(_pod_uses_runtime_or_pvc(item, resource=resource) for item in items):
            raise _refuse()
    except (ClaimConflict, StaleFence, MetadataConflict, DriverRetryable):
        raise
    except Exception as error:  # noqa: BLE001 - provider payloads must not escape
        if _retryable_kubernetes_error(error):
            raise _retry() from None
        raise _refuse() from None
