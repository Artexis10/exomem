"""Real custody/session coverage for vocabulary-authority activation."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import test_authorization_session_wire_reconnect as wire
from starlette.testclient import TestClient

from exomem import (
    commands,
    entity_types,
    init,
    vocabulary_admission,
    vocabulary_authority,
    vocabulary_control,
    writer_lease,
)
from exomem.governance import (
    authorization_custody,
    authorization_serving_membership,
    authorization_session_lifecycle,
    store,
)
from exomem.governance.principal import RequestPrincipal, request_scope


def _publish_floor_two(custody: authorization_custody.AuthorizationCustody) -> None:
    """Publish the authenticated floor-two successor used by activation."""

    control = custody.control
    keyring = custody.keyring
    provisional = authorization_custody.AuthorizationControlRecord(
        version=2,
        keyring_id=control.keyring_id,
        cell_id=control.cell_id,
        logical_vault_id=control.logical_vault_id,
        registry_attachment_id=control.registry_attachment_id,
        attachment_epoch=control.attachment_epoch,
        governance_enrolled=True,
        activation_store_id=control.activation_store_id,
        activation_epoch=control.activation_epoch,
        activation_state_digest=control.activation_state_digest,
        serving_membership_epoch=control.serving_membership_epoch,
        serving_membership_digest="0" * 64,
        issued_at=control.issued_at,
        expires_at=control.expires_at,
        signing_key_id=control.signing_key_id,
        vocabulary_authority_floor=2,
    )
    attestation = authorization_serving_membership.ReplicaReadinessAttestation(
        version=1,
        epoch=1,
        replica_id=custody.local_replica_id,
        state="SERVING",
        software_version=authorization_custody.runtime_software_version(),
        schema_version=4,
        cell_id=control.cell_id,
        active_key_id=keyring.active_key_id,
        accepted_key_ids=(keyring.active_key_id,),
        control_digest=authorization_custody.control_attestation_digest(provisional),
        keyring_digest=authorization_custody.keyring_attestation_digest(keyring),
        attested_at=control.issued_at,
        expires_at=control.expires_at,
        issuance_stopped=False,
        no_in_flight=False,
        signing_key_id=keyring.active_key_id,
    )
    membership = authorization_serving_membership.encode_serving_membership(
        authorization_serving_membership.ServingMembershipEpoch(
            version=1,
            epoch=1,
            cell_id=control.cell_id,
            logical_vault_id=control.logical_vault_id,
            previous_epoch_digest=None,
            issued_at=control.issued_at,
            expires_at=control.expires_at,
            replicas=(attestation,),
            signing_key_id=keyring.active_key_id,
        ),
        verifier_keys={keyring.active_key_id: keyring.active_key.key},
    )
    target = authorization_custody.AuthorizationControlRecord(
        **{
            **{
                field: getattr(provisional, field)
                for field in provisional.__dataclass_fields__
            },
            "serving_membership_digest": authorization_serving_membership.serving_membership_digest(
                membership
            ),
        }
    )
    control_path = custody.control_path
    membership_path = Path(os.environ[authorization_custody.MEMBERSHIP_FILE_ENV])
    control_path.write_bytes(
        authorization_custody._signed_control_bytes(  # noqa: SLF001 - test fixture publication
            target, signing_key=keyring.active_key.key
        )
    )
    membership_path.write_bytes(membership)
    control_path.chmod(0o600)
    membership_path.chmod(0o600)


def _principal(context: authorization_session_lifecycle.AuthorizationSessionContext) -> RequestPrincipal:
    return RequestPrincipal(
        audience_id=context.principal_id,
        surface="test",
        authorization_session_id=context.session_id,
        issuer_family=context.issuer_family,
        verified_authorization_session=context,
    )


def test_floor_two_pending_session_activation_and_session_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    vault = tmp_path / "vault"
    init.init_vault(vault)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease"))
    wire._configure_v4_authority(vault, tmp_path / "custody", monkeypatch)
    (tmp_path / "custody").chmod(0o700)
    _publish_floor_two(authorization_custody.load_authorization_custody(vault, now=now))

    with pytest.raises(vocabulary_admission.VocabularyAdmissionError):
        vocabulary_admission.require_mutation_admission(vault, now=now)
    vocabulary_admission.require_session_open_admission(vault, now=now)

    replica = wire._build_replica(vault, monkeypatch)
    with TestClient(replica) as client:
        opened = wire._call(
            client,
            1,
            {"operation": "session", "session_action": "open", "ttl_seconds": 60},
        )
    issued = opened["diagnostics"]["issued_credential"]
    assert isinstance(issued, dict)
    bearer = issued["bearer"]
    assert isinstance(bearer, str)

    custody = authorization_custody.load_authorization_custody(vault, now=now)
    audience, issuer = wire._service_identity()
    connection = store.open_authorization_session_connection(vault)
    try:
        context = authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=bearer,
            principal_id=audience,
            issuer_family=issuer,
            now=now,
        )
    finally:
        connection.close()
    principal = _principal(context)
    authority = vocabulary_authority.VocabularyAuthority(vault)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease")
    )
    control = vocabulary_control.VocabularyControl(
        authority,
        owner_decision_callback=lambda intent: vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
            owner_id="owner",
            ceremony_id=f"control-{intent.binding_digest[:20]}",
            binding_digest=intent.binding_digest,
            expires_at=now + 60,
        ),
        deployment_floor_callback=lambda _intent: vocabulary_authority._deployment_floor_for_adapter(  # noqa: SLF001
            runtime="vocabulary-authority/v2", generation=custody.control.activation_epoch
        ),
        activation_guard_factory=lambda root: manager.mutation_guard(
            root,
            operation="vocabulary-authority-activation",
            holder_kind="vocabulary-authority-control",
            activation_admission=True,
        ),
    )
    assert control.activate(principal=principal, body={}).mode == "v2"

    schema_command = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "schema_memory"
    )
    venue = {
        "folder": "Venues",
        "label": "Venue",
        "aliases": [],
        "capture_guidance": "Capture a stable place for recurring events.",
        "status": "active",
        "parent": "concept",
    }
    venue_arguments = {
        "operation": "save-entity-types",
        "proposal": {"schema_version": 1, "entity_types": {"venue": venue}},
        "expected_hash": entity_types.load_entity_types(vault).extension_hash,
        "why": "Register the exact reviewed structural meaning.",
    }
    with request_scope(principal), pytest.raises(vocabulary_authority.VocabularyAuthorityDenied) as denied:
        manager.invoke(
            schema_command,
            (vault,),
            venue_arguments,
            idempotency_key="venue-type-v1",
            read_only=False,
        )
    assert isinstance(denied.value.details.get("vocabulary_request_id"), str)
    assert entity_types.load_entity_types(vault).resolve("venue") is None

    authority_id = control.grant(
        principal=principal,
        body={
            "actions": ["entity_type.add"],
            "scope": "vault",
            "expires_at": now + 60,
        },
    )
    with request_scope(principal):
        committed = manager.invoke(
            schema_command,
            (vault,),
            venue_arguments,
            idempotency_key="venue-type-v1",
            read_only=False,
        )
    assert committed["state"] == "committed"
    assert isinstance(committed.get("receipt_id"), str) and committed["receipt_id"]
    registry_path = entity_types.extension_registry_path(vault)
    assert "venue:" in registry_path.read_text(encoding="utf-8")
    assert entity_types.load_entity_types(vault).resolve("venue") is not None

    control.revoke(principal=principal, body={"authority_id": authority_id})
    guild = {
        "folder": "Guilds",
        "label": "Guild",
        "aliases": [],
        "capture_guidance": "Capture a durable membership organization.",
        "status": "active",
        "parent": "organization",
    }
    guild_arguments = {
        "operation": "save-entity-types",
        "proposal": {"schema_version": 1, "entity_types": {"venue": venue, "guild": guild}},
        "expected_hash": entity_types.load_entity_types(vault).extension_hash,
        "why": "Register a second exact structural meaning.",
    }
    with request_scope(principal), pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        manager.invoke(
            schema_command,
            (vault,),
            guild_arguments,
            idempotency_key="guild-type-v1",
            read_only=False,
        )
    assert entity_types.load_entity_types(vault).resolve("guild") is None

    connection = store.open_authorization_session_connection(vault)
    try:
        rotated = authorization_session_lifecycle.rotate_verified_session(
            connection, custody=custody, context=context, now=now, ttl_seconds=60
        )
        with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
            authority.status(principal, now=now)
        authorization_session_lifecycle.close_verified_session(
            connection, custody=custody, context=rotated.context, now=now
        )
    finally:
        connection.close()
    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        authority.status(_principal(rotated.context), now=now)
