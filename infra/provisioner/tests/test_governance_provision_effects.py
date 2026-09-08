"""Bootstrap creates cannot overwrite custody or outlive their worker claim."""

import base64

import pytest
from test_governance_live_recovery import Missing
from test_governance_readiness import METADATA, NOW, SOFTWARE_VERSION

from exomem_provisioner.adapters import KubernetesCellAdapter
from exomem_provisioner.authorization_membership import build_initial_hosted_authorization_bundle
from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.repository import ClaimConflict


async def allow():
    pass


async def lost():
    raise ClaimConflict("claim lost")


class Core:
    def __init__(self):
        self.secret = None
        self.creates = []
        self.patches = []

    def read_namespaced_secret(self, *args):
        if self.secret is None:
            raise Missing
        return self.secret

    def create_namespaced_secret(self, namespace, body):
        self.creates.append(body)

    def patch_namespaced_secret(self, *args):
        self.patches.append(args)
        raise AssertionError("bootstrap must not patch an existing Secret")


def bootstrap():
    return build_initial_hosted_authorization_bundle(
        cell_id=METADATA.subject_id,
        logical_vault_id=METADATA.tenant_id,
        replica_id=METADATA.resource_name + "-0",
        software_version=SOFTWARE_VERSION,
        schema_version=3,
        recovery_envelope="original-envelope",
        now=NOW,
    )


async def create_custody(adapter, guard):
    bundle = bootstrap()
    await adapter.write_authorization_session_bundle(
        METADATA,
        bundle.files,
        recovery_envelope="original-envelope",
        membership_epoch=bundle.epoch,
        membership_digest=bundle.membership_digest,
        revision=bundle.revision,
        create_only=True,
        effect_guard=guard,
    )


@pytest.mark.asyncio
async def test_guarded_genesis_creates_without_patch():
    core = Core()
    await create_custody(KubernetesCellAdapter(core_v1=core, apps_v1=None), allow)
    assert len(core.creates) == 1 and not core.patches


@pytest.mark.asyncio
async def test_guarded_genesis_claim_loss_prevents_create():
    core = Core()
    with pytest.raises(ClaimConflict):
        await create_custody(KubernetesCellAdapter(core_v1=core, apps_v1=None), lost)
    assert not core.creates and not core.patches


@pytest.mark.asyncio
async def test_guarded_genesis_conflict_is_uncertain_never_patch():
    class Conflict(Exception):
        status = 409

    core = Core()

    def conflict(*args):
        raise Conflict

    core.create_namespaced_secret = conflict
    with pytest.raises(DriverRetryable):
        await create_custody(KubernetesCellAdapter(core_v1=core, apps_v1=None), allow)
    assert not core.patches


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [allow, lost])
async def test_guarded_bootstrap_credential_create_honors_authority(guard):
    core = Core()
    adapter = KubernetesCellAdapter(core_v1=core, apps_v1=None)
    args = {
        "lifecycle_annotations": {"exomem.io/recovery-envelope": "credential-envelope"},
        "effect_guard": guard,
    }
    credential = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    if guard is lost:
        with pytest.raises(ClaimConflict):
            await adapter.write_credential_bundle(METADATA, {"1": credential}, **args)
        assert not core.creates
    else:
        await adapter.write_credential_bundle(METADATA, {"1": credential}, **args)
        assert len(core.creates) == 1
    assert not core.patches
