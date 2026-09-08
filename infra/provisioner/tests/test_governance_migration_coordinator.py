from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from exomem_provisioner import authorization_membership as membership
from exomem_provisioner.driver import DriverPending, DriverRetryable, DriverTerminal, EffectContext
from exomem_provisioner.governance_migration_checkpoint import (
    MigrationCheckpoint,
    migration_binding,
)
from exomem_provisioner.governance_migration_job import MigrationJobEvidence
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.repository import ClaimConflict
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2

NOW = 1_900_000_000
OWNER = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "original-operation", 2)
CURRENT = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "upgrade-operation", 7)
SECRET_ENVELOPE = "original-secret-recovery-envelope"
JOB_ENVELOPE = "current-job-recovery-envelope"
IMAGE = "ghcr.io/example/runtime@sha256:" + "a" * 64
FINGERPRINT = "b" * 64
SOURCE = "c" * 64
PLAN = "d" * 64
TARGET = {
    "activation_store_id": "activation-alpha",
    "activation_epoch": 9,
    "activation_state_digest": "e" * 64,
}


def identity(schema=None):
    return {
        "expected_cell_id": OWNER.subject_id,
        "expected_logical_vault_id": OWNER.tenant_id,
        "expected_replica_id": OWNER.resource_name + "-0",
        "expected_software_version": None,
        "expected_schema_version": schema,
        "expected_recovery_envelope": SECRET_ENVELOPE,
    }


def source_bundle(schema=3):
    source = membership.build_initial_hosted_authorization_bundle(
        cell_id=OWNER.subject_id,
        logical_vault_id=OWNER.tenant_id,
        replica_id=OWNER.resource_name + "-0",
        software_version="0.48.0",
        schema_version=schema,
        recovery_envelope=SECRET_ENVELOPE,
        now=NOW,
        entropy=lambda size: bytes(range(size)),
    )
    return membership.transition_hosted_authorization_bundle(
        source.files,
        **identity(schema),
        target_state="DRAINING",
        target_no_in_flight=True,
        now=NOW + 1,
    )


class Cell:
    def __init__(self, source):
        self.files = source.files
        self.writes = []
        self.fail_after_write = False

    async def read_authorization_session_bundle(self, metadata):
        assert metadata == OWNER
        return dict(self.files)

    async def write_authorization_session_bundle(self, metadata, files, **kwargs):
        assert metadata == OWNER
        assert kwargs["recovery_envelope"] == SECRET_ENVELOPE
        current_revision = hashlib.sha256(
            self.files["keyring.json"]
            + self.files["control.json"]
            + self.files["serving-membership.json"]
        ).hexdigest()
        assert kwargs["expected_revision"] == current_revision
        await kwargs["effect_guard"]()
        self.files = dict(files)
        self.writes.append(dict(files))
        if self.fail_after_write:
            self.fail_after_write = False
            raise DriverRetryable("publication acknowledgement lost")


class Jobs:
    def __init__(self, cell):
        self.cell = cell
        self.requests = []
        self.failure = None
        self.plan = PLAN

    async def run(self, request, *, recovery_envelope, effect_guard):
        assert request.metadata == CURRENT
        assert recovery_envelope == JOB_ENVELOPE
        await effect_guard()
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        bundle = membership.inspect_hosted_authorization_bundle(
            self.cell.files, **identity(), now=NOW + 2, _require_fresh=False
        )
        assert request.custody_revision == bundle.revision
        terminal = {
            "artifact": "exomem-hosted-governance-migration",
            "schemaVersion": 1,
            "phase": request.phase,
            "requestSha256": request.sha256,
            "custodyRevision": request.custody_revision,
            "actualSchema": 4 if request.phase == "commit" else 3,
            "membershipSchema": bundle.membership_schema_version,
            "governanceEnrolled": bundle.governance_enrolled,
            "sourceStoreDigest": SOURCE,
        }
        if request.phase != "inspect":
            terminal.update(
                planDigest=self.plan,
                backupReference="exomem-governance-v3-backup://sha256/" + "f" * 64,
                activationStoreId=TARGET["activation_store_id"],
                activationEpoch=TARGET["activation_epoch"],
                activationStateDigest=TARGET["activation_state_digest"],
            )
            terminal.update(
                {"replayed": True} if request.phase == "commit" else {"backupDigest": "f" * 64}
            )
        return MigrationJobEvidence("job-uid", "pod-uid", json.dumps(terminal).encode())


class Scenario:
    def __init__(self, schema=3):
        self.cell = Cell(source_bundle(schema))
        self.jobs = Jobs(self.cell)
        self.guards = 0
        self.now = NOW + 2
        self.context = EffectContext(
            "internal-upgrade",
            CURRENT.operation_id,
            CURRENT.tenant_id,
            CURRENT.subject_id,
            CURRENT.fence_generation,
            checkpoint="vault-fingerprinted-" + FINGERPRINT,
            wire_protocol=WIRE_PROTOCOL_V2,
            effect_guard=self.guard,
        )

    async def guard(self):
        self.guards += 1

    async def step(self, **overrides):
        from exomem_provisioner.governance_migration_coordinator import (
            HostedGovernanceMigrationCoordinator,
        )

        # A new object every retry proves there is no process-local progress authority.
        result = await HostedGovernanceMigrationCoordinator(
            cell=self.cell, jobs=self.jobs, now=lambda: self.now
        ).advance(
            **{
                "context": self.context,
                "metadata": CURRENT,
                "owner": OWNER,
                "pvc_uid": "pvc-alpha",
                "runtime_image": IMAGE,
                "job_recovery_envelope": JOB_ENVELOPE,
                "custody_recovery_envelope": SECRET_ENVELOPE,
                **overrides,
            }
        )
        assert isinstance(result, DriverPending)
        self.context = replace(self.context, checkpoint=result.checkpoint)
        return MigrationCheckpoint.decode(result.checkpoint)


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [3, 4])
async def test_coordinator_persists_each_irreversible_phase_and_keeps_original_custody_owner(
    schema,
):
    scenario = Scenario(schema)
    expected = ["inspect", "prepare", "enroll", "enroll", "commit", "complete"]
    for index, phase in enumerate(expected):
        progress = await scenario.step()
        assert progress.phase == phase
        assert progress.vault_fingerprint == FINGERPRINT
        if index == 0:
            assert not scenario.jobs.requests and not scenario.cell.writes
        if phase == "enroll" and index == 2:
            assert not json.loads(scenario.cell.files["control.json"])["governance_enrolled"]
        if phase == "enroll" and index == 3:
            assert json.loads(scenario.cell.files["control.json"])["governance_enrolled"]
        if phase == "commit":
            assert json.loads(scenario.cell.files["control.json"])["governance_enrolled"]
            assert (
                json.loads(scenario.cell.files["serving-membership.json"])["replicas"][0][
                    "schema_version"
                ]
                == 3
            )
    result = membership.inspect_hosted_authorization_bundle(
        scenario.cell.files, **identity(4), now=NOW + 2
    )
    assert result.governance_enrolled and result.replica_state == "DRAINING"
    assert result.no_in_flight and result.issuance_stopped
    assert len(scenario.cell.writes) == (3 if schema == 4 else 2)
    assert [request.phase for request in scenario.jobs.requests] == [
        "inspect",
        "prepare",
        "prepare",
        "commit",
        "commit",
    ]
    assert scenario.guards >= len(scenario.jobs.requests) + len(scenario.cell.writes)


@pytest.mark.asyncio
async def test_acknowledged_enrollment_keeps_e_until_commit_evidence_is_durable():
    scenario = Scenario()
    for _ in range(3):
        await scenario.step()
    prepared = scenario.context.checkpoint
    assert (await scenario.step()).phase == "enroll"
    assert scenario.context.checkpoint == prepared
    assert len(scenario.cell.writes) == 1
    assert scenario.jobs.requests[-1].phase == "prepare"
    assert (await scenario.step()).phase == "commit"
    assert scenario.jobs.requests[-1].phase == "commit"
    assert len(scenario.cell.writes) == 1
    assert (await scenario.step()).phase == "complete"
    assert len(scenario.cell.writes) == 2


@pytest.mark.asyncio
async def test_enrollment_lost_ack_commits_exact_plan_before_advancing_but_defers_completion():
    scenario = Scenario()
    for _ in range(3):
        await scenario.step()
    prior = scenario.context.checkpoint
    scenario.cell.fail_after_write = True
    assert (await scenario.step()).phase == "enroll"
    assert scenario.context.checkpoint == prior
    assert (await scenario.step()).phase == "commit"
    assert len(scenario.cell.writes) == 1
    assert scenario.jobs.requests[-1].phase == "commit"
    assert (await scenario.step()).phase == "complete"
    assert len(scenario.cell.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["inspect", "prepare", "enroll", "commit"])
async def test_retryable_effect_preserves_exact_checkpoint(phase):
    scenario = Scenario()
    while (await scenario.step()).phase != phase:
        pass
    prior = scenario.context.checkpoint
    scenario.jobs.failure = DriverRetryable("unknown provider acknowledgement")
    await scenario.step()
    assert scenario.context.checkpoint == prior


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [{"pvc_uid": "replacement-pvc"}, {"runtime_image": IMAGE.replace("a" * 64, "f" * 64)}],
)
async def test_bound_checkpoint_refuses_replacement_before_any_job_or_publication(override):
    scenario = Scenario()
    await scenario.step()
    with pytest.raises(DriverTerminal, match="PROVISIONER_CHECKPOINT_INVALID"):
        await scenario.step(**override)
    assert not scenario.jobs.requests and not scenario.cell.writes


@pytest.mark.asyncio
async def test_prepared_plan_drift_cannot_publish_enrollment():
    scenario = Scenario()
    for _ in range(3):
        await scenario.step()
    scenario.jobs.plan = "0" * 64
    with pytest.raises((MetadataConflict, DriverTerminal)):
        await scenario.step()
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_missing_worker_authority_cannot_enter_migration():
    scenario = Scenario()
    scenario.context = replace(scenario.context, effect_guard=None)
    with pytest.raises(DriverTerminal, match="PROVISIONER_EFFECT_AUTHORITY_UNAVAILABLE"):
        await scenario.step()
    assert not scenario.jobs.requests and not scenario.cell.writes


@pytest.mark.asyncio
async def test_checkpoint_binding_is_the_canonical_worker_binding():
    scenario = Scenario()
    checkpoint = await scenario.step()
    assert checkpoint.binding == migration_binding(
        scenario.context, pvc_uid="pvc-alpha", runtime_image=IMAGE
    )


@pytest.mark.asyncio
async def test_completion_lost_ack_replays_commit_against_exact_schema_four_successor():
    scenario = Scenario()
    for _ in range(5):
        await scenario.step()
    before = scenario.context.checkpoint
    scenario.cell.fail_after_write = True
    assert (await scenario.step()).phase == "commit"
    assert scenario.context.checkpoint == before
    published = dict(scenario.cell.files)
    assert (await scenario.step()).phase == "complete"
    assert scenario.cell.files == published
    assert len(scenario.cell.writes) == 2
    assert [job.phase for job in scenario.jobs.requests[-2:]] == ["commit", "commit"]


@pytest.mark.asyncio
async def test_expired_enrollment_resume_does_not_prepare_or_renew_the_bound_window():
    scenario = Scenario()
    for _ in range(3):
        await scenario.step()
    scenario.cell.fail_after_write = True
    await scenario.step()
    expires = json.loads(scenario.cell.files["control.json"])["expires_at"]
    scenario.now = expires + 10
    count = len(scenario.jobs.requests)
    assert (await scenario.step()).phase == "commit"
    assert (await scenario.step()).phase == "complete"
    assert [job.phase for job in scenario.jobs.requests[count:]] == ["commit", "commit"]
    assert json.loads(scenario.cell.files["control.json"])["expires_at"] == expires


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["inspect", "prepare", "enroll"])
async def test_expired_unenrolled_custody_refuses_before_job_creation(phase):
    scenario = Scenario()
    while (await scenario.step()).phase != phase:
        pass
    scenario.now = NOW + 4000
    jobs_before = len(scenario.jobs.requests)
    with pytest.raises(MetadataConflict):
        await scenario.step()
    assert len(scenario.jobs.requests) == jobs_before
    assert not json.loads(scenario.cell.files["control.json"])["governance_enrolled"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "replacement-internal-operation"},
        {"provider_operation_id": "replacement-provider-operation"},
        {"tenant_id": "other-tenant"},
        {"cell_id": "other-cell"},
        {"fence_generation": 8},
        {"wire_protocol": "exomem-cell-provisioner.v1"},
    ],
)
async def test_foreign_worker_context_cannot_inherit_a_checkpoint(change):
    scenario = Scenario()
    await scenario.step()
    scenario.context = replace(scenario.context, **change)
    with pytest.raises(DriverTerminal, match="PROVISIONER_CHECKPOINT_INVALID"):
        await scenario.step()
    assert not scenario.jobs.requests and not scenario.cell.writes


@pytest.mark.asyncio
async def test_claim_loss_propagates_instead_of_becoming_pending():
    scenario = Scenario()
    await scenario.step()

    async def lost():
        raise ClaimConflict("claim no longer owned")

    scenario.context = replace(scenario.context, effect_guard=lost)
    with pytest.raises(ClaimConflict):
        await scenario.step()
    assert not scenario.jobs.requests and not scenario.cell.writes


@pytest.mark.asyncio
async def test_claim_loss_after_enrollment_publication_keeps_durable_enrollment_boundary():
    scenario = Scenario()
    for _ in range(3):
        await scenario.step()
    before = scenario.context.checkpoint

    async def lost_after_write():
        if scenario.cell.writes:
            raise ClaimConflict("claim no longer owned")

    scenario.context = replace(scenario.context, effect_guard=lost_after_write)
    with pytest.raises(ClaimConflict):
        await scenario.step()
    assert scenario.context.checkpoint == before
    assert json.loads(scenario.cell.files["control.json"])["governance_enrolled"]
    scenario.context = replace(scenario.context, effect_guard=scenario.guard)
    assert (await scenario.step()).phase == "commit"
    assert len(scenario.cell.writes) == 1
