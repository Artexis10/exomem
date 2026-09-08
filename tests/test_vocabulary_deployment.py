"""Reviewed standalone floor publication uses real custody and temporary state."""

from __future__ import annotations

import importlib
import json
from dataclasses import replace

import pytest
from test_authorization_standalone_provisioning import (
    _custody_paths,  # noqa: F401 - production custody fixture
    _enrolled_v4,
    _vault,
)

from exomem import state_migration, vocabulary_authority
from exomem.governance import authorization_custody as custody

pytestmark = pytest.mark.usefixtures("_custody_paths")
NOW = 1_800_000_010


@pytest.fixture
def deployment():
    assert importlib.util.find_spec("exomem.vocabulary_deployment") is not None
    return importlib.import_module("exomem.vocabulary_deployment")


@pytest.fixture
def enrolled(tmp_path):
    vault = _vault(tmp_path)
    source, _ = _enrolled_v4(vault, now=NOW - 10)
    return vault, source


def _decision(plan, **overrides):
    fields = dict(
        owner_id="owner",
        ceremony_id="floor-review",
        binding_digest=plan.review_digest,
        expires_at=plan.expires_at,
    )
    fields.update(overrides)
    return vocabulary_authority._trusted_owner_decision_for_adapter(**fields)


def _offline():
    return state_migration.assert_offline_migration_authority(source="stopped test service")


def _publish(deployment, vault, plan, **overrides):
    fields = dict(decision=_decision(plan), offline_authority=_offline(), now=NOW + 1)
    fields.update(overrides)
    return deployment.publish_standalone_floor(vault, plan, **fields)


def _snapshot(source):
    paths = (
        source.keyring_path,
        source.control_path,
        source.membership_path,
        custody._host_registry_path(source.control.logical_vault_id),
    )
    return {path: path.read_bytes() for path in paths}


def test_prepare_is_read_only_and_private_plan_roundtrips(deployment, enrolled):
    vault, source = enrolled
    before = _snapshot(source)
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    assert _snapshot(source) == before
    assert custody.load_authorization_custody(vault, now=NOW).control.version == 1
    assert plan.source_control == before[source.control_path]
    assert plan.source_membership == before[source.membership_path]
    assert plan.runtime == vocabulary_authority.RUNTIME_FLOOR
    assert plan.as_dict()["target_floor"] == 2
    assert plan.as_dict()["source_membership_epoch"] == source.control.serving_membership_epoch
    assert plan.as_dict()["target_membership_epoch"] == source.control.serving_membership_epoch + 1
    assert plan.source_control.decode() not in repr(plan)
    assert "accepted_keys" not in json.dumps(plan.as_dict())
    assert deployment.FloorPlan.from_record(json.loads(json.dumps(plan.as_record()))) == plan


@pytest.mark.parametrize("kind", ["missing", "unsealed", "mismatched", "expired", "plan-expired"])
def test_publish_requires_exact_current_owner_acceptance(deployment, enrolled, kind):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    before = _snapshot(source)
    decision = _decision(plan)
    moment = NOW + 1
    if kind == "missing":
        decision = None
    elif kind == "unsealed":
        decision = replace(decision, _seal=None)
    elif kind == "mismatched":
        decision = _decision(plan, binding_digest="0" * 64)
    elif kind == "expired":
        decision = _decision(plan, expires_at=NOW)
    else:
        moment = plan.expires_at + 1
        decision = _decision(plan, expires_at=moment + 10)
    with pytest.raises(
        (vocabulary_authority.VocabularyAuthorityDenied, custody.AuthorizationCustodyUnavailable)
    ):
        _publish(deployment, vault, plan, decision=decision, now=moment)
    assert _snapshot(source) == before


def test_publish_requires_offline_capability(deployment, enrolled):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    before = _snapshot(source)
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        _publish(deployment, vault, plan, offline_authority=None)
    assert _snapshot(source) == before


def test_publish_advances_membership_and_host_registry_once(deployment, enrolled):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    proof = _publish(deployment, vault, plan)
    target = custody.load_authorization_custody(vault, now=NOW + 1)
    assert target.control.version == 2
    assert target.control.vocabulary_authority_floor == 2
    assert target.control.registry_attachment_id == source.control.registry_attachment_id
    assert target.serving_membership.epoch == source.serving_membership.epoch + 1
    assert (
        target.serving_membership.previous_epoch_digest == source.serving_membership.record_digest
    )
    assert target.control_path.read_bytes() == plan.target_control
    assert target.membership_path.read_bytes() == plan.target_membership
    custody.require_current_standalone_registry(target, now=NOW + 1, require_serving=True)
    assert proof.runtime == vocabulary_authority.RUNTIME_FLOOR
    assert proof.generation == source.control.activation_epoch
    before = _snapshot(target)
    assert _publish(deployment, vault, plan, now=NOW + 2) == proof
    assert _snapshot(target) == before


@pytest.mark.parametrize("changed", ["control", "keyring", "membership", "plan", "root"])
def test_publish_rejects_changed_review_inputs(deployment, enrolled, changed, tmp_path):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    if changed == "control":
        source.control_path.write_bytes(
            custody._signed_control_bytes(
                replace(source.control, expires_at=source.control.expires_at - 1),
                signing_key=source.keyring.active_key.key,
            )
        )
    elif changed == "keyring":
        source.keyring_path.write_bytes(source.keyring_path.read_bytes() + b"\n")
    elif changed == "membership":
        source.membership_path.write_bytes(source.membership_path.read_bytes() + b"\n")
    elif changed == "plan":
        plan = replace(plan, target_control=plan.target_control + b"\n")
    else:
        vault = _vault(tmp_path, "other")
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        _publish(deployment, vault, plan)
    assert _snapshot(source) == before


@pytest.mark.parametrize("boundary", ["membership", "control", "registry"])
def test_crash_retries_only_exact_reviewed_publication(deployment, enrolled, monkeypatch, boundary):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    path = {
        "membership": source.membership_path,
        "control": source.control_path,
        "registry": custody._host_registry_path(source.control.logical_vault_id),
    }[boundary]
    original = custody._replace_control_bytes

    def crash_after_publication(destination, **kwargs):
        original(destination, **kwargs)
        if destination == path:
            raise OSError("simulated process interruption")

    with monkeypatch.context() as patch:
        patch.setattr(custody, "_replace_control_bytes", crash_after_publication)
        with pytest.raises(OSError, match="interruption"):
            _publish(deployment, vault, plan)
    plan = deployment.FloorPlan.from_record(plan.as_record())
    _publish(deployment, vault, plan, now=NOW + 2)
    target = custody.load_authorization_custody(vault, now=NOW + 2)
    assert target.control_path.read_bytes() == plan.target_control
    assert target.membership_path.read_bytes() == plan.target_membership
    assert (
        target.serving_membership.previous_epoch_digest == source.serving_membership.record_digest
    )


def test_private_record_rejects_unknown_fields_and_digest_tampering(deployment, enrolled):
    vault, _ = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    for changes in (
        {"extra": True},
        {"review_digest": "0" * 64},
        {"expires_at": plan.expires_at + 1},
    ):
        with pytest.raises(custody.AuthorizationCustodyUnavailable):
            deployment.FloorPlan.from_record(plan.as_record() | changes)


def test_owner_decision_at_expiry_is_not_current(deployment, enrolled):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    before = _snapshot(source)
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        _publish(deployment, vault, plan, decision=_decision(plan, expires_at=NOW + 1))
    assert _snapshot(source) == before


def test_prepare_requires_real_serving_host_registration(deployment, enrolled):
    vault, source = enrolled
    custody._host_registry_path(source.control.logical_vault_id).unlink()
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        deployment.prepare_standalone_floor(vault, now=NOW)


def test_prepare_refuses_unenrolled_custody(deployment, tmp_path):
    vault = _vault(tmp_path)
    custody.provision_standalone_custody(vault, now=NOW)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        deployment.prepare_standalone_floor(vault, now=NOW + 1)


def test_prepare_refuses_already_published_floor(deployment, enrolled):
    vault, _ = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _publish(deployment, vault, plan)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        deployment.prepare_standalone_floor(vault, now=NOW + 2)


def test_publish_refuses_runtime_change(deployment, enrolled, monkeypatch):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    before = _snapshot(source)
    monkeypatch.setattr(custody, "runtime_software_version", lambda: "different-runtime")
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        _publish(deployment, vault, plan)
    assert _snapshot(source) == before


def _interrupt_floor(deployment, vault, plan, source, monkeypatch, boundary="membership"):
    path = {
        "membership": source.membership_path,
        "control": source.control_path,
        "registry": custody._host_registry_path(source.control.logical_vault_id),
    }[boundary]
    original = custody._replace_control_bytes

    def interrupt(destination, **kwargs):
        original(destination, **kwargs)
        if destination == path:
            raise OSError("floor interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(custody, "_replace_control_bytes", interrupt)
        with pytest.raises(OSError, match="floor interrupted"):
            _publish(deployment, vault, plan)


def _recover(deployment, vault, plan, **overrides):
    options = dict(
        decision=_decision(plan),
        started_at=NOW + 1,
        offline_authority=_offline(),
        now=plan.expires_at + 1,
    )
    options.update(overrides)
    return deployment.recover_standalone_floor(vault, plan, **options)


@pytest.mark.parametrize("boundary", ["membership", "control", "registry"])
def test_expired_consent_recovers_exact_started_floor(deployment, enrolled, monkeypatch, boundary):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _interrupt_floor(deployment, vault, plan, source, monkeypatch, boundary)
    assert hasattr(deployment, "recover_standalone_floor")
    original_decision = _decision(plan)
    proof = _recover(deployment, vault, plan, decision=original_decision)
    target = custody.load_authorization_custody(vault, now=plan.expires_at + 1)
    assert original_decision.expires_at == plan.expires_at
    assert target.control_path.read_bytes() == plan.target_control
    assert target.membership_path.read_bytes() == plan.target_membership
    assert proof.generation == source.control.activation_epoch


def test_expired_recovery_never_starts_untouched_floor(deployment, enrolled):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        _recover(deployment, vault, plan, allow_same_authority_renewal=True)
    assert _snapshot(source) == before


@pytest.mark.parametrize(
    "invalid", ["before-preparation", "at-expiry", "future-start", "unsealed", "binding", "offline"]
)
def test_recovery_requires_original_bound_consent_and_stop_proof(
    deployment, enrolled, monkeypatch, invalid
):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _interrupt_floor(deployment, vault, plan, source, monkeypatch)
    options = {}
    if invalid == "before-preparation":
        options["started_at"] = NOW - 1
    elif invalid == "at-expiry":
        options["started_at"] = plan.expires_at
    elif invalid == "future-start":
        options.update(started_at=NOW + 2, now=NOW + 1)
    elif invalid == "unsealed":
        options["decision"] = replace(_decision(plan), _seal=None)
    elif invalid == "binding":
        options["decision"] = _decision(plan, binding_digest="0" * 64)
    else:
        options["offline_authority"] = None
    before = _snapshot(source)
    with pytest.raises(
        (
            custody.AuthorizationCustodyUnavailable,
            vocabulary_authority.VocabularyAuthorityDenied,
            state_migration.StateMigrationOfflineRequired,
        )
    ):
        _recover(deployment, vault, plan, **options)
    assert _snapshot(source) == before


def test_stale_floor_requires_explicit_renewal_permission(deployment, enrolled, monkeypatch):
    from exomem.governance import authorization_serving_membership as membership

    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _interrupt_floor(deployment, vault, plan, source, monkeypatch)
    moment = NOW + membership.MAX_ATTESTATION_TTL_SECONDS + 86400
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        _recover(deployment, vault, plan, now=moment)
    assert source.control_path.read_bytes() == plan.target_control
    assert source.membership_path.read_bytes() == plan.target_membership
    proof = _recover(deployment, vault, plan, now=moment, allow_same_authority_renewal=True)
    target = custody.load_authorization_custody(vault, now=moment)
    assert target.control.vocabulary_authority_floor == 2
    assert target.serving_membership.epoch == source.serving_membership.epoch + 2
    assert proof.generation == source.control.activation_epoch


def test_recovery_refuses_expired_signing_key(deployment, enrolled, monkeypatch):
    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _interrupt_floor(deployment, vault, plan, source, monkeypatch)
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        _recover(
            deployment,
            vault,
            plan,
            now=source.keyring.active_key.not_after,
            allow_same_authority_renewal=True,
        )
    assert _snapshot(source) == before


def test_recovery_after_renewal_requires_exact_floor_ancestor(deployment, enrolled, monkeypatch):
    from exomem.governance import authorization_serving_membership as membership

    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _interrupt_floor(deployment, vault, plan, source, monkeypatch)
    moment = NOW + membership.MAX_ATTESTATION_TTL_SECONDS + 86400
    first = _recover(deployment, vault, plan, now=moment, allow_same_authority_renewal=True)
    before = _snapshot(source)
    assert (
        _recover(deployment, vault, plan, now=moment + 1, allow_same_authority_renewal=True)
        == first
    )
    assert _snapshot(source) == before
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        _recover(deployment, vault, plan, now=moment + 1)


def test_recovery_resumes_partial_renewal_from_exact_floor_target(
    deployment, enrolled, monkeypatch
):
    from exomem.governance import authorization_serving_membership as membership

    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _interrupt_floor(deployment, vault, plan, source, monkeypatch)
    moment = NOW + membership.MAX_ATTESTATION_TTL_SECONDS + 86400
    original = custody._replace_control_bytes

    def crash_renewal(destination, **kwargs):
        original(destination, **kwargs)
        if destination == source.membership_path:
            raise OSError("renewal interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(custody, "_replace_control_bytes", crash_renewal)
        with pytest.raises(OSError, match="renewal interrupted"):
            _recover(deployment, vault, plan, now=moment, allow_same_authority_renewal=True)
    proof = _recover(deployment, vault, plan, now=moment + 1, allow_same_authority_renewal=True)
    assert (
        custody.load_authorization_custody(vault, now=moment + 1).control.activation_epoch
        == proof.generation
    )


def test_recovery_rejects_a_different_signed_renewal_ancestor(deployment, enrolled, monkeypatch):
    from exomem import native_custody_renewal as renewal
    from exomem.governance import authorization_serving_membership as membership

    vault, source = enrolled
    plan = deployment.prepare_standalone_floor(vault, now=NOW)
    _interrupt_floor(deployment, vault, plan, source, monkeypatch)
    moment = NOW + membership.MAX_ATTESTATION_TTL_SECONDS + 86400
    _recover(deployment, vault, plan, now=moment, allow_same_authority_renewal=True)
    path = source.control_path.parent / renewal.CHECKPOINT_NAME
    checkpoint = renewal._decode_checkpoint(path.read_bytes())
    original = custody.parse_control_record(plan.target_control, keyring=source.keyring, now=NOW)
    changed_ancestor = custody._signed_control_bytes(
        replace(original, expires_at=original.expires_at - 1),
        signing_key=source.keyring.active_key.key,
    )
    path.write_bytes(replace(checkpoint, source_control=changed_ancestor).encode())
    before = _snapshot(source)
    with pytest.raises(custody.AuthorizationCustodyUnavailable):
        _recover(deployment, vault, plan, now=moment + 1, allow_same_authority_renewal=True)
    assert _snapshot(source) == before
