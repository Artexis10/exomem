"""Real custody/proof adapters exercised through the live rollforward dispatch."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from test_governance_live_recovery import Missing, RecoveryHarness
from test_governance_readiness import METADATA, _proof
from test_governance_stopped_cell import _model

from exomem_provisioner.authorization_membership import inspect_hosted_authorization_bundle
from exomem_provisioner.driver import DriverPending, DriverTerminal, LostAcknowledgement
from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
from exomem_provisioner.repository import ClaimConflict


class RollforwardHarness(RecoveryHarness):
    def __init__(self):
        super().__init__()
        self.effects = []
        self.replicas = 0
        self.open_routes = False
        self.admitted = False
        self.helm_calls = []
        self.after_helm = lambda: None
        self.before_helm_guard = lambda: None
        self.plane._helm_requests[self.plane._key(self.current)]["provisionMode"] = "serve"

        async def acquire(metadata, operation_id, *, effect_guard):
            await effect_guard()
            self.lease.spec.renew_time = datetime.fromtimestamp(self.now, UTC)
            self.effects.append("maintenance")
            return True

        async def scale(metadata, replicas, *, effect_guard):
            await effect_guard()
            assert replicas == 0
            self.effects.append("stop")
            self.replicas = 0

        self.plane._maintenance.acquire = acquire
        self.plane._cell.scale = scale
        self.plane._helm = SimpleNamespace(transition_release=self.helm)
        self.plane._fingerprint = SimpleNamespace(fingerprint=self.fingerprint)
        self.plane._recovery_envelopes[self.plane._key(self.current)] = {"initJob": "job-envelope"}

        async def release(metadata, operation_id, *, effect_guard):
            await effect_guard()
            self.effects.append("release")

        self.plane._maintenance.release = release

    def custody(self):
        return inspect_hosted_authorization_bundle(
            self.files,
            expected_cell_id=METADATA.subject_id,
            expected_logical_vault_id=METADATA.tenant_id,
            expected_replica_id=METADATA.resource_name + "-0",
            expected_software_version=None,
            expected_schema_version=4,
            expected_recovery_envelope=self.envelope,
            now=self.now,
            _require_fresh=False,
        )

    async def helm(self, metadata, values, *, operation_id, rollback_on_failure, effect_guard):
        self.before_helm_guard()
        await effect_guard()
        assert rollback_on_failure is False
        assert values["image"] == self.config.image
        assert values["authorizationSessionRevision"] == self.custody().revision
        self.helm_calls.append(values)
        self.replicas = 1
        self.open_routes = values["routes"]["enabled"]
        self.effects.append("routes-open" if self.open_routes else "start")
        self.ready["governance"] = _proof(self.custody())
        self.after_helm()

    async def fingerprint(self, metadata, *, operation_id, phase, recovery_envelope, effect_guard):
        await effect_guard()
        self.effects.append("fingerprint-" + phase)
        return "a" * 64

    async def http(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/lifecycle/resume"):
            self.effects.append("resume")
            return SimpleNamespace(status_code=200, json=lambda: {"success": True, "data": {}})
        return await super().http(method, url, **kwargs)

    def read_namespaced_stateful_set(self, name, namespace):
        return _model(
            {
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "uid": "sts-alpha",
                    "resourceVersion": "1",
                    "annotations": METADATA.kubernetes_annotations,
                },
                "spec": {
                    "replicas": self.replicas,
                    "serviceName": name,
                    "selector": {"matchLabels": {"app": name}},
                    "template": {
                        "spec": {"containers": [{"name": "runtime", "image": self.config.image}]}
                    },
                },
            },
            "V1StatefulSet",
        )

    def read_namespace(self, name):
        result = super().read_namespace(name)
        result.metadata.uid = "namespace-alpha"
        result.metadata.resource_version = "1"
        result.metadata.name = name
        if self.admitted:
            result.metadata.annotations["exomem.io/runtime-admitted"] = "true"
        return result

    def patch_namespace(self, name, body):
        self.admitted = True
        self.effects.append("admit")

    def get_namespaced_custom_object(self, **kwargs):
        if not self.open_routes:
            raise Missing
        name = kwargs["name"]
        envelope = "controlIngressRoute" if name.endswith("-control") else "transferIngressRoute"
        return {
            "metadata": {
                "name": name,
                "namespace": kwargs["namespace"],
                "uid": name + "-uid",
                "resourceVersion": "1",
                "annotations": self.annotations_for(envelope),
            }
        }

    async def step(self):
        return await self.plane.governance_rollforward(self.current, self.request, self.context)

    def delete_namespaced_custom_object(self, **kwargs):
        self.open_routes = False

    async def drained(self):
        result = await self.step()
        self.context = replace(self.context, checkpoint=result.checkpoint)
        result = await self.step()
        self.context = replace(self.context, checkpoint=result.checkpoint)
        self.effects.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["complete", "confirmed"])
async def test_expired_target_dispatch_commits_recovery_then_reconciles_before_start(phase):
    h = RollforwardHarness()
    h.context = replace(
        h.context,
        checkpoint=replace(MigrationCheckpoint.decode(h.context.checkpoint), phase=phase).encode(),
    )
    result = await h.step()
    assert isinstance(result, DriverPending)
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "recover-" + phase
    assert h.effects == ["maintenance", "stop"]
    assert not h.patches
    h.context = replace(h.context, checkpoint=result.checkpoint)
    result = await h.step()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "complete"
    assert len(h.patches) == 1
    assert h.effects == ["maintenance", "stop", "maintenance", "stop"]


@pytest.mark.asyncio
async def test_live_target_claim_loss_prevents_all_lifecycle_effects():
    h = RollforwardHarness()
    h.claim_valid = False
    with pytest.raises(ClaimConflict):
        await h.step()
    assert not h.effects and not h.patches


@pytest.mark.asyncio
async def test_live_target_replacement_volume_refuses_before_maintenance_or_stop():
    h = RollforwardHarness()
    h.pvc_uid = "replacement"
    with pytest.raises(DriverTerminal, match="PROVISIONER_CHECKPOINT_INVALID"):
        await h.step()
    assert not h.effects and not h.patches


@pytest.mark.asyncio
async def test_live_target_lost_stop_ack_keeps_same_checkpoint():
    h = RollforwardHarness()

    async def scale(metadata, replicas, *, effect_guard):
        await effect_guard()
        raise LostAcknowledgement("stop acknowledgement lost")

    h.plane._cell.scale = scale
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert not h.patches


@pytest.mark.asyncio
async def test_initial_fingerprint_dispatch_reaches_real_coordinator_without_job():
    h = RollforwardHarness()
    h.context = replace(h.context, checkpoint="vault-fingerprinted-" + "a" * 64)
    # The durable denial checkpoint must precede every Job effect.
    h.plane._governance_migration._jobs = SimpleNamespace()
    h.plane._recovery_envelopes[h.plane._key(h.current)] = {"initJob": "job-envelope"}
    result = await h.step()
    checkpoint = MigrationCheckpoint.decode(result.checkpoint)
    assert checkpoint.phase == "inspect"
    assert checkpoint.vault_fingerprint == "a" * 64
    assert not h.patches


@pytest.mark.asyncio
async def test_recovered_target_starts_then_admits_with_fresh_private_proof():
    h = RollforwardHarness()
    await h.drained()
    result = await h.step()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "confirmed"
    assert h.effects == ["maintenance", "stop", "start", "fingerprint-after", "resume"]
    assert not h.open_routes and not h.admitted
    h.context = replace(h.context, checkpoint=result.checkpoint)
    final = await h.step()
    assert final.result["code"] == "rollforward_preserved"
    assert h.open_routes and h.admitted
    assert h.effects[-4:] == ["maintenance", "admit", "routes-open", "release"]
    assert h.custody().replica_state == "SERVING"


@pytest.mark.asyncio
async def test_changed_vault_after_start_stops_before_resume_or_admission():
    h = RollforwardHarness()
    await h.drained()

    async def changed_fingerprint(*args, effect_guard, **kwargs):
        await effect_guard()
        return "f" * 64

    h.plane._fingerprint.fingerprint = changed_fingerprint
    with pytest.raises(DriverTerminal, match="PROVISIONER_VAULT_PRESERVATION_MISMATCH"):
        await h.step()
    assert "start" in h.effects
    assert "resume" not in h.effects
    assert h.replicas == 0 and not h.open_routes and not h.admitted


@pytest.mark.asyncio
async def test_lost_target_start_ack_replays_without_another_serving_publication():
    h = RollforwardHarness()
    await h.drained()

    def lost():
        raise LostAcknowledgement("start applied")

    h.after_helm = lost
    result = await h.step()
    assert result == DriverPending(h.context.checkpoint, 30)
    revision = h.custody().revision
    h.after_helm = lambda: None
    result = await h.step()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "confirmed"
    assert h.custody().revision == revision


@pytest.mark.asyncio
async def test_expiry_during_helm_start_does_not_publish_confirmation():
    h = RollforwardHarness()
    await h.drained()
    h.after_helm = lambda: setattr(h, "now", h.custody().expires_at + 1)
    result = await h.step()
    assert result == DriverPending(h.context.checkpoint, 30)
    assert not h.admitted and not h.open_routes


@pytest.mark.asyncio
async def test_claim_loss_inside_helm_predecessor_reads_prevents_start():
    h = RollforwardHarness()
    await h.drained()
    h.before_helm_guard = lambda: setattr(h, "claim_valid", False)
    with pytest.raises(ClaimConflict):
        await h.step()
    assert not h.helm_calls and not h.open_routes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before,after",
    [
        ("effect-prepared", "maintenance-acquired"),
        ("maintenance-acquired", "routes-closed"),
        ("runtime-drained", "runtime-stop-wait"),
        ("runtime-stop-wait", "runtime-stopped"),
        ("runtime-stopped", "vault-fingerprinted-" + "a" * 64),
    ],
)
async def test_pre_migration_dispatch_reuses_guarded_maintenance_stop_and_fingerprint(
    before, after
):
    h = RollforwardHarness()
    h.context = replace(h.context, checkpoint=before)
    result = await h.step()
    assert result.checkpoint == after
    assert not h.patches and not h.helm_calls


@pytest.mark.asyncio
async def test_closed_routes_dispatch_requires_guarded_source_quiescence():
    h = RollforwardHarness()
    h.context = replace(h.context, checkpoint="routes-closed")
    calls = []

    async def quiesce(metadata, request, operation_id, *, effect_guard):
        await effect_guard()
        calls.append(operation_id)

    h.plane.quiesce = quiesce
    assert (await h.step()).checkpoint == "runtime-drained"
    assert calls == [h.context.provider_operation_id]


@pytest.mark.asyncio
async def test_unknown_checkpoint_never_restarts_migration():
    h = RollforwardHarness()
    h.context = replace(h.context, checkpoint="migration-complete-" + "a" * 64)
    with pytest.raises(DriverTerminal, match="PROVISIONER_CHECKPOINT_INVALID"):
        await h.step()
    assert not h.helm_calls and not h.patches


@pytest.mark.asyncio
async def test_route_ack_loss_retains_confirmation_and_expiry_recovers_same_migration():
    h = RollforwardHarness()
    await h.drained()
    result = await h.step()
    h.context = replace(h.context, checkpoint=result.checkpoint)

    def lost():
        raise LostAcknowledgement("routes applied")

    h.after_helm = lost
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert h.open_routes
    h.now = h.custody().expires_at + 1
    h.after_helm = lambda: None
    result = await h.step()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "recover-confirmed"
    assert not h.open_routes


@pytest.mark.asyncio
async def test_changed_private_proof_prevents_admission():
    h = RollforwardHarness()
    await h.drained()
    result = await h.step()
    h.context = replace(h.context, checkpoint=result.checkpoint)
    h.ready["governance"]["activationEpoch"] += 1
    from exomem_provisioner.lifecycle import MetadataConflict

    with pytest.raises(MetadataConflict):
        await h.step()
    assert not h.admitted and not h.open_routes


@pytest.mark.asyncio
async def test_expiry_inside_post_start_route_proof_prevents_resume():
    h = RollforwardHarness()
    await h.drained()

    async def probe(*args):
        if "start" in h.effects:
            h.now = h.custody().expires_at + 1
            h.lease.spec.renew_time = datetime.fromtimestamp(h.now, UTC)
        return 404

    h.plane._routes._probe = probe
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert "resume" not in h.effects


@pytest.mark.asyncio
@pytest.mark.parametrize("at_routes", [False, True])
async def test_long_successful_helm_outlasting_maintenance_keeps_checkpoint(at_routes):
    h = RollforwardHarness()
    await h.drained()
    if at_routes:
        result = await h.step()
        h.context = replace(h.context, checkpoint=result.checkpoint)
    h.after_helm = lambda: setattr(h, "now", h.now + 121)
    assert await h.step() == DriverPending(h.context.checkpoint, 30)


@pytest.mark.asyncio
async def test_expected_runtime_termination_keeps_stop_checkpoint():
    h = RollforwardHarness()
    h.context = replace(h.context, checkpoint="runtime-stop-wait")
    h.pods = [
        _model(
            {
                "metadata": {
                    "name": METADATA.resource_name + "-0",
                    "namespace": METADATA.resource_name,
                    "uid": "runtime-pod",
                    "annotations": METADATA.kubernetes_annotations,
                    "ownerReferences": [
                        {
                            "kind": "StatefulSet",
                            "apiVersion": "apps/v1",
                            "name": METADATA.resource_name,
                            "uid": "sts-alpha",
                            "controller": True,
                        }
                    ],
                },
                "spec": {
                    "containers": [{"name": "runtime", "image": h.config.image}],
                    "volumes": [
                        {
                            "name": "data",
                            "persistentVolumeClaim": {
                                "claimName": METADATA.resource_name + "-data"
                            },
                        }
                    ],
                },
            },
            "V1Pod",
        )
    ]
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert not h.patches and not h.helm_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["proof", "key-expired"])
async def test_failed_confirmation_replay_closes_previously_published_routes(failure):
    h = RollforwardHarness()
    await h.drained()
    result = await h.step()
    h.context = replace(h.context, checkpoint=result.checkpoint)
    h.open_routes = True
    if failure == "proof":
        h.ready["governance"]["activationEpoch"] += 1
    else:
        h.now = json.loads(h.custody().keyring)["accepted_keys"][0]["not_after"]
    from exomem_provisioner.lifecycle import MetadataConflict

    with pytest.raises(MetadataConflict):
        await h.step()
    assert not h.open_routes and h.replicas == 0


@pytest.mark.asyncio
async def test_failed_confirmation_cannot_close_routes_after_claim_loss():
    h = RollforwardHarness()
    await h.drained()
    result = await h.step()
    h.context = replace(h.context, checkpoint=result.checkpoint)
    h.open_routes = True
    h.claim_valid = False
    with pytest.raises(ClaimConflict):
        await h.step()
    assert h.open_routes


@pytest.mark.asyncio
async def test_authorization_expiry_during_private_readiness_keeps_recovery_checkpoint():
    h = RollforwardHarness()
    await h.drained()
    result = await h.step()
    h.context = replace(h.context, checkpoint=result.checkpoint)

    def elapsed():
        h.now = h.custody().expires_at + 1
        h.lease.spec.renew_time = datetime.fromtimestamp(h.now, UTC)

    h.after_ready = elapsed
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert not h.admitted and not h.open_routes


@pytest.mark.asyncio
async def test_production_httpx_resume_timeout_keeps_same_migration_checkpoint():
    h = RollforwardHarness()
    await h.drained()
    original = h.http

    async def timeout(method, url, **kwargs):
        if url.endswith("/lifecycle/resume"):
            raise httpx.ReadTimeout("private acknowledgement lost")
        return await original(method, url, **kwargs)

    h.plane._runtime._request = timeout
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert not h.open_routes


@pytest.mark.asyncio
async def test_private_readiness_outage_retries_same_confirmation_checkpoint():
    h = RollforwardHarness()
    await h.drained()
    result = await h.step()
    h.context = replace(h.context, checkpoint=result.checkpoint)
    original = h.http

    async def unavailable(method, url, **kwargs):
        if url.endswith("/ready"):
            return SimpleNamespace(status_code=503)
        return await original(method, url, **kwargs)

    h.plane._runtime._request = unavailable
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert not h.open_routes and not h.admitted


@pytest.mark.asyncio
async def test_external_probe_transport_outage_prevents_start_and_keeps_checkpoint():
    h = RollforwardHarness()
    await h.drained()

    async def timeout(*args):
        raise httpx.ReadTimeout("private probe destination")

    h.plane._routes._probe = timeout
    assert await h.step() == DriverPending(h.context.checkpoint, 30)
    assert not h.helm_calls and not h.open_routes
