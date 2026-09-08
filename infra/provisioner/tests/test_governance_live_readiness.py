"""Live readiness uses real Secret, custody and private-HTTP adapters."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_governance_readiness import (
    CELL_ID,
    METADATA,
    NOW,
    SOFTWARE_VERSION,
    VAULT_ID,
    _identity,
    _proof,
    _serving_bundle,
)

from exomem_provisioner.adapters import KubernetesCellAdapter, PrivateCellApiAdapter
from exomem_provisioner.authorization_membership import (
    AUTHORIZATION_SESSION_SECRET_NAME,
    build_initial_hosted_authorization_bundle,
    transition_hosted_authorization_bundle,
)
from exomem_provisioner.lifecycle import HealthObservation, LifecycleConfig, MetadataConflict
from exomem_provisioner.live import LiveLifecyclePlane
from exomem_provisioner.provider_identity import (
    ProviderRecoveryIdentityCodec,
    cell_provider_recovery_envelopes,
    provider_operation_resource_name,
)


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReadinessHarness:
    def __init__(self, *, mode: str = "governance-v3-to-v4") -> None:
        codec = ProviderRecoveryIdentityCodec.from_secret("readiness-test-provider-root")
        envelopes = cell_provider_recovery_envelopes(
            codec,
            tenant_id=VAULT_ID,
            cell_id=CELL_ID,
            operation_id=METADATA.operation_id,
            fence_generation=METADATA.fence_generation,
            resource_name=METADATA.resource_name,
            operation_resource_name=provider_operation_resource_name(METADATA.operation_id),
        )
        self.envelope = envelopes["authorizationSessionSecret"]
        self.bundle = _serving_bundle(recovery_envelope=self.envelope)
        self.files = self.bundle.files
        self.annotations = METADATA.kubernetes_annotations | {
            "exomem.io/recovery-envelope": self.envelope
        }
        self.now = NOW + 5
        self.secret_reads = 0
        self.calls: list[str] = []
        self.after_ready = lambda: None
        self.agent_contract = {
            "schema_version": 1,
            "protocol_version": "1",
            "exomem_release": SOFTWARE_VERSION,
            "agent_profile": {
                "profile": "hosted-alpha-agent-v1",
                "active_capability_sha256": "c" * 64,
            },
            "commands": [],
        }
        target = {
            "releaseVersion": SOFTWARE_VERSION,
            "protocolVersion": "1",
            "agentProfile": "hosted-alpha-agent-v1",
            "gatewayContractDigest": "b" * 64,
            "commandFingerprint": "c" * 64,
            "schemaDigest": _sha(
                {k: v for k, v in self.agent_contract.items() if k != "exomem_release"}
            ),
        }
        self.config = LifecycleConfig(
            image="repo@sha256:" + "a" * 64,
            chart_path="chart",
            chart_version="0.1.0",
            helm_version="3.19.4",
            control_hostname="control.example.invalid",
            transfer_hostname="transfer.example.invalid",
            browser_origin="https://app.example.invalid",
            release_version=SOFTWARE_VERSION,
            protocol_version="1",
            contract_digest="b" * 64,
            location="fsn1",
            runtime_target=target,
            legacy_runtime_units={(SOFTWARE_VERSION, "1"): target},
            migration_mode=mode,  # type: ignore[arg-type] # Not enabled by configuration yet.
        )
        worker_policy = {"workerCount": 2, "semantic": True, "media": False}
        self.request = {
            "runtimeTarget": target,
            "serviceCredential": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode(),
            "workerPolicy": worker_policy,
        }
        self.ready = {
            "cell_id": CELL_ID,
            "vault_id": VAULT_ID,
            "exomem_release": SOFTWARE_VERSION,
            "hosted_protocol": "1",
            "authenticated_credential_version": "1",
            "security_revision": 1,
            "service_authenticated": True,
            "mutation_authority": True,
            "admission_phase": "active",
            "read_admission": True,
            "write_admission": True,
            "worker_policy_digest": _sha(worker_policy),
            "governance": _proof(self.bundle),
        }
        self.runtime = PrivateCellApiAdapter(
            request=self.http, internal_origin="http://cells.invalid"
        )
        self.plane = LiveLifecyclePlane(
            repository=SimpleNamespace(),
            registry=SimpleNamespace(),
            cell=KubernetesCellAdapter(
                core_v1=self, apps_v1=SimpleNamespace(), identity_verifier=codec.verifier()
            ),
            helm=SimpleNamespace(),
            runtime=self.runtime,
            routes=SimpleNamespace(),
            maintenance=SimpleNamespace(),
            capacity=SimpleNamespace(),
            identity_verifier=codec.verifier(),
            config=self.config,
            now=lambda: self.now,
        )
        # Model the maps authenticated by observe_operation. Custody belongs to the
        # original provision operation, not the newer rollforward operation.
        self.current = replace(METADATA, operation_id="rollforward-beta", fence_generation=8)
        key = self.plane._key(self.current)
        self.plane._owned[key] = METADATA
        self.plane._helm_requests[key] = self.request | {"_providerRecoveryEnvelopes": envelopes}

    def read_namespaced_secret(self, name: str, namespace: str):
        assert name == AUTHORIZATION_SESSION_SECRET_NAME
        assert namespace == METADATA.resource_name
        self.secret_reads += 1
        return SimpleNamespace(
            metadata=SimpleNamespace(annotations=self.annotations),
            data={name: base64.b64encode(raw).decode() for name, raw in self.files.items()},
        )

    async def http(self, method: str, url: str, **kwargs):
        assert method == "GET"
        self.calls.append(url)
        if url.endswith("/agent/hosted-alpha-agent-v1/contract"):
            response = self.agent_contract | {
                "digest": {"algorithm": "sha256", "value": _sha(self.agent_contract)}
            }
        elif url.endswith("/contract"):
            response = {"digest": {"algorithm": "sha256", "value": "b" * 64}}
        elif url.endswith("/ready"):
            response = {"success": True, "data": self.ready}
            self.after_ready()
        else:
            assert url.endswith("/live")
            response = {
                "success": True,
                "data": {"live": True, "cell_id": CELL_ID, "protocol_version": "1"},
            }
        return SimpleNamespace(status_code=200, json=lambda: response)

    async def health(self, *, v2: bool = True):
        return await self.plane.health(self.current, self.request, v2=v2)


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", [None, {}, {"actualSchema": 4}])
async def test_live_governance_readiness_refuses_absent_or_partial_proof(proof) -> None:
    h = ReadinessHarness()
    if proof is None:
        del h.ready["governance"]
    else:
        h.ready["governance"] = proof
    with pytest.raises(MetadataConflict, match="governance readiness is unavailable"):
        await h.health()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("actualSchema", 3),
        ("governanceEnrolled", False),
        ("storeAgreement", False),
        ("custodyRevision", "f" * 64),
        ("activationEpoch", True),
        ("replicaId", "job-pod-123"),
    ],
)
async def test_live_governance_readiness_refuses_mismatched_proof(field, value) -> None:
    h = ReadinessHarness()
    h.ready["governance"] = _proof(h.bundle) | {field: value}
    with pytest.raises(MetadataConflict, match="governance readiness is unavailable"):
        await h.health()


@pytest.mark.asyncio
async def test_live_governance_readiness_authenticates_original_owner_before_and_after_http() -> (
    None
):
    h = ReadinessHarness()
    health = await h.health()
    assert health == HealthObservation.ready_for(METADATA, h.request, h.config)
    assert h.secret_reads == 2
    assert len(h.calls) == 4  # Reuse the existing ready response; no extra HTTP round trip.
    assert "governance" not in health.flattened(v2=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "during"])
async def test_live_governance_readiness_refuses_expired_custody(when) -> None:
    h = ReadinessHarness()
    if when == "before":
        h.now = h.bundle.expires_at + 1
    else:
        h.after_ready = lambda: setattr(h, "now", h.bundle.expires_at + 1)
    with pytest.raises(MetadataConflict):
        await h.health()
    if when == "before":
        assert not h.calls


@pytest.mark.asyncio
async def test_live_governance_readiness_refuses_a_different_fresh_successor_during_http() -> None:
    h = ReadinessHarness()
    successor = transition_hosted_authorization_bundle(
        h.bundle.files,
        **(_identity(4) | {"expected_recovery_envelope": h.envelope}),
        target_state="SERVING",
        target_no_in_flight=False,
        now=NOW + 5,
        renew=True,
    )
    h.after_ready = lambda: setattr(h, "files", successor.files)
    with pytest.raises(MetadataConflict, match="governance readiness is unavailable"):
        await h.health()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "during"])
async def test_live_governance_readiness_refuses_secret_owner_drift(when) -> None:
    h = ReadinessHarness()

    def change_owner():
        h.annotations = h.annotations | {"exomem.io/operation-id": h.current.operation_id}

    if when == "before":
        change_owner()
    else:
        h.after_ready = change_owner
    with pytest.raises(MetadataConflict):
        await h.health()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["unenrolled", "draining", "tampered", "wrong-envelope", "no-owner"]
)
async def test_live_governance_readiness_requires_authentic_enrolled_serving_custody(kind) -> None:
    h = ReadinessHarness()
    if kind == "unenrolled":
        h.files = build_initial_hosted_authorization_bundle(
            cell_id=CELL_ID,
            logical_vault_id=VAULT_ID,
            replica_id=METADATA.resource_name + "-0",
            software_version=SOFTWARE_VERSION,
            schema_version=4,
            recovery_envelope=h.envelope,
            now=NOW,
        ).files
    elif kind == "draining":
        h.files = transition_hosted_authorization_bundle(
            h.bundle.files,
            **(_identity(4) | {"expected_recovery_envelope": h.envelope}),
            target_state="DRAINING",
            target_no_in_flight=True,
            now=NOW + 5,
        ).files
    elif kind == "tampered":
        control = json.loads(h.files["control.json"])
        h.files = h.files | {
            "control.json": json.dumps(control | {"activation_epoch": 999}).encode()
        }
    elif kind == "wrong-envelope":
        h.annotations = h.annotations | {"exomem.io/recovery-envelope": "foreign-envelope"}
    else:
        h.plane._helm_requests.clear()
    with pytest.raises(MetadataConflict):
        await h.health()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,v2", [("none", True), ("state-root-v1", True), ("governance-v3-to-v4", False)]
)
async def test_legacy_readiness_does_not_require_governance_or_read_secrets(mode, v2) -> None:
    h = ReadinessHarness(mode=mode)
    del h.ready["governance"]
    assert (await h.health(v2=v2)).ready is True
    assert h.secret_reads == 0


@pytest.mark.asyncio
async def test_private_adapter_cannot_bypass_governance_with_missing_expected_custody() -> None:
    h = ReadinessHarness()
    with pytest.raises(MetadataConflict, match="governance readiness is unavailable"):
        await h.runtime.health(
            METADATA,
            credential=h.request["serviceCredential"],
            protocol_version="1",
            config=h.config,
            expected_release=SOFTWARE_VERSION,
            expected_worker_policy=h.request["workerPolicy"],
            require_runtime_identity=True,
        )
