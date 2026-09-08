"""Native setup reaches real custody and authority using disposable state."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from test_native_owner_maintenance import _systemd_unit

from exomem import native_owner_preparation, state_migration, vocabulary_authority
from exomem.governance import authorization_custody as custody
from exomem.governance import authorization_session_lifecycle, store
from exomem.native_owner_control import NativeOwnerControl
from exomem.native_owner_reviews import NativeOwnerReviewDenied, _thaw

OWNER = "github:123"


@pytest.fixture
def managed(vault, tmp_path, monkeypatch):
    unit = _systemd_unit(tmp_path, vault=vault, state=Path(os.environ["EXOMEM_STATE_ROOT"]))
    monkeypatch.setenv("EXOMEM_OWNER_SERVICE_UNIT", str(unit))
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(tmp_path / "authority"))
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "123")
    monkeypatch.setattr(custody, "_standalone_host_control_root", lambda: tmp_path / "host-control")
    for key in native_owner_preparation._CONFIG:
        monkeypatch.delenv(key, raising=False)
    return NativeOwnerControl(vault), unit


def _policy(control, now):
    assert control.setup_status(now=now) == "policy"
    review = control.prepare(owner_id=OWNER, action="policy", body={}, now=now)
    assert "canonical_yaml" in review.display
    assert control.accept(review.review_id, owner_id=OWNER, now=now).state == "completed"


def _maintenance(control, unit, review, monkeypatch, now):
    accepted = control.accept(review.review_id, owner_id=OWNER, now=now)
    assert accepted.state == "accepted"
    for key, value in review.body["custody_environment"].items():
        monkeypatch.setenv(key, value)
    result = control.apply_maintenance(
        review.review_id,
        owner_id=OWNER,
        offline_authority=state_migration.assert_offline_migration_authority(
            source="disposable test service"
        ),
        now=now,
    )
    # Production runner persists exactly these approved settings after apply.
    from exomem.native_owner_maintenance import service_binding

    envfile = service_binding(unit).binding_path
    current = envfile.read_text()
    for key, value in review.body["custody_environment"].items():
        if f"{key}=" not in current:
            current += f'{key}="{value}"\n'
    envfile.write_text(current)
    return result


def test_real_policy_child_migration_floor_activation_and_owner_grant(managed, monkeypatch):
    from exomem import commands, entity_types, writer_lease
    from exomem.governance.principal import RequestPrincipal, request_scope

    control, unit = managed
    now = int(time.time())
    _policy(control, now)
    assert control.setup_status(now=now) == "migration"
    before = {key: os.environ.get(key) for key in native_owner_preparation._CONFIG}
    migration = control.prepare(owner_id=OWNER, action="migration", body={}, now=now)
    assert {key: os.environ.get(key) for key in native_owner_preparation._CONFIG} == before
    assert not Path(migration.body["custody_environment"][custody.CONTROL_FILE_ENV]).exists()
    assert store.authorization_session_schema_version(control.vault_root) == 3
    result = _maintenance(control, unit, migration, monkeypatch, now)
    now = int(time.time())
    assert result["schema_version"] == 4
    assert control.setup_status(now=now) == "activation"
    activation = control.prepare(owner_id=OWNER, action="activation", body={}, now=now)
    assert activation.display["default_grants"] == ()
    assert activation.body["renewal"] == "same-authority"
    result = _maintenance(control, unit, activation, monkeypatch, now)
    now = int(time.time())
    assert result == {"status": "completed", "mode": "v2", "default_grants": []}
    assert control.setup_status(now=now) == "active"
    assert control.reviews.renewal_review(owner_id=OWNER, now=now).review_id == activation.review_id
    assert control.sessions(now=now)["items"] == []
    current = custody.load_authorization_custody(control.vault_root, now=now)
    connection = store.open_authorization_session_connection(control.vault_root)
    try:
        issued = authorization_session_lifecycle.open_session(
            connection,
            custody=current,
            principal_id="agent",
            issuer_family="test-service",
            now=now,
            ttl_seconds=3600,
        )
    finally:
        connection.close()
    session_id = issued.context.session_id
    assert control.sessions(now=now)["items"][0]["session_id"] == session_id
    assert control.grants(session_id, now=now)["items"] == []
    grant = control.prepare(
        owner_id=OWNER,
        action="grant",
        body={
            "session_id": session_id,
            "actions": ["entity_type.add"],
            "scope": "vault",
            "expires_at": now + 3600,
        },
        now=now,
    )
    completed = control.accept(grant.review_id, owner_id=OWNER, now=now)
    grants = control.grants(session_id, now=now)["items"]
    assert grants[0]["authority_id"] == completed.result["authority_id"]
    principal = RequestPrincipal(
        audience_id=issued.context.principal_id,
        surface="native-owner-test",
        authorization_session_id=session_id,
        issuer_family=issued.context.issuer_family,
        verified_authorization_session=issued.context,
    )
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "schema_memory")
    venue = {
        "folder": "Venues",
        "label": "Venue",
        "aliases": [],
        "capture_guidance": "Capture a stable place for recurring events.",
        "status": "active",
        "parent": "concept",
    }
    arguments = {
        "operation": "save-entity-types",
        "proposal": {"schema_version": 1, "entity_types": {"venue": venue}},
        "expected_hash": entity_types.load_entity_types(control.vault_root).extension_hash,
        "why": "Register the reviewed structural meaning.",
    }
    with request_scope(principal):
        written = writer_lease.get_manager().invoke(
            command,
            (control.vault_root,),
            arguments,
            idempotency_key="native-owner-venue",
            read_only=False,
        )
    assert written["state"] == "committed" and written["receipt_id"]
    assert entity_types.load_entity_types(control.vault_root).resolve("venue") is not None
    revoke = control.prepare(
        owner_id=OWNER,
        action="revoke",
        body={"session_id": session_id, "authority_id": grants[0]["authority_id"]},
        now=now,
    )
    control.accept(revoke.review_id, owner_id=OWNER, now=now)
    assert control.grants(session_id, now=now)["items"] == []
    arguments["expected_hash"] = entity_types.load_entity_types(control.vault_root).extension_hash
    arguments["proposal"]["entity_types"]["guild"] = {**venue, "folder": "Guilds", "label": "Guild"}
    with request_scope(principal), pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        writer_lease.get_manager().invoke(
            command,
            (control.vault_root,),
            arguments,
            idempotency_key="native-owner-guild",
            read_only=False,
        )
    assert entity_types.load_entity_types(control.vault_root).resolve("guild") is None


def test_browser_acceptance_never_runs_offline_migration(managed):
    control, _unit = managed
    now = int(time.time())
    _policy(control, now)
    review = control.prepare(owner_id=OWNER, action="migration", body={}, now=now)
    assert control.accept(review.review_id, owner_id=OWNER, now=now).state == "accepted"
    assert control.accept(review.review_id, owner_id=OWNER, now=now).state == "accepted"
    assert store.authorization_session_schema_version(control.vault_root) == 3
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        control.apply_maintenance(review.review_id, owner_id=OWNER, offline_authority=None, now=now)


def test_expired_unstarted_migration_cannot_gain_authority(managed, monkeypatch):
    control, _unit = managed
    now = int(time.time())
    _policy(control, now)
    review = control.prepare(owner_id=OWNER, action="migration", body={}, now=now)
    control.accept(review.review_id, owner_id=OWNER, now=now)
    for key, value in _thaw(review.body["custody_environment"]).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(NativeOwnerReviewDenied):
        control.apply_maintenance(
            review.review_id,
            owner_id=OWNER,
            offline_authority=state_migration.assert_offline_migration_authority(
                source="test stop"
            ),
            now=review.expires_at + 1,
        )
    assert store.authorization_session_schema_version(control.vault_root) == 3
    assert (
        vocabulary_authority.VocabularyAuthority(control.vault_root).runtime_status(now=now).mode
        != "v2"
    )


def test_slow_floor_publication_needs_fresh_review_for_activation(managed, monkeypatch):
    from exomem import native_owner_control, vocabulary_deployment

    control, unit = managed
    now = int(time.time())
    _policy(control, now)
    migration = control.prepare(owner_id=OWNER, action="migration", body={}, now=now)
    _maintenance(control, unit, migration, monkeypatch, now)
    now = int(time.time())
    activation = control.prepare(owner_id=OWNER, action="activation", body={}, now=now)
    original = vocabulary_deployment.publish_standalone_floor

    def slow_publish(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(native_owner_control.time, "time", lambda: now + 301)
        return result

    monkeypatch.setattr(vocabulary_deployment, "publish_standalone_floor", slow_publish)
    result = _maintenance(control, unit, activation, monkeypatch, now)
    assert result["activation"] == "owner_review_required"
    assert (
        control.reviews.renewal_review(owner_id=OWNER, now=now + 301).review_id
        == activation.review_id
    )
    fresh = control.prepare(owner_id=OWNER, action="activation", body={}, now=now + 301)
    assert fresh.body["setup"]["kind"] == "activate_current_floor"
    assert _maintenance(control, unit, fresh, monkeypatch, now + 301)["mode"] == "v2"


def test_activation_guard_wait_cannot_extend_owner_consent(managed, monkeypatch):
    from contextlib import contextmanager

    from exomem import native_owner_control, writer_lease

    control, unit = managed
    now = int(time.time())
    _policy(control, now)
    migration = control.prepare(owner_id=OWNER, action="migration", body={}, now=now)
    _maintenance(control, unit, migration, monkeypatch, now)
    now = int(time.time())
    activation = control.prepare(owner_id=OWNER, action="activation", body={}, now=now)
    manager = writer_lease.get_manager()
    original = manager.consistency_guard

    @contextmanager
    def delayed_guard(*args, **kwargs):
        with original(*args, **kwargs):
            if kwargs.get("operation") == "native-owner-activation":
                monkeypatch.setattr(native_owner_control.time, "time", lambda: now + 301)
            yield

    monkeypatch.setattr(manager, "consistency_guard", delayed_guard)
    with pytest.raises(NativeOwnerReviewDenied):
        _maintenance(control, unit, activation, monkeypatch, now)
    assert (
        vocabulary_authority.VocabularyAuthority(control.vault_root)
        .runtime_status(now=now + 301)
        .mode
        != "v2"
    )
