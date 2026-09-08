from __future__ import annotations

import pytest
from test_governance_migration_job import Cluster, _adapter, _request

from exomem_provisioner.driver import DriverRetryable, DriverTerminal
from exomem_provisioner.repository import ClaimConflict, StaleFence


async def _allow():
    return None


async def test_migration_job_requires_current_effect_authority():
    request = _request()
    cluster = Cluster(request)
    with pytest.raises(DriverTerminal, match="^PROVISIONER_EFFECT_AUTHORITY_UNAVAILABLE$"):
        await _adapter(cluster).run(request, recovery_envelope="signed-envelope")
    assert cluster.created == cluster.deleted == []


@pytest.mark.parametrize("failure", [ClaimConflict, StaleFence, DriverTerminal])
@pytest.mark.parametrize("point", ["entry", "before-create", "before-delete", "after-delete"])
async def test_claim_loss_blocks_each_job_effect_and_preserves_failure_identity(failure, point):
    request = _request()
    cluster = Cluster(request)
    lost = point == "entry"
    refused = failure("AUTHORITY_LOST")

    def lose():
        nonlocal lost
        lost = True

    async def guard():
        if lost:
            raise refused

    if point == "before-create":
        read = cluster.read_namespaced_persistent_volume_claim

        def changed(*args, **kwargs):
            value = read(*args, **kwargs)
            lose()
            return value

        cluster.read_namespaced_persistent_volume_claim = changed
    elif point == "before-delete":
        cluster.create_hook = lose
    elif point == "after-delete":
        delete = cluster.delete_namespaced_job

        def changed(*args, **kwargs):
            delete(*args, **kwargs)
            lose()

        cluster.delete_namespaced_job = changed
    with pytest.raises(failure) as caught:
        await _adapter(cluster).run(
            request, recovery_envelope="signed-envelope", effect_guard=guard
        )
    assert caught.value is refused
    assert len(cluster.created) == int(point in {"before-delete", "after-delete"})
    assert len(cluster.deleted) == int(point == "after-delete")


@pytest.mark.parametrize("status", [0, 408, 409, 429, 500, 502, 503, 504])
@pytest.mark.parametrize(
    "method", ["read_namespaced_job", "create_namespaced_job", "delete_namespaced_job"]
)
async def test_ambiguous_provider_failure_is_retryable_and_content_free(status, method):
    request = _request()
    cluster = Cluster(request)

    class ProviderError(Exception):
        pass

    def unavailable(*args, **kwargs):
        error = ProviderError("private-provider-payload")
        error.status = status
        raise error

    setattr(cluster, method, unavailable)
    with pytest.raises(DriverRetryable) as caught:
        await _adapter(cluster).run(
            request, recovery_envelope="signed-envelope", effect_guard=_allow
        )
    assert str(caught.value) == "governance migration Job is temporarily unavailable"
    assert caught.value.__suppress_context__
    assert "private-provider-payload" not in str(caught.value)
    assert cluster.deleted == []


async def test_running_job_retains_the_fixed_slot_for_retry():
    request = _request()
    cluster = Cluster(request)
    cluster.create_hook = lambda: cluster.job.update(status={"active": 1})
    with pytest.raises(DriverRetryable):
        await _adapter(cluster).run(
            request, recovery_envelope="signed-envelope", effect_guard=_allow
        )
    assert len(cluster.created) == 1 and cluster.deleted == [] and cluster.job is not None
