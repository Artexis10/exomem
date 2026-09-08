from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient, V1ListMeta, V1PodList

from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.repository import ClaimConflict, StaleFence


def test_stopped_cell_verifier_has_a_dedicated_provisioner_boundary() -> None:
    assert importlib.util.find_spec("exomem_provisioner.governance_stopped_cell") is not None


METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)


def _model(value: dict[str, object], kind: str):
    return ApiClient().deserialize(SimpleNamespace(data=json.dumps(value)), kind)


def _pvc(**changes: object):
    resource = METADATA.resource_name
    value: dict[str, object] = {
        "metadata": {
            "name": resource + "-data",
            "namespace": resource,
            "uid": "pvc-alpha",
            "annotations": dict(METADATA.kubernetes_annotations),
        },
        "spec": {"volumeName": "pv-alpha"},
        "status": {"phase": "Bound"},
    }
    for path, replacement in changes.items():
        parent = value
        *keys, key = path.split(".")
        for item in keys:
            parent = parent[item]  # type: ignore[assignment,index]
        parent[key] = replacement
    return _model(value, "V1PersistentVolumeClaim")


def _stateful_set(replicas: object = 0):
    resource = METADATA.resource_name
    return _model(
        {
            "metadata": {"name": resource, "namespace": resource},
            "spec": {
                "replicas": replicas,
                "serviceName": resource,
                "selector": {"matchLabels": {"app": "example"}},
                "template": {
                    "metadata": {"labels": {"app": "example"}},
                    "spec": {"containers": [{"name": "exomem", "image": "example"}]},
                },
            },
        },
        "V1StatefulSet",
    )


def _pod(
    name: str = "unrelated",
    *,
    labels: dict[str, str] | None = None,
    owners: list[dict[str, object]] | None = None,
    volumes: list[dict[str, object]] | None = None,
    deleting: bool = False,
    phase: str | None = None,
):
    resource = METADATA.resource_name
    metadata: dict[str, object] = {
        "name": name,
        "namespace": resource,
        "uid": "pod-" + name,
    }
    if labels is not None:
        metadata["labels"] = labels
    if owners is not None:
        metadata["ownerReferences"] = [
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "uid": "owner-alpha",
                "controller": True,
                "blockOwnerDeletion": True,
                **owner,
            }
            for owner in owners
        ]
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-08T12:00:00Z"
    value: dict[str, object] = {
        "metadata": metadata,
        "spec": {"containers": [{"name": "exomem", "image": "example"}], "volumes": volumes or []},
    }
    if phase is not None:
        value["status"] = {"phase": phase}
    return _model(value, "V1Pod")


def _pod_page(*items: object, continuation: str | None = None) -> V1PodList:
    return V1PodList(items=list(items), metadata=V1ListMeta(_continue=continuation))


class ApiMissing(Exception):
    status = 404


class Cluster:
    def __init__(self) -> None:
        self.pvc = _pvc()
        self.runtime: object = ApiMissing
        self.pods: object = _pod_page()

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name + "-data", METADATA.resource_name)
        return self.pvc

    def read_namespaced_stateful_set(self, name: str, namespace: str):
        assert (name, namespace) == (METADATA.resource_name, METADATA.resource_name)
        if self.runtime is ApiMissing:
            raise ApiMissing
        return self.runtime

    def list_namespaced_pod(self, namespace: str, **kwargs: object):
        assert namespace == METADATA.resource_name
        assert kwargs == {}
        return self.pods


async def _verify(cluster: Cluster) -> None:
    from exomem_provisioner.governance_stopped_cell import verify_stopped_cell

    await verify_stopped_cell(
        cluster,
        cluster,
        metadata=METADATA,
        pvc_uid="pvc-alpha",
    )


async def test_verifies_bound_identity_matched_pvc_and_absent_runtime() -> None:
    await _verify(Cluster())


async def test_accepts_the_original_pvc_operation_and_fence_annotations() -> None:
    cluster = Cluster()
    annotations = dict(METADATA.kubernetes_annotations)
    annotations.update({"exomem.io/operation-id": "original-operation", "exomem.io/fence": "1"})
    cluster.pvc = _pvc(**{"metadata.annotations": annotations})
    await _verify(cluster)


@pytest.mark.parametrize("replicas", [1, True, None])
async def test_refuses_present_runtime_that_is_not_strictly_zero(replicas: object) -> None:
    cluster = Cluster()
    cluster.runtime = _stateful_set(replicas)
    with pytest.raises(MetadataConflict):
        await _verify(cluster)


@pytest.mark.parametrize(
    "pvc",
    [
        lambda: _pvc(**{"metadata.uid": "wrong-pvc"}),
        lambda: _pvc(**{"metadata.deletionTimestamp": "2026-09-08T12:00:00Z"}),
        lambda: _pvc(**{"status.phase": "Pending"}),
        lambda: _pvc(**{"spec.volumeName": None}),
        lambda: _pvc(**{"metadata.annotations": {}}),
    ],
)
async def test_refuses_wrong_or_nonbound_pvc_evidence(pvc) -> None:
    cluster = Cluster()
    cluster.pvc = pvc()
    with pytest.raises(MetadataConflict):
        await _verify(cluster)


async def test_unlabelled_pvc_mount_blocks_stopped_proof() -> None:
    cluster = Cluster()
    cluster.pods = _pod_page(
        _pod(
            volumes=[
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": METADATA.resource_name + "-data"},
                }
            ]
        )
    )
    with pytest.raises(MetadataConflict):
        await _verify(cluster)


@pytest.mark.parametrize(
    "pod",
    [
        lambda: _pod(METADATA.resource_name + "-0"),
        lambda: _pod(
            owners=[
                {"apiVersion": "apps/v1", "kind": "StatefulSet", "name": METADATA.resource_name}
            ]
        ),
        lambda: _pod(
            labels={
                "app.kubernetes.io/name": "exomem-cell",
                "exomem.io/cell": METADATA.resource_name,
            }
        ),
    ],
)
async def test_runtime_identity_blocks_stopped_proof_without_a_pvc_mount(pod) -> None:
    cluster = Cluster()
    cluster.pods = _pod_page(pod())
    with pytest.raises(MetadataConflict):
        await _verify(cluster)


@pytest.mark.parametrize(
    "pod",
    [
        lambda: _pod(
            volumes=[
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": METADATA.resource_name + "-data"},
                }
            ],
            phase="Succeeded",
        ),
        lambda: _pod(
            volumes=[
                {
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": METADATA.resource_name + "-data"},
                }
            ],
            deleting=True,
        ),
    ],
)
async def test_terminal_or_terminating_pvc_pods_remain_present(pod) -> None:
    cluster = Cluster()
    cluster.pods = _pod_page(pod())
    with pytest.raises(MetadataConflict):
        await _verify(cluster)


async def test_well_formed_unrelated_pod_does_not_block_stopped_proof() -> None:
    cluster = Cluster()
    cluster.pods = _pod_page(_pod())
    await _verify(cluster)


async def test_incomplete_pod_inventory_is_retryable_not_absence() -> None:
    cluster = Cluster()
    cluster.pods = _pod_page(continuation="next-page")
    with pytest.raises(DriverRetryable) as raised:
        await _verify(cluster)
    assert str(raised.value) == "governance migration Job is temporarily unavailable"


async def test_empty_continuation_is_a_complete_inventory() -> None:
    cluster = Cluster()
    cluster.pods = _pod_page(continuation="")
    await _verify(cluster)


async def test_remaining_items_without_a_token_do_not_prove_absence() -> None:
    cluster = Cluster()
    cluster.pods = V1PodList(items=[], metadata=V1ListMeta(remaining_item_count=1))
    with pytest.raises(DriverRetryable):
        await _verify(cluster)


@pytest.mark.parametrize("bad", [{"metadata": {}, "spec": []}, {"metadata": [], "spec": {}}])
async def test_malformed_pod_evidence_fails_closed(bad: dict[str, object]) -> None:
    cluster = Cluster()
    cluster.pods = _pod_page(bad)
    with pytest.raises(MetadataConflict):
        await _verify(cluster)


async def test_transient_provider_errors_are_retryable_and_content_free() -> None:
    cluster = Cluster()

    class ProviderError(Exception):
        status = 503

    def unavailable(*args: object, **kwargs: object):
        raise ProviderError("private-provider-payload")

    cluster.list_namespaced_pod = unavailable  # type: ignore[method-assign]
    with pytest.raises(DriverRetryable) as raised:
        await _verify(cluster)
    assert str(raised.value) == "governance migration Job is temporarily unavailable"
    assert "private-provider-payload" not in str(raised.value)


@pytest.mark.parametrize("failure", [ClaimConflict, StaleFence])
async def test_claim_and_fence_conflicts_are_not_disguised_as_provider_state(failure) -> None:
    cluster = Cluster()
    refused = failure("AUTHORITY_LOST")

    def unavailable(*args: object, **kwargs: object):
        raise refused

    cluster.read_namespaced_persistent_volume_claim = unavailable  # type: ignore[method-assign]
    with pytest.raises(failure) as raised:
        await _verify(cluster)
    assert raised.value is refused
