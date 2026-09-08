"""Recovery exercises real live, registry, custody, lease and route adapters."""

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kubernetes.client import V1Lease, V1LeaseSpec, V1ListMeta, V1ObjectMeta, V1PodList
from test_governance_live_readiness import ReadinessHarness
from test_governance_readiness import METADATA
from test_governance_stopped_cell import _model

from exomem_provisioner.adapters import KubernetesMaintenanceLeaseAdapter, TraefikRoutingAdapter
from exomem_provisioner.driver import DriverPending, EffectContext
from exomem_provisioner.governance_migration_checkpoint import (
    MigrationCheckpoint,
    migration_binding,
)
from exomem_provisioner.governance_migration_coordinator import HostedGovernanceMigrationCoordinator
from exomem_provisioner.lifecycle import MetadataConflict
from exomem_provisioner.live import KubernetesProviderRegistry
from exomem_provisioner.provider_identity import ProviderRecoveryIdentityCodec
from exomem_provisioner.repository import ClaimConflict
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2


class Missing(Exception):
    status = 404


class RecoveryHarness(ReadinessHarness):
    def __init__(self):
        super().__init__()
        self.now = self.bundle.expires_at + 1
        self.envelopes = self.plane._helm_requests[self.plane._key(self.current)][
            "_providerRecoveryEnvelopes"
        ]
        self.rv = 1
        self.patches = []
        self.pvc_uid = "pvc-alpha"
        self.pods = []
        self.probe_status = 404
        self.claim_valid = True
        self.after_secret_read = lambda: None
        self.events = []
        self.lease = V1Lease(
            metadata=V1ObjectMeta(
                name=METADATA.resource_name + "-maintenance",
                namespace=METADATA.resource_name,
                uid="lease-alpha",
                resource_version="1",
                annotations=self.current.kubernetes_annotations,
            ),
            spec=V1LeaseSpec(
                holder_identity=self.current.operation_id,
                lease_duration_seconds=120,
                renew_time=datetime.fromtimestamp(self.now, UTC),
            ),
        )
        verifier = ProviderRecoveryIdentityCodec.from_secret(
            "readiness-test-provider-root"
        ).verifier()
        self.plane._registry = KubernetesProviderRegistry(
            core_v1=self,
            apps_v1=self,
            batch_v1=self,
            custom_objects=self,
            identity_verifier=verifier,
        )
        self.plane._cell._apps = self
        self.plane._maintenance = KubernetesMaintenanceLeaseAdapter(
            coordination_v1=self,
            now=lambda: datetime.fromtimestamp(self.now, UTC),
        )
        self.plane._routes = TraefikRoutingAdapter(
            custom_objects=self,
            control_hostname=self.config.control_hostname,
            transfer_hostname=self.config.transfer_hostname,
            probe=self.probe,
        )
        self.plane._governance_migration = HostedGovernanceMigrationCoordinator(
            cell=self.plane._cell,
            jobs=SimpleNamespace(),
            now=lambda: self.now,
        )
        context = EffectContext(
            "internal-upgrade",
            self.current.operation_id,
            self.current.tenant_id,
            self.current.subject_id,
            self.current.fence_generation,
            wire_protocol=WIRE_PROTOCOL_V2,
            effect_guard=self.guard,
        )
        self.context = replace(
            context,
            checkpoint=MigrationCheckpoint(
                "confirmed",
                "a" * 64,
                migration_binding(context, pvc_uid=self.pvc_uid, runtime_image=self.config.image),
                "b" * 64,
                "c" * 64,
            ).encode(),
        )

    async def guard(self):
        self.events.append("claim")
        if not self.claim_valid:
            raise ClaimConflict("claim lost")

    def annotations_for(self, key):
        return METADATA.kubernetes_annotations | {
            "exomem.io/recovery-envelope": self.envelopes[key]
        }

    def read_namespace(self, name):
        return SimpleNamespace(
            metadata=SimpleNamespace(annotations=self.annotations_for("namespace"))
        )

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        self.events.append("pvc")
        return _model(
            {
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "uid": self.pvc_uid,
                    "annotations": self.annotations_for("vaultPvc"),
                },
                "spec": {"volumeName": "pv-alpha"},
                "status": {"phase": "Bound"},
            },
            "V1PersistentVolumeClaim",
        )

    def read_namespaced_stateful_set(self, *args):
        raise Missing

    def read_namespaced_job(self, *args):
        raise Missing

    def list_namespaced_config_map(self, *args, **kwargs):
        return SimpleNamespace(items=[])

    def list_namespaced_pod(self, *args, **kwargs):
        self.events.append("pods-all" if not kwargs else "pods-selected")
        return V1PodList(items=self.pods if not kwargs else [], metadata=V1ListMeta())

    def get_namespaced_custom_object(self, **kwargs):
        self.events.append("route")
        raise Missing

    def read_namespaced_lease(self, *args):
        self.events.append("lease")
        return self.lease

    async def probe(self, *args):
        self.events.append("probe")
        return self.probe_status

    def read_namespaced_secret(self, name, namespace):
        if name == "exomem-cell-credentials":
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    annotations=self.annotations_for("credentialSecret")
                    | {"exomem.io/active-credential-version": "1"}
                ),
                data={
                    "credentials.json": base64.b64encode(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "credentials": {"1": self.request["serviceCredential"]},
                            }
                        ).encode()
                    ).decode()
                },
            )
        result = super().read_namespaced_secret(name, namespace)
        result.metadata.uid = "secret-alpha"
        result.metadata.resource_version = str(self.rv)
        result.metadata.annotations = result.metadata.annotations | {
            "exomem.io/authorization-session-revision": hashlib.sha256(
                self.files["keyring.json"]
                + self.files["control.json"]
                + self.files["serving-membership.json"]
            ).hexdigest()
        }
        self.events.append("secret")
        self.after_secret_read()
        return result

    def patch_namespaced_secret(self, name, namespace, body):
        assert body["metadata"]["uid"] == "secret-alpha"
        assert body["metadata"]["resourceVersion"] == str(self.rv)
        self.events.append("patch")
        self.patches.append(body)
        self.files = {key: value.encode() for key, value in body["stringData"].items()}
        self.rv += 1

    async def recover(self):
        return await self.plane.recover_governance_target(self.current, self.request, self.context)


@pytest.mark.asyncio
async def test_live_recovery_uses_fresh_real_proofs_and_original_owner_cas():
    h = RecoveryHarness()
    result = await h.recover()
    assert isinstance(result, DriverPending) and not h.patches
    h.context = replace(h.context, checkpoint=result.checkpoint)
    result = await h.recover()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "complete"
    assert len(h.patches) == 1
    assert (
        h.patches[0]["metadata"]["annotations"]["exomem.io/operation-id"] == METADATA.operation_id
    )
    before_patch = h.events[: h.events.index("patch")]
    after_read = before_patch[
        1 + max(i for i, event in enumerate(before_patch) if event == "secret") :
    ]
    for required in ("claim", "lease", "route", "probe", "pvc", "pods-all"):
        assert required in after_read


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["claim", "lease", "pvc", "pod", "probe"])
async def test_live_recovery_rechecks_each_authority_after_secret_predecessor_read(failure):
    h = RecoveryHarness()
    result = await h.recover()
    h.context = replace(h.context, checkpoint=result.checkpoint)

    def change():
        if failure == "claim":
            h.claim_valid = False
        elif failure == "lease":
            h.lease.spec.holder_identity = "foreign"
        elif failure == "pvc":
            h.pvc_uid = "replacement"
        elif failure == "probe":
            h.probe_status = 200
        else:
            h.pods = [
                _model(
                    {
                        "metadata": {
                            "name": "unlabelled",
                            "namespace": METADATA.resource_name,
                            "uid": "pod-alpha",
                        },
                        "spec": {
                            "containers": [{"name": "runtime", "image": "example"}],
                            "volumes": [
                                {
                                    "name": "vault",
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

    h.after_secret_read = change
    with pytest.raises((MetadataConflict, ClaimConflict)):
        await h.recover()
    assert not h.patches


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "binding-v1-to-v2", "state-root-v1"])
async def test_live_recovery_cannot_be_used_by_ordinary_modes(mode):
    h = RecoveryHarness()
    h.plane._config = replace(h.config, migration_mode=mode)
    with pytest.raises(MetadataConflict):
        await h.recover()
    assert not h.patches


@pytest.mark.asyncio
async def test_delayed_exact_cas_between_predecessor_reads_is_reconciled_not_terminal():
    from test_governance_readiness import SOFTWARE_VERSION, _identity

    from exomem_provisioner.governance_target_recovery import recover_expired_serving_bundle

    h = RecoveryHarness()
    result = await h.recover()
    checkpoint = MigrationCheckpoint.decode(result.checkpoint)
    h.context = replace(h.context, checkpoint=result.checkpoint)
    successor = recover_expired_serving_bundle(
        h.files,
        **{
            k: v
            for k, v in _identity(4).items()
            if k not in {"expected_schema_version", "expected_recovery_envelope"}
        },
        expected_recovery_envelope=h.envelope,
        now=h.now,
        successor_issued_at=checkpoint.recovery_issued_at,
    )
    assert successor.software_version == SOFTWARE_VERSION

    def delayed_cas():
        h.files = successor.files
        h.rv += 1
        h.after_secret_read = lambda: None

    h.after_secret_read = delayed_cas
    pending = await h.recover()
    assert pending.checkpoint == h.context.checkpoint
    result = await h.recover()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "complete"
    assert not h.patches


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", [False, True])
async def test_guarded_api_conflict_retains_commitment_until_exact_reread(applied):
    h = RecoveryHarness()
    result = await h.recover()
    h.context = replace(h.context, checkpoint=result.checkpoint)
    patch = h.patch_namespaced_secret

    class Conflict(Exception):
        status = 409

    def conflict(*args):
        if applied:
            patch(*args)
        h.patch_namespaced_secret = patch
        raise Conflict("provider-private-response")

    h.patch_namespaced_secret = conflict
    pending = await h.recover()
    assert pending.checkpoint == h.context.checkpoint
    result = await h.recover()
    assert MigrationCheckpoint.decode(result.checkpoint).phase == "complete"
    assert len(h.patches) == 1
