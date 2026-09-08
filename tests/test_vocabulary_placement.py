from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import mutation_lock, state_paths, vocabulary_authority, vocabulary_placement
from exomem.governance import authorization_custody, authorization_hosted_mount


@dataclass(frozen=True)
class _Keyring:
    keyring_id: str = "keyring-1"


@dataclass(frozen=True)
class _Control:
    cell_id: str = "cell-1"
    logical_vault_id: str = "vault-1"
    activation_epoch: int = 7
    vocabulary_authority_floor: int = 2


@dataclass(frozen=True)
class _Custody:
    control_path: Path
    keyring: _Keyring = _Keyring()
    control: _Control = _Control()


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True)
    if os.name == "nt":
        mutation_lock._windows_apply_private_dacl(
            path, mutation_lock._windows_current_user_sid()
        )
    else:
        path.chmod(0o700)
    return path


def test_unset_placement_retains_control_parent_and_canonical_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", raising=False)
    control = tmp_path / "custody" / "control.json"

    marker, database = vocabulary_authority.authority_artifact_paths(
        control, "logical-vault", vault_root=tmp_path / "vault"
    )

    token = "d684ea28ee9b1660a6029796f502e977b02bfbbad679bb8a245174731e56e4b0"
    assert marker == control.parent / f"{token}.vocabulary-authority.activation.json"
    assert database == control.parent / f"{token}.vocabulary-authority.sqlite"


def test_configured_durable_directory_is_stable_across_runtime_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    control_parent = _private_directory(tmp_path / "custody")
    control = control_parent / "control.json"
    control.write_text("control")
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))
    custody = _Custody(control)
    first = vocabulary_authority.VocabularyAuthority(
        vault, custody_loader=lambda _root, *, now: custody, clock=lambda: 1_700_000_000
    )
    marker = first._create_marker(  # noqa: SLF001
        custody,
        floor=vocabulary_authority._deployment_floor_for_adapter(  # noqa: SLF001
            runtime="vocabulary-authority/v2", generation=7
        ),
        owner=vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
            owner_id="owner-1", ceremony_id="ceremony-1"
        ),
        now=1_700_000_000,
    )
    connection = first._connect(custody, create=True, marker=marker)  # noqa: SLF001
    assert connection is not None
    connection.execute(
        "INSERT INTO activation VALUES (1, 2, 7, ?, ?, ?, ?, ?, 'active')",
        ("cell-1", "vault-1", "keyring-1", marker["store_id"], "vocabulary-authority/v2"),
    )
    connection.close()

    restarted = vocabulary_authority.VocabularyAuthority(
        vault, custody_loader=lambda _root, *, now: custody, clock=lambda: 1_700_000_001
    )

    assert restarted.runtime_status().mode == "v2"
    assert restarted._marker_path(custody).parent == durable  # noqa: SLF001
    assert restarted._database_path(custody).parent == durable  # noqa: SLF001
    for path in (
        restarted._marker_path(custody),  # noqa: SLF001
        restarted._database_path(custody),  # noqa: SLF001
    ):
        retained = mutation_lock.retain_regular_file(path)
        try:
            assert authorization_custody._file_is_owner_protected(  # noqa: SLF001
                retained.fd, os.fstat(retained.fd)
            )
        finally:
            retained.close()


def test_fresh_authority_artifacts_are_protected_before_create_only_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    control_parent = _private_directory(tmp_path / "custody")
    control = control_parent / "control.json"
    control.write_text("control")
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))
    custody = _Custody(control)
    authority = vocabulary_authority.VocabularyAuthority(vault)
    protected: list[Path] = []
    pinned: list[tuple[Path, bool]] = []
    original = authorization_custody._prepare_private_stage  # noqa: SLF001
    original_retain = mutation_lock.retain_regular_file

    def prepare(path, staged) -> None:
        assert not os.path.lexists(path)
        original(path, staged)
        protected.append(path)

    def retain(path, *, delete_access=True):
        pinned.append((Path(path), delete_access))
        return original_retain(path, delete_access=delete_access)

    monkeypatch.setattr(authorization_custody, "_prepare_private_stage", prepare)
    monkeypatch.setattr(mutation_lock, "retain_regular_file", retain)
    marker = authority._create_marker(  # noqa: SLF001
        custody,
        floor=vocabulary_authority._deployment_floor_for_adapter(  # noqa: SLF001
            runtime="vocabulary-authority/v2", generation=7
        ),
        owner=vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
            owner_id="owner-1", ceremony_id="ceremony-1"
        ),
        now=1_700_000_000,
    )
    connection = authority._connect(custody, create=True, marker=marker)  # noqa: SLF001
    assert connection is not None
    connection.close()

    paths = vocabulary_authority.authority_artifact_paths(
        control, "vault-1", vault_root=vault
    )
    assert protected == [paths.marker_path, paths.database_path]
    assert pinned == [(paths.database_path, False)]


def test_private_stage_refusal_leaves_no_published_authority_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    control = _private_directory(tmp_path / "custody") / "control.json"
    control.write_text("control")
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))
    custody = _Custody(control)
    authority = vocabulary_authority.VocabularyAuthority(vault)
    monkeypatch.setattr(
        authorization_custody,
        "_prepare_private_stage",
        lambda *_args: (_ for _ in ()).throw(
            authorization_custody.AuthorizationCustodyUnavailable()
        ),
    )

    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        authority._create_marker(  # noqa: SLF001
            custody,
            floor=vocabulary_authority._deployment_floor_for_adapter(  # noqa: SLF001
                runtime="vocabulary-authority/v2", generation=7
            ),
            owner=vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
                owner_id="owner-1", ceremony_id="ceremony-1"
            ),
            now=1_700_000_000,
        )

    paths = vocabulary_authority.authority_artifact_paths(
        control, "vault-1", vault_root=vault
    )
    assert not any(os.path.lexists(path) for path in vocabulary_placement.artifact_family(paths))
    assert not any(entry.name.startswith(".exomem-held-publish-") for entry in durable.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory write bits")
def test_configured_directory_does_not_require_writable_custody_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    control_parent = _private_directory(tmp_path / "custody")
    control = control_parent / "control.json"
    control.write_text("control")
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))
    custody = _Custody(control)
    authority = vocabulary_authority.VocabularyAuthority(vault)
    control_parent.chmod(0o500)
    try:
        paths = vocabulary_authority.authority_artifact_paths(
            control, "vault-1", vault_root=vault
        )
        authority._create_marker(  # noqa: SLF001
            custody,
            floor=vocabulary_authority._deployment_floor_for_adapter(  # noqa: SLF001
                runtime="vocabulary-authority/v2", generation=7
            ),
            owner=vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
                owner_id="owner-1", ceremony_id="ceremony-1"
            ),
            now=1_700_000_000,
        )
    finally:
        control_parent.chmod(0o700)

    assert paths.marker_path.parent == durable
    assert paths.database_path.parent == durable
    assert paths.marker_path.is_file()


def test_owner_setup_can_require_the_validated_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))

    assert vocabulary_placement.validated_authority_directory(vault) == durable

    monkeypatch.delenv("EXOMEM_VOCABULARY_AUTHORITY_DIR")
    with pytest.raises(vocabulary_placement.VocabularyAuthorityPlacementUnavailable):
        vocabulary_placement.validated_authority_directory(vault)


@pytest.mark.parametrize("configured", ["relative", "missing", "inside-vault", "inside-state", "unsafe", "symlink"])
def test_configured_directory_must_be_existing_private_unambiguous_external_storage(
    configured: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _private_directory(tmp_path / "vault")
    state_root = _private_directory(tmp_path / "state")
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(state_root))
    control = tmp_path / "custody" / "control.json"
    if configured == "relative":
        value = Path("relative")
    elif configured == "missing":
        value = tmp_path / "missing"
    elif configured == "inside-vault":
        value = _private_directory(vault / "authority")
    elif configured == "inside-state":
        value = _private_directory(state_paths.vault_state_dir(vault))
    elif configured == "unsafe":
        if os.name == "nt":
            pytest.skip("requires POSIX directory privacy bits")
        value = _private_directory(tmp_path / "unsafe")
        value.chmod(0o755)
    else:
        target = _private_directory(tmp_path / "target")
        value = tmp_path / "authority-link"
        try:
            value.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name == "nt":
                pytest.skip("directory symlink creation is unavailable")
            raise
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(value))

    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        vocabulary_authority.authority_artifact_paths(
            control, "vault-1", vault_root=vault
        )


@pytest.mark.parametrize("artifact", ["marker", "database", "-journal", "-wal", "-shm"])
def test_configured_directory_refuses_each_legacy_colocated_artifact(
    artifact: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    control_parent = _private_directory(tmp_path / "custody")
    control = control_parent / "control.json"
    monkeypatch.delenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", raising=False)
    legacy = vocabulary_authority.authority_artifact_paths(control, "vault-1")
    path = (
        legacy.marker_path
        if artifact == "marker"
        else legacy.database_path
        if artifact == "database"
        else legacy.database_path.with_name(f"{legacy.database_path.name}{artifact}")
    )
    path.write_text("legacy")
    path.chmod(0o600)
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))

    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        vocabulary_authority.authority_artifact_paths(
            control, "vault-1", vault_root=vault
        )


def test_floor_two_missing_configured_store_stays_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    control_parent = _private_directory(tmp_path / "custody")
    custody = _Custody(control_parent / "control.json")
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))

    status = vocabulary_authority.VocabularyAuthority(
        vault, custody_loader=lambda _root, *, now: custody, clock=lambda: 1_700_000_000
    ).runtime_status()

    assert status.mode == "unavailable"


def test_runtime_refuses_an_explicit_missing_directory_before_local_v1_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "EXOMEM_VOCABULARY_AUTHORITY_DIR", str(tmp_path / "missing-authority")
    )
    monkeypatch.delenv(authorization_custody.KEYRING_FILE_ENV, raising=False)
    monkeypatch.delenv(authorization_custody.CONTROL_FILE_ENV, raising=False)

    status = vocabulary_authority.VocabularyAuthority(tmp_path / "vault").runtime_status()

    assert status.mode == "unavailable"


@pytest.mark.parametrize(
    "suffix",
    [
        vocabulary_placement.MARKER_SUFFIX,
        vocabulary_placement.DATABASE_SUFFIX,
        *(
            vocabulary_placement.DATABASE_SUFFIX + sidecar
            for sidecar in vocabulary_placement.SQLITE_SIDECAR_SUFFIXES
        ),
    ],
)
def test_runtime_refuses_configured_authority_residue_without_custody_identity(
    suffix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    durable = _private_directory(tmp_path / "durable-authority")
    artifact = durable / ("a" * 64 + suffix)
    artifact.write_text("retained")
    artifact.chmod(0o600)
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))
    monkeypatch.delenv(authorization_custody.KEYRING_FILE_ENV, raising=False)
    monkeypatch.delenv(authorization_custody.CONTROL_FILE_ENV, raising=False)

    status = vocabulary_authority.VocabularyAuthority(tmp_path / "vault").runtime_status()

    assert status.mode == "unavailable"


def test_custody_identity_guard_uses_configured_authority_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    control_parent = _private_directory(tmp_path / "custody")
    control = control_parent / "control.json"
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))
    marker, _database = vocabulary_authority.authority_artifact_paths(
        control, "vault-1", vault_root=vault
    )
    marker.write_text("retained")
    marker.chmod(0o600)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody._require_vocabulary_identity_unused(  # noqa: SLF001
            control, "vault-1", vault_root=vault
        )


def test_hosted_republish_identity_guard_uses_configured_authority_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _private_directory(tmp_path / "custody")
    (destination / "keyring.json").write_bytes(b"old-keyring")
    (destination / "control.json").write_bytes(b"old-control")
    durable = _private_directory(tmp_path / "durable-authority")
    monkeypatch.setenv("EXOMEM_VOCABULARY_AUTHORITY_DIR", str(durable))
    marker, _database = vocabulary_authority.authority_artifact_paths(
        destination / "control.json", "old-vault"
    )
    marker.write_text("retained")
    marker.chmod(0o600)
    monkeypatch.setattr(
        authorization_custody,
        "parse_keyring",
        lambda raw: SimpleNamespace(keyring_id=raw.decode()),
    )
    monkeypatch.setattr(
        authorization_custody,
        "parse_control_record",
        lambda raw, *, keyring, now: SimpleNamespace(
            cell_id="old-cell" if raw == b"old-control" else "new-cell",
            logical_vault_id="old-vault" if raw == b"old-control" else "new-vault",
            keyring_id=keyring.keyring_id,
            vocabulary_authority_floor=1,
        ),
    )

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount._refuse_authority_identity_change(  # noqa: SLF001
            destination,
            {"keyring.json": b"new-keyring", "control.json": b"new-control"},
        )
