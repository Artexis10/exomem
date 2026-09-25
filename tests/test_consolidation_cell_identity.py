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


def _required(name: str):
    value = getattr(consolidation_identity, name, None)
    assert callable(value), f"missing consolidation identity API: {name}"
    return value


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
    assert moved.root_binding_id != before.root_binding_id
    assert moved.active_fence_digest != before.active_fence_digest
