from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from exomem import __version__, hosted_runtime
from exomem.hosted_runtime import (
    HOSTED_PROTOCOL_VERSION,
    HostedBindingV2,
    HostedCellConfig,
    HostedConfigError,
    HostedMigrationLimits,
    initialize_hosted_cell_v2,
    validate_hosted_binding_v2,
)


def _binding(tmp_path: Path, **overrides: object) -> HostedBindingV2:
    values: dict[str, object] = {
        "cell_id": "cell-v2-alpha",
        "vault_id": "vault-logical-alpha",
        "vault_root": tmp_path / "vault",
        "state_root": tmp_path / "state",
        "log_root": tmp_path / "log",
        "runtime_uid": os.getuid(),
        "runtime_gid": os.getgid(),
    }
    values.update(overrides)
    return HostedBindingV2(**values)  # type: ignore[arg-type]


def _bootstrap(**_kwargs: object) -> int:
    return 1


def test_v2_runtime_config_uses_bound_identity_without_plaintext_env_credential(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
    )
    config = HostedCellConfig.from_env(
        {
            "EXOMEM_HOSTED_CELL_ID": binding.cell_id,
            "EXOMEM_HOSTED_VAULT_ID": binding.vault_id,
            "EXOMEM_VAULT_PATH": str(binding.vault_root),
            "EXOMEM_HOSTED_STATE_ROOT": str(binding.state_root),
            "EXOMEM_LOG_DIR": str(binding.log_root),
            "EXOMEM_HOSTED_RUNTIME_UID": str(binding.runtime_uid),
            "EXOMEM_HOSTED_RUNTIME_GID": str(binding.runtime_gid),
            "EXOMEM_HOSTED_WORKER_POLICY_DIGEST": "a" * 64,
        },
        require_provisioned=True,
    )

    assert config.vault_id == binding.vault_id
    assert config.runtime_uid == binding.runtime_uid
    assert config.runtime_gid == binding.runtime_gid
    assert config.worker_policy_digest == "a" * 64
    assert config.service_credential is None
    assert config.requires_dynamic_security is True
    assert config.matches_service_credential("legacy-must-not-work") is False


def test_binding_v2_persists_storage_identity_not_release_proof(tmp_path: Path) -> None:
    binding = _binding(tmp_path)

    result = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
    )

    assert result.status == "provisioned"
    assert result.credential_revision == 1
    assert result.binding_version == 2
    for kind, root in binding.roots():
        marker = root / ".exomem-hosted-cell.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload == {
            "binding_version": 2,
            "cell_id": "cell-v2-alpha",
            "log_root": str(binding.log_root),
            "root_kind": kind,
            "runtime_gid": os.getgid(),
            "runtime_uid": os.getuid(),
            "state_root": str(binding.state_root),
            "vault_id": "vault-logical-alpha",
            "vault_root": str(binding.vault_root),
        }
        assert "release" not in payload and "protocol" not in payload
        assert stat.S_IMODE(root.lstat().st_mode) == 0o700
        assert stat.S_IMODE(marker.lstat().st_mode) == 0o600
        assert root.lstat().st_uid == os.getuid()
        assert root.lstat().st_gid == os.getgid()
    validate_hosted_binding_v2(binding, require_scaffold=True)


def test_binding_v2_init_is_idempotent_and_never_rewrites_canonical_bytes(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    first = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
    )
    canonical = binding.vault_root / "Knowledge Base/index.md"
    before = canonical.read_bytes()

    second = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
    )

    assert first.status == "provisioned"
    assert second.status == "existing"
    assert canonical.read_bytes() == before

    foreign = _binding(tmp_path, vault_id="vault-foreign")
    with pytest.raises(HostedConfigError) as conflict:
        initialize_hosted_cell_v2(
            foreign,
            expected_release=__version__,
            expected_protocol=HOSTED_PROTOCOL_VERSION,
            active_credential_version="credential-v1",
            bootstrap_security=_bootstrap,
        )
    assert conflict.value.code == "HOSTED_BINDING_CONFLICT"
    assert canonical.read_bytes() == before


def test_binding_v2_operator_retry_replays_status_and_conflicts_changed_request(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    first = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        operation_id="init-operation",
        request_digest="a" * 64,
        bootstrap_security=_bootstrap,
    )
    replay = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        operation_id="init-operation",
        request_digest="a" * 64,
        bootstrap_security=_bootstrap,
    )

    assert first.status == replay.status == "provisioned"
    assert first.credential_revision == replay.credential_revision == 1

    with pytest.raises(HostedConfigError) as conflict:
        initialize_hosted_cell_v2(
            binding,
            expected_release=__version__,
            expected_protocol=HOSTED_PROTOCOL_VERSION,
            active_credential_version="credential-v1",
            operation_id="init-operation",
            request_digest="b" * 64,
            bootstrap_security=_bootstrap,
        )
    assert conflict.value.code == "HOSTED_OPERATION_CONFLICT"


@pytest.mark.parametrize("runtime_uid,runtime_gid", [(0, 1), (1, 0), (-1, 1)])
def test_binding_v2_rejects_root_or_out_of_range_runtime_identity(
    tmp_path: Path, runtime_uid: int, runtime_gid: int
) -> None:
    with pytest.raises(HostedConfigError) as error:
        _binding(tmp_path, runtime_uid=runtime_uid, runtime_gid=runtime_gid)
    assert error.value.code == "HOSTED_RUNTIME_ID_INVALID"


def test_binding_v2_rejects_unowned_data_and_actual_marker_mode_drift(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    binding.vault_root.mkdir()
    sentinel = binding.vault_root / "do-not-touch.md"
    sentinel.write_text("foreign", encoding="utf-8")

    with pytest.raises(HostedConfigError) as unowned:
        initialize_hosted_cell_v2(
            binding,
            expected_release=__version__,
            expected_protocol=HOSTED_PROTOCOL_VERSION,
            active_credential_version="credential-v1",
            bootstrap_security=_bootstrap,
        )
    assert unowned.value.code == "HOSTED_PROVISIONING_CONFLICT"
    assert sentinel.read_text(encoding="utf-8") == "foreign"
    assert not binding.state_root.exists()

    sentinel.unlink()
    binding.vault_root.rmdir()
    initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
    )
    marker = binding.state_root / ".exomem-hosted-cell.json"
    marker.chmod(0o640)
    with pytest.raises(HostedConfigError) as drift:
        validate_hosted_binding_v2(binding, require_scaffold=True)
    assert drift.value.code == "HOSTED_ROOT_OWNERSHIP_MISMATCH"


def test_privileged_v1_migration_is_bounded_retryable_and_rejects_unsafe_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_values = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": "cell-v2-alpha",
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
        "EXOMEM_HOSTED_STATE_ROOT": str(tmp_path / "state"),
        "EXOMEM_LOG_DIR": str(tmp_path / "log"),
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": "x" * 32,
    }
    legacy = hosted_runtime.HostedCellConfig.from_env(legacy_values)
    hosted_runtime.provision_hosted_cell(legacy)
    binding = _binding(tmp_path)
    monkeypatch.setattr(hosted_runtime.os, "geteuid", lambda: 0)

    migrated = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
        allow_privileged_migration=True,
    )
    assert migrated.status == "migrated"
    validate_hosted_binding_v2(binding, require_scaffold=True)

    # Recreate a matching v1 tree and prove the complete preflight rejects a
    # hard link before changing any marker to v2.
    other = tmp_path / "unsafe"
    legacy_values.update(
        {
            "EXOMEM_VAULT_PATH": str(other / "vault"),
            "EXOMEM_HOSTED_STATE_ROOT": str(other / "state"),
            "EXOMEM_LOG_DIR": str(other / "log"),
        }
    )
    legacy_unsafe = hosted_runtime.HostedCellConfig.from_env(legacy_values)
    hosted_runtime.provision_hosted_cell(legacy_unsafe)
    source = legacy_unsafe.state_root / "linked"
    source.write_text("runtime", encoding="utf-8")
    os.link(source, legacy_unsafe.state_root / "linked-again")
    unsafe_binding = _binding(
        other,
        vault_root=other / "vault",
        state_root=other / "state",
        log_root=other / "log",
    )

    with pytest.raises(HostedConfigError) as unsafe:
        initialize_hosted_cell_v2(
            unsafe_binding,
            expected_release=__version__,
            expected_protocol=HOSTED_PROTOCOL_VERSION,
            active_credential_version="credential-v1",
            bootstrap_security=_bootstrap,
            allow_privileged_migration=True,
            migration_limits=HostedMigrationLimits(max_entries=100, max_bytes=1024 * 1024),
        )
    assert unsafe.value.code == "HOSTED_ROOT_UNSAFE_ENTRY"
    assert json.loads(
        (unsafe_binding.state_root / ".exomem-hosted-cell.json").read_text(encoding="utf-8")
    )["version"] == 1


def test_privileged_v1_migration_retries_after_partial_descriptor_chown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": "cell-v2-alpha",
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
        "EXOMEM_HOSTED_STATE_ROOT": str(tmp_path / "state"),
        "EXOMEM_LOG_DIR": str(tmp_path / "log"),
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": "x" * 32,
    }
    legacy = hosted_runtime.HostedCellConfig.from_env(values)
    hosted_runtime.provision_hosted_cell(legacy)
    binding = _binding(tmp_path)
    monkeypatch.setattr(hosted_runtime.os, "geteuid", lambda: 0)
    real_fchown = hosted_runtime.os.fchown
    calls = 0

    def fail_after_progress(fd: int, uid: int, gid: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected chown interruption")
        real_fchown(fd, uid, gid)

    monkeypatch.setattr(hosted_runtime.os, "fchown", fail_after_progress)
    with pytest.raises(HostedConfigError) as interrupted:
        initialize_hosted_cell_v2(
            binding,
            expected_release=__version__,
            expected_protocol=HOSTED_PROTOCOL_VERSION,
            active_credential_version="credential-v1",
            bootstrap_security=_bootstrap,
            allow_privileged_migration=True,
        )
    assert interrupted.value.code == "HOSTED_ROOT_OWNERSHIP_MISMATCH"

    result = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
        allow_privileged_migration=True,
    )
    assert result.status == "migrated"
    validate_hosted_binding_v2(binding, require_scaffold=True)


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo", "socket", "device"])
def test_migration_preflight_rejects_every_unsafe_entry_type(
    tmp_path: Path, unsafe_kind: str
) -> None:
    root = tmp_path / "migration-root"
    root.mkdir()
    unsafe = root / "unsafe"
    opened_socket: socket.socket | None = None
    try:
        if unsafe_kind == "symlink":
            target = tmp_path / "target"
            target.write_text("target", encoding="utf-8")
            try:
                unsafe.symlink_to(target)
            except OSError:
                pytest.skip("symlinks unavailable")
        elif unsafe_kind == "fifo":
            if not hasattr(os, "mkfifo"):
                pytest.skip("FIFOs unavailable")
            os.mkfifo(unsafe)
        elif unsafe_kind == "socket":
            if not hasattr(socket, "AF_UNIX"):
                pytest.skip("Unix sockets unavailable")
            opened_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            opened_socket.bind(str(unsafe))
        else:
            if not hasattr(os, "mknod") or not hasattr(os, "makedev"):
                pytest.skip("device nodes unavailable")
            try:
                os.mknod(unsafe, stat.S_IFCHR | 0o600, os.makedev(1, 3))
            except PermissionError:
                pytest.skip("device node creation requires privilege")

        with pytest.raises(HostedConfigError) as error:
            hosted_runtime._preflight_migration_tree(
                root, HostedMigrationLimits(max_entries=10, max_bytes=1024)
            )
        assert error.value.code == "HOSTED_ROOT_UNSAFE_ENTRY"
    finally:
        if opened_socket is not None:
            opened_socket.close()


def test_descriptor_migration_rejects_replacement_race_without_following_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "migration-root"
    root.mkdir()
    payload = root / "payload"
    payload.write_text("owned", encoding="utf-8")
    entries = hosted_runtime._preflight_migration_tree(
        root, HostedMigrationLimits(max_entries=10, max_bytes=1024)
    )
    outside = tmp_path / "outside"
    outside.write_text("must remain untouched", encoding="utf-8")
    payload.unlink()
    try:
        payload.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    binding = _binding(
        tmp_path,
        vault_root=root,
        state_root=tmp_path / "state-other",
        log_root=tmp_path / "log-other",
    )

    with pytest.raises(HostedConfigError) as error:
        hosted_runtime._converge_tree_ownership(entries, binding)

    assert error.value.code == "HOSTED_ROOT_UNSAFE_ENTRY"
    assert outside.read_text(encoding="utf-8") == "must remain untouched"
