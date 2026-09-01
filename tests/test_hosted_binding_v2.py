from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import __version__, hosted_runtime, init
from exomem.hosted_runtime import (
    HOSTED_PROTOCOL_VERSION,
    HostedBindingV2,
    HostedCellConfig,
    HostedConfigError,
    HostedMigrationLimits,
    initialize_hosted_cell_v2,
    validate_hosted_binding_v2,
)


@pytest.mark.skipif(os.name == "nt", reason="hosted migration jobs run on POSIX")
def test_target_image_init_job_holds_lifetime_then_state_migration_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from exomem import hosted_restore, state_migration

    binding = _binding(tmp_path)
    events: list[object] = []
    authority = object()

    monkeypatch.setattr(
        hosted_runtime,
        "initialize_hosted_cell_v2",
        lambda *_args, **_kwargs: SimpleNamespace(as_operator_data=lambda: {}),
    )

    @contextmanager
    def lifetime(root, *, binding):
        events.append(("lifetime-enter", Path(root), binding.cell_id))
        try:
            yield
        finally:
            events.append("lifetime-exit")

    monkeypatch.setattr(hosted_restore, "acquire_hosted_lifetime_lock", lifetime)
    monkeypatch.setattr(
        state_migration,
        "assert_offline_migration_authority",
        lambda *, source: events.append(("authority", source)) or authority,
    )
    monkeypatch.setattr(
        state_migration,
        "migrate_vault_state_offline",
        lambda root, *, authority: events.append(
            ("state-migration-lock", Path(root), authority)
        ),
    )
    monkeypatch.setattr(hosted_runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        hosted_runtime,
        "_preflight_migration_tree",
        lambda root, _limits: events.append(("preflight", Path(root))) or {},
    )
    monkeypatch.setattr(
        hosted_runtime,
        "_converge_tree_ownership",
        lambda _entries, owner: events.append(("ownership", owner.cell_id)),
    )
    monkeypatch.setenv("EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION", "1")

    code, data = hosted_runtime.execute_hosted_init_v2(
        {
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "operation_id": "operation-state-migration",
            "cell_id": binding.cell_id,
            "vault_id": binding.vault_id,
            "vault_root": str(binding.vault_root),
            "state_root": str(binding.state_root),
            "log_root": str(binding.log_root),
            "expected_release": __version__,
            "expected_protocol": HOSTED_PROTOCOL_VERSION,
            "runtime_uid": binding.runtime_uid,
            "runtime_gid": binding.runtime_gid,
            "active_credential_version": "credential-v1",
        }
    )

    assert code == "HOSTED_CELL_INITIALIZED"
    assert data == {}
    assert events == [
        ("lifetime-enter", binding.state_root, binding.cell_id),
        ("authority", "hosted target-image initialization job"),
        ("state-migration-lock", binding.vault_root, authority),
        ("preflight", binding.state_root),
        ("ownership", binding.cell_id),
        "lifetime-exit",
    ]


@pytest.mark.skipif(os.name == "nt", reason="hosted lifetime locking requires POSIX flock")
def test_real_hosted_lifetime_holder_excludes_target_image_state_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import hosted_restore, state_migration
    from exomem.hosted_operator import OperatorFailure

    binding = _binding(tmp_path)
    authority = object()
    calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(
        state_migration,
        "assert_offline_migration_authority",
        lambda *, source: authority,
    )
    monkeypatch.setattr(
        state_migration,
        "migrate_vault_state_offline",
        lambda vault, *, authority: calls.append((Path(vault), authority)),
    )

    with hosted_restore.acquire_hosted_lifetime_lock(binding.state_root, binding=binding):
        with pytest.raises(OperatorFailure) as error:
            hosted_runtime._migrate_hosted_machine_state_offline(binding)
        assert error.value.code == "HOSTED_RESTORE_BUSY"
        assert calls == []

    hosted_runtime._migrate_hosted_machine_state_offline(binding)

    assert calls == [(binding.vault_root, authority)]


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


def test_hosted_cell_config_defaults_are_import_safe_without_posix_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(hosted_runtime.os, "geteuid", raising=False)
    monkeypatch.delattr(hosted_runtime.os, "getegid", raising=False)

    config = HostedCellConfig(
        cell_id="cell-portable",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential=None,
    )

    assert config.runtime_uid == 0
    assert config.runtime_gid == 0


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

    conflicting = {
        "EXOMEM_HOSTED_CELL_ID": binding.cell_id,
        "EXOMEM_HOSTED_VAULT_ID": binding.vault_id,
        "EXOMEM_VAULT_PATH": str(binding.vault_root),
        "EXOMEM_HOSTED_STATE_ROOT": str(binding.state_root),
        "EXOMEM_LOG_DIR": str(binding.log_root),
        "EXOMEM_HOSTED_RUNTIME_UID": str(binding.runtime_uid),
        "EXOMEM_HOSTED_RUNTIME_GID": str(binding.runtime_gid),
        "EXOMEM_HOSTED_WORKER_POLICY_DIGEST": "a" * 64,
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": "legacy-secret-must-not-coexist-with-v2",
    }
    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(conflicting, require_provisioned=True)
    assert error.value.code == "HOSTED_CONFIG_CONFLICT"


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
            # macOS caps `sun_path` at 104 bytes, and a pytest tmp_path under
            # /private/var/folders/<...> has already spent most of it before
            # this name is appended -- the bind then fails with "AF_UNIX path
            # too long" and the test never reaches its subject. Bind through a
            # relative name from the entry's own directory so the length of the
            # temp root stops counting.
            previous_cwd = Path.cwd()
            os.chdir(root)
            try:
                opened_socket.bind(unsafe.name)
            finally:
                os.chdir(previous_cwd)
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


def test_fresh_provisioning_converges_the_scaffold_it_wrote_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh cell must own every page it was given, not just its vault root.

    Provisioning chowns the staging directory while it is still empty, and
    `init_vault` then writes the whole Knowledge Base as whoever runs
    provisioning. In production that is root, so the scaffold landed root-owned
    at mode 755 inside a root the runtime did own. The cell runs as
    `runtime_uid`: 755 grants it read and traverse but not write, so reads and
    `bootstrap` answered normally while every single write failed, and the
    tenant reported itself healthy having never stored anything.

    Ownership itself is not observable here, because the test's `runtime_uid`
    is the test user and a chown to yourself is a no-op. Convergence is
    observable through the modes it normalizes: directories to owner-only
    `rwx` and files to owner-only `rw`. Any group or other bit left anywhere
    under the vault means the tree was published exactly as `init_vault`
    wrote it.
    """
    monkeypatch.setattr(hosted_runtime.os, "geteuid", lambda: 0)
    binding = _binding(tmp_path)

    result = initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
    )
    assert result.status in {"provisioned", "existing", "migrated"}
    validate_hosted_binding_v2(binding, require_scaffold=True)

    knowledge_base = binding.vault_root / "Knowledge Base"
    assert knowledge_base.is_dir(), "the scaffold must exist to be worth converging"

    shared = {}
    for path in [binding.vault_root, *binding.vault_root.rglob("*")]:
        mode = stat.S_IMODE(path.lstat().st_mode)
        if mode & 0o077:
            shared[str(path.relative_to(binding.vault_root))] = oct(mode)
    assert shared == {}, f"vault entries still readable or worse beyond the owner: {shared}"

    # The three directories every hosted write actually targets. Each one was
    # `drwxr-xr-x root root` on the live alpha tenant.
    for relative in ("Sources", "Evidence", "Notes/Insights"):
        target = knowledge_base / relative
        assert target.is_dir(), f"{relative} missing from the scaffold"
        assert stat.S_IMODE(target.lstat().st_mode) == 0o700, f"{relative} is not owner-only"


def test_resumed_staging_is_converged_before_it_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between `init_vault` and publish must not ship an unowned tree.

    The staging branch that finds an existing directory accepts it as complete
    on the strength of its marker and scaffold, and never wrote the scaffold
    itself. If convergence lived only on the branch that creates the stage,
    this retry would publish exactly the tree that broke the alpha tenant.
    """
    monkeypatch.setattr(hosted_runtime.os, "geteuid", lambda: 0)
    binding = _binding(tmp_path)

    # Build the staging root the way a first attempt does, then stop, standing
    # in for a process that died before it could publish.
    stage = binding.vault_root.parent / (
        f".{binding.vault_root.name}.hosted-v2-stage-"
        f"{hashlib.sha256(binding.cell_id.encode()).hexdigest()[:12]}"
    )
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir(mode=0o700)
    hosted_runtime._write_v2_marker(stage, "vault", binding)
    init.init_vault(stage)
    assert any(
        stat.S_IMODE(path.lstat().st_mode) & 0o077 for path in stage.rglob("*")
    ), "the staged scaffold should start group/other-readable, or this proves nothing"

    initialize_hosted_cell_v2(
        binding,
        expected_release=__version__,
        expected_protocol=HOSTED_PROTOCOL_VERSION,
        active_credential_version="credential-v1",
        bootstrap_security=_bootstrap,
    )

    validate_hosted_binding_v2(binding, require_scaffold=True)
    leaked = {
        str(path.relative_to(binding.vault_root)): oct(stat.S_IMODE(path.lstat().st_mode))
        for path in binding.vault_root.rglob("*")
        if stat.S_IMODE(path.lstat().st_mode) & 0o077
    }
    assert leaked == {}, f"resumed staging published unconverged entries: {leaked}"
