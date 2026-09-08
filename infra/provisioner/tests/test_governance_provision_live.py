"""Fresh storage stays offline until the shared governance coordinator completes."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_governance_live_recovery import Missing
from test_governance_readiness import METADATA, NOW, SOFTWARE_VERSION
from test_governance_rollforward_live import RollforwardHarness

from exomem_provisioner.authorization_membership import (
    build_initial_hosted_authorization_bundle,
    inspect_hosted_authorization_bundle,
)
from exomem_provisioner.driver import (
    DriverFinal,
    DriverPending,
    DriverTerminal,
    LostAcknowledgement,
)
from exomem_provisioner.governance_migration_checkpoint import (
    MigrationCheckpoint,
    migration_binding,
)
from exomem_provisioner.lifecycle import MetadataConflict
from exomem_provisioner.repository import ClaimConflict


class InitialEffects:
    def __init__(self, h):
        self.h = h

    async def require_active(self, **kwargs):
        self.h.effects.append("capacity")

    async def ensure_release(self, metadata, values, *, rollback_on_failure, effect_guard):
        await effect_guard()
        assert rollback_on_failure is False
        assert values["workloadMode"] in {"initialize", "restore"}
        assert values["migrationMode"] == "none"
        assert values["routes"]["enabled"] is False
        self.h.helm_calls.append(values)
        self.h.effects.append(
            "initialize" if values["workloadMode"] == "initialize" else "storage-shell"
        )


class ProvisionHarness(RollforwardHarness):
    def __init__(self):
        super().__init__()
        self.current = METADATA
        self.now = NOW + 5
        self.context = replace(
            self.context,
            provider_operation_id=METADATA.operation_id,
            fence_generation=METADATA.fence_generation,
            checkpoint="volume-owned",
        )
        self.request["provisionMode"] = "serve"
        self.lease.metadata.annotations = METADATA.kubernetes_annotations
        self.lease.spec.holder_identity = METADATA.operation_id
        self.plane._recovery_envelopes[self.plane._key(self.current)] = self.envelopes
        self.files = build_initial_hosted_authorization_bundle(
            cell_id=METADATA.subject_id,
            logical_vault_id=METADATA.tenant_id,
            replica_id=METADATA.resource_name + "-0",
            software_version=SOFTWARE_VERSION,
            schema_version=3,
            recovery_envelope=self.envelope,
            now=NOW,
        ).files
        self.init_complete = False
        self.cleanup_lost = False
        self.plane._storage_init = SimpleNamespace(completed=self.completed, cleanup=self.cleanup)
        initial = InitialEffects(self)
        self.plane._capacity = initial
        self.plane._operation_ids[self.plane._key(self.current)] = self.context.operation_id
        self.plane._helm.ensure_release = initial.ensure_release
        self.plane._fingerprint.prove_stopped_before_fingerprint = self.prove_fingerprint

    def read_namespaced_stateful_set(self, *args):
        if self.replicas:
            return super().read_namespaced_stateful_set(*args)
        raise Missing

    async def prove_fingerprint(self, *args, pvc_uid, **kwargs):
        await self.plane._cell.verify_governance_stopped(self.current, pvc_uid=pvc_uid)

    async def completed(self, *args, effect_guard, **kwargs):
        await effect_guard()
        self.effects.append("init-observed")
        return self.init_complete

    async def cleanup(self, *args, effect_guard, **kwargs):
        await effect_guard()
        self.effects.append("init-cleanup")
        if self.cleanup_lost:
            raise LostAcknowledgement("cleanup outcome uncertain")

    def custody(self):
        return inspect_hosted_authorization_bundle(
            self.files,
            expected_cell_id=METADATA.subject_id,
            expected_logical_vault_id=METADATA.tenant_id,
            expected_replica_id=METADATA.resource_name + "-0",
            expected_software_version=SOFTWARE_VERSION,
            expected_schema_version=None,
            expected_recovery_envelope=self.envelope,
            now=self.now,
            _require_fresh=False,
        )

    def bound_checkpoint(self, phase):
        return (
            "gpi1:"
            + phase
            + ":"
            + migration_binding(self.context, pvc_uid=self.pvc_uid, runtime_image=self.config.image)
        )

    async def step(self):
        return await self.plane.governance_provision(self.current, self.request, self.context)


@pytest.mark.asyncio
async def test_fresh_init_is_observed_without_starting_runtime_or_minting_custody():
    h = ProvisionHarness()
    result = await h.step()
    assert isinstance(result, DriverPending)
    assert result.checkpoint == h.bound_checkpoint("initializing")
    assert not h.patches and not h.helm_calls
    assert "start" not in h.effects


@pytest.mark.asyncio
async def test_successful_init_is_checkpointed_before_cleanup_or_draining_publication():
    h = ProvisionHarness()
    h.init_complete = True
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("initializing"))
    result = await h.step()
    assert result.checkpoint == h.bound_checkpoint("complete")
    assert "init-cleanup" not in h.effects and not h.patches


@pytest.mark.asyncio
async def test_completed_init_is_cleaned_then_drained_without_starting_runtime():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))
    result = await h.step()
    assert result.checkpoint == h.bound_checkpoint("drained")
    assert h.custody().replica_state == "DRAINING"
    assert h.custody().no_in_flight and not h.custody().governance_enrolled
    assert h.custody().membership_schema_version == 3
    assert len(h.patches) == 1 and "init-cleanup" in h.effects
    assert not h.helm_calls and not h.admitted and not h.open_routes


@pytest.mark.asyncio
async def test_init_cleanup_uncertainty_retains_bound_checkpoint_and_custody():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))
    h.cleanup_lost = True
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert not h.patches and not h.helm_calls


@pytest.mark.asyncio
async def test_fresh_provision_replacement_pvc_refuses_before_any_effect():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))
    h.pvc_uid = "replacement"
    with pytest.raises(DriverTerminal, match="PROVISIONER_CHECKPOINT_INVALID"):
        await h.step()
    assert not h.effects and not h.patches


@pytest.mark.asyncio
async def test_fresh_provision_claim_loss_prevents_every_effect():
    h = ProvisionHarness()
    h.claim_valid = False
    with pytest.raises(ClaimConflict):
        await h.step()
    assert not h.effects and not h.patches


@pytest.mark.asyncio
async def test_expired_ttl_init_job_replays_only_original_offline_initializer():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("initializing"))
    result = await h.step()
    assert result == DriverPending(h.context.checkpoint, 30)
    assert len(h.helm_calls) == 1 and h.effects[-1] == "initialize"
    assert not h.patches and not h.admitted and not h.open_routes


@pytest.mark.asyncio
async def test_completed_checkpoint_never_replays_initializer():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))
    await h.step()
    assert "initialize" not in h.effects


@pytest.mark.asyncio
async def test_prebinding_release_is_recorded_without_calling_legacy_initialize():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint="namespace-ready")

    async def credentials(*args, effect_guard, **kwargs):
        await effect_guard()
        h.effects.append("credentials")

    h.plane._cell.write_credential_bundle = credentials
    result = await h.step()
    assert result.checkpoint == "release-applied"
    assert [resource.kind.value for resource in result.resources] == ["helm-release", "pvc"]
    assert h.effects[-1] == "storage-shell" and not h.patches
    assert h.helm_calls[0]["workloadMode"] == "restore"


@pytest.mark.asyncio
async def test_first_initializer_submission_has_a_durable_bound_denial_marker():
    h = ProvisionHarness()
    result = await h.step()
    assert result.checkpoint == h.bound_checkpoint("initializing")
    assert not h.helm_calls
    h.context = replace(h.context, checkpoint=result.checkpoint)
    result = await h.step()
    assert result.checkpoint == h.context.checkpoint
    assert h.helm_calls[0]["workloadMode"] == "initialize"


@pytest.mark.asyncio
async def test_provision_rejects_foreign_owner_before_initializer_replay():
    h = ProvisionHarness()
    h.plane._owned[h.plane._key(h.current)] = replace(METADATA, operation_id="foreign")
    with pytest.raises(DriverTerminal, match="PROVISIONER_CHECKPOINT_INVALID"):
        await h.step()
    assert not h.effects and not h.patches


@pytest.mark.asyncio
async def test_expired_bootstrap_drains_without_genesis_reset():
    h = ProvisionHarness()
    before = h.custody()
    h.now = before.expires_at + 1
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))
    result = await h.step()
    assert result.checkpoint == h.bound_checkpoint("drained")
    after = h.custody()
    assert after.keyring == before.keyring and after.epoch == before.epoch + 1
    assert after.replica_state == "DRAINING" and not after.governance_enrolled


@pytest.mark.asyncio
async def test_lost_draining_ack_reconciles_same_revision_even_after_window_elapsed():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))
    await h.step()
    successor = h.custody()
    h.now = successor.expires_at + 1
    result = await h.step()
    assert result.checkpoint == h.bound_checkpoint("drained")
    assert h.custody().files == successor.files and len(h.patches) == 1


@pytest.mark.asyncio
async def test_drained_provision_enters_same_coordinator_without_losing_volume_binding():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))
    result = await h.step()
    h.context = replace(h.context, checkpoint=result.checkpoint)
    result = await h.step()
    checkpoint = MigrationCheckpoint.decode(result.checkpoint)
    assert checkpoint.phase == "inspect"
    assert checkpoint.binding == migration_binding(
        h.context, pvc_uid=h.pvc_uid, runtime_image=h.config.image
    )
    assert checkpoint.vault_fingerprint == "a" * 64
    assert "start" not in h.effects and not h.admitted and not h.open_routes


@pytest.mark.asyncio
async def test_fresh_final_uses_shared_readiness_admission_and_records_routes():
    from test_governance_readiness import _serving_bundle

    h = ProvisionHarness()
    h.files = _serving_bundle(recovery_envelope=h.envelope).files
    h.context = replace(
        h.context,
        checkpoint=MigrationCheckpoint(
            "complete",
            "a" * 64,
            migration_binding(h.context, pvc_uid=h.pvc_uid, runtime_image=h.config.image),
            "b" * 64,
            "c" * 64,
        ).encode(),
    )
    result = await h.step()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "confirmed"
    assert not h.admitted and not h.open_routes
    h.context = replace(h.context, checkpoint=result.checkpoint)
    final = await h.step()
    assert isinstance(final, DriverFinal)
    assert set(final.result) == {"providerRef", "privateEndpoint"}
    assert [resource.kind.value for resource in final.resources] == ["route"]
    assert h.admitted and h.open_routes


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["claim", "pvc", "routes", "admission", "pod"])
async def test_bootstrap_cas_rechecks_physical_authority_after_secret_reads(change):
    from test_governance_stopped_cell import _pod

    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("complete"))

    def interfere():
        if change == "claim":
            h.claim_valid = False
        elif change == "pvc":
            h.pvc_uid = "replacement"
        elif change == "routes":
            h.open_routes = True
        elif change == "admission":
            h.admitted = True
        else:
            h.pods = [
                _pod(
                    volumes=[
                        {
                            "name": "data",
                            "persistentVolumeClaim": {
                                "claimName": METADATA.resource_name + "-data"
                            },
                        }
                    ]
                )
            ]

    h.after_secret_read = interfere
    with pytest.raises((ClaimConflict, MetadataConflict, DriverTerminal)):
        await h.step()
    assert not h.patches and not h.helm_calls


@pytest.mark.asyncio
async def test_initializer_lost_ack_keeps_bound_marker_and_never_starts_service():
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("initializing"))
    apply = h.plane._helm.ensure_release

    async def lost(*args, **kwargs):
        await apply(*args, **kwargs)
        raise LostAcknowledgement("initializer applied")

    h.plane._helm.ensure_release = lost
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert h.effects[-1] == "initialize" and not h.open_routes and not h.admitted
