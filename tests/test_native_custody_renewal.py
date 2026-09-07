"""Same-authority renewal authenticates history but returns only fresh custody."""

from __future__ import annotations

import importlib
import json
from dataclasses import replace

import pytest
from test_authorization_standalone_provisioning import (
    _custody_paths,  # noqa: F401 - isolated production host registry
    _enrolled_v4,
    _vault,
)
from test_vocabulary_deployment import NOW, _decision, _offline, _snapshot

from exomem import vocabulary_authority, vocabulary_deployment
from exomem.governance import authorization_custody as custody
from exomem.governance import authorization_serving_membership as membership

pytestmark = pytest.mark.usefixtures("_custody_paths")


@pytest.fixture
def renewal():
    assert importlib.util.find_spec("exomem.native_custody_renewal") is not None
    return importlib.import_module("exomem.native_custody_renewal")


@pytest.fixture
def active_floor(tmp_path):
    vault = _vault(tmp_path)
    _enrolled_v4(vault, now=NOW - 10)
    plan = vocabulary_deployment.prepare_standalone_floor(vault, now=NOW)
    vocabulary_deployment.publish_standalone_floor(
        vault,
        plan,
        decision=_decision(plan),
        offline_authority=_offline(),
        now=NOW + 1,
    )
    return vault, custody.load_authorization_custody(vault, now=NOW + 1)


@pytest.mark.parametrize("delay", [60, membership.MAX_ATTESTATION_TTL_SECONDS + 86400])
def test_renewal_preserves_identity_and_returns_fresh_successor(renewal, active_floor, delay):
    vault, source = active_floor
    keyring_bytes = source.keyring_path.read_bytes()
    moment = NOW + delay
    result = renewal.renew_standalone_custody(vault, now=moment)
    assert result == custody.load_authorization_custody(vault, now=moment)
    assert result.control == replace(
        source.control,
        issued_at=moment,
        expires_at=result.control.expires_at,
        serving_membership_epoch=source.control.serving_membership_epoch + 1,
        serving_membership_digest=result.serving_membership.record_digest,
    )
    assert (
        result.serving_membership.previous_epoch_digest == source.serving_membership.record_digest
    )
    assert result.serving_membership.issued_at == moment
    assert (
        moment
        < result.serving_membership.expires_at
        <= moment + membership.MAX_ATTESTATION_TTL_SECONDS
    )
    assert result.keyring_path.read_bytes() == keyring_bytes
    assert not result.serving_membership.replicas[0].no_in_flight
    custody.require_current_standalone_registry(result, now=moment, require_serving=True)


def test_completed_checkpoint_reuses_exact_target_until_half_ttl(renewal, active_floor):
    vault, source = active_floor
    first = renewal.renew_standalone_custody(vault, now=NOW + 60)
    before = _snapshot(first)
    assert renewal.renew_standalone_custody(vault, now=NOW + 61) == first
    assert _snapshot(first) == before
    next_time = first.serving_membership.issued_at + membership.MAX_ATTESTATION_TTL_SECONDS // 2
    second = renewal.renew_standalone_custody(vault, now=next_time)
    assert second.serving_membership.epoch == first.serving_membership.epoch + 1
    assert second.serving_membership.previous_epoch_digest == first.serving_membership.record_digest


@pytest.mark.parametrize(
    "boundary", ["checkpoint", "membership", "control", "registry", "completed"]
)
@pytest.mark.parametrize("downtime", [1, membership.MAX_ATTESTATION_TTL_SECONDS + 86400])
def test_crash_recovery_completes_exact_checkpoint_before_fresh_successor(
    renewal,
    active_floor,
    monkeypatch,
    boundary,
    downtime,
):
    vault, source = active_floor
    checkpoint = source.control_path.parent / renewal.CHECKPOINT_NAME
    crash_path = {
        "checkpoint": checkpoint,
        "completed": checkpoint,
        "membership": source.membership_path,
        "control": source.control_path,
        "registry": custody._host_registry_path(source.control.logical_vault_id),
    }[boundary]
    original_replace = custody._replace_control_bytes
    original_publish = custody._publish_private_file
    observed = {}

    def crash_after_replace(path, *, expected, target):
        original_replace(path, expected=expected, target=target)
        if path == crash_path and boundary != "checkpoint":
            if boundary == "completed" and not json.loads(target)["completed"]:
                return
            observed["checkpoint"] = checkpoint.read_bytes()
            raise OSError("renewal interrupted")

    def crash_after_create(path, raw):
        result = original_publish(path, raw)
        if path == crash_path and boundary == "checkpoint":
            observed["checkpoint"] = checkpoint.read_bytes()
            raise OSError("renewal interrupted")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(custody, "_replace_control_bytes", crash_after_replace)
        patch.setattr(custody, "_publish_private_file", crash_after_create)
        with pytest.raises(OSError, match="renewal interrupted"):
            renewal.renew_standalone_custody(vault, now=NOW + 60)
    planned = json.loads(observed["checkpoint"])
    completed_targets = []

    def record_completed(path, *, expected, target):
        if path == checkpoint and json.loads(target)["completed"]:
            completed_targets.append(json.loads(target)["target_control"])
        return original_replace(path, expected=expected, target=target)

    monkeypatch.setattr(custody, "_replace_control_bytes", record_completed)
    result = renewal.renew_standalone_custody(vault, now=NOW + 60 + downtime)
    if boundary != "completed":
        assert completed_targets[0] == planned["target_control"]
    if downtime == 1:
        assert result.serving_membership.epoch == source.serving_membership.epoch + 1
    else:
        assert result.serving_membership.epoch == source.serving_membership.epoch + 2
    assert result == custody.load_authorization_custody(vault, now=NOW + 60 + downtime)


@pytest.mark.parametrize(
    "changed",
    [
        "control-signature",
        "membership-signature",
        "attachment",
        "floor",
        "runtime",
        "future-time",
        "key-expired",
    ],
)
def test_renewal_refuses_invalid_authority_without_publication(
    renewal,
    active_floor,
    monkeypatch,
    changed,
):
    vault, source = active_floor
    moment = NOW + 60
    if changed == "control-signature":
        raw = json.loads(source.control_path.read_bytes())
        raw["mac"] = "A" * len(raw["mac"])
        source.control_path.write_text(json.dumps(raw))
    elif changed == "membership-signature":
        raw = json.loads(source.membership_path.read_bytes())
        raw["mac"] = "A" * len(raw["mac"])
        source.membership_path.write_text(json.dumps(raw))
    elif changed in {"attachment", "floor"}:
        changes = (
            {"registry_attachment_id": "attachment-v1-" + "0" * 64}
            if changed == "attachment"
            else {"version": 1, "vocabulary_authority_floor": 1}
        )
        source.control_path.write_bytes(
            custody._signed_control_bytes(
                replace(source.control, **changes),
                signing_key=source.keyring.active_key.key,
            )
        )
    elif changed == "runtime":
        monkeypatch.setattr(custody, "runtime_software_version", lambda: "different-release")
    elif changed == "future-time":
        moment = source.control.issued_at - 1
    else:
        moment = source.keyring.active_key.not_after
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        renewal.renew_standalone_custody(vault, now=moment)
    assert _snapshot(source) == before
    assert not (source.control_path.parent / renewal.CHECKPOINT_NAME).exists()


def test_checkpoint_rejects_changed_keyring_bytes(renewal, active_floor):
    vault, source = active_floor
    renewal.renew_standalone_custody(vault, now=NOW + 60)
    source.keyring_path.write_bytes(source.keyring_path.read_bytes() + b"\n")
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        renewal.renew_standalone_custody(vault, now=NOW + 61)
    assert _snapshot(source) == before


def test_checkpoint_refuses_unknown_or_corrupted_record(renewal, active_floor):
    vault, source = active_floor
    renewal.renew_standalone_custody(vault, now=NOW + 60)
    checkpoint = source.control_path.parent / renewal.CHECKPOINT_NAME
    raw = json.loads(checkpoint.read_bytes())
    raw["unknown"] = True
    checkpoint.write_text(json.dumps(raw))
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        renewal.renew_standalone_custody(vault, now=NOW + 61)
    assert _snapshot(source) == before


def test_expired_control_is_authenticated_as_history_then_renewed(renewal, tmp_path):
    vault = _vault(tmp_path)
    enrolled, _ = _enrolled_v4(vault, now=NOW - 10)
    enrolled.control_path.write_bytes(
        custody._signed_control_bytes(
            replace(enrolled.control, expires_at=NOW + 120),
            signing_key=enrolled.keyring.active_key.key,
        )
    )
    plan = vocabulary_deployment.prepare_standalone_floor(vault, now=NOW)
    vocabulary_deployment.publish_standalone_floor(
        vault,
        plan,
        decision=_decision(plan),
        offline_authority=_offline(),
        now=NOW + 1,
    )
    moment = NOW + 86400
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        custody.load_authorization_custody(vault, now=moment)
    result = renewal.renew_standalone_custody(vault, now=moment)
    assert result.control.issued_at == moment
    assert result.control.expires_at > moment
    assert custody.load_authorization_custody(vault, now=moment) == result


def test_renewal_leaves_real_activation_grants_and_session_records_unchanged(renewal, active_floor):
    from exomem.governance import authorization_session_lifecycle, store
    from exomem.governance.principal import RequestPrincipal

    vault, source = active_floor
    connection = store.open_authorization_session_connection(vault)
    try:
        issued = authorization_session_lifecycle.open_session(
            connection,
            custody=source,
            principal_id="agent",
            issuer_family="test-service",
            now=NOW + 2,
            ttl_seconds=3600,
        )
    finally:
        connection.close()
    principal = RequestPrincipal(
        audience_id=issued.context.principal_id,
        surface="test",
        issuer_family=issued.context.issuer_family,
        authorization_session_id=issued.context.session_id,
        verified_authorization_session=issued.context,
    )
    authority = vocabulary_authority.VocabularyAuthority(vault, clock=lambda: NOW + 2)
    floor = vocabulary_authority._deployment_floor_for_adapter(
        runtime=vocabulary_authority.RUNTIME_FLOOR,
        generation=source.control.activation_epoch,
    )
    binding = vocabulary_authority._owner_binding(
        "activate", principal, operation={"runtime": floor.runtime, "generation": floor.generation}
    )
    decision = vocabulary_authority._trusted_owner_decision_for_adapter(
        owner_id="owner",
        ceremony_id="activate",
        binding_digest=binding,
        expires_at=NOW + 100,
    )
    authority.activate(
        principal=principal, decision=decision, deployment_floor=floor, binding=binding, now=NOW + 2
    )
    scope = vocabulary_authority.AuthorityScope.vault_wide()
    binding = vocabulary_authority._owner_binding(
        "grant",
        principal,
        grant={"actions": ["entity.create"], "scope": scope.as_dict(), "expires_at": NOW + 100000},
    )
    decision = vocabulary_authority._trusted_owner_decision_for_adapter(
        owner_id="owner",
        ceremony_id="grant",
        binding_digest=binding,
        expires_at=NOW + 100,
    )
    authority.grant(
        principal=principal,
        decision=decision,
        audience=principal,
        actions=["entity.create"],
        scope=scope,
        expires_at=NOW + 100000,
        binding=binding,
        now=NOW + 2,
    )
    paths = (
        authority._database_path(source),
        authority._marker_path(source),
        store.sidecar_path(vault),
    )
    before = {path: path.read_bytes() for path in paths}
    renewal.renew_standalone_custody(
        vault, now=NOW + membership.MAX_ATTESTATION_TTL_SECONDS + 86400
    )
    assert {path: path.read_bytes() for path in paths} == before


def test_renewal_never_admits_expired_checkpoint_as_current_custody(
    renewal, active_floor, monkeypatch
):
    vault, source = active_floor
    seen = []
    original = custody.load_authorization_custody

    def actual_time_only(root, *, now):
        seen.append(now)
        return original(root, now=now)

    moment = NOW + membership.MAX_ATTESTATION_TTL_SECONDS + 86400
    monkeypatch.setattr(custody, "load_authorization_custody", actual_time_only)
    renewal.renew_standalone_custody(vault, now=moment)
    assert seen and all(item == moment for item in seen)


@pytest.mark.parametrize("changed", ["schema", "keyring", "singleton", "state"])
def test_current_authority_contract_is_checked_before_checkpoint(renewal, active_floor, changed):
    import sqlite3

    from exomem.governance import store

    vault, source = active_floor
    if changed == "schema":
        connection = sqlite3.connect(store.sidecar_path(vault))
        try:
            connection.execute("PRAGMA user_version=5")
        finally:
            connection.close()
    elif changed == "keyring":
        keyring = replace(
            source.keyring,
            accepted_keys=(
                replace(
                    source.keyring.active_key, not_after=source.keyring.active_key.not_after - 1
                ),
            ),
        )
        source.keyring_path.write_bytes(custody._keyring_bytes(keyring))
    else:
        record = source.serving_membership
        if changed == "singleton":
            record = replace(
                record,
                replicas=record.replicas
                + (replace(record.replicas[0], replica_id="zz-second-replica"),),
            )
        else:
            record = replace(
                record,
                replicas=(replace(record.replicas[0], state="DRAINING", issuance_stopped=True),),
            )
        raw = membership.encode_serving_membership(
            record, verifier_keys={key.key_id: key.key for key in source.keyring.accepted_keys}
        )
        control = replace(
            source.control, serving_membership_digest=membership.serving_membership_digest(raw)
        )
        source.membership_path.write_bytes(raw)
        source.control_path.write_bytes(
            custody._signed_control_bytes(control, signing_key=source.keyring.active_key.key)
        )
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        renewal.renew_standalone_custody(vault, now=NOW + 60)
    assert _snapshot(source) == before
    assert not (source.control_path.parent / renewal.CHECKPOINT_NAME).exists()


def test_expired_key_during_partial_checkpoint_refuses_all_recovery_writes(
    renewal, active_floor, monkeypatch
):
    vault, source = active_floor
    original = custody._replace_control_bytes

    def crash_after_membership(path, **kwargs):
        original(path, **kwargs)
        if path == source.membership_path:
            raise OSError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(custody, "_replace_control_bytes", crash_after_membership)
        with pytest.raises(OSError):
            renewal.renew_standalone_custody(vault, now=NOW + 60)
    checkpoint = source.control_path.parent / renewal.CHECKPOINT_NAME
    before = _snapshot(source) | {checkpoint: checkpoint.read_bytes()}
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        renewal.renew_standalone_custody(vault, now=source.keyring.active_key.not_after)
    assert _snapshot(source) | {checkpoint: checkpoint.read_bytes()} == before


def _publish_activation_successor(vault, source, *, now, publication_kind="policy"):
    from exomem.governance import schema_v4, store

    connection = store.open_authorization_session_connection(vault)
    try:
        generation, fingerprint, projector, catalog = connection.execute(
            "SELECT policy_generation_id, policy_fingerprint, projector_schema_version, "
            "catalog_generation FROM active_governance_tuple"
        ).fetchone()
        policy_digest = connection.execute(
            "SELECT immutable_row_digest FROM compiled_policy_generations WHERE generation_id=?",
            (generation,),
        ).fetchone()[0]
        catalog_digest = connection.execute(
            "SELECT descriptor_digest FROM catalog_generation_descriptors WHERE catalog_generation=?",
            (catalog,),
        ).fetchone()[0]
        namespace_digest = connection.execute(
            "SELECT namespace_digest FROM governance_projection_namespaces WHERE policy_fingerprint=? "
            "AND projector_schema_version=? AND catalog_generation=?",
            (fingerprint, projector, catalog),
        ).fetchone()[0]
        control = source.control
        epoch = control.activation_epoch + 1
        event_id = f"renewal-{publication_kind}-{epoch}"
        if publication_kind == "policy":
            policy_columns = (
                "generation_id, source_documents, source_fingerprint, conflict_digest, "
                "compiled_policy, policy_fingerprint, compiler_schema_version, "
                "projector_schema_version, predecessor_generation_id, authoring_event_id, "
                "receipt_event_id, immutable_row_digest, created_at"
            )
            row = list(
                connection.execute(
                    f"SELECT {policy_columns} FROM compiled_policy_generations WHERE generation_id=?",
                    (generation,),
                ).fetchone()
            )
            row[0], row[8] = f"renewal-policy-{epoch}", generation
            row[9], row[10], row[12] = f"renewal-authoring-{epoch}", event_id, now
            row[11] = schema_v4._stored_policy_row_digest(tuple(row))
            connection.execute(
                f"INSERT INTO compiled_policy_generations ({policy_columns}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            generation, policy_digest = row[0], row[11]
            connection.execute(
                "UPDATE active_governance_tuple SET policy_generation_id=? WHERE singleton=1",
                (generation,),
            )
        digest = schema_v4.activation_state_digest(
            logical_vault_id=control.logical_vault_id,
            activation_store_id=control.activation_store_id,
            activation_epoch=epoch,
            policy_generation_id=generation,
            policy_fingerprint=fingerprint,
            policy_row_digest=policy_digest,
            projector_schema_version=projector,
            catalog_generation=catalog,
            catalog_descriptor_digest=catalog_digest,
            projection_namespace_identity=namespace_digest,
        )
        connection.execute(
            "INSERT INTO governance_tuple_publications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?)",
            (
                event_id,
                publication_kind,
                control.activation_state_digest,
                digest,
                generation,
                fingerprint,
                projector,
                catalog,
                epoch,
                now,
            ),
        )
        connection.execute(
            "UPDATE governance_activation_store SET activation_epoch=?, activation_state_digest=? WHERE singleton=1",
            (epoch, digest),
        )
        connection.commit()
        active = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=control.activation_store_id,
            expected_activation_epoch=epoch,
            expected_activation_state_digest=digest,
        )
    finally:
        connection.close()
    custody.acknowledge_activation_tuple(
        vault, expected_control=source.control, target=active, now=now
    )
    return custody.load_authorization_custody(vault, now=now)


@pytest.mark.parametrize("publication_kind", ["policy", "catalog"])
def test_completed_checkpoint_reconciles_proven_direct_activation_successor(
    renewal, active_floor, publication_kind
):
    vault, source = active_floor
    previous = renewal.renew_standalone_custody(vault, now=NOW + 60)
    current = _publish_activation_successor(
        vault, previous, now=NOW + 120, publication_kind=publication_kind
    )
    assert current.control.version == 2 and current.control.vocabulary_authority_floor == 2
    assert current.serving_membership == previous.serving_membership
    result = renewal.renew_standalone_custody(vault, now=NOW + 121)
    assert result.control.activation_epoch == current.control.activation_epoch
    assert result.control.activation_state_digest == current.control.activation_state_digest
    assert (
        result.serving_membership.previous_epoch_digest == current.serving_membership.record_digest
    )
    assert result.serving_membership.epoch == current.serving_membership.epoch + 1


def test_completed_checkpoint_refuses_skipped_activation_successor(renewal, active_floor):
    vault, source = active_floor
    previous = renewal.renew_standalone_custody(vault, now=NOW + 60)
    current = _publish_activation_successor(vault, previous, now=NOW + 120)
    current = _publish_activation_successor(vault, current, now=NOW + 121)
    before = _snapshot(current)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        renewal.renew_standalone_custody(vault, now=NOW + 122)
    assert _snapshot(current) == before
