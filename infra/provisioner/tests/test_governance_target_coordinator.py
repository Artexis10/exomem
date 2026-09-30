"""Persisted target recovery survives fresh coordinators and uncertain publication."""

import json
from dataclasses import replace

import pytest
from test_governance_migration_coordinator import (
    CURRENT,
    IMAGE,
    OWNER,
    PLAN,
    SECRET_ENVELOPE,
    SOURCE,
    Scenario,
    identity,
)

from exomem_provisioner import authorization_membership as membership
from exomem_provisioner.driver import DriverPending, DriverTerminal
from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
from exomem_provisioner.governance_migration_coordinator import HostedGovernanceMigrationCoordinator
from exomem_provisioner.lifecycle import MetadataConflict
from exomem_provisioner.repository import ClaimConflict


async def scenario_at_target(phase="complete"):
    scenario = Scenario()
    for _ in range(6):
        await scenario.step()
    source = membership.transition_hosted_authorization_bundle(
        scenario.cell.files,
        **identity(4),
        target_state="SERVING",
        target_no_in_flight=False,
        target_software_version="0.75.0",
        now=scenario.now,
    )
    scenario.cell.files = source.files
    checkpoint = replace(MigrationCheckpoint.decode(scenario.context.checkpoint), phase=phase)
    scenario.context = replace(scenario.context, checkpoint=checkpoint.encode())
    scenario.cell.writes.clear()
    scenario.jobs.requests.clear()
    scenario.now = source.expires_at + 1
    return scenario


async def recover(scenario, **overrides):
    return await HostedGovernanceMigrationCoordinator(
        cell=scenario.cell,
        jobs=scenario.jobs,
        now=lambda: scenario.now,
    ).recover_target(
        **{
            "context": scenario.context,
            "metadata": CURRENT,
            "owner": OWNER,
            "pvc_uid": "pvc-alpha",
            "runtime_image": IMAGE,
            "custody_recovery_envelope": SECRET_ENVELOPE,
            "software_version": "0.75.0",
            **overrides,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["complete", "confirmed"])
async def test_recovery_commits_exact_intent_before_any_cas_then_returns_to_complete(phase):
    scenario = await scenario_at_target(phase)
    initial = scenario.cell.files
    result = await recover(scenario)
    assert isinstance(result, DriverPending)
    committed = MigrationCheckpoint.decode(result.checkpoint)
    assert committed.phase == "recover-" + phase
    assert committed.recovery_issued_at == scenario.now
    assert (committed.source_store_digest, committed.plan_digest) == (SOURCE, PLAN)
    assert scenario.cell.files == initial
    assert not scenario.cell.writes and not scenario.jobs.requests

    scenario.context = replace(scenario.context, checkpoint=result.checkpoint)
    result = await recover(scenario)
    completed = MigrationCheckpoint.decode(result.checkpoint)
    assert completed.phase == "complete" and completed.recovery_revision is None
    observed = membership.inspect_hosted_authorization_bundle(
        scenario.cell.files,
        **identity(4),
        now=scenario.now,
    )
    assert observed.revision == committed.recovery_revision
    assert observed.replica_state == "DRAINING"
    assert observed.governance_enrolled and observed.no_in_flight
    assert len(scenario.cell.writes) == 1 and not scenario.jobs.requests


@pytest.mark.asyncio
async def test_uncertain_cas_keeps_commitment_and_reconciles_exact_successor_after_expiry():
    scenario = await scenario_at_target("confirmed")
    result = await recover(scenario)
    scenario.context = replace(scenario.context, checkpoint=result.checkpoint)
    scenario.cell.fail_after_write = True
    uncertain = await recover(scenario)
    assert uncertain.checkpoint == result.checkpoint
    scenario.now += 4000
    recovered = await recover(scenario)
    assert MigrationCheckpoint.decode(recovered.checkpoint).phase == "complete"
    assert len(scenario.cell.writes) == 1


@pytest.mark.asyncio
async def test_late_unapplied_commitment_reconstructs_same_bytes_without_replanning():
    scenario = await scenario_at_target()
    result = await recover(scenario)
    commitment = MigrationCheckpoint.decode(result.checkpoint)
    scenario.context = replace(scenario.context, checkpoint=result.checkpoint)
    scenario.now += 4000
    await recover(scenario)
    observed = membership.inspect_hosted_authorization_bundle(
        scenario.cell.files,
        **identity(4),
        now=scenario.now,
        _require_fresh=False,
    )
    assert observed.revision == commitment.recovery_revision
    assert observed.expires_at < scenario.now


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override", [{"pvc_uid": "replacement"}, {"runtime_image": "repo@sha256:" + "f" * 64}]
)
async def test_recovery_recomputes_operation_binding(override):
    scenario = await scenario_at_target()
    with pytest.raises(DriverTerminal):
        await recover(scenario, **override)
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_recovery_guard_is_rechecked_after_predecessor_read_before_cas():
    scenario = await scenario_at_target()
    result = await recover(scenario)
    scenario.context = replace(scenario.context, checkpoint=result.checkpoint)
    read = scenario.cell.read_authorization_session_bundle
    lost = False

    async def lose_after_read(metadata):
        nonlocal lost
        value = await read(metadata)
        lost = True
        return value

    async def guard():
        if lost:
            raise ClaimConflict("claim lost")

    scenario.cell.read_authorization_session_bundle = lose_after_read
    scenario.context = replace(scenario.context, effect_guard=guard)
    with pytest.raises(ClaimConflict):
        await recover(scenario)
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_recovery_refuses_a_different_legitimate_successor():
    from exomem_provisioner.governance_target_recovery import recover_expired_serving_bundle

    scenario = await scenario_at_target()
    result = await recover(scenario)
    scenario.context = replace(scenario.context, checkpoint=result.checkpoint)
    scenario.now += 1
    different = recover_expired_serving_bundle(
        scenario.cell.files,
        **{
            k: v
            for k, v in identity(4).items()
            if k not in {"expected_schema_version", "expected_software_version"}
        },
        expected_software_version="0.75.0",
        now=scenario.now,
        successor_issued_at=scenario.now,
    )
    scenario.cell.files = different.files
    with pytest.raises(MetadataConflict):
        await recover(scenario)
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_fresh_target_is_not_renewed_or_moved_to_recovery():
    scenario = await scenario_at_target()
    scenario.now = json.loads(scenario.cell.files["control.json"])["expires_at"] - 1
    assert await recover(scenario) is None
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_a_new_commitment_requires_enough_key_lifetime_for_bounded_start(monkeypatch):
    monkeypatch.setattr(membership, "_KEY_TTL_SECONDS", 3670)
    scenario = await scenario_at_target()
    with pytest.raises(MetadataConflict):
        await recover(scenario)
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_an_existing_commitment_never_uses_its_old_time_to_accept_an_expired_key():
    scenario = await scenario_at_target()
    result = await recover(scenario)
    scenario.context = replace(scenario.context, checkpoint=result.checkpoint)
    scenario.now = json.loads(scenario.cell.files["keyring.json"])["accepted_keys"][0]["not_after"]
    with pytest.raises(MetadataConflict):
        await recover(scenario)
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_key_expiry_during_async_effect_proofs_prevents_publication():
    scenario = await scenario_at_target()
    result = await recover(scenario)
    scenario.context = replace(scenario.context, checkpoint=result.checkpoint)
    key_expiry = json.loads(scenario.cell.files["keyring.json"])["accepted_keys"][0]["not_after"]
    calls = 0

    async def slow_guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            scenario.now = key_expiry

    scenario.context = replace(scenario.context, effect_guard=slow_guard)
    with pytest.raises(MetadataConflict):
        await recover(scenario)
    assert not scenario.cell.writes
