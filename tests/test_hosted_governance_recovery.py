from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import replace

import pytest
from test_hosted_governance_job import (
    _canonical,
    _custody_bytes,
    _job,
    _prepare,
    _revision,
    _write_hosted_custody,
)
from test_hosted_governance_job import cell as cell
from test_hosted_governance_migration import _offline_state as _offline_state

from exomem.governance import authorization_custody as custody_module
from exomem.governance import authorization_serving_membership as membership_module
from exomem.governance import schema_migration, schema_v4, store

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Hosted recovery uses Linux root ownership")


def _enrolled(cell, monkeypatch):
    binding, now, request = cell
    job = _job(monkeypatch, binding)
    request, prepared = _prepare(job, request, now)
    backup_root = binding.state_root / "governance-migration-backups"
    backup = schema_migration.verify_forward_migration_backup(
        binding.vault_root, expected_plan_digest=prepared["planDigest"], backup_root=backup_root
    )
    _write_hosted_custody(binding.vault_root, now=now, enrolled_target=backup.target)
    request.update(phase="commit", planDigest=backup.plan_digest, custodyRevision=_revision())
    custody = custody_module.load_authorization_custody(binding.vault_root, now=now)
    return binding, now, job, request, custody


@pytest.mark.parametrize(
    "case",
    [
        "fresh",
        "control-future",
        "control-empty-window",
        "member-future",
        "member-empty-window",
        "member-oversize-window",
        "attestation-future",
        "attestation-empty-window",
        "attestation-oversize-window",
        "missing-membership",
        "multiple-keys",
        "nonfixed-path",
    ],
)
def test_recovery_reader_does_not_relax_any_other_custody_rule(cell, monkeypatch, case):
    binding, now, _job_module, _request, custody = _enrolled(cell, monkeypatch)
    current = now + 3601
    control = custody.control
    if case == "fresh":
        current = now
    elif case == "nonfixed-path":
        monkeypatch.setenv(custody_module.CONTROL_FILE_ENV, str(binding.state_root / "other.json"))
    elif case == "missing-membership":
        custody.membership_path.unlink()
    elif case == "multiple-keys":
        keyring = replace(
            custody.keyring,
            accepted_keys=(
                *custody.keyring.accepted_keys,
                replace(custody.keyring.active_key, key_id="another-key", key=b"z" * 32),
            ),
        )
        custody.keyring_path.write_bytes(custody_module._keyring_bytes(keyring))
    elif case.startswith("control-"):
        control = replace(
            control,
            issued_at=now + 7200 if case == "control-future" else now,
            expires_at=now + 10800 if case == "control-future" else now,
        )
        custody.control_path.write_bytes(
            custody_module._signed_control_bytes(
                control, signing_key=custody.keyring.active_key.key
            )
        )
    else:
        value = json.loads(custody.membership_path.read_bytes())
        target = value["replicas"][0] if case.startswith("attestation-") else value
        start = "attested_at" if case.startswith("attestation-") else "issued_at"
        if case.endswith("future"):
            target[start], target["expires_at"] = now + 7200, now + 10800
        elif case.endswith("empty-window"):
            target["expires_at"] = target[start]
        else:
            target["expires_at"] = target[start] + membership_module.MAX_ATTESTATION_TTL_SECONDS + 1

        def mac(fields):
            return (
                base64.urlsafe_b64encode(
                    hmac.new(custody.keyring.active_key.key, fields, hashlib.sha256).digest()
                )
                .rstrip(b"=")
                .decode()
            )

        if target is not value:
            target["mac"] = mac(membership_module._attestation_mac_input(target))
        value["mac"] = mac(membership_module._record_mac_input(value))
        raw = _canonical(value)
        # This is an authenticated malformed/future record, not merely a bad MAC.
        with pytest.raises(membership_module.ServingMembershipUnavailable):
            membership_module._parse_serving_membership(
                raw,
                verifier_keys={custody.keyring.active_key_id: custody.keyring.active_key.key},
                now=current,
                expected_cell_id=control.cell_id,
                expected_logical_vault_id=control.logical_vault_id,
                expected_epoch=control.serving_membership_epoch,
                expected_digest=hashlib.sha256(raw).hexdigest(),
                allow_expired=True,
            )
        custody.membership_path.write_bytes(raw)
        control = replace(control, serving_membership_digest=hashlib.sha256(raw).hexdigest())
        custody.control_path.write_bytes(
            custody_module._signed_control_bytes(
                control, signing_key=custody.keyring.active_key.key
            )
        )
    with pytest.raises(custody_module.AuthorizationCustodyUnavailable):
        custody_module.load_hosted_migration_custody(binding.vault_root, now=current)
    assert store.authorization_session_schema_version(binding.vault_root) == 3


@pytest.mark.parametrize("point", ["transaction-entry", "after-commit"])
def test_recovery_rechecks_custody_around_database_effect(cell, monkeypatch, point):
    binding, now, job, request, custody = _enrolled(cell, monkeypatch)

    def change():
        control = replace(custody.control, attachment_epoch=custody.control.attachment_epoch + 1)
        custody.control_path.write_bytes(
            custody_module._signed_control_bytes(
                control, signing_key=custody.keyring.active_key.key
            )
        )

    if point == "transaction-entry":
        commit = store.commit_hosted_enrolled_v3_store

        def changed(*args, **kwargs):
            change()
            return commit(*args, **kwargs)

        monkeypatch.setattr(store, "commit_hosted_enrolled_v3_store", changed)
    else:
        commit = schema_migration.commit_hosted_forward_migration

        def changed(*args, **kwargs):
            result = commit(*args, **kwargs)
            change()
            return result

        monkeypatch.setattr(schema_migration, "commit_hosted_forward_migration", changed)
    with pytest.raises(job.HostedGovernanceJobError):
        job.execute(_canonical(request), now=now + 3601)
    assert store.authorization_session_schema_version(binding.vault_root) == (
        3 if point == "transaction-entry" else 4
    )


def test_expired_replay_never_reenters_cutover(cell, monkeypatch):
    binding, now, job, request, _custody = _enrolled(cell, monkeypatch)
    job.execute(_canonical(request), now=now)
    before = store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("verified v4 replay attempted a second cutover")

    monkeypatch.setattr(schema_v4, "migrate_v3_connection", forbidden)
    assert job.execute(_canonical(request), now=now + 3601)["replayed"]
    assert (store.sidecar_path(binding.vault_root).read_bytes(), _custody_bytes()) == before
