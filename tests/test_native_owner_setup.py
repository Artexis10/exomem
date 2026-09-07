from __future__ import annotations

import json
import time

import pytest
import test_governance_schema_migration_plan as migration_tests
import yaml

from exomem import state_migration
from exomem.governance import policy, store


@pytest.fixture
def migration_environment(tmp_path, monkeypatch):
    yield from migration_tests._offline_state.__wrapped__(tmp_path, monkeypatch)


def setup_type():
    from exomem import native_owner_setup

    return native_owner_setup.NativeOwnerSetup


def test_initial_policy_uses_canonical_proposal_and_commit(vault):
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_policy(now=now)
    assert policy.load(vault).empty
    document = prepared.display["canonical_yaml"]["scopes/existing-access.yaml"]
    parsed = yaml.safe_load(document)
    assert parsed["name"] == "Existing access"
    assert parsed["paths"] == ["**"]
    assert parsed["default_deny"] is False
    assert len(parsed["id"]) == 26
    assert prepared.display["consequences"]["direction"] in {"narrowing", "widening"}
    with pytest.raises(TypeError):
        prepared.body["proposal_id"] = "changed"
    setup.recheck_policy(prepared.body, now=now + 1)
    result = setup.execute_policy(prepared.body, now=now + 1)
    assert result["status"] == "committed"
    compiled = policy.load(vault)
    assert not compiled.empty and not compiled.blocked
    assert len(compiled.scopes) == 1
    assert not compiled.rules
    with pytest.raises(RuntimeError):
        setup.prepare_policy(now=now + 2)


@pytest.mark.parametrize("drift", ["proposal", "membership", "expired", "body"])
def test_policy_rechecks_exact_stored_proposal_and_baseline(vault, drift):
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_policy(now=now)
    body = dict(prepared.body)
    if drift == "proposal":
        connection = store.open_connection(vault)
        connection.execute("UPDATE governance_proposals SET proposal_json=?", (json.dumps({}),))
        connection.commit()
        connection.close()
    elif drift == "membership":
        (vault / "Knowledge Base" / "new.md").write_text("---\ntype: insight\n---\nNew fact\n")
    elif drift == "body":
        body["canonical_yaml"] = {"scopes/evil.yaml": "different"}
    else:
        now = prepared.expires_at + 1
    with pytest.raises(RuntimeError):
        setup.recheck_policy(body, now=now)
    assert policy.load(vault).empty


def test_migration_requires_offline_proof_and_uses_production_coordinator(
    tmp_path, migration_environment
):
    vault = migration_tests._vault(tmp_path)
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_migration(now=now)
    assert prepared.display["plan_digest"] == prepared.body["expected_plan_digest"]
    assert store.authorization_session_schema_version(vault) == 3
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        setup.execute_migration(prepared.body, offline_authority=None, now=now + 1)
    result = setup.execute_migration(
        prepared.body,
        offline_authority=state_migration.assert_offline_migration_authority(
            source="isolated fixture stop window"
        ),
        now=now + 1,
    )
    assert result["schema_version"] == 4
    assert result["backup_reference"]
    assert store.authorization_session_schema_version(vault) == 4


@pytest.mark.parametrize("drift", ["content", "digest"])
def test_migration_rechecks_reviewed_plan(tmp_path, drift, migration_environment):
    vault = migration_tests._vault(tmp_path)
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_migration(now=now)
    body = dict(prepared.body)
    if drift == "content":
        (vault / "Knowledge Base" / "Notes" / "governed.md").write_text("Changed\n")
    else:
        body["expected_plan_digest"] = "0" * 64
    with pytest.raises(RuntimeError):
        setup.recheck_migration(body, now=now + 1)
    assert store.authorization_session_schema_version(vault) == 3


def test_initial_policy_then_configured_migration(vault, request):
    setup = setup_type()(vault)
    now = int(time.time())
    initial = setup.prepare_policy(now=now)
    setup.execute_policy(initial.body, now=now)
    request.getfixturevalue("migration_environment")
    migration = setup.prepare_migration(now=now)
    result = setup.execute_migration(
        migration.body,
        offline_authority=state_migration.assert_offline_migration_authority(
            source="isolated fixture stop window"
        ),
        now=now + 1,
    )
    assert result["schema_version"] == 4
    compiled = policy.load(vault)
    assert not compiled.blocked
    assert len(compiled.scopes) == 1
    assert not compiled.rules


def test_initial_policy_refuses_prospective_custody_in_serving_environment(
    vault, migration_environment
):
    setup = setup_type()(vault)
    with pytest.raises(RuntimeError):
        setup.prepare_policy(now=int(time.time()))
    connection = store.open_readonly_connection(vault)
    assert connection is None


def test_migration_refuses_changed_configured_destination(
    tmp_path, migration_environment, monkeypatch
):
    from exomem.governance import authorization_custody

    vault = migration_tests._vault(tmp_path)
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_migration(now=now)
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV, str(tmp_path / "external" / "other-control.json")
    )
    with pytest.raises(RuntimeError):
        setup.recheck_migration(prepared.body, now=now)
    assert store.authorization_session_schema_version(vault) == 3


@pytest.mark.parametrize(
    "boundary", ["after_backup", "after_enrollment", "after_store_commit", "completed"]
)
def test_migration_recovery_uses_exact_backup_after_original_expiry(
    tmp_path,
    migration_environment,
    monkeypatch,
    boundary,
):
    from exomem.governance import schema_migration

    vault = migration_tests._vault(tmp_path)
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_migration(now=now)
    authority = state_migration.assert_offline_migration_authority(source="fixture stop window")

    def crash(point):
        if point == boundary:
            raise schema_migration._ForwardMigrationCrash("fixture interrupted migration")

    with monkeypatch.context() as patch:
        patch.setattr(schema_migration, "_forward_migration_barrier", crash)
        patch.setattr(store, "_schema_migration_barrier", crash)
        if boundary == "completed":
            setup.execute_migration(prepared.body, offline_authority=authority, now=now + 1)
        else:
            with pytest.raises(schema_migration._ForwardMigrationCrash):
                setup.execute_migration(prepared.body, offline_authority=authority, now=now + 1)
    recovery_now = prepared.expires_at + 60
    actual_commit = schema_migration.commit_forward_migration
    calls = []

    def commit(*args, **kwargs):
        calls.append(kwargs["now"])
        return actual_commit(*args, **kwargs)

    monkeypatch.setattr(schema_migration, "commit_forward_migration", commit)
    recovered = setup.recover_migration(
        prepared.body, started_at=now + 1, offline_authority=authority, now=recovery_now
    )
    assert recovered["schema_version"] == 4
    assert recovered["plan_digest"] == prepared.body["expected_plan_digest"]
    assert recovered["backup_reference"]
    assert calls == [recovery_now]
    assert recovered["replayed"] is (boundary in {"after_store_commit", "completed"})


@pytest.mark.parametrize("evidence", ["absent", "staged", "tampered"])
def test_expired_applying_marker_cannot_begin_migration(
    tmp_path,
    migration_environment,
    evidence,
):
    from exomem.governance import schema_migration

    vault = migration_tests._vault(tmp_path)
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_migration(now=now)
    if evidence in {"staged", "tampered"}:
        schema_migration.stage_forward_migration(
            vault, expected_plan_digest=prepared.body["expected_plan_digest"], now=now + 1
        )
    if evidence == "tampered":
        path = schema_migration.forward_migration_backup_path(
            vault, plan_digest=prepared.body["expected_plan_digest"]
        )
        path.write_bytes(b"not a verified predecessor backup")
        path.chmod(0o600)
    with pytest.raises(RuntimeError):
        setup.recover_migration(
            prepared.body,
            started_at=now + 1,
            offline_authority=state_migration.assert_offline_migration_authority(
                source="fixture stop window"
            ),
            now=prepared.expires_at + 1,
        )
    assert store.authorization_session_schema_version(vault) == 3


@pytest.mark.parametrize(
    "failure",
    [
        "late_start",
        "early_start",
        "future_start",
        "offline",
        "digest",
        "config",
        "source",
        "attachment",
    ],
)
def test_migration_recovery_rechecks_authority_and_current_bindings(
    tmp_path,
    migration_environment,
    monkeypatch,
    failure,
):
    from exomem.governance import authorization_custody

    vault = migration_tests._vault(tmp_path)
    setup = setup_type()(vault)
    now = int(time.time())
    prepared = setup.prepare_migration(now=now)
    authority = state_migration.assert_offline_migration_authority(source="fixture stop window")
    setup.execute_migration(prepared.body, offline_authority=authority, now=now + 1)
    body = dict(prepared.body)
    started_at = now + 1
    recovery_now = prepared.expires_at + 1
    if failure == "late_start":
        started_at = prepared.expires_at
    elif failure == "early_start":
        started_at = now - 1
    elif failure == "future_start":
        recovery_now = now
    elif failure == "offline":
        authority = None
    elif failure == "digest":
        body["expected_plan_digest"] = "0" * 64
    elif failure == "config":
        monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "another-replica")
    elif failure == "attachment":
        vault.rename(vault.with_name("detached-vault"))
        vault.mkdir()
    elif failure == "source":
        (vault / "Knowledge Base" / "Notes" / "governed.md").write_text("changed source\n")
    with pytest.raises(RuntimeError):
        setup.recover_migration(
            body, started_at=started_at, offline_authority=authority, now=recovery_now
        )
