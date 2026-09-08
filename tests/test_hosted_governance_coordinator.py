"""Real migration engine and custody; only Kubernetes transport is simulated."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
from dataclasses import replace
from itertools import groupby
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hosted_governance_job import _canonical, _custody_bytes, _job
from test_hosted_governance_job import cell as cell
from test_hosted_governance_migration import _offline_state as _offline_state

from exomem.governance import authorization_custody, schema_migration, store

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Hosted Job uses Linux root ownership")


@pytest.mark.parametrize("cell", ["provider"], indirect=True)
@pytest.mark.parametrize(
    "interruption",
    [
        "none",
        "repair-ack",
        "enrollment-ack",
        "commit-crash",
        "completion-ack",
        "inspect-delete-ack",
        "prepare-delete-ack",
        "commit-delete-ack",
        "enrollment-expiry",
    ],
)
def test_coordinator_restarts_across_real_custody_and_database_effects(
    cell, monkeypatch, interruption
):
    pytest.importorskip("exomem_provisioner")
    from exomem_provisioner import authorization_membership as membership
    from exomem_provisioner.adapters import KubernetesCellAdapter
    from exomem_provisioner.driver import DriverPending, EffectContext
    from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
    from exomem_provisioner.governance_migration_coordinator import (
        HostedGovernanceMigrationCoordinator,
    )
    from exomem_provisioner.governance_migration_job import MigrationJobRequest
    from exomem_provisioner.lifecycle import OpaqueProviderMetadata
    from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from infra.provisioner.tests.test_governance_migration_job import Cluster, _adapter

    binding, now, initial_request = cell
    clock = [now]
    owner = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "original-operation", 2)
    current = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "migration-alpha", 7)
    paths = dict(
        zip(
            ("keyring.json", "control.json", "serving-membership.json"),
            (
                Path(os.environ[variable])
                for variable in (
                    authorization_custody.KEYRING_FILE_ENV,
                    authorization_custody.CONTROL_FILE_ENV,
                    authorization_custody.MEMBERSHIP_FILE_ENV,
                )
            ),
            strict=True,
        )
    )
    identity = {
        "expected_cell_id": current.subject_id,
        "expected_logical_vault_id": current.tenant_id,
        "expected_replica_id": current.resource_name + "-0",
        "expected_software_version": None,
        "expected_schema_version": None,
        "expected_recovery_envelope": "signed-envelope",
    }
    if interruption == "repair-ack":
        # Reproduce the deployed legacy claim: actual v3, signed claim v4.
        source = membership.build_initial_hosted_authorization_bundle(
            cell_id=current.subject_id,
            logical_vault_id=current.tenant_id,
            replica_id=current.resource_name + "-0",
            software_version="0.48.0",
            schema_version=4,
            recovery_envelope="signed-envelope",
            now=now,
            entropy=lambda size: bytes(range(size)),
        )
        source = membership.transition_hosted_authorization_bundle(
            source.files,
            **{**identity, "expected_schema_version": 4},
            target_state="DRAINING",
            target_no_in_flight=True,
            now=now,
        )
        for name, payload in source.files.items():
            paths[name].write_bytes(payload)

    runner = _job(monkeypatch, binding)
    keyring_before = paths["keyring.json"].read_bytes()
    vault_before = {
        str(path.relative_to(binding.vault_root)): path.read_bytes()
        for path in binding.vault_root.rglob("*")
        if path.is_file()
    }
    cutovers = []
    fault_fired = []

    class LostProviderAcknowledgement(Exception):
        status = 503

    def crash_after_commit(point):
        if point == "after_store_commit":
            cutovers.append(point)
            if interruption == "commit-crash" and not fault_fired:
                fault_fired.append(interruption)
                raise schema_migration._ForwardMigrationCrash("injected lost acknowledgement")

    monkeypatch.setattr(store, "_schema_migration_barrier", crash_after_commit)

    class RuntimeCluster(Cluster):
        def __init__(self):
            super().__init__(
                MigrationJobRequest(
                    current,
                    current.tenant_id,
                    "pvc-alpha",
                    initial_request["runtimeImage"],
                    "a" * 64,
                    "inspect",
                )
            )
            self.secret_version = 1
            self.publications = []
            self.terminals = []

        def read_namespaced_secret(self, name, namespace):
            assert name == "exomem-authorization-session"
            assert namespace == owner.resource_name
            files = {name: path.read_bytes() for name, path in paths.items()}
            revision = hashlib.sha256(b"".join(files.values())).hexdigest()
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    uid="secret-alpha",
                    resource_version=str(self.secret_version),
                    deletion_timestamp=None,
                    annotations={
                        **owner.kubernetes_annotations,
                        "exomem.io/recovery-envelope": "signed-envelope",
                        "exomem.io/authorization-session-revision": revision,
                    },
                ),
                data={name: base64.b64encode(value).decode() for name, value in files.items()},
            )

        def patch_namespaced_secret(self, name, namespace, body):
            assert name == "exomem-authorization-session"
            assert namespace == owner.resource_name
            assert body["metadata"]["uid"] == "secret-alpha"
            assert body["metadata"]["resourceVersion"] == str(self.secret_version)
            for key, value in owner.kubernetes_annotations.items():
                assert body["metadata"]["annotations"][key] == value
            successor = membership.inspect_hosted_authorization_bundle(
                {name: value.encode() for name, value in body["stringData"].items()},
                **identity,
                now=clock[0],
                _require_fresh=False,
            )
            for name, value in successor.files.items():
                paths[name].write_bytes(value)
            self.secret_version += 1
            self.publications.append(successor)
            point = (
                "completion-ack"
                if successor.membership_schema_version == 4
                else "enrollment-ack"
                if successor.governance_enrolled
                else "repair-ack"
            )
            if interruption == "enrollment-expiry" and point == "enrollment-ack":
                clock[0] = successor.expires_at + 1
                fault_fired.append(interruption)
            if point == interruption and not fault_fired:
                fault_fired.append(interruption)
                raise LostProviderAcknowledgement

        def create_namespaced_job(self, namespace, body):
            raw = next(
                entry["value"]
                for entry in body["spec"]["template"]["spec"]["containers"][0]["env"]
                if entry["name"] == runner.REQUEST_ENV
            )
            request = json.loads(raw)
            self.request = MigrationJobRequest(
                current,
                request["vaultId"],
                request["pvcUid"],
                request["runtimeImage"],
                request["custodyRevision"],
                request["phase"],
                request["sourceStoreDigest"],
                request["planDigest"],
            )
            super().create_namespaced_job(namespace, body)
            terminated = self.job_pods[0]["status"]["containerStatuses"][0]["state"]["terminated"]
            try:
                terminal = runner.execute(raw.encode(), now=clock[0])
            except runner.HostedGovernanceJobError:
                if interruption != "commit-crash" or not fault_fired:
                    raise
                self.job["status"] = {
                    "failed": 1,
                    "active": 0,
                    "terminating": 0,
                    "conditions": [{"type": "Failed", "status": "True"}],
                }
                self.job_pods[0]["status"]["phase"] = "Failed"
                terminated.update(exitCode=1, message="")
            else:
                self.terminals.append(terminal)
                terminated["message"] = _canonical(terminal).decode()
            return copy.deepcopy(self.job)

        def delete_namespaced_job(self, name, namespace, body):
            super().delete_namespaced_job(name, namespace, body)
            if interruption == self.request.phase + "-delete-ack" and not fault_fired:
                fault_fired.append(interruption)
                raise LostProviderAcknowledgement

    cluster = RuntimeCluster()
    guarded = []

    async def guard():
        guarded.append(True)

    context = EffectContext(
        operation_id="internal-migration",
        provider_operation_id=current.operation_id,
        tenant_id=current.tenant_id,
        cell_id=current.subject_id,
        fence_generation=current.fence_generation,
        wire_protocol=WIRE_PROTOCOL_V2,
        checkpoint="vault-fingerprinted-" + "b" * 64,
        effect_guard=guard,
    )
    checkpoints = []
    observations = []
    for _ in range(20):
        # Every driver call begins with a new coordinator and adapter instance.
        coordinator = HostedGovernanceMigrationCoordinator(
            cell=KubernetesCellAdapter(core_v1=cluster, apps_v1=cluster),
            jobs=_adapter(cluster),
            now=lambda: clock[0],
        )
        pending = asyncio.run(
            coordinator.advance(
                context=context,
                metadata=current,
                owner=owner,
                pvc_uid="pvc-alpha",
                runtime_image=initial_request["runtimeImage"],
                job_recovery_envelope="current-job-envelope",
                custody_recovery_envelope="signed-envelope",
            )
        )
        assert isinstance(pending, DriverPending)
        decoded = MigrationCheckpoint.decode(pending.checkpoint)
        checkpoints.append(decoded)
        observed = membership.inspect_hosted_authorization_bundle(
            {name: path.read_bytes() for name, path in paths.items()},
            **identity,
            now=clock[0],
            _require_fresh=False,
        )
        observations.append(
            (
                decoded.phase,
                store.authorization_session_schema_version(binding.vault_root),
                observed.governance_enrolled,
                observed.membership_schema_version,
            )
        )
        context = replace(context, checkpoint=pending.checkpoint)
        if decoded.phase == "complete":
            break
    else:
        pytest.fail("coordinator did not reach migration completion")

    assert fault_fired == ([] if interruption == "none" else [interruption])
    assert [phase for phase, _ in groupby(checkpoint.phase for checkpoint in checkpoints)] == [
        "inspect",
        "prepare",
        "enroll",
        "commit",
        "complete",
    ]
    assert ("enroll", 3, True, 3) in observations
    first_commit = next(item for item in observations if item[0] == "commit")
    assert first_commit == ("commit", 4, True, 3)
    assert observations[-1] == ("complete", 4, True, 4)
    if interruption == "none":
        assert [checkpoint.phase for checkpoint in checkpoints] == [
            "inspect",
            "prepare",
            "enroll",
            "enroll",
            "commit",
            "complete",
        ]
    assert len(cutovers) == 1
    assert store.authorization_session_schema_version(binding.vault_root) == 4
    assert paths["keyring.json"].read_bytes() == keyring_before
    assert cluster.job is None and cluster.job_pods == []
    assert len(cluster.created) == len(cluster.deleted)
    assert guarded
    final = membership.inspect_hosted_authorization_bundle(
        {name: path.read_bytes() for name, path in paths.items()},
        **identity,
        now=clock[0],
        _require_fresh=False,
    )
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root,
        expected_plan_digest=checkpoints[-1].plan_digest,
        backup_root=binding.state_root / "governance-migration-backups",
    )
    assert final.replica_state == "DRAINING" and final.no_in_flight
    assert final.governance_enrolled and final.membership_schema_version == 4
    assert (final.activation_store_id, final.activation_epoch, final.activation_state_digest) == (
        backup.target.activation_store_id,
        backup.target.activation_epoch,
        backup.target.activation_state_digest,
    )
    assert len(cluster.publications) == (3 if interruption == "repair-ack" else 2)
    assert {
        str(path.relative_to(binding.vault_root)): path.read_bytes()
        for path in binding.vault_root.rglob("*")
        if path.is_file()
    } == vault_before
    assert cluster.terminals[-1]["actualSchema"] == 4
    assert _custody_bytes() == (final.keyring, final.control, final.membership)
