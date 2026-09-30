import json

import pytest
from test_governance_readiness import NOW
from test_governance_rollforward_live import RollforwardHarness

from exomem_provisioner.authorization_membership import transition_hosted_authorization_bundle
from exomem_provisioner.lifecycle import MetadataConflict
from exomem_provisioner.repository import ClaimConflict


@pytest.mark.asyncio
async def test_provider_operation_record_requires_guard_before_create():
    h = RollforwardHarness()
    writes = []
    h.create_namespaced_config_map = lambda *args: writes.append(args)
    h.claim_valid = False
    with pytest.raises(ClaimConflict):
        await h.plane._registry.record_operation(h.current, "record-envelope", effect_guard=h.guard)
    assert not writes


@pytest.mark.asyncio
async def test_runtime_admission_guard_runs_after_namespace_predecessor_read():
    h = RollforwardHarness()
    original = h.read_namespace

    def changed(name):
        result = original(name)
        h.claim_valid = False
        return result

    h.read_namespace = changed
    with pytest.raises(ClaimConflict):
        await h.plane._registry.mark_runtime_admitted(
            h.plane._owner(h.current), effect_guard=h.guard
        )
    assert not h.admitted


@pytest.mark.asyncio
async def test_guarded_source_drain_reauthenticates_after_slow_publication_guard():
    h = RollforwardHarness()
    h.now = NOW + 5

    async def expired():
        h.now = h.bundle.expires_at + 1

    with pytest.raises(MetadataConflict):
        await h.plane._transition_authorization_session_membership(
            h.current, target_state="DRAINING", target_no_in_flight=True, effect_guard=expired
        )
    assert not h.patches


@pytest.mark.asyncio
async def test_guarded_drain_uses_current_time_after_runtime_attestation_roundtrip():
    h = RollforwardHarness()
    h.now = NOW + 5

    async def attest(*args, **kwargs):
        h.now += 2
        drained = transition_hosted_authorization_bundle(
            h.files,
            expected_cell_id=h.current.subject_id,
            expected_logical_vault_id=h.current.tenant_id,
            expected_replica_id=h.current.resource_name + "-0",
            expected_software_version=None,
            expected_schema_version=4,
            expected_recovery_envelope=h.envelope,
            target_state="DRAINING",
            target_no_in_flight=True,
            now=h.now,
        )
        return json.dumps(
            json.loads(drained.membership)["replicas"][0], sort_keys=True, separators=(",", ":")
        ).encode()

    h.plane._runtime.attest_authorization_session_membership = attest
    await h.plane._transition_authorization_session_membership(
        h.current,
        target_state="DRAINING",
        target_no_in_flight=True,
        require_runtime_attestation=True,
        runtime_credential=h.request["serviceCredential"],
        runtime_protocol_version="1",
        effect_guard=h.guard,
    )
    assert len(h.patches) == 1 and h.custody().replica_state == "DRAINING"
