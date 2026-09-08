"""A retry may observe its own read-only fingerprint, never an arbitrary PVC user."""

from types import SimpleNamespace

import pytest
from test_governance_live_recovery import Missing
from test_governance_stopped_cell import _pod as unrelated_pod
from test_governance_stopped_cell import _pvc
from test_vault_fingerprint_execution_proof import ENVELOPE, METADATA, OPERATION, Cluster, _adapter

from exomem_provisioner.lifecycle import MetadataConflict


def cluster(**kwargs):
    adapter = _adapter()
    result = Cluster(adapter, **kwargs)
    adapter._core.read_namespaced_persistent_volume_claim = lambda *args: _pvc()

    def missing(*args):
        raise Missing

    adapter._apps = SimpleNamespace(read_namespaced_stateful_set=missing)
    return result


async def prove(h):
    await h.adapter.prove_stopped_before_fingerprint(
        METADATA,
        owner=METADATA,
        pvc_uid="pvc-alpha",
        operation_id=OPERATION,
        recovery_envelope=ENVELOPE,
    )


async def test_exact_owned_fingerprint_pod_can_be_reconciled_after_interruption():
    await prove(cluster())


async def test_stopped_fingerprint_proof_refuses_unlabelled_pvc_user():
    h = cluster()
    h.pods.append(
        unrelated_pod(
            volumes=[
                {
                    "name": "vault",
                    "persistentVolumeClaim": {"claimName": METADATA.resource_name + "-data"},
                }
            ]
        )
    )
    with pytest.raises(MetadataConflict):
        await prove(h)


async def test_stopped_fingerprint_proof_requires_canonical_executable_job():
    h = cluster(
        job_mutate=lambda job: job["spec"]["template"]["spec"]["containers"][0].update(
            command=["foreign"]
        )
    )
    with pytest.raises(MetadataConflict):
        await prove(h)


async def test_stopped_fingerprint_proof_requires_canonical_executable_pod():
    h = cluster(pod_mutate=lambda pod: pod["spec"]["containers"][0].update(command=["foreign"]))
    with pytest.raises(MetadataConflict):
        await prove(h)


async def test_stopped_fingerprint_proof_refuses_two_owned_candidate_pods():
    h = cluster()
    h.pods = h.pods * 2
    with pytest.raises(MetadataConflict):
        await prove(h)
