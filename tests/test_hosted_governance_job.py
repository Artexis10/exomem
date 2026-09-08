from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_hosted_governance_migration import (
    _configure_fixed_hosted_paths,
    _open_v3,
    _vault,
    _write_hosted_custody,
)
from test_hosted_governance_migration import (
    _offline_state as _offline_state,
)

from exomem import hosted_runtime
from exomem.governance import authorization_custody, schema_migration, store

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Hosted Job uses Linux root ownership")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _custody_bytes() -> tuple[bytes, ...]:
    return tuple(
        Path(os.environ[name]).read_bytes()
        for name in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
            authorization_custody.MEMBERSHIP_FILE_ENV,
        )
    )


def _revision() -> str:
    return hashlib.sha256(b"".join(_custody_bytes())).hexdigest()


@pytest.fixture
def cell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(state / "vault-state"))
    vault = _vault(tmp_path)
    vault.chmod(0o700)
    now = int(time.time())
    _write_hosted_custody(vault, now=now)
    _configure_fixed_hosted_paths(monkeypatch)
    _open_v3(vault)
    custody = authorization_custody.load_authorization_custody(vault, now=now)
    binding = hosted_runtime.HostedBindingV2(
        cell_id=custody.control.cell_id,
        vault_id=custody.control.logical_vault_id,
        vault_root=vault,
        state_root=state,
        log_root=logs,
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    for kind, root in binding.roots():
        hosted_runtime._write_v2_marker(root, kind, binding)
    for name, value in {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_HOSTED_STATE_ROOT": str(state),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state),
    }.items():
        monkeypatch.setenv(name, value)
    return (
        binding,
        now,
        {
            "schemaVersion": 1,
            "phase": "inspect",
            "cellId": custody.control.cell_id,
            "vaultId": custody.control.logical_vault_id,
            "replicaId": custody.local_replica_id,
            "operationId": "migration-alpha",
            "fenceGeneration": 7,
            "pvcUid": "pvc-alpha",
            "runtimeImage": "ghcr.io/example/runtime@sha256:" + "a" * 64,
            "custodyRevision": _revision(),
            "sourceStoreDigest": None,
            "planDigest": None,
        },
    )


def _job(monkeypatch: pytest.MonkeyPatch, binding):
    from exomem import hosted_governance_job

    monkeypatch.setattr(hosted_governance_job, "VAULT_ROOT", binding.vault_root)
    monkeypatch.setattr(hosted_governance_job, "STATE_ROOT", binding.state_root)
    monkeypatch.setattr(hosted_governance_job, "LOG_ROOT", binding.log_root)
    monkeypatch.setattr(hosted_governance_job, "RUNTIME_UID", binding.runtime_uid)
    monkeypatch.setattr(hosted_governance_job, "RUNTIME_GID", binding.runtime_gid)
    return hosted_governance_job


def _prepare(job, request, now):
    inspected = job.execute(_canonical(request), now=now)
    prepare_request = {
        **request,
        "phase": "prepare",
        "sourceStoreDigest": inspected["sourceStoreDigest"],
    }
    prepared = job.execute(_canonical(prepare_request), now=now)
    return prepare_request, prepared


def _replace_membership(binding, now, **changes):
    custody = authorization_custody.load_authorization_custody(binding.vault_root, now=now)
    fields = {
        "state": "DRAINING",
        "schema_version": 3,
        "issuance_stopped": True,
        "no_in_flight": True,
        **changes,
    }
    membership = authorization_custody._standalone_membership_bytes(
        keyring=custody.keyring,
        control=custody.control,
        replica_id=custody.local_replica_id,
        previous_epoch_digest="c" * 64,
        **fields,
    )
    control = replace(
        custody.control, serving_membership_digest=hashlib.sha256(membership).hexdigest()
    )
    custody.membership_path.write_bytes(membership)
    custody.control_path.write_bytes(
        authorization_custody._signed_control_bytes(
            control, signing_key=custody.keyring.active_key.key
        )
    )


def test_job_inspection_observes_real_v3_without_writes(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    database = store.sidecar_path(binding.vault_root)
    before = database.read_bytes(), _custody_bytes()

    result = job.execute(_canonical(request), now=now)

    assert result["phase"] == "inspect"
    assert result["requestSha256"] == hashlib.sha256(_canonical(request)).hexdigest()
    assert result["actualSchema"] == 3
    assert result["membershipSchema"] == 3
    assert result["governanceEnrolled"] is False
    assert result["sourceStoreDigest"] == schema_migration._source_store_digest(binding.vault_root)
    assert (database.read_bytes(), _custody_bytes()) == before
    assert not (binding.state_root / "governance-migration-backups").exists()


def test_job_prepares_then_commits_only_after_external_enrollment(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    before = _custody_bytes()
    prepare_request, prepared = _prepare(job, request, now)
    assert _custody_bytes() == before
    assert prepared["actualSchema"] == 3
    assert store.authorization_session_schema_version(binding.vault_root) == 3
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root,
        expected_plan_digest=prepared["planDigest"],
        backup_root=binding.state_root / "governance-migration-backups",
    )
    assert prepared["backupReference"] == backup.backup_reference
    assert prepared["backupDigest"] == backup.backup_digest
    assert prepared["activationStoreId"] == backup.target.activation_store_id
    commit_request = {**prepare_request, "phase": "commit", "planDigest": prepared["planDigest"]}
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical(commit_request), now=now)
    assert store.authorization_session_schema_version(binding.vault_root) == 3

    _write_hosted_custody(binding.vault_root, now=now, enrolled_target=backup.target)
    commit_request["custodyRevision"] = _revision()
    enrolled_bytes = _custody_bytes()
    result = job.execute(_canonical(commit_request), now=now)
    replay = job.execute(_canonical(commit_request), now=now)
    assert result["actualSchema"] == replay["actualSchema"] == 4
    assert result["replayed"] is False
    assert replay["replayed"] is True
    assert result["activationStateDigest"] == backup.target.activation_state_digest
    assert _custody_bytes() == enrolled_bytes
    assert len(_canonical(result)) < 4096
    assert b"governance_version" not in _canonical(result)
    assert authorization_custody.load_authorization_custody(
        binding.vault_root, now=now
    ).keyring.active_key.key not in _canonical(result)


@pytest.mark.parametrize("interrupted_after_commit", [False, True])
def test_enrolled_job_recovers_after_expiry_without_restoring_serving_authority(
    cell, monkeypatch, interrupted_after_commit
):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    prepare_request, prepared = _prepare(job, request, now)
    backup_root = binding.state_root / "governance-migration-backups"
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root,
        expected_plan_digest=prepared["planDigest"],
        backup_root=backup_root,
    )
    _write_hosted_custody(binding.vault_root, now=now, enrolled_target=backup.target)
    request = {
        **prepare_request,
        "phase": "commit",
        "planDigest": prepared["planDigest"],
        "custodyRevision": _revision(),
    }
    before = _custody_bytes()
    if interrupted_after_commit:

        def crash(point):
            if point == "after_store_commit":
                raise schema_migration._ForwardMigrationCrash("lost acknowledgement")

        with monkeypatch.context() as crash_patch:
            crash_patch.setattr(store, "_schema_migration_barrier", crash)
            with pytest.raises(job.HostedGovernanceJobError):
                job.execute(_canonical(request), now=now)
        assert store.authorization_session_schema_version(binding.vault_root) == 4

    expired = now + 3601
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(binding.vault_root, now=expired)
    with pytest.raises(schema_migration.ForwardMigrationUnavailable):
        schema_migration.commit_enrolled_forward_migration(
            binding.vault_root,
            expected_plan_digest=prepared["planDigest"],
            now=expired,
            backup_root=backup_root,
        )

    result = job.execute(_canonical(request), now=expired)
    assert result["actualSchema"] == 4
    assert result["replayed"] is interrupted_after_commit
    assert result["activationStateDigest"] == backup.target.activation_state_digest
    assert _custody_bytes() == before
    database = store.sidecar_path(binding.vault_root).read_bytes()
    assert job.execute(_canonical(request), now=expired)["replayed"] is True
    assert store.sidecar_path(binding.vault_root).read_bytes() == database
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(binding.vault_root, now=expired)


@pytest.mark.parametrize(
    "drift",
    [
        "unenrolled",
        "serving",
        "in-flight",
        "schema-claim",
        "target",
        "revision",
        "source",
        "plan",
        "backup",
        "missing-backup",
        "workspace",
        "key-expiry",
    ],
)
def test_expired_commit_still_refuses_unproven_recovery(cell, monkeypatch, drift):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    prepare_request, prepared = _prepare(job, request, now)
    backup_root = binding.state_root / "governance-migration-backups"
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root, expected_plan_digest=prepared["planDigest"], backup_root=backup_root
    )
    if drift != "unenrolled":
        target = backup.target
        if drift == "target":
            target = replace(target, activation_state_digest="f" * 64)
        _write_hosted_custody(binding.vault_root, now=now, enrolled_target=target)
    if drift == "serving":
        _replace_membership(
            binding, now, state="SERVING", issuance_stopped=False, no_in_flight=False
        )
    elif drift == "in-flight":
        _replace_membership(binding, now, no_in_flight=False)
    elif drift == "schema-claim":
        _replace_membership(binding, now, schema_version=4)
    elif drift == "backup":
        schema_migration.forward_migration_backup_path(
            binding.vault_root, plan_digest=prepared["planDigest"], backup_root=backup_root
        ).write_bytes(b"corrupt-backup")
    elif drift == "missing-backup":
        schema_migration.forward_migration_backup_path(
            binding.vault_root, plan_digest=prepared["planDigest"], backup_root=backup_root
        ).unlink()
    elif drift == "workspace":
        policy_path = binding.vault_root / "Knowledge Base/_Governance/scopes/migration.yaml"
        policy_path.write_bytes(policy_path.read_bytes() + b"# changed after preparation\n")
    request = {
        **prepare_request,
        "phase": "commit",
        "planDigest": prepared["planDigest"],
        "custodyRevision": _revision(),
    }
    if drift in {"revision", "source", "plan"}:
        request[
            {"revision": "custodyRevision", "source": "sourceStoreDigest", "plan": "planDigest"}[
                drift
            ]
        ] = "f" * 64
    expired = now + 3601
    if drift == "key-expiry":
        expired = authorization_custody.parse_keyring(_custody_bytes()[0]).active_key.not_after
    before = store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical(request), now=expired)
    assert store.authorization_session_schema_version(binding.vault_root) == 3
    assert (store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()) == before


@pytest.mark.parametrize("phase", ["inspect", "prepare"])
def test_expiry_cannot_start_a_new_migration(cell, monkeypatch, phase):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    if phase == "prepare":
        inspected = job.execute(_canonical(request), now=now)
        request.update(phase="prepare", sourceStoreDigest=inspected["sourceStoreDigest"])
    before = store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical(request), now=now + 3601)
    assert (store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()) == before
    assert not (binding.state_root / "governance-migration-backups").exists()


@pytest.mark.parametrize("successor", ["exact", "renewed", "wrong-predecessor", "skipped"])
def test_expired_v4_replay_binds_the_prepared_window_and_membership_lineage(
    cell, monkeypatch, successor
):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    prepare_request, prepared = _prepare(job, request, now)
    backup_root = binding.state_root / "governance-migration-backups"
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root, expected_plan_digest=prepared["planDigest"], backup_root=backup_root
    )
    _write_hosted_custody(binding.vault_root, now=now, enrolled_target=backup.target)
    request = {
        **prepare_request,
        "phase": "commit",
        "planDigest": prepared["planDigest"],
        "custodyRevision": _revision(),
    }
    job.execute(_canonical(request), now=now)
    custody = authorization_custody.load_authorization_custody(binding.vault_root, now=now)
    control = replace(
        custody.control,
        serving_membership_epoch=custody.control.serving_membership_epoch
        + (2 if successor == "skipped" else 1),
        issued_at=custody.control.issued_at + (1 if successor == "renewed" else 0),
        expires_at=custody.control.expires_at + (1 if successor == "renewed" else 0),
    )
    membership = authorization_custody._standalone_membership_bytes(
        keyring=custody.keyring,
        control=control,
        replica_id=custody.local_replica_id,
        state="DRAINING",
        schema_version=4,
        issuance_stopped=True,
        no_in_flight=True,
        previous_epoch_digest="f" * 64
        if successor == "wrong-predecessor"
        else custody.control.serving_membership_digest,
    )
    control = replace(control, serving_membership_digest=hashlib.sha256(membership).hexdigest())
    custody.membership_path.write_bytes(membership)
    custody.control_path.write_bytes(
        authorization_custody._signed_control_bytes(
            control, signing_key=custody.keyring.active_key.key
        )
    )
    request["custodyRevision"] = _revision()
    if successor == "renewed":
        # Normal fresh replay retains its existing coherent-renewal contract.
        assert schema_migration.commit_enrolled_forward_migration(
            binding.vault_root,
            expected_plan_digest=prepared["planDigest"],
            now=now + 5,
            backup_root=backup_root,
        ).replayed
    before = store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()
    if successor == "exact":
        assert job.execute(_canonical(request), now=now + 7200)["replayed"]
    else:
        with pytest.raises(job.HostedGovernanceJobError):
            job.execute(_canonical(request), now=now + 7200)
    assert (store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()) == before


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"schemaVersion": True},
        {"phase": "enroll"},
        {"fenceGeneration": False},
        {"fenceGeneration": 0},
        {"runtimeImage": "runtime:latest"},
        {"pvcUid": ""},
        {"operationId": "x" * 513},
        {"cellId": "foreign-cell"},
        {"vaultId": "foreign-vault"},
        {"replicaId": "foreign-replica"},
        {"custodyRevision": "b" * 64},
        {"planDigest": "b" * 64},
        {"sourceStoreDigest": "b" * 64},
    ],
)
def test_job_rejects_invalid_or_foreign_request_before_store_access(cell, monkeypatch, mutation):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    monkeypatch.setattr(
        store, "authorization_session_schema_version", lambda *_: pytest.fail("store opened")
    )
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical({**request, **mutation}), now=now)


def test_job_rejects_source_and_plan_drift(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(
            _canonical({**request, "phase": "prepare", "sourceStoreDigest": "b" * 64}), now=now
        )
    assert not (binding.state_root / "governance-migration-backups").exists()
    prepare_request, prepared = _prepare(job, request, now)
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root,
        expected_plan_digest=prepared["planDigest"],
        backup_root=binding.state_root / "governance-migration-backups",
    )
    _write_hosted_custody(binding.vault_root, now=now, enrolled_target=backup.target)
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(
            _canonical(
                {
                    **prepare_request,
                    "phase": "commit",
                    "planDigest": "b" * 64,
                    "custodyRevision": _revision(),
                }
            ),
            now=now,
        )
    assert store.authorization_session_schema_version(binding.vault_root) == 3


def test_job_legacy_schema_claim_is_observed_but_never_repaired(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    _replace_membership(binding, now, schema_version=4)
    request["custodyRevision"] = _revision()
    before = _custody_bytes()
    result = job.execute(_canonical(request), now=now)
    assert result["actualSchema"] == 3
    assert result["membershipSchema"] == 4
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(
            _canonical(
                {**request, "phase": "prepare", "sourceStoreDigest": result["sourceStoreDigest"]}
            ),
            now=now,
        )
    assert _custody_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [b"{}", b"[]", b"{", b"x" * 8193, b'{"phase":"inspect","phase":"commit"}'],
    ids=["empty", "array", "invalid-json", "oversized", "duplicate-key"],
)
def test_job_refuses_malformed_bounded_wire(cell, monkeypatch, raw):
    binding, now, _ = cell
    job = _job(monkeypatch, binding)
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(raw, now=now)


def test_job_entrypoint_failure_is_content_free(cell, monkeypatch, capsys, tmp_path):
    binding, _, _ = cell
    job = _job(monkeypatch, binding)
    terminal = tmp_path / "termination-log"
    monkeypatch.setattr(job, "TERMINATION_LOG", terminal)
    monkeypatch.setenv("EXOMEM_GOVERNANCE_MIGRATION_REQUEST", "private-request-sentinel")
    assert job.main() == 1
    assert json.loads(terminal.read_bytes()) == {"code": "HOSTED_GOVERNANCE_JOB_FAILED"}
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("phase", ["prepare", "commit"])
def test_job_mutation_refuses_live_lifetime_lock(cell, monkeypatch, phase):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    request.update(phase=phase, sourceStoreDigest="b" * 64)
    if phase == "commit":
        request["planDigest"] = "c" * 64
    monkeypatch.setattr(
        store, "authorization_session_schema_version", lambda *_: pytest.fail("store opened")
    )
    with job.hosted_restore.acquire_hosted_lifetime_lock(binding.state_root, binding=binding):
        with pytest.raises(job.HostedGovernanceJobError):
            job.execute(_canonical(request), now=now)
    assert not (binding.state_root / "governance-migration-backups").exists()


@pytest.mark.parametrize(
    "name",
    [
        "EXOMEM_HOSTED_CELL",
        "EXOMEM_VAULT_PATH",
        "EXOMEM_HOSTED_STATE_ROOT",
        "EXOMEM_STATE_ROOT",
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
    ],
)
def test_job_requires_exact_hosted_environment_before_store_access(cell, monkeypatch, name):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    monkeypatch.delenv(name)
    monkeypatch.setattr(
        store, "authorization_session_schema_version", lambda *_: pytest.fail("store opened")
    )
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical(request), now=now)


def test_job_refuses_tampered_root_marker_before_store_access(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    (binding.log_root / ".exomem-hosted-cell.json").write_text("{}")
    monkeypatch.setattr(
        store, "authorization_session_schema_version", lambda *_: pytest.fail("store opened")
    )
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical(request), now=now)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "SERVING", "issuance_stopped": False, "no_in_flight": False},
        {"no_in_flight": False},
    ],
)
def test_job_refuses_membership_without_complete_drain(cell, monkeypatch, changes):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    _replace_membership(binding, now, **changes)
    request["custodyRevision"] = _revision()
    monkeypatch.setattr(
        store, "authorization_session_schema_version", lambda *_: pytest.fail("store opened")
    )
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical(request), now=now)


@pytest.mark.parametrize("kind", ["public", "symlink"])
def test_job_refuses_unsafe_backup_root_without_publication(cell, monkeypatch, tmp_path, kind):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    backup_root = binding.state_root / "governance-migration-backups"
    if kind == "public":
        backup_root.mkdir(mode=0o755)
        destination = backup_root
    else:
        destination = tmp_path / "foreign-backup-root"
        destination.mkdir(mode=0o700)
        backup_root.symlink_to(destination, target_is_directory=True)
    with pytest.raises(job.HostedGovernanceJobError):
        _prepare(job, request, now)
    assert list(destination.iterdir()) == []
    assert store.authorization_session_schema_version(binding.vault_root) == 3


def test_job_custody_drift_after_staging_refuses_before_backup(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    stage = schema_migration.stage_forward_migration

    def stage_then_change_custody(*args, **kwargs):
        result = stage(*args, **kwargs)
        _write_hosted_custody(binding.vault_root, now=now + 1)
        return result

    monkeypatch.setattr(schema_migration, "stage_forward_migration", stage_then_change_custody)
    with pytest.raises(job.HostedGovernanceJobError):
        _prepare(job, request, now)
    assert not (binding.state_root / "governance-migration-backups").exists()
    assert store.authorization_session_schema_version(binding.vault_root) == 3


def test_job_missing_backup_refuses_before_cutover(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    prepare_request, prepared = _prepare(job, request, now)
    backup_root = binding.state_root / "governance-migration-backups"
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root,
        expected_plan_digest=prepared["planDigest"],
        backup_root=backup_root,
    )
    _write_hosted_custody(binding.vault_root, now=now, enrolled_target=backup.target)
    schema_migration.forward_migration_backup_path(
        binding.vault_root,
        plan_digest=prepared["planDigest"],
        backup_root=backup_root,
    ).unlink()
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(
            _canonical(
                {
                    **prepare_request,
                    "phase": "commit",
                    "planDigest": prepared["planDigest"],
                    "custodyRevision": _revision(),
                }
            ),
            now=now,
        )
    assert store.authorization_session_schema_version(binding.vault_root) == 3


def test_job_entrypoint_success_is_bounded_and_silent(cell, monkeypatch, capsys, tmp_path):
    binding, _, request = cell
    job = _job(monkeypatch, binding)
    terminal = tmp_path / "termination-log"
    monkeypatch.setattr(job, "TERMINATION_LOG", terminal)
    monkeypatch.setenv(job.REQUEST_ENV, _canonical(request).decode())
    assert job.main() == 0
    assert json.loads(terminal.read_bytes())["actualSchema"] == 3
    assert len(terminal.read_bytes()) < job.MAX_TERMINAL_BYTES
    assert capsys.readouterr() == ("", "")


def test_job_entrypoint_never_logs_unexpected_exception(cell, monkeypatch, capsys, tmp_path):
    binding, _, request = cell
    job = _job(monkeypatch, binding)
    terminal = tmp_path / "termination-log"

    def fail(*_args, **_kwargs):
        raise KeyError("private-exception-sentinel")

    monkeypatch.setattr(job, "TERMINATION_LOG", terminal)
    monkeypatch.setattr(job, "execute", fail)
    monkeypatch.setenv(job.REQUEST_ENV, _canonical(request).decode())
    assert job.main() == 1
    assert json.loads(terminal.read_bytes()) == {"code": "HOSTED_GOVERNANCE_JOB_FAILED"}
    assert capsys.readouterr() == ("", "")


def test_job_tests_collect_without_linux_lock_module():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
import test_hosted_governance_migration

class NoFcntl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'fcntl':
            raise ModuleNotFoundError("No module named 'fcntl'")

sys.modules.pop('fcntl', None)
sys.meta_path.insert(0, NoFcntl())
import test_hosted_governance_job
""",
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
