from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from exomem_provisioner import authorization_membership as membership
from exomem_provisioner.conflict_reason import ConflictReason
from exomem_provisioner.driver import DriverPending, DriverRetryable, DriverTerminal, EffectContext
from exomem_provisioner.governance_migration_checkpoint import (
    MigrationCheckpoint,
    migration_binding,
)
from exomem_provisioner.governance_migration_job import MigrationJobEvidence
from exomem_provisioner.governance_provision_membership import (
    _REISSUE_BACKDATE_SECONDS,
    refresh_drained_source_bundle,
)
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
    def __init__(self, cell, clock=lambda: NOW + 2):
        self.cell = cell
        self.clock = clock
        self.requests = []
        self.failure = None
        self.plan = PLAN
        self.slot = False

    async def occupied(self, request):
        return self.slot

    async def run(self, request, *, recovery_envelope, effect_guard):
        assert request.metadata == CURRENT
        assert recovery_envelope == JOB_ENVELOPE
        await effect_guard()
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        if (
            request.phase != "commit"
            and self.clock() >= json.loads(self.cell.files["control.json"])["expires_at"]
        ):
            # The runtime Job loads custody with an open window in every phase but
            # commit, so a closed window fails the Job after it has been created.
            raise DriverRetryable("migration Job refused a closed window")
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
        self.jobs = Jobs(self.cell, lambda: self.now)
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
@pytest.mark.parametrize("phase", ["inspect", "prepare"])
async def test_unenrolled_custody_whose_window_closed_is_reissued_before_its_job(phase):
    # Only this generation's own replica could renew the window and a fenced
    # generation has none, while the migration Job refuses to inspect or prepare
    # under a closed window. Before any plan exists the coordinator reissues the
    # drained window as its own step, then runs the Job against the stored bytes.
    scenario = Scenario()
    while (await scenario.step()).phase != phase:
        pass
    scenario.now = NOW + 4000
    jobs_before, writes_before = len(scenario.jobs.requests), len(scenario.cell.writes)
    assert (await scenario.step()).phase == phase
    assert len(scenario.jobs.requests) == jobs_before, "a Job started on the closed window"
    assert len(scenario.cell.writes) == writes_before + 1
    control = json.loads(scenario.cell.files["control.json"])
    replica = json.loads(scenario.cell.files["serving-membership.json"])["replicas"][0]
    assert control["issued_at"] == scenario.now - _REISSUE_BACKDATE_SECONDS
    assert control["expires_at"] > scenario.now
    assert not control["governance_enrolled"]
    assert (replica["state"], replica["issuance_stopped"]) == ("DRAINING", True)

    assert (await scenario.step()).phase == {"inspect": "prepare", "prepare": "enroll"}[phase]
    assert len(scenario.jobs.requests) == jobs_before + 1
    assert len(scenario.cell.writes) == writes_before + 1


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


@pytest.mark.asyncio
async def test_migration_advances_after_the_fenced_generation_window_closed():
    # A drained, issuance-stopped, never-enrolled generation cannot authorize
    # anything, and only its own running replica could renew the window. Recovery
    # that takes longer than one attestation lifetime must still migrate the cell.
    scenario = Scenario()
    scenario.now = NOW + membership.DEFAULT_ATTESTATION_TTL_SECONDS + 60
    control = json.loads(scenario.cell.files["control.json"])
    assert control["expires_at"] < scenario.now
    assert not control["governance_enrolled"]

    # The second inspect pass reissues the drained window; nothing else changes.
    for phase in ["inspect", "inspect", "prepare", "enroll", "enroll", "commit", "complete"]:
        assert (await scenario.step()).phase == phase
    reissued = json.loads(scenario.cell.writes[0]["control.json"])
    assert reissued["issued_at"] == scenario.now - _REISSUE_BACKDATE_SECONDS
    assert not reissued["governance_enrolled"]
    assert json.loads(scenario.cell.files["control.json"])["governance_enrolled"]


@pytest.mark.asyncio
async def test_closed_window_recovery_still_refuses_a_control_issued_in_the_future():
    scenario = Scenario()
    # The first advance only records the inspect checkpoint; the bundle is read next.
    assert (await scenario.step()).phase == "inspect"
    # Renew the drained generation inside its window so the control is issued later
    # than the clock we then run at, while the keyring stays valid at that clock.
    # Winding the clock back instead would trip the keyring's own not_before check,
    # and the test would pass with the guard it names deleted.
    scenario.cell.files = membership.transition_hosted_authorization_bundle(
        scenario.cell.files,
        **identity(3),
        target_state="DRAINING",
        target_no_in_flight=True,
        now=NOW + 1_800,
        renew=True,
    ).files
    scenario.now = NOW + 10
    keyring = json.loads(scenario.cell.files["keyring.json"])
    assert keyring["accepted_keys"][0]["not_before"] <= scenario.now
    assert json.loads(scenario.cell.files["control.json"])["issued_at"] > scenario.now
    jobs_before = len(scenario.jobs.requests)
    with pytest.raises(MetadataConflict):
        await scenario.step()
    # It must refuse before creating a Job; a later refusal would leave a migration
    # Job running against custody the coordinator never accepted.
    assert len(scenario.jobs.requests) == jobs_before
    assert not scenario.cell.writes


@pytest.mark.asyncio
async def test_closed_window_recovery_still_refuses_an_expired_signing_key():
    scenario = Scenario()
    assert (await scenario.step()).phase == "inspect"
    scenario.now = NOW + membership._KEY_TTL_SECONDS + 60
    with pytest.raises(MetadataConflict):
        await scenario.step()


@pytest.mark.asyncio
async def test_enrollment_under_a_closed_window_returns_to_prepare_and_reissues():
    # Enrollment re-runs the prepare Job, which refuses a closed window, and the
    # prepared plan binds that window. With nothing enrolled yet the coordinator
    # returns to prepare, reissues there and derives a fresh plan, instead of
    # retrying the refused Job forever or reissuing under the old plan.
    scenario = Scenario()
    while (await scenario.step()).phase != "enroll":
        pass
    scenario.now = NOW + 4000
    jobs_before, writes_before = len(scenario.jobs.requests), len(scenario.cell.writes)
    rewound = await scenario.step()
    assert (rewound.phase, rewound.plan_digest, rewound.source_store_digest) == (
        "prepare",
        None,
        SOURCE,
    )
    assert (len(scenario.jobs.requests), len(scenario.cell.writes)) == (
        jobs_before,
        writes_before,
    )

    assert (await scenario.step()).phase == "prepare"
    reissued = json.loads(scenario.cell.writes[writes_before]["control.json"])
    assert reissued["issued_at"] == scenario.now - _REISSUE_BACKDATE_SECONDS
    assert not reissued["governance_enrolled"]
    for phase in ["enroll", "enroll", "commit", "complete"]:
        assert (await scenario.step()).phase == phase
    assert json.loads(scenario.cell.files["control.json"])["governance_enrolled"]


@pytest.mark.asyncio
async def test_a_job_still_in_the_slot_is_resumed_not_reissued_under():
    scenario = Scenario()
    assert (await scenario.step()).phase == "inspect"
    scenario.now = NOW + 4000
    scenario.jobs.slot = True
    assert (await scenario.step()).phase == "inspect"
    assert not scenario.cell.writes
    assert len(scenario.jobs.requests) == 1, "the Job holding the slot was not resumed"
    scenario.jobs.slot = False
    assert (await scenario.step()).phase == "inspect"
    assert len(scenario.cell.writes) == 1


@pytest.mark.asyncio
async def test_a_lost_reissue_acknowledgement_runs_the_job_without_reissuing_twice():
    scenario = Scenario()
    assert (await scenario.step()).phase == "inspect"
    scenario.now = NOW + 4000
    scenario.cell.fail_after_write = True
    assert (await scenario.step()).phase == "inspect"
    assert len(scenario.cell.writes) == 1
    assert (await scenario.step()).phase == "prepare"
    assert len(scenario.cell.writes) == 1


@pytest.mark.asyncio
async def test_a_reissue_that_loses_its_publication_race_decides_again_on_the_stored_bytes():
    scenario = Scenario()
    assert (await scenario.step()).phase == "inspect"
    scenario.now = NOW + 4000

    async def lost_race(metadata, files, **kwargs):
        raise MetadataConflict(
            "authorization session bundle predecessor differs",
            reason=ConflictReason.AUTHORIZATION_SESSION_BUNDLE_PREDECESSOR_DIFFERS,
        )

    scenario.cell.write_authorization_session_bundle = lost_race
    assert (await scenario.step()).phase == "inspect"
    assert not scenario.jobs.requests


@pytest.mark.asyncio
async def test_a_window_capped_by_its_signing_key_is_reissued_once_then_used():
    scenario = Scenario()
    assert (await scenario.step()).phase == "inspect"
    key = json.loads(scenario.cell.files["keyring.json"])["accepted_keys"][0]
    scenario.now = key["not_after"] - 800
    assert (await scenario.step()).phase == "inspect"
    assert json.loads(scenario.cell.files["control.json"])["expires_at"] == key["not_after"]
    # Still under the floor, but reissuing again cannot extend it: run the Job.
    assert (await scenario.step()).phase == "prepare"
    assert len(scenario.cell.writes) == 1


@pytest.mark.asyncio
async def test_reissue_refuses_a_signing_key_that_ends_before_a_job_could_finish():
    scenario = Scenario()
    assert (await scenario.step()).phase == "inspect"
    key = json.loads(scenario.cell.files["keyring.json"])["accepted_keys"][0]
    scenario.now = key["not_after"] - 300
    with pytest.raises(MetadataConflict):
        await scenario.step()
    assert not scenario.cell.writes
    assert not scenario.jobs.requests


def test_reissue_refuses_a_generation_that_is_still_serving():
    from exomem_provisioner.governance_provision_membership import (
        refresh_drained_source_bundle,
    )

    serving = membership.build_initial_hosted_authorization_bundle(
        cell_id=OWNER.subject_id,
        logical_vault_id=OWNER.tenant_id,
        replica_id=OWNER.resource_name + "-0",
        software_version="0.48.0",
        schema_version=3,
        recovery_envelope=SECRET_ENVELOPE,
        now=NOW,
        entropy=lambda size: bytes(range(size)),
    )
    assert serving.replica_state == "SERVING"
    with pytest.raises(MetadataConflict):
        refresh_drained_source_bundle(serving.files, **identity(), now=NOW + 4000)


@pytest.mark.parametrize("shape", ["enrolled", "issued-later"])
def test_reissue_itself_refuses_custody_outside_the_drained_unenrolled_state(shape):
    # The coordinator gates these first; the reissue must not rely on its caller.
    now = NOW + 4000
    if shape == "enrolled":
        files = membership.enroll_hosted_governance_bundle(
            source_bundle().files,
            **identity(3),
            activation_store_id=TARGET["activation_store_id"],
            activation_epoch=TARGET["activation_epoch"],
            activation_state_digest=TARGET["activation_state_digest"],
            now=NOW + 2,
        ).files
    else:
        files = membership.transition_hosted_authorization_bundle(
            source_bundle().files,
            **identity(3),
            target_state="DRAINING",
            target_no_in_flight=True,
            now=NOW + 1_800,
            renew=True,
        ).files
        now = NOW + 10
    with pytest.raises(MetadataConflict):
        refresh_drained_source_bundle(files, **identity(), now=now)
