from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import __version__, server_runtime
from exomem import hosted_portability as portability
from exomem import init as init_module
from exomem.hosted_operator import OperatorFailure
from exomem.hosted_restore import (
    HostedRestoreCrash,
    acquire_hosted_lifetime_lock,
    execute_restore_candidate,
    restore_candidate,
)
from exomem.hosted_runtime import (
    HOSTED_PROTOCOL_VERSION,
    HostedBindingV2,
    HostedCellConfig,
    HostedProcessSettings,
    validate_hosted_binding_v2,
)


def _export(tmp_path: Path) -> portability.ExportResult:
    source = tmp_path / "source"
    init_module.init_vault(source)
    note = source / "Knowledge Base/Notes/restore-proof.md"
    note.write_text("# Restore proof\n\ncanonical-sentinel\n", encoding="utf-8")
    return portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=portability.PortabilityContext(
            cell_id="source-cell",
            vault_id="logical-vault",
            operation_id="export-operation",
            created_at="2026-07-14T10:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )


def _request(tmp_path: Path, exported: portability.ExportResult, **overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "operation_id": "restore-operation",
        "artifact_reference": "artifact://private-alpha/restore-1",
        "archive_path": str(exported.archive_path),
        "expected_archive_sha256": exported.archive_sha256,
        "source_cell_id": "source-cell",
        "source_vault_id": "logical-vault",
        "target_cell_id": "target-cell",
        "target_vault_id": "logical-vault",
        "target_vault_root": str(tmp_path / "target-vault"),
        "target_state_root": str(tmp_path / "target-state"),
        "target_log_root": str(tmp_path / "target-log"),
        "expected_release": __version__,
        "expected_protocol": HOSTED_PROTOCOL_VERSION,
        "runtime_uid": os.getuid(),
        "runtime_gid": os.getgid(),
        "active_credential_version": "credential-v1",
        "routing_stopped": True,
        "workload_stopped": True,
    }
    request.update(overrides)
    return request


def _bootstrap(**_kwargs: object) -> int:
    return 1


def _binding(tmp_path: Path, *, runtime_uid: int | None = None) -> HostedBindingV2:
    return HostedBindingV2(
        cell_id="target-cell",
        vault_id="logical-vault",
        vault_root=tmp_path / "target-vault",
        state_root=tmp_path / "target-state",
        log_root=tmp_path / "target-log",
        runtime_uid=os.getuid() if runtime_uid is None else runtime_uid,
        runtime_gid=os.getgid() if runtime_uid is None else runtime_uid,
    )


def _configure_hosted_server(
    monkeypatch: pytest.MonkeyPatch,
    binding: HostedBindingV2,
) -> None:
    values = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": binding.cell_id,
        "EXOMEM_HOSTED_VAULT_ID": binding.vault_id,
        "EXOMEM_VAULT_PATH": str(binding.vault_root),
        "EXOMEM_HOSTED_STATE_ROOT": str(binding.state_root),
        "EXOMEM_LOG_DIR": str(binding.log_root),
        "EXOMEM_HOSTED_RUNTIME_UID": str(binding.runtime_uid),
        "EXOMEM_HOSTED_RUNTIME_GID": str(binding.runtime_gid),
        "EXOMEM_HOSTED_WORKER_POLICY_DIGEST": "a" * 64,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("EXOMEM_HOSTED_SERVICE_CREDENTIAL", raising=False)
    monkeypatch.setattr(
        HostedCellConfig,
        "apply_process_environment",
        lambda _self: HostedProcessSettings(disabled_background_workers=()),
    )
    monkeypatch.setattr(server_runtime, "_initialize_hosted_security", lambda _config: object())
    monkeypatch.setattr(
        server_runtime,
        "probe_hosted_mutation_authority",
        lambda _root: (True, "HOSTED_READY"),
    )
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _root: SimpleNamespace(source_types=()),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _root: "")


def test_restore_candidate_pins_archive_identity_and_publishes_fresh_target_binding(
    tmp_path: Path,
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)

    code, data = execute_restore_candidate(request, bootstrap_security=_bootstrap)

    assert code == "HOSTED_RESTORE_CANDIDATE_READY"
    assert data == {
        "archive_sha256": exported.archive_sha256,
        "artifact_reference_digest": hashlib.sha256(
            b"artifact://private-alpha/restore-1"
        ).hexdigest(),
        "binding_version": 2,
        "credential_revision": 1,
        "credential_version": "credential-v1",
        "derived_error_code": None,
        "derived_state": "ready",
        "exomem_release": __version__,
        "hosted_protocol": HOSTED_PROTOCOL_VERSION,
        "journal_phase": "complete",
        "manifest_sha256": exported.manifest_sha256,
        "source_cell_id": "source-cell",
        "source_vault_id": "logical-vault",
        "status": "ready",
        "target_cell_id": "target-cell",
        "target_vault_id": "logical-vault",
    }
    assert str(tmp_path) not in json.dumps(data)
    binding = HostedBindingV2(
        cell_id="target-cell",
        vault_id="logical-vault",
        vault_root=tmp_path / "target-vault",
        state_root=tmp_path / "target-state",
        log_root=tmp_path / "target-log",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    validate_hosted_binding_v2(binding, require_scaffold=True)
    assert (
        binding.vault_root / "Knowledge Base/Notes/restore-proof.md"
    ).read_text(encoding="utf-8").endswith("canonical-sentinel\n")
    assert not (binding.vault_root / "hosted-security.sqlite").exists()


def test_restore_candidate_converges_all_canonical_modes_to_private(
    tmp_path: Path,
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)

    restore_candidate(request, bootstrap_security=_bootstrap)

    binding = _binding(tmp_path)
    for current, directory_names, file_names in os.walk(binding.vault_root):
        current_path = Path(current)
        assert stat.S_IMODE(current_path.lstat().st_mode) == 0o700
        for name in directory_names:
            assert stat.S_IMODE((current_path / name).lstat().st_mode) == 0o700
        for name in file_names:
            assert stat.S_IMODE((current_path / name).lstat().st_mode) == 0o600


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to change runtime ownership")
def test_root_restore_converges_verified_tree_to_nonroot_runtime_identity(
    tmp_path: Path,
) -> None:
    exported = _export(tmp_path)
    runtime_uid = 10_001
    request = _request(
        tmp_path,
        exported,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_uid,
    )

    restore_candidate(request, bootstrap_security=_bootstrap)

    binding = _binding(tmp_path, runtime_uid=runtime_uid)
    for current, directory_names, file_names in os.walk(binding.vault_root):
        current_path = Path(current)
        current_stat = current_path.lstat()
        assert (current_stat.st_uid, current_stat.st_gid) == (runtime_uid, runtime_uid)
        assert stat.S_IMODE(current_stat.st_mode) == 0o700
        for name in (*directory_names, *file_names):
            child = (current_path / name).lstat()
            assert (child.st_uid, child.st_gid) == (runtime_uid, runtime_uid)
            assert stat.S_IMODE(child.st_mode) == (
                0o700 if stat.S_ISDIR(child.st_mode) else 0o600
            )


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"expected_archive_sha256": "0" * 64}, "HOSTED_ARTIFACT_DIGEST_MISMATCH"),
        ({"source_cell_id": "wrong-source"}, "HOSTED_ARCHIVE_INTEGRITY_FAILURE"),
        ({"target_cell_id": "source-cell"}, "HOSTED_RESTORE_IDENTITY_CONFLICT"),
        ({"target_vault_id": "another-vault"}, "HOSTED_RESTORE_IDENTITY_CONFLICT"),
        ({"routing_stopped": False}, "HOSTED_RESTORE_NOT_OFFLINE"),
    ],
)
def test_restore_candidate_rejects_unpinned_or_online_inputs_before_publication(
    tmp_path: Path, override: dict[str, object], code: str
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported, **override)

    with pytest.raises(OperatorFailure) as error:
        restore_candidate(request, bootstrap_security=_bootstrap)

    assert error.value.code == code
    assert not (tmp_path / "target-vault").exists()


def test_restore_candidate_requires_empty_target_and_exclusive_lifetime_lock(
    tmp_path: Path,
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)
    target = tmp_path / "target-vault"
    target.mkdir()
    sentinel = target / "foreign.md"
    sentinel.write_text("do not overlay", encoding="utf-8")

    with pytest.raises(OperatorFailure) as conflict:
        restore_candidate(request, bootstrap_security=_bootstrap)
    assert conflict.value.code == "HOSTED_RESTORE_TARGET_CONFLICT"
    assert sentinel.read_text(encoding="utf-8") == "do not overlay"

    sentinel.unlink()
    target.rmdir()
    state = tmp_path / "target-state"
    with acquire_hosted_lifetime_lock(state):
        with pytest.raises(OperatorFailure) as busy:
            restore_candidate(request, bootstrap_security=_bootstrap)
    assert busy.value.code == "HOSTED_RESTORE_BUSY"
    assert not target.exists()


def test_restore_candidate_never_deletes_unproven_staging_ownership(tmp_path: Path) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)
    operation_key = hashlib.sha256(b"restore-operation").hexdigest()[:16]
    staging = tmp_path / f".target-vault.restore-{operation_key}"
    staging.mkdir()
    sentinel = staging / "foreign.md"
    sentinel.write_text("foreign staging", encoding="utf-8")

    with pytest.raises(OperatorFailure) as error:
        restore_candidate(request, bootstrap_security=_bootstrap)

    assert error.value.code == "HOSTED_RESTORE_TARGET_CONFLICT"
    assert sentinel.read_text(encoding="utf-8") == "foreign staging"


def test_lifetime_lock_rejects_a_symlink_leaf_without_touching_target(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("do not open", encoding="utf-8")
    lock = state / ".exomem-hosted-lifetime.lock"
    try:
        lock.symlink_to(victim)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(OperatorFailure) as error:
        with acquire_hosted_lifetime_lock(state):
            pass

    assert error.value.code == "HOSTED_RESTORE_BUSY"
    assert victim.read_text(encoding="utf-8") == "do not open"


def test_restore_owned_lifetime_lock_excludes_hosted_server_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)
    restore_candidate(request, bootstrap_security=_bootstrap)
    binding = _binding(tmp_path)
    _configure_hosted_server(monkeypatch, binding)

    with acquire_hosted_lifetime_lock(binding.state_root, binding=binding):
        with pytest.raises(OperatorFailure) as error:
            server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)

    assert error.value.code == "HOSTED_RESTORE_BUSY"


def test_server_lifetime_lock_excludes_restore_and_wraps_temp_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)
    restore_candidate(request, bootstrap_security=_bootstrap)
    binding = _binding(tmp_path)
    _configure_hosted_server(monkeypatch, binding)
    cleanup_observations: list[str] = []

    def cleanup_while_locked(state_root: Path) -> None:
        with pytest.raises(OperatorFailure) as error:
            with acquire_hosted_lifetime_lock(state_root, binding=binding):
                pass
        cleanup_observations.append(error.value.code)

    monkeypatch.setattr(
        server_runtime,
        "_cleanup_hosted_transfer_temp",
        cleanup_while_locked,
        raising=False,
    )

    runtime = server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)
    try:
        assert cleanup_observations == ["HOSTED_RESTORE_BUSY"]
        with pytest.raises(OperatorFailure) as error:
            restore_candidate(request, bootstrap_security=_bootstrap)
        assert error.value.code == "HOSTED_RESTORE_BUSY"
    finally:
        lifetime_lock = getattr(runtime, "hosted_lifetime_lock", None)
        if lifetime_lock is not None:
            lifetime_lock.__exit__(None, None, None)


@pytest.mark.parametrize(
    "crash_event",
    [
        "state_bound",
        "log_bound",
        "roots_bound",
        "journal:roots_bound",
        "archive_prepared",
        "journal:archive_prepared",
        "canonical_renamed",
        "journal:canonical_published",
        "derived_rebuilt",
        "journal:derived_ready",
        "journal:complete",
        "proof_written",
    ],
)
def test_restore_candidate_resumes_exact_request_at_every_durable_boundary(
    tmp_path: Path, crash_event: str
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)
    crashed = False

    def crash_once(event: str) -> None:
        nonlocal crashed
        if not crashed and event == crash_event:
            crashed = True
            raise HostedRestoreCrash(event)

    with pytest.raises(HostedRestoreCrash):
        restore_candidate(
            request,
            bootstrap_security=_bootstrap,
            crash_hook=crash_once,
        )

    result = restore_candidate(request, bootstrap_security=_bootstrap)

    assert result.status == "ready"
    canonical = tmp_path / "target-vault/Knowledge Base/Notes/restore-proof.md"
    assert canonical.read_text(encoding="utf-8").endswith("canonical-sentinel\n")
    staging = list(tmp_path.glob(".target-vault.restore-*"))
    assert staging == []


def test_restore_candidate_conflicts_changed_request_and_degrades_only_derived_state(
    tmp_path: Path,
) -> None:
    exported = _export(tmp_path)
    request = _request(tmp_path, exported)

    def stop_after_roots(event: str) -> None:
        if event == "journal:roots_bound":
            raise HostedRestoreCrash(event)

    with pytest.raises(HostedRestoreCrash):
        restore_candidate(
            request,
            bootstrap_security=_bootstrap,
            crash_hook=stop_after_roots,
        )

    changed = dict(request)
    changed["artifact_reference"] = "artifact://private-alpha/changed"
    with pytest.raises(OperatorFailure) as conflict:
        restore_candidate(changed, bootstrap_security=_bootstrap)
    assert conflict.value.code == "HOSTED_RESTORE_JOURNAL_CONFLICT"

    def rebuild_fails(_root: Path) -> None:
        raise RuntimeError("derived backend unavailable")

    result = restore_candidate(
        request,
        bootstrap_security=_bootstrap,
        rebuild_derived=rebuild_fails,
    )
    assert result.status == "degraded"
    assert result.derived_state == "degraded"
    assert result.derived_error_code == "DERIVED_REBUILD_FAILED"
    assert (
        tmp_path / "target-vault/Knowledge Base/Notes/restore-proof.md"
    ).read_text(encoding="utf-8").endswith("canonical-sentinel\n")


def test_restore_candidate_rejects_manifested_runtime_state(tmp_path: Path) -> None:
    exported = _export(tmp_path)
    malicious = tmp_path / "runtime-state.zip"
    with zipfile.ZipFile(exported.archive_path, "r") as source:
        manifest = json.loads(source.read(portability.MANIFEST_NAME))
        payloads = {
            info.filename: source.read(info)
            for info in source.infolist()
            if info.filename != portability.MANIFEST_NAME
        }
    runtime_body = b'{"binding_version":2}'
    runtime_path = ".exomem-hosted-cell.json"
    payloads[runtime_path] = runtime_body
    manifest["files"].append(
        {
            "path": runtime_path,
            "size": len(runtime_body),
            "sha256": hashlib.sha256(runtime_body).hexdigest(),
            "classification": "canonical",
        }
    )
    manifest["files"].sort(key=lambda item: item["path"])
    manifest.pop("overall_digest")
    manifest["overall_digest"] = {
        "algorithm": "sha256",
        "value": portability._manifest_digest(manifest),
    }
    with zipfile.ZipFile(malicious, "w", compression=zipfile.ZIP_STORED) as archive:
        manifest_info = zipfile.ZipInfo(portability.MANIFEST_NAME)
        manifest_info.create_system = 3
        manifest_info.external_attr = (stat.S_IFREG | 0o600) << 16
        archive.writestr(
            manifest_info,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        for name, body in sorted(payloads.items()):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, body)
    request = _request(
        tmp_path,
        exported,
        archive_path=str(malicious),
        expected_archive_sha256=hashlib.sha256(malicious.read_bytes()).hexdigest(),
    )

    with pytest.raises(OperatorFailure) as error:
        restore_candidate(request, bootstrap_security=_bootstrap)

    assert error.value.code == "HOSTED_ARCHIVE_RUNTIME_STATE"
    assert not (tmp_path / "target-vault").exists()
