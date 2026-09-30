"""Owned init cleanup can be observed without relaxing ordinary provider recovery."""

import pytest
from test_governance_migration_provider_barrier import _envelopes, _registry
from test_live_provider import _metadata

from exomem_provisioner.lifecycle import MetadataConflict


def registry():
    result, job = _registry(terminating=True)
    metadata = _metadata()
    job.metadata.annotations = metadata.kubernetes_annotations | {
        "exomem.io/recovery-envelope": _envelopes(metadata)["initJob"],
    }
    job.metadata.labels = {
        "app.kubernetes.io/name": "exomem-cell",
        "exomem.io/cell": metadata.resource_name,
        "exomem.io/storage-init": "true",
    }
    return result, job


async def test_governed_read_can_observe_exact_owned_terminating_init_job():
    adapter, _ = registry()
    snapshot = await adapter.inspect(_metadata(), _metadata(), allow_owned_job_cleanup=True)
    assert snapshot.init_job_present
    assert not snapshot.init_complete and not snapshot.serving


async def test_ordinary_observation_still_refuses_terminating_init_job():
    adapter, _ = registry()
    with pytest.raises(MetadataConflict):
        await adapter.inspect(_metadata(), _metadata())


@pytest.mark.parametrize(
    "field", ["envelope", "owner", "name", "namespace", "uid", "resource_version"]
)
async def test_governed_read_refuses_unowned_or_malformed_job(field):
    adapter, job = registry()
    if field == "envelope":
        job.metadata.annotations["exomem.io/recovery-envelope"] = "forged"
    elif field == "owner":
        job.metadata.annotations["exomem.io/operation-id"] = "foreign"
    else:
        setattr(job.metadata, field, None if field in {"uid", "resource_version"} else "foreign")
    with pytest.raises(MetadataConflict):
        await adapter.inspect(_metadata(), _metadata(), allow_owned_job_cleanup=True)
