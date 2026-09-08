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


def _runtime_is_stopped(value: object, *, resource: str, require_uid: bool = False) -> str | None:
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
    uid = identity.get("uid")
    if require_uid:
        return _string(uid)
    return None


def _pod_evidence(
    value: object,
    *,
    metadata: OpaqueProviderMetadata,
    runtime_uid: str | None,
) -> tuple[bool, bool]:
    """Return whether a pod is forbidden or the sole retryable runtime remnant."""
    resource = metadata.resource_name
    pod = _mapping(value)
    identity = _mapping(pod.get("metadata"))
    if identity.get("namespace") != resource:
        raise _refuse()
    name = _string(identity.get("name"))
    _string(identity.get("uid"))

    annotations_value = identity.get("annotations")
    if annotations_value is None:
        annotations: dict[str, Any] = {}
    else:
        annotations = _mapping(annotations_value)
        if any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in annotations.items()
        ):
            raise _refuse()

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
    owner_identities: list[dict[str, Any]] = []
    for owner in owners:
        owner_identity = _mapping(owner)
        _string(owner_identity.get("name"))
        if "controller" in owner_identity and type(owner_identity["controller"]) is not bool:
            raise _refuse()
        owner_identities.append(owner_identity)

    spec = _mapping(pod.get("spec"))
    volumes_value = spec.get("volumes")
    if volumes_value is None:
        volumes: list[object] = []
    elif isinstance(volumes_value, list):
        volumes = volumes_value
    else:
        raise _refuse()
    uses_pvc = False
    for volume in volumes:
        source = _mapping(volume)
        claim = source.get("persistentVolumeClaim")
        if claim is None:
            continue
        claim_identity = _mapping(claim)
        if _string(claim_identity.get("claimName")) == resource + "-data":
            uses_pvc = True

    runtime_signal = (
        name == resource + "-0"
        or any(owner["name"] == resource for owner in owner_identities)
        or (
            labels.get("app.kubernetes.io/name") == "exomem-cell"
            and labels.get("exomem.io/cell") == resource
        )
    )
    exact_controller = (
        len(owner_identities) == 1
        and owner_identities[0].get("kind") == "StatefulSet"
        and owner_identities[0].get("name") == resource
        and owner_identities[0].get("uid") == runtime_uid
        and owner_identities[0].get("controller") is True
    )
    retryable_runtime = (
        runtime_uid is not None
        and name == resource + "-0"
        and exact_controller
        and all(
            annotations.get(key) == expected
            for key, expected in metadata.kubernetes_annotations.items()
        )
    )
    return uses_pvc or runtime_signal, retryable_runtime


async def verify_stopped_cell(
    core_v1: Any,
    apps_v1: Any,
    *,
    metadata: OpaqueProviderMetadata,
    pvc_uid: str,
    wait_for_runtime: bool = False,
) -> None:
    """Prove that a bound cell volume has no workload before target recovery."""
    try:
        if (
            not isinstance(metadata, OpaqueProviderMetadata)
            or not isinstance(pvc_uid, str)
            or not pvc_uid
            or type(wait_for_runtime) is not bool
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
        runtime_uid: str | None = None
        try:
            runtime = serializer.sanitize_for_serialization(
                await asyncio.to_thread(apps_v1.read_namespaced_stateful_set, resource, resource)
            )
        except Exception as error:
            if getattr(error, "status", None) != 404:
                raise
        else:
            runtime_uid = _runtime_is_stopped(
                runtime,
                resource=resource,
                require_uid=wait_for_runtime,
            )

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
        retryable_runtime = False
        forbidden_runtime = False
        for item in items:
            forbidden, retryable = _pod_evidence(
                item,
                metadata=metadata,
                runtime_uid=runtime_uid if wait_for_runtime else None,
            )
            if retryable and wait_for_runtime:
                retryable_runtime = True
            elif forbidden:
                forbidden_runtime = True
        if forbidden_runtime:
            raise _refuse()
        if retryable_runtime:
            raise _retry()
    except (ClaimConflict, StaleFence, MetadataConflict, DriverRetryable):
        raise
    except Exception as error:  # noqa: BLE001 - provider payloads must not escape
        if _retryable_kubernetes_error(error):
            raise _retry() from None
        raise _refuse() from None
