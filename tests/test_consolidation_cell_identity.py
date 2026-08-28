from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from exomem import hosted_portability, hosted_runtime, writer_lease
from exomem.governance import authorization_custody, consolidation_identity, principal


def test_consolidation_identity_store_is_an_explicit_private_subsystem() -> None:
    assert importlib.util.find_spec("exomem.governance.consolidation_identity") is not None, (
        "consolidation-capable cells need a dedicated authenticated identity store"
    )


@pytest.fixture(autouse=True)
def _private_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    external = tmp_path / "external"
    host_control = tmp_path / "host-control"
    lease_state = tmp_path / "lease-state"
    external.mkdir(mode=0o700)
    lease_state.mkdir(mode=0o700)
    monkeypatch.setattr(
        authorization_custody,
        "_standalone_host_control_root",
        lambda: host_control,
    )
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(external / "authorization-keyring.json"),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(external / "authorization-control.json"),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(external / "authorization-serving-membership.json"),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "standalone")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(lease_state))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    root = tmp_path / name
    (root / "Knowledge Base/Notes").mkdir(parents=True)
    (root / "Knowledge Base/Notes/identity.md").write_text(
        "---\ntype: insight\n---\nidentity baseline\n",
        encoding="utf-8",
    )
    return root


def _select_custody_paths(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root.mkdir(mode=0o700)
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(root / "authorization-keyring.json"),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(root / "authorization-control.json"),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(root / "authorization-serving-membership.json"),
    )


def _required(name: str):
    value = getattr(consolidation_identity, name, None)
    assert callable(value), f"missing consolidation identity API: {name}"
    return value


def _failover_target_candidate(
    target: Path,
    *,
    operation_id: str,
    owner: principal.RequestPrincipal,
    now: int,
):
    return _required("prepare_local_failover_target_candidate")(
        target,
        operation_id=operation_id,
        principal=owner,
        now=now,
    )


def _quiesced_failover_basis(
    tmp_path: Path,
    *,
    now: int,
    export_operation_id: str,
) -> tuple[
    Path,
    Path,
    principal.RequestPrincipal,
    consolidation_identity.ConsolidationCellIdentity,
    authorization_custody.AuthorizationCustody,
    hosted_portability.ExportResult,
]:
    source = _vault(tmp_path, "source")
    target = tmp_path / "target"
    owner = principal.owner_principal(surface="cli")
    identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=identity.cell_id,
            vault_id=identity.vault_id,
            operation_id=export_operation_id,
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    return source, target, owner, identity, drained, exported


def test_local_owner_adoption_generates_and_authenticates_private_identity(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    adopt = _required("adopt_local_identity")
    load = _required("load_local_identity")

    adopted = adopt(
        vault,
        principal=principal.owner_principal(surface="cli"),
        now=1_800_000_000,
    )
    loaded = load(vault, now=1_800_000_001)

    assert loaded == adopted
    assert adopted.schema == consolidation_identity.IDENTITY_SCHEMA
    assert adopted.installation_generation == 1
    assert len(adopted.adoption_census_digest) == 64
    assert len(adopted.root_binding_digest) == 64
    assert len(adopted.active_fence_digest) == 64
    assert adopted.vault_id != adopted.installation_id
    assert adopted.machine_key_id
    assert adopted.identity_path.is_relative_to(
        authorization_custody._standalone_host_control_root()
    )
    assert not adopted.identity_path.is_relative_to(vault)
    assert stat.S_IMODE(adopted.identity_path.stat().st_mode) == 0o600
    stored = json.loads(adopted.identity_path.read_text(encoding="utf-8"))
    assert set(stored) == consolidation_identity.IDENTITY_RECORD_FIELDS


def test_local_adoption_refuses_untrusted_or_caller_selected_identity(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    adopt = _required("adopt_local_identity")
    parameters = inspect.signature(adopt).parameters
    assert "vault_id" not in parameters
    assert "installation_id" not in parameters
    assert "machine_key" not in parameters
    assert "root_binding" not in parameters

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        adopt(
            vault,
            principal=principal.most_restrictive_principal(surface="rest"),
            now=1_800_000_000,
        )
    assert not authorization_custody._standalone_host_control_root().exists()


def _hosted_custody(
    binding: hosted_runtime.HostedBindingV2,
    *,
    now: int,
) -> authorization_custody.AuthorizationCustody:
    verifier = authorization_custody.AuthorizationVerifierKey(
        key_id="hosted-cell-key-v1",
        key=b"h" * 32,
        not_before=now - 10,
        not_after=now + 10_000,
    )
    keyring = authorization_custody.AuthorizationKeyring(
        version=4,
        keyring_id="hosted-keyring-v1",
        cell_id=binding.cell_id,
        logical_vault_id=binding.vault_id,
        active_key_id=verifier.key_id,
        accepted_keys=(verifier,),
    )
    control = authorization_custody.AuthorizationControlRecord(
        version=4,
        keyring_id=keyring.keyring_id,
        cell_id=binding.cell_id,
        logical_vault_id=binding.vault_id,
        registry_attachment_id="hosted-control-attachment-v1",
        attachment_epoch=1,
        governance_enrolled=True,
        activation_store_id="hosted-activation-store-v1",
        activation_epoch=1,
        activation_state_digest="a" * 64,
        serving_membership_epoch=1,
        serving_membership_digest="b" * 64,
        issued_at=now - 1,
        expires_at=now + 1_000,
        signing_key_id=verifier.key_id,
    )
    return authorization_custody.AuthorizationCustody(
        keyring_path=binding.state_root / "authorization-keyring.json",
        control_path=binding.state_root / "authorization-control.json",
        keyring=keyring,
        control=control,
    )


def test_hosted_identity_keeps_routing_logical_and_installation_ids_distinct(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    binding = hosted_runtime.HostedBindingV2(
        cell_id="hosted-cell-alpha",
        vault_id="logical-vault-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    for root in binding.roots():
        root[1].mkdir(mode=0o700)
    custody = _hosted_custody(binding, now=now)
    adopt = _required("adopt_hosted_identity")
    load = _required("load_hosted_identity")

    identity = adopt(binding, custody=custody, now=now)
    loaded = load(binding, custody=custody, now=now + 1)

    assert loaded == identity
    assert identity.cell_id == binding.cell_id
    assert identity.vault_id == binding.vault_id
    assert identity.installation_id not in {binding.cell_id, binding.vault_id}
    assert identity.identity_path.parent == binding.state_root
    assert not identity.identity_path.is_relative_to(binding.vault_root)


def test_hosted_cell_vault_alias_fails_before_identity_work(
    tmp_path: Path,
) -> None:
    with pytest.raises(hosted_runtime.HostedConfigError) as error:
        hosted_runtime.HostedBindingV2(
            cell_id="same-typed-value",
            vault_id="same-typed-value",
            vault_root=tmp_path / "vault",
            state_root=tmp_path / "state",
            log_root=tmp_path / "logs",
            runtime_uid=os.getuid(),
            runtime_gid=os.getgid(),
        )
    assert error.value.code == "HOSTED_BINDING_CONFLICT"


def test_local_registry_rejects_reused_installation_id_across_vaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    fixed = "installation-v1-" + "c" * 64
    monkeypatch.setattr(consolidation_identity, "_new_installation_id", lambda: fixed)
    first = _vault(tmp_path, "first")
    second = _vault(tmp_path, "second")
    adopt = _required("adopt_local_identity")
    owner = principal.owner_principal(surface="cli")

    adopted = adopt(first, principal=owner, now=now)
    assert adopted.installation_id == fixed

    second_external = tmp_path / "second-external"
    second_external.mkdir(mode=0o700)
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(second_external / "authorization-keyring.json"),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(second_external / "authorization-control.json"),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(second_external / "authorization-serving-membership.json"),
    )

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        adopt(second, principal=owner, now=now + 1)


@pytest.mark.parametrize(
    "mutation",
    (
        "unknown-field",
        "unknown-version",
        "bad-authentication",
        "forged-fence",
        "wrong-mode",
        "symlink",
        "hard-link",
        "missing-machine-key",
    ),
)
def test_local_identity_refuses_malformed_or_aliased_private_state(
    tmp_path: Path,
    mutation: str,
) -> None:
    now = 1_800_000_000
    vault = _vault(tmp_path)
    identity = _required("adopt_local_identity")(
        vault,
        principal=principal.owner_principal(surface="cli"),
        now=now,
    )
    path = identity.identity_path

    if mutation == "wrong-mode":
        path.chmod(0o644)
    elif mutation == "symlink":
        saved = path.with_name("saved-identity")
        path.rename(saved)
        path.symlink_to(saved)
    elif mutation == "hard-link":
        saved = path.with_name("saved-identity")
        path.rename(saved)
        os.link(saved, path)
    elif mutation == "missing-machine-key":
        authorization_custody._host_control_key_path().unlink()
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "unknown-field":
            value["unexpected"] = "not allowed"
        elif mutation == "unknown-version":
            value["schema"] = "exomem.consolidation-cell-identity/v2"
        elif mutation == "bad-authentication":
            value["authentication"] = "A" * 43
        elif mutation == "forged-fence":
            value["active_fence_digest"] = "f" * 64
            value["record_digest"] = consolidation_identity._record_digest(
                consolidation_identity._without_commitments(value)
            )
            host_key = authorization_custody._load_host_control_key()
            value["authentication"] = consolidation_identity._authentication(
                consolidation_identity._without_authentication(value),
                key=host_key,
            )
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("load_local_identity")(vault, now=now + 1)


def test_copied_local_identity_cannot_claim_a_second_root(tmp_path: Path) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    _required("adopt_local_identity")(
        source,
        principal=principal.owner_principal(surface="cli"),
        now=now,
    )
    copied = tmp_path / "copied"
    shutil.copytree(source, copied)

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("load_local_identity")(copied, now=now + 1)


def test_adoption_is_idempotent_and_content_changes_do_not_rewrite_identity(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    vault = _vault(tmp_path)
    adopt = _required("adopt_local_identity")
    owner = principal.owner_principal(surface="cli")
    first = adopt(vault, principal=owner, now=now)
    before = first.identity_path.read_bytes()

    (vault / "Knowledge Base/Notes/after-adoption.md").write_text(
        "---\ntype: insight\n---\nlegitimate later write\n",
        encoding="utf-8",
    )
    current_census = hosted_portability.canonical_vault_fingerprint(vault)
    second = adopt(vault, principal=owner, now=now + 1)

    assert second == first
    assert second.adoption_census_digest != current_census
    assert second.identity_path.read_bytes() == before


def test_adoption_failure_before_commit_leaves_no_identity_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    monkeypatch.setattr(
        consolidation_identity,
        "_canonical_adoption_census",
        lambda _root: (_ for _ in ()).throw(
            consolidation_identity.ConsolidationIdentityUnavailable()
        ),
    )

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("adopt_local_identity")(
            vault,
            principal=principal.owner_principal(surface="cli"),
            now=1_800_000_000,
        )

    directory = (
        authorization_custody._standalone_host_control_root()
        / "consolidation-cell-identities-v1"
    )
    assert not directory.exists()


def test_lost_adoption_ack_leaves_one_complete_idempotent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    vault = _vault(tmp_path)
    original = consolidation_identity._load_local_with_custody

    def lose_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated abrupt loss after durable identity commit")

    with monkeypatch.context() as fault:
        fault.setattr(consolidation_identity, "_load_local_with_custody", lose_ack)
        with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
            _required("adopt_local_identity")(
                vault,
                principal=principal.owner_principal(surface="cli"),
                now=now,
            )

    recovered = _required("load_local_identity")(vault, now=now + 1)
    replayed = _required("adopt_local_identity")(
        vault,
        principal=principal.owner_principal(surface="cli"),
        now=now + 1,
    )
    assert replayed == recovered


def test_copied_hosted_record_cannot_claim_different_bound_roots(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    source = hosted_runtime.HostedBindingV2(
        cell_id="hosted-cell-copy",
        vault_id="logical-vault-copy",
        vault_root=tmp_path / "source-vault",
        state_root=tmp_path / "source-state",
        log_root=tmp_path / "source-logs",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    for _kind, root in source.roots():
        root.mkdir(mode=0o700)
    custody = _hosted_custody(source, now=now)
    _required("adopt_hosted_identity")(source, custody=custody, now=now)

    copied = hosted_runtime.HostedBindingV2(
        cell_id=source.cell_id,
        vault_id=source.vault_id,
        vault_root=tmp_path / "copied-vault",
        state_root=tmp_path / "copied-state",
        log_root=tmp_path / "copied-logs",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    shutil.copytree(source.vault_root, copied.vault_root)
    shutil.copytree(source.state_root, copied.state_root)
    shutil.copytree(source.log_root, copied.log_root)

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("adopt_hosted_identity")(copied, custody=custody, now=now + 1)


def test_owner_authorized_attachment_move_rebinds_root_but_preserves_identity(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    owner = principal.owner_principal(surface="cli")
    before = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    host_key = authorization_custody._load_host_control_key()
    encoded = consolidation_identity._identity_value(
        cell_id=before.cell_id,
        vault_id=before.vault_id,
        installation_id=before.installation_id,
        installation_generation=2,
        root_binding_id=before.root_binding_id,
        machine_key_id=before.machine_key_id,
        adoption_census_digest=before.adoption_census_digest,
        created_at=before.created_at,
        clone_of_vault_id="vault-source-lineage",
        clone_of_installation_id="installation-v1-" + "a" * 64,
        clone_of_snapshot_digest="b" * 64,
    )
    authorization_custody._replace_control_bytes(
        before.identity_path,
        expected=before.identity_path.read_bytes(),
        target=consolidation_identity._encode_identity(encoded, key=host_key),
    )
    before = _required("load_local_identity")(source, now=now + 1)
    target = tmp_path / "target"
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    acknowledgement = authorization_custody.prepare_standalone_attachment_transfer(
        source,
        target,
        expected_control=drained.control,
        now=now + 3,
    )
    authorization_custody.complete_standalone_attachment_transfer(
        target,
        acknowledgement=acknowledgement,
        now=now + 4,
    )

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("load_local_identity")(target, now=now + 4)

    rebind = _required("rebind_local_identity")
    assert "vault_id" not in inspect.signature(rebind).parameters
    assert "installation_id" not in inspect.signature(rebind).parameters
    moved = rebind(source, target, principal=owner, now=now + 4)
    replay = rebind(source, target, principal=owner, now=now + 4)

    assert moved == replay
    assert moved.vault_id == before.vault_id
    assert moved.installation_id == before.installation_id
    assert moved.installation_generation == before.installation_generation
    assert moved.adoption_census_digest == before.adoption_census_digest
    assert moved.clone_of_vault_id == before.clone_of_vault_id
    assert moved.clone_of_installation_id == before.clone_of_installation_id
    assert moved.clone_of_snapshot_digest == before.clone_of_snapshot_digest
    assert moved.root_binding_id != before.root_binding_id
    assert moved.active_fence_digest != before.active_fence_digest


def test_explicit_rehearsal_clone_gets_fresh_ids_and_authenticated_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    owner = principal.owner_principal(surface="cli")
    source_identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    clone = tmp_path / "clone"
    shutil.copytree(source, clone)
    _select_custody_paths(tmp_path / "clone-external", monkeypatch)
    authorization_custody.provision_standalone_custody(clone, now=now + 1)
    create_clone = _required("create_rehearsal_clone_identity")
    parameters = inspect.signature(create_clone).parameters
    assert "vault_id" not in parameters
    assert "installation_id" not in parameters
    assert "clone_of_vault_id" not in parameters
    assert "clone_of_snapshot_digest" not in parameters

    cloned = create_clone(source, clone, principal=owner, now=now + 1)
    replay = create_clone(source, clone, principal=owner, now=now + 1)

    assert replay == cloned
    assert cloned.vault_id != source_identity.vault_id
    assert cloned.installation_id != source_identity.installation_id
    assert cloned.clone_of_vault_id == source_identity.vault_id
    assert cloned.clone_of_installation_id == source_identity.installation_id
    assert cloned.clone_of_snapshot_digest == hosted_portability.canonical_vault_fingerprint(
        source
    )
    assert cloned.adoption_census_digest == cloned.clone_of_snapshot_digest


def test_rehearsal_clone_refuses_snapshot_drift_and_copied_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    owner = principal.owner_principal(surface="cli")
    source_identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    clone = tmp_path / "clone"
    shutil.copytree(source, clone)
    (clone / "Knowledge Base/Notes/drift.md").write_text(
        "---\ntype: insight\n---\nnot the authenticated snapshot\n",
        encoding="utf-8",
    )
    _select_custody_paths(tmp_path / "clone-external", monkeypatch)
    provisioned = authorization_custody.provision_standalone_custody(
        clone,
        now=now + 1,
    )
    create_clone = _required("create_rehearsal_clone_identity")

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        create_clone(source, clone, principal=owner, now=now + 1)

    (clone / "Knowledge Base/Notes/drift.md").unlink()
    target_path = consolidation_identity._local_identity_path(
        provisioned.logical_vault_id
    )
    target_path.parent.mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(source_identity.identity_path, target_path)
    target_path.chmod(0o600)
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        create_clone(source, clone, principal=owner, now=now + 1)


def test_two_rehearsal_clones_never_share_active_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    owner = principal.owner_principal(surface="cli")
    _required("adopt_local_identity")(source, principal=owner, now=now)
    create_clone = _required("create_rehearsal_clone_identity")
    created = []

    for ordinal in (1, 2):
        clone = tmp_path / f"clone-{ordinal}"
        shutil.copytree(source, clone)
        _select_custody_paths(tmp_path / f"clone-{ordinal}-external", monkeypatch)
        authorization_custody.provision_standalone_custody(
            clone,
            now=now + ordinal,
        )
        created.append(
            create_clone(
                source,
                clone,
                principal=owner,
                now=now + ordinal,
            )
        )

    assert created[0].vault_id != created[1].vault_id
    assert created[0].installation_id != created[1].installation_id
    assert created[0].clone_of_vault_id == created[1].clone_of_vault_id
    assert (
        created[0].clone_of_installation_id
        == created[1].clone_of_installation_id
    )
    assert created[0].clone_of_snapshot_digest == created[1].clone_of_snapshot_digest


def test_failover_transfer_preserves_logical_vault_and_fences_source(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    target = tmp_path / "target"
    owner = principal.owner_principal(surface="cli")
    source_identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=source_identity.cell_id,
            vault_id=source_identity.vault_id,
            operation_id="failover-export-1",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    prepare = _required("prepare_local_failover_identity_transfer")
    complete = _required("complete_local_failover_identity_transfer")
    parameters = inspect.signature(prepare).parameters
    assert "vault_id" not in parameters
    assert "installation_id" not in parameters
    assert "target_installation_id" not in parameters
    assert "target_challenge" not in parameters
    assert "target_generation" not in parameters

    candidate = _failover_target_candidate(
        target,
        operation_id="failover-transfer-1",
        owner=owner,
        now=now + 3,
    )

    transfer = prepare(
        source,
        target,
        operation_id="failover-transfer-1",
        target_candidate=candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    activated = complete(
        source,
        target,
        transfer=transfer,
        principal=owner,
        now=now + 4,
    )

    assert transfer.schema == "vault-identity-transfer/v1"
    assert transfer.operation_id == "failover-transfer-1"
    assert transfer.source_installation_id == source_identity.installation_id
    assert transfer.source_installation_generation == 1
    assert transfer.target_installation_generation == 2
    assert transfer.target_installation_id != source_identity.installation_id
    assert transfer.target_challenge
    assert transfer.target_installation_id == candidate.target_installation_id
    assert transfer.target_challenge == candidate.target_challenge
    assert transfer.archive_digest == exported.archive_sha256
    assert transfer.census_digest == source_identity.adoption_census_digest
    assert activated.vault_id == source_identity.vault_id
    assert activated.installation_id == transfer.target_installation_id
    assert activated.installation_generation == 2
    assert activated.clone_of_vault_id is None
    assert activated.clone_of_installation_id is None
    assert activated.clone_of_snapshot_digest is None
    assert _required("load_local_identity")(target, now=now + 4) == activated
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("load_local_identity")(source, now=now + 4)
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.require_standalone_mutation_admission(
            source,
            now=now + 4,
        )
    shutil.rmtree(source)
    replay = complete(
        source,
        target,
        transfer=transfer,
        principal=owner,
        now=now + 5,
    )
    assert replay == activated


def test_failover_preserves_authenticated_rehearsal_clone_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    original = _vault(tmp_path, "original")
    owner = principal.owner_principal(surface="cli")
    _required("adopt_local_identity")(original, principal=owner, now=now)
    source = tmp_path / "rehearsal-source"
    shutil.copytree(original, source)
    _select_custody_paths(tmp_path / "rehearsal-external", monkeypatch)
    authorization_custody.provision_standalone_custody(source, now=now + 1)
    source_identity = _required("create_rehearsal_clone_identity")(
        original,
        source,
        principal=owner,
        now=now + 1,
    )
    target = tmp_path / "failover-target"
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 2)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 3,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 4,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=source_identity.cell_id,
            vault_id=source_identity.vault_id,
            operation_id="failover-clone-export",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    candidate = _failover_target_candidate(
        target,
        operation_id="failover-clone-transfer",
        owner=owner,
        now=now + 4,
    )
    transfer = _required("prepare_local_failover_identity_transfer")(
        source,
        target,
        operation_id="failover-clone-transfer",
        target_candidate=candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 4,
    )
    activated = _required("complete_local_failover_identity_transfer")(
        source,
        target,
        transfer=transfer,
        principal=owner,
        now=now + 5,
    )

    assert activated.clone_of_vault_id == source_identity.clone_of_vault_id
    assert (
        activated.clone_of_installation_id
        == source_identity.clone_of_installation_id
    )
    assert (
        activated.clone_of_snapshot_digest
        == source_identity.clone_of_snapshot_digest
    )


def test_failover_transfer_refuses_stale_export_even_when_roots_match(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    target = tmp_path / "target"
    owner = principal.owner_principal(surface="cli")
    identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=identity.cell_id,
            vault_id=identity.vault_id,
            operation_id="failover-export-stale",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    changed = "---\ntype: insight\n---\nchanged after the signed export\n"
    (source / "Knowledge Base/Notes/identity.md").write_text(
        changed,
        encoding="utf-8",
    )
    (target / "Knowledge Base/Notes/identity.md").write_text(
        changed,
        encoding="utf-8",
    )

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        candidate = _failover_target_candidate(
            target,
            operation_id="failover-transfer-stale",
            owner=owner,
            now=now + 3,
        )
        _required("prepare_local_failover_identity_transfer")(
            source,
            target,
            operation_id="failover-transfer-stale",
            target_candidate=candidate,
            archive_path=exported.archive_path,
            expected_control=drained.control,
            principal=owner,
            now=now + 3,
        )


@pytest.mark.parametrize(
    "crash_point",
    (
        "after-reservation",
        "after-pending-record",
        "after-target-identity",
        "after-activation",
        "activation-after-membership",
        "activation-after-control",
        "activation-after-registry",
    ),
)
def test_failover_transfer_recovers_every_identity_publication_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    target = tmp_path / "target"
    owner = principal.owner_principal(surface="cli")
    identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=identity.cell_id,
            vault_id=identity.vault_id,
            operation_id=f"export-{crash_point}",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    operation_id = f"transfer-{crash_point}"
    candidate = _failover_target_candidate(
        target,
        operation_id=operation_id,
        owner=owner,
        now=now + 3,
    )
    transfer = _required("prepare_local_failover_identity_transfer")(
        source,
        target,
        operation_id=operation_id,
        target_candidate=candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    raised = False

    def crash_once(point: str) -> None:
        nonlocal raised
        if point == crash_point and not raised:
            raised = True
            raise RuntimeError("simulated failover crash")

    monkeypatch.setattr(
        consolidation_identity,
        "_failover_identity_barrier",
        crash_once,
    )
    monkeypatch.setattr(
        authorization_custody,
        "_standalone_transition_barrier",
        lambda point: crash_once(f"activation-{point}"),
    )
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("complete_local_failover_identity_transfer")(
            source,
            target,
            transfer=transfer,
            principal=owner,
            now=now + 4,
        )
    assert raised
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.require_standalone_mutation_admission(
            source,
            now=now + 4,
        )
    if crash_point in {
        "activation-after-membership",
        "activation-after-control",
    }:
        with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
            authorization_custody.require_standalone_mutation_admission(
                target,
                now=now + 4,
            )
    else:
        interrupted = authorization_custody.load_authorization_custody(
            target,
            now=now + 4,
        )
        assert interrupted.serving_membership is not None
        expected_state = (
            "SERVING"
            if crash_point in {"after-activation", "activation-after-registry"}
            else "DRAINING"
        )
        assert interrupted.serving_membership.replicas[0].state == expected_state
    if crash_point in {
        "after-reservation",
        "after-pending-record",
        "after-target-identity",
    }:
        with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
            _required("load_local_identity")(target, now=now + 4)

    monkeypatch.setattr(
        consolidation_identity,
        "_failover_identity_barrier",
        lambda _point: None,
    )
    monkeypatch.setattr(
        authorization_custody,
        "_standalone_transition_barrier",
        lambda _point: None,
    )
    recovered = _required("complete_local_failover_identity_transfer")(
        source,
        target,
        transfer=transfer,
        principal=owner,
        now=now + 5,
    )

    assert recovered.vault_id == identity.vault_id
    assert recovered.installation_id == transfer.target_installation_id
    assert recovered.installation_generation == 2
    assert _required("load_local_identity")(target, now=now + 5) == recovered


def test_failover_recovers_durable_reservation_after_issuance_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    target = tmp_path / "target"
    owner = principal.owner_principal(surface="cli")
    identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=identity.cell_id,
            vault_id=identity.vault_id,
            operation_id="failover-expiry-export",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    candidate = _failover_target_candidate(
        target,
        operation_id="failover-expiry-transfer",
        owner=owner,
        now=now + 3,
    )
    transfer = _required("prepare_local_failover_identity_transfer")(
        source,
        target,
        operation_id="failover-expiry-transfer",
        target_candidate=candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )

    monkeypatch.setattr(
        consolidation_identity,
        "_failover_identity_barrier",
        lambda point: (_ for _ in ()).throw(RuntimeError("crash"))
        if point == "after-reservation"
        else None,
    )
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("complete_local_failover_identity_transfer")(
            source,
            target,
            transfer=transfer,
            principal=owner,
            now=now + 4,
        )
    monkeypatch.setattr(
        consolidation_identity,
        "_failover_identity_barrier",
        lambda _point: None,
    )

    recovered = _required("complete_local_failover_identity_transfer")(
        source,
        target,
        transfer=transfer,
        principal=owner,
        now=transfer.expires_at + 1,
    )
    assert recovered.installation_id == transfer.target_installation_id
    assert recovered.installation_generation == 2


def test_failover_expiry_cannot_start_a_new_reservation(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    target = tmp_path / "target"
    owner = principal.owner_principal(surface="cli")
    identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=identity.cell_id,
            vault_id=identity.vault_id,
            operation_id="failover-expiry-no-progress-export",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    candidate = _failover_target_candidate(
        target,
        operation_id="failover-expiry-no-progress-transfer",
        owner=owner,
        now=now + 3,
    )
    transfer = _required("prepare_local_failover_identity_transfer")(
        source,
        target,
        operation_id="failover-expiry-no-progress-transfer",
        target_candidate=candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    before = authorization_custody.load_external_custody(target)
    before_identity = identity.identity_path.read_bytes()
    membership_path = Path(
        os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]
    )
    before_membership = membership_path.read_bytes()

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("complete_local_failover_identity_transfer")(
            source,
            target,
            transfer=transfer,
            principal=owner,
            now=transfer.expires_at + 1,
        )

    after = authorization_custody.load_external_custody(target)
    assert after.keyring == before.keyring
    assert after.control == before.control
    assert membership_path.read_bytes() == before_membership
    assert identity.identity_path.read_bytes() == before_identity
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("load_local_identity")(target, now=now + 4)


def test_failover_refuses_two_live_operations_for_one_source_fence(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    source, target, owner, _identity, drained, exported = (
        _quiesced_failover_basis(
            tmp_path,
            now=now,
            export_operation_id="failover-double-prepare-export",
        )
    )
    prepare = _required("prepare_local_failover_identity_transfer")
    first_candidate = _failover_target_candidate(
        target,
        operation_id="failover-double-prepare-a",
        owner=owner,
        now=now + 3,
    )
    first = prepare(
        source,
        target,
        operation_id="failover-double-prepare-a",
        target_candidate=first_candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    second_candidate = _failover_target_candidate(
        target,
        operation_id="failover-double-prepare-b",
        owner=owner,
        now=now + 3,
    )

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        prepare(
            source,
            target,
            operation_id="failover-double-prepare-b",
            target_candidate=second_candidate,
            archive_path=exported.archive_path,
            expected_control=drained.control,
            principal=owner,
            now=now + 3,
        )

    assert (
        prepare(
            source,
            target,
            operation_id=first.operation_id,
            target_candidate=first_candidate,
            archive_path=exported.archive_path,
            expected_control=drained.control,
            principal=owner,
            now=now + 3,
        )
        == first
    )


@pytest.mark.parametrize(
    "crash_point",
    ("after-membership", "after-control"),
)
@pytest.mark.parametrize(
    "recovery_crash_point",
    ("after-membership", "after-control"),
)
def test_failover_recovers_expired_activation_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    recovery_crash_point: str,
) -> None:
    now = 1_800_000_000
    source, target, owner, _identity, drained, exported = (
        _quiesced_failover_basis(
            tmp_path,
            now=now,
            export_operation_id=f"failover-expired-activation-{crash_point}-export",
        )
    )
    operation_id = f"failover-expired-activation-{crash_point}"
    candidate = _failover_target_candidate(
        target,
        operation_id=operation_id,
        owner=owner,
        now=now + 3,
    )
    transfer = _required("prepare_local_failover_identity_transfer")(
        source,
        target,
        operation_id=operation_id,
        target_candidate=candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    raised = False

    def crash_once(point: str) -> None:
        nonlocal raised
        if point == crash_point and not raised:
            raised = True
            raise RuntimeError("simulated activation crash")

    monkeypatch.setattr(
        authorization_custody,
        "_standalone_transition_barrier",
        crash_once,
    )
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("complete_local_failover_identity_transfer")(
            source,
            target,
            transfer=transfer,
            principal=owner,
            now=now + 4,
        )
    assert raised

    membership_path = Path(
        os.environ[authorization_custody.MEMBERSHIP_FILE_ENV]
    )
    expired_at = int(
        json.loads(membership_path.read_text(encoding="utf-8"))["expires_at"]
    )
    recovery_raised = False

    def crash_recovery_once(point: str) -> None:
        nonlocal recovery_raised
        if point == recovery_crash_point and not recovery_raised:
            recovery_raised = True
            raise RuntimeError("simulated recovery crash")

    monkeypatch.setattr(
        authorization_custody,
        "_standalone_transition_barrier",
        crash_recovery_once,
    )
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("complete_local_failover_identity_transfer")(
            source,
            target,
            transfer=transfer,
            principal=owner,
            now=expired_at + 1,
        )
    assert recovery_raised

    monkeypatch.setattr(
        authorization_custody,
        "_standalone_transition_barrier",
        lambda _point: None,
    )
    recovered = _required("complete_local_failover_identity_transfer")(
        source,
        target,
        transfer=transfer,
        principal=owner,
        now=expired_at + 2,
    )

    assert recovered.installation_id == transfer.target_installation_id
    active = authorization_custody.load_authorization_custody(
        target,
        now=expired_at + 2,
    )
    assert active.serving_membership is not None
    assert active.serving_membership.replicas[0].state == "SERVING"


def test_failover_operation_id_cannot_be_rebound_to_a_second_target(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    first_target = tmp_path / "target-a"
    second_target = tmp_path / "target-b"
    owner = principal.owner_principal(surface="cli")
    identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, first_target)
    shutil.copytree(source, second_target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=identity.cell_id,
            vault_id=identity.vault_id,
            operation_id="failover-export-operation-conflict",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    prepare = _required("prepare_local_failover_identity_transfer")
    first_candidate = _failover_target_candidate(
        first_target,
        operation_id="failover-operation-one",
        owner=owner,
        now=now + 3,
    )
    first = prepare(
        source,
        first_target,
        operation_id="failover-operation-one",
        target_candidate=first_candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    replay = prepare(
        source,
        first_target,
        operation_id="failover-operation-one",
        target_candidate=first_candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    assert replay == first

    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        second_candidate = _failover_target_candidate(
            second_target,
            operation_id=first.operation_id,
            owner=owner,
            now=now + 3,
        )
        prepare(
            source,
            second_target,
            operation_id=first.operation_id,
            target_candidate=second_candidate,
            archive_path=exported.archive_path,
            expected_control=drained.control,
            principal=owner,
            now=now + 3,
        )


def test_failover_rechecks_target_census_after_authority_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    source = _vault(tmp_path, "source")
    target = tmp_path / "target"
    owner = principal.owner_principal(surface="cli")
    identity = _required("adopt_local_identity")(
        source,
        principal=owner,
        now=now,
    )
    shutil.copytree(source, target)
    serving = authorization_custody.load_authorization_custody(source, now=now + 1)
    draining = authorization_custody.begin_standalone_attachment_drain(
        source,
        expected_control=serving.control,
        now=now + 2,
    )
    drained = authorization_custody.acknowledge_standalone_attachment_drain(
        source,
        expected_control=draining.control,
        now=now + 3,
    )
    exported = hosted_portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=hosted_portability.PortabilityContext(
            cell_id=identity.cell_id,
            vault_id=identity.vault_id,
            operation_id="failover-export-reservation-race",
            created_at="2027-01-15T08:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )
    candidate = _failover_target_candidate(
        target,
        operation_id="failover-transfer-reservation-race",
        owner=owner,
        now=now + 3,
    )
    transfer = _required("prepare_local_failover_identity_transfer")(
        source,
        target,
        operation_id="failover-transfer-reservation-race",
        target_candidate=candidate,
        archive_path=exported.archive_path,
        expected_control=drained.control,
        principal=owner,
        now=now + 3,
    )
    original = authorization_custody.reserve_standalone_attachment_transfer

    def drift_after_reservation(*args: object, **kwargs: object):
        reserved = original(*args, **kwargs)
        (target / "Knowledge Base/Notes/identity.md").write_text(
            "---\ntype: insight\n---\ndrifted after reservation\n",
            encoding="utf-8",
        )
        return reserved

    monkeypatch.setattr(
        authorization_custody,
        "reserve_standalone_attachment_transfer",
        drift_after_reservation,
    )
    with pytest.raises(consolidation_identity.ConsolidationIdentityUnavailable):
        _required("complete_local_failover_identity_transfer")(
            source,
            target,
            transfer=transfer,
            principal=owner,
            now=now + 4,
        )
    interrupted = authorization_custody.load_authorization_custody(
        target,
        now=now + 4,
    )
    assert interrupted.serving_membership is not None
    assert interrupted.serving_membership.replicas[0].state == "DRAINING"
