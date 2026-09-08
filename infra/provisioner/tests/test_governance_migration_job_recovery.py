from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from test_governance_migration_job import Cluster, _adapter, _allow, _module, _request

from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.lifecycle import MetadataConflict
from exomem_provisioner.repository import ClaimConflict


def _existing(*, failed=False, terminating=False):
    request = _request(phase="commit", source_store_digest="c" * 64, plan_digest="d" * 64)
    cluster = Cluster(request)
    body = _module().build_governance_migration_job(request, recovery_envelope="signed-envelope")
    cluster.create_namespaced_job(request.metadata.resource_name, body)
    cluster.created.clear()
    if failed:
        cluster.job["status"] = {
            "failed": 1,
            "active": 0,
            "terminating": 0,
            "conditions": [{"type": "Failed", "status": "True"}],
        }
        cluster.job_pods[0]["status"]["phase"] = "Failed"
        cluster.job_pods[0]["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 1
    if terminating:
        cluster.job["metadata"]["deletionTimestamp"] = "2030-01-01T00:00:00Z"
    return request, cluster


async def _run(request, cluster, *, adapter=None, guard=_allow):
    return await (adapter or _adapter(cluster)).run(
        request, recovery_envelope="signed-envelope", effect_guard=guard
    )


@pytest.mark.parametrize("failed", [False, True])
async def test_exact_terminating_job_waits_for_pods_then_requests_replay(failed):
    request, cluster = _existing(failed=failed, terminating=True)
    adapter = _adapter(cluster)
    observations = []

    def pause(_):
        observations.append((cluster.job is None, bool(cluster.job_pods)))
        if cluster.job is not None:
            cluster.job = None
        else:
            cluster.job_pods = []

    adapter._sleep = pause
    with pytest.raises(DriverRetryable):
        await _run(request, cluster, adapter=adapter)
    assert observations == [(False, True), (True, True)]
    assert cluster.created == cluster.deleted == []
    assert cluster.job is None and cluster.job_pods == []


@pytest.mark.parametrize("pod_count", [0, 1, 2])
@pytest.mark.parametrize("sdk_model", [False, True])
async def test_exact_failed_commit_cleans_only_for_same_request_replay(pod_count, sdk_model):
    request, cluster = _existing(failed=True)
    cluster.model_job = sdk_model
    cluster.job_pods *= pod_count
    with pytest.raises(DriverRetryable):
        await _run(request, cluster)
    assert cluster.created == []
    assert len(cluster.deleted) == 1
    assert cluster.job is None and cluster.job_pods == []
    evidence = await _run(request, cluster)
    assert evidence.terminal["requestSha256"] == request.sha256
    assert evidence.terminal["phase"] == "commit"
    assert len(cluster.created) == 1


@pytest.mark.parametrize(
    "kind", ["counter-only", "failure-target", "active", "terminating", "running-pod"]
)
async def test_failure_before_terminal_stop_remains_pending_without_cleanup(kind):
    request, cluster = _existing(failed=True)
    if kind == "counter-only":
        cluster.job["status"].pop("conditions")
    elif kind == "failure-target":
        cluster.job["status"]["conditions"][0]["type"] = "FailureTarget"
    elif kind == "running-pod":
        cluster.job_pods[0]["status"]["phase"] = "Running"
    else:
        cluster.job["status"][kind] = 1
    with pytest.raises(DriverRetryable):
        await _run(request, cluster)
    assert cluster.created == cluster.deleted == []


@pytest.mark.parametrize("kind", ["owner", "image", "markers", "foreign-pvc-user"])
async def test_failed_cleanup_preserves_foreign_or_malformed_pods(kind):
    request, cluster = _existing(failed=True)
    pod = cluster.job_pods[0]
    if kind == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign-job"
    elif kind == "image":
        pod["spec"]["containers"][0]["image"] = "foreign:latest"
    elif kind == "markers":
        pod["metadata"]["labels"] = {}
    else:
        foreign = copy.deepcopy(pod)
        foreign["metadata"].update(name="another-job-pod", labels={}, annotations={})
        cluster.job_pods.append(foreign)
    with pytest.raises(MetadataConflict):
        await _run(request, cluster)
    assert cluster.created == cluster.deleted == []


@pytest.mark.parametrize("kind", ["ordinary", "stripped-labels", "foreign-pvc-user"])
async def test_empty_slot_does_not_create_over_leftover_pvc_or_slot_pods(kind):
    request, cluster = _existing()
    cluster.job = None
    if kind != "ordinary":
        cluster.job_pods[0]["metadata"]["labels"] = {}
    if kind == "foreign-pvc-user":
        cluster.job_pods[0]["metadata"].update(name="other-pod", ownerReferences=[])
    with pytest.raises(DriverRetryable):
        await _run(request, cluster)
    assert cluster.created == cluster.deleted == []


async def test_deletion_wait_rechecks_claim_before_each_observation():
    request, cluster = _existing(terminating=True)
    calls = 0

    async def guard():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise ClaimConflict("claim lost")

    with pytest.raises(ClaimConflict):
        await _run(request, cluster, guard=guard)
    assert cluster.created == cluster.deleted == []


async def test_failed_cleanup_rechecks_job_identity_before_delete():
    request, cluster = _existing(failed=True)
    reads = 0

    def replace():
        nonlocal reads
        reads += 1
        if reads == 2:
            cluster.job["metadata"]["uid"] = "replacement-job"

    cluster.read_hook = replace
    with pytest.raises(MetadataConflict):
        await _run(request, cluster)
    assert reads == 2
    assert cluster.created == cluster.deleted == []


@pytest.mark.parametrize("target", ["job", "pod"])
@pytest.mark.parametrize("field", ["labels", "annotations"])
async def test_failed_cleanup_rejects_extra_provisioner_metadata(target, field):
    request, cluster = _existing(failed=True)
    resource = cluster.job if target == "job" else cluster.job_pods[0]
    resource["metadata"][field]["exomem.io/foreign-purpose"] = "true"
    with pytest.raises(MetadataConflict):
        await _run(request, cluster)
    assert cluster.created == cluster.deleted == []


@pytest.mark.parametrize("status", [404, 409, 503])
async def test_cleanup_lost_delete_acknowledgement_reconciles_exact_slot(status):
    request, cluster = _existing(failed=True)
    delete = cluster.delete_namespaced_job

    class ProviderError(Exception):
        pass

    def lost_ack(*args, **kwargs):
        delete(*args, **kwargs)
        error = ProviderError("private-provider-payload")
        error.status = status
        raise error

    cluster.delete_namespaced_job = lost_ack
    with pytest.raises(DriverRetryable):
        await _run(request, cluster)
    assert cluster.job is None and cluster.job_pods == []
    assert cluster.created == [] and len(cluster.deleted) == 1


@pytest.mark.parametrize("kind", ["condition", "active", "extra-metadata"])
async def test_failed_cleanup_revalidates_latest_job_before_delete(kind):
    request, cluster = _existing(failed=True)
    reads = 0

    def mutate():
        nonlocal reads
        reads += 1
        if reads == 2:
            if kind == "condition":
                cluster.job["status"]["conditions"] = []
            elif kind == "active":
                cluster.job["status"]["active"] = 1
            else:
                cluster.job["metadata"]["annotations"]["exomem.io/foreign-purpose"] = "true"

    cluster.read_hook = mutate
    with pytest.raises(MetadataConflict if kind == "extra-metadata" else DriverRetryable):
        await _run(request, cluster)
    assert reads == 2
    assert cluster.created == cluster.deleted == []


@pytest.mark.parametrize("kind", ["read-error", "list-error", "partial-list"])
async def test_unproven_namespace_absence_never_submits_a_job(kind):
    request, cluster = _existing()
    cluster.job = None
    cluster.job_pods = []
    original_list = cluster.list_namespaced_pod

    class ProviderError(Exception):
        status = 503

    def error(*args, **kwargs):
        raise ProviderError("private-provider-payload")

    def list_pods(*args, **kwargs):
        if kwargs.get("label_selector") is None:
            if kind == "list-error":
                error()
            return SimpleNamespace(items=[], metadata=SimpleNamespace(_continue="next-page"))
        return original_list(*args, **kwargs)

    if kind == "read-error":
        cluster.read_namespaced_job = error
    else:
        cluster.list_namespaced_pod = list_pods
    with pytest.raises(DriverRetryable) as caught:
        await _run(request, cluster)
    assert "private-provider" not in str(caught.value)
    assert cluster.created == cluster.deleted == []


@pytest.mark.parametrize(
    "status",
    [
        {"failed": True},
        {"active": -1},
        {"conditions": [{"type": "Failed", "status": True}]},
        {"conditions": [{"type": "Failed", "status": "True"}] * 2},
        {
            "conditions": [
                {"type": "Failed", "status": "True"},
                {"type": "Complete", "status": "True"},
            ]
        },
        {"succeeded": 1},
    ],
)
async def test_invalid_or_contradictory_failure_evidence_remains_untouched(status):
    request, cluster = _existing(failed=True)
    cluster.job["status"].update(status)
    with pytest.raises(MetadataConflict):
        await _run(request, cluster)
    assert cluster.created == cluster.deleted == []


async def test_lost_deletion_ack_does_not_hide_a_lingering_unlabelled_pvc_pod():
    request, cluster = _existing(failed=True)

    class ProviderError(Exception):
        status = 503

    def lost_ack(*args, **kwargs):
        cluster.job = None
        cluster.job_pods[0]["metadata"]["labels"] = {}
        raise ProviderError("acknowledgement lost")

    cluster.delete_namespaced_job = lost_ack
    with pytest.raises(DriverRetryable):
        await _run(request, cluster)
    with pytest.raises(DriverRetryable):
        await _run(request, cluster)
    assert cluster.job is None and len(cluster.job_pods) == 1
    assert cluster.created == []
