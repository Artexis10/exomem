from __future__ import annotations

import dataclasses
import os
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import hosted_runtime, server_runtime, vault
from exomem.hosted_runtime import (
    HostedCellConfig,
    HostedCellLifecycle,
    HostedConfigError,
    HostedLifecycleError,
    provision_hosted_cell,
)

SECRET = "hosted-service-credential-sentinel"
_HOSTED_PROCESS_ENV_KEYS = (
    "EXOMEM_DIARIZE",
    "EXOMEM_DISABLE_CLIP",
    "EXOMEM_DISABLE_EMBEDDINGS",
    "EXOMEM_DISABLE_FILE_WATCHER",
    "EXOMEM_DISABLE_MEDIA_EXTRACTION",
    "EXOMEM_DISABLE_QUERY_LOG",
    "EXOMEM_DISABLE_RELEVANCE_CHECK",
    "EXOMEM_DISABLE_USAGE_BOOST",
    "EXOMEM_BASE_URL",
    "EXOMEM_CF_ACCESS_AUD",
    "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
    "EXOMEM_GITHUB_USERNAME",
    "EXOMEM_HOSTED_STATE_ROOT",
    "EXOMEM_LARGE_UPLOAD_BASE_URL",
    "EXOMEM_LOG_DIR",
    "EXOMEM_REST_API_KEY",
    "EXOMEM_UPLOAD_TOKEN",
    "EXOMEM_UPLOAD_MAX_BYTES",
    "EXOMEM_VAULT_PATH",
    "EXOMEM_VISION_CAPTION",
    "EXOMEM_WRITER_LEASE_STATE_DIR",
    "EXOMEM_WRITER_LEASE_PREFERRED",
    "EXOMEM_WRITER_LEASE_REPLICA_ID",
    "EXOMEM_WRITER_LEASE_TIMEOUT",
    "EXOMEM_WRITER_LEASE_TOKEN",
    "EXOMEM_WRITER_LEASE_TTL",
    "EXOMEM_WRITER_LEASE_URL",
    "EXOMEM_WRITER_LEASE_VAULT_ID",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def _restore_hosted_process_environment() -> Iterator[None]:
    environment = hosted_runtime.os.environ
    original = {key: environment[key] for key in _HOSTED_PROCESS_ENV_KEYS if key in environment}
    missing = set(_HOSTED_PROCESS_ENV_KEYS) - original.keys()
    try:
        yield
    finally:
        for key in missing:
            environment.pop(key, None)
        environment.update(original)


def _env(
    tmp_path: Path,
    *,
    cell_id: str = "cell-alpha",
    vault_root: Path | None = None,
    state_root: Path | None = None,
    log_root: Path | None = None,
    grants: str = "",
) -> dict[str, str]:
    return {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": cell_id,
        "EXOMEM_VAULT_PATH": str(vault_root or tmp_path / "vault"),
        "EXOMEM_HOSTED_STATE_ROOT": str(state_root or tmp_path / "state"),
        "EXOMEM_LOG_DIR": str(log_root or tmp_path / "logs"),
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": SECRET,
        "EXOMEM_HOSTED_FEATURE_GRANTS": grants,
    }


def _provisioned(
    tmp_path: Path, *, cell_id: str = "cell-alpha", grants: str = ""
) -> tuple[dict[str, str], HostedCellConfig]:
    values = _env(tmp_path, cell_id=cell_id, grants=grants)
    config = HostedCellConfig.from_env(values, require_provisioned=False)
    provision_hosted_cell(config)
    return values, HostedCellConfig.from_env(values, require_provisioned=True)


def test_hosted_mode_is_explicit_and_local_mode_remains_ordinary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert hosted_runtime.hosted_mode_enabled({}) is False
    assert hosted_runtime.hosted_mode_enabled({"EXOMEM_HOSTED_CELL": "0"}) is False
    assert hosted_runtime.hosted_mode_enabled({"EXOMEM_HOSTED_CELL": "1"}) is True
    with pytest.raises(HostedConfigError) as mode_error:
        hosted_runtime.hosted_mode_enabled({"EXOMEM_HOSTED_CELL": "sometimes"})
    assert mode_error.value.code == "HOSTED_MODE_INVALID"

    from exomem.init import init_vault

    init_vault(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        server_runtime, "_start_compute_runtime", lambda _vault: calls.append("compute")
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_media_worker",
        lambda _vault: calls.append("media") or object(),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda _vault: calls.append("watcher") or object(),
    )

    dotenv_calls: list[dict] = []
    runtime = server_runtime.initialize_runtime(
        load_dotenv_func=lambda **kwargs: dotenv_calls.append(kwargs)
    )

    assert dotenv_calls == [{"override": True}]
    assert calls == ["compute", "media", "watcher"]
    assert runtime.vault_root == tmp_path
    assert runtime.hosted_config is None
    assert runtime.hosted_lifecycle is None


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda values, _tmp: values.pop("EXOMEM_HOSTED_CELL_ID"), "HOSTED_CONFIG_MISSING"),
        (
            lambda values, _tmp: values.__setitem__("EXOMEM_VAULT_PATH", "relative/vault"),
            "HOSTED_ROOT_NOT_ABSOLUTE",
        ),
        (
            lambda values, _tmp: values.__setitem__(
                "EXOMEM_HOSTED_STATE_ROOT", values["EXOMEM_VAULT_PATH"]
            ),
            "HOSTED_ROOT_OVERLAP",
        ),
        (
            lambda values, _tmp: values.__setitem__(
                "EXOMEM_LOG_DIR", str(Path(values["EXOMEM_HOSTED_STATE_ROOT"]) / "logs")
            ),
            "HOSTED_ROOT_OVERLAP",
        ),
    ],
)
def test_hosted_config_rejects_missing_relative_and_overlapping_roots(
    tmp_path: Path, mutate, code: str
) -> None:
    values = _env(tmp_path)
    mutate(values, tmp_path)

    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(values, require_provisioned=False)

    assert error.value.code == code
    assert SECRET not in str(error.value)


def test_hosted_config_rejects_symlinked_roots(tmp_path: Path) -> None:
    real = tmp_path / "real-vault"
    real.mkdir()
    link = tmp_path / "vault-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(_env(tmp_path, vault_root=link), require_provisioned=False)

    assert error.value.code == "HOSTED_ROOT_SYMLINK"


def test_hosted_runtime_requires_existing_owned_roots(tmp_path: Path) -> None:
    values = _env(tmp_path)
    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(values, require_provisioned=True)
    assert error.value.code == "HOSTED_ROOT_MISSING"


def test_cell_binding_is_immutable_and_shared_roots_reject_another_cell(
    tmp_path: Path,
) -> None:
    values, config = _provisioned(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.cell_id = "cell-mutated"  # type: ignore[misc]

    other = dict(values)
    other["EXOMEM_HOSTED_CELL_ID"] = "cell-bravo"
    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(other, require_provisioned=True)
    assert error.value.code == "HOSTED_BINDING_MISMATCH"


def test_feature_grants_limits_and_privacy_defaults_are_deterministic(
    tmp_path: Path,
) -> None:
    values = _env(
        tmp_path,
        grants=" vision, embeddings, media, EMBEDDINGS ",
    )
    values.update(
        {
            "EXOMEM_HOSTED_STORAGE_LIMIT_BYTES": "2048",
            "EXOMEM_HOSTED_UPLOAD_LIMIT_BYTES": "1024",
            "EXOMEM_HOSTED_WORKER_LIMIT": "2",
        }
    )
    config = HostedCellConfig.from_env(values, require_provisioned=False)

    assert config.feature_grants == ("embeddings", "media", "vision")
    assert config.resource_limits.storage_bytes == 2048
    assert config.resource_limits.upload_bytes == 1024
    assert config.resource_limits.worker_count == 2
    assert config.protocol_version == hosted_runtime.HOSTED_PROTOCOL_VERSION

    process_env = {
        "EXOMEM_BASE_URL": "https://local-transfer.invalid",
        "EXOMEM_CF_ACCESS_AUD": "cf-audience-sentinel",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.invalid",
        "EXOMEM_DISABLE_EMBEDDINGS": "ambient",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "ambient",
        "EXOMEM_DISABLE_CLIP": "ambient",
        "EXOMEM_GITHUB_USERNAME": "github-user-sentinel",
        "EXOMEM_LARGE_UPLOAD_BASE_URL": "https://large-transfer.invalid",
        "EXOMEM_REST_API_KEY": "rest-key-sentinel",
        "EXOMEM_UPLOAD_TOKEN": "local-transfer-token-sentinel",
        "EXOMEM_WRITER_LEASE_PREFERRED": "1",
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "foreign-replica-sentinel",
        "EXOMEM_WRITER_LEASE_TIMEOUT": "99",
        "EXOMEM_WRITER_LEASE_TOKEN": "foreign-writer-token-sentinel",
        "EXOMEM_WRITER_LEASE_TTL": "99",
        "EXOMEM_WRITER_LEASE_URL": "https://foreign-coordinator.invalid",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "foreign-vault-sentinel",
        "GITHUB_CLIENT_ID": "github-client-id-sentinel",
        "GITHUB_CLIENT_SECRET": "github-client-secret-sentinel",
    }
    applied = config.apply_process_environment(process_env)
    assert process_env["EXOMEM_DISABLE_QUERY_LOG"] == "1"
    assert process_env["EXOMEM_DISABLE_USAGE_BOOST"] == "1"
    assert process_env["EXOMEM_DISABLE_FILE_WATCHER"] == "1"
    assert "EXOMEM_DISABLE_EMBEDDINGS" not in process_env
    assert "EXOMEM_DISABLE_MEDIA_EXTRACTION" not in process_env
    assert "EXOMEM_DISABLE_CLIP" not in process_env
    for personal_ingress_setting in (
        "EXOMEM_BASE_URL",
        "EXOMEM_CF_ACCESS_AUD",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
        "EXOMEM_GITHUB_USERNAME",
        "EXOMEM_LARGE_UPLOAD_BASE_URL",
        "EXOMEM_REST_API_KEY",
        "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_WRITER_LEASE_PREFERRED",
        "EXOMEM_WRITER_LEASE_REPLICA_ID",
        "EXOMEM_WRITER_LEASE_TIMEOUT",
        "EXOMEM_WRITER_LEASE_TOKEN",
        "EXOMEM_WRITER_LEASE_TTL",
        "EXOMEM_WRITER_LEASE_URL",
        "EXOMEM_WRITER_LEASE_VAULT_ID",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
    ):
        assert personal_ingress_setting not in process_env
    assert applied.disabled_background_workers == ("file-watcher",)

    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(
            _env(tmp_path, grants="embeddings,reasoning-model"),
            require_provisioned=False,
        )
    assert error.value.code == "HOSTED_FEATURE_UNKNOWN"


def test_hosted_config_rejects_protocol_versions_not_implemented_by_this_release(
    tmp_path: Path,
) -> None:
    values = _env(tmp_path)
    values["EXOMEM_HOSTED_PROTOCOL_VERSION"] = "999"

    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(values, require_provisioned=False)

    assert error.value.code == "HOSTED_PROTOCOL_UNSUPPORTED"
    assert "999" not in str(error.value)


def test_private_service_credential_is_never_represented_or_echoed(
    tmp_path: Path,
) -> None:
    config = HostedCellConfig.from_env(_env(tmp_path), require_provisioned=False)
    assert SECRET not in repr(config)
    assert SECRET not in str(config)
    assert config.matches_service_credential(SECRET)
    assert not config.matches_service_credential("wrong")

    missing = _env(tmp_path)
    missing["EXOMEM_HOSTED_SERVICE_CREDENTIAL"] = ""
    with pytest.raises(HostedConfigError) as error:
        HostedCellConfig.from_env(missing, require_provisioned=False)
    assert error.value.code == "HOSTED_CONFIG_MISSING"
    assert SECRET not in repr(error.value)

    weak = _env(tmp_path)
    weak["EXOMEM_HOSTED_SERVICE_CREDENTIAL"] = "weak-secret"
    with pytest.raises(HostedConfigError) as weak_error:
        HostedCellConfig.from_env(weak)
    assert weak_error.value.code == "HOSTED_CREDENTIAL_WEAK"
    assert "weak-secret" not in str(weak_error.value)


def test_hosted_initialize_skips_dotenv_and_starts_no_ungranted_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, _config = _provisioned(tmp_path)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in (
        "EXOMEM_DISABLE_QUERY_LOG",
        "EXOMEM_DISABLE_USAGE_BOOST",
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        "EXOMEM_UPLOAD_MAX_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda _vault: pytest.fail("ungranted compute runtime started"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_media_worker",
        lambda _vault: pytest.fail("ungranted media worker started"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda _vault: pytest.fail("ungranted file watcher started"),
    )

    runtime = server_runtime.initialize_runtime(
        load_dotenv_func=lambda **_kwargs: pytest.fail("hosted startup loaded dotenv")
    )

    assert runtime.hosted_config is not None
    assert runtime.hosted_config.cell_id == "cell-alpha"
    assert runtime.hosted_lifecycle is not None
    readiness = runtime.hosted_lifecycle.readiness().as_dict()
    assert readiness["ready"] is True
    assert readiness["write_admitted"] is True
    assert readiness["reason_code"] == "HOSTED_READY"
    assert runtime.media_worker is None
    assert runtime.file_watcher is None
    assert SECRET not in repr(runtime)
    assert "EXOMEM_DISABLE_QUERY_LOG" in hosted_runtime.os.environ


def test_hosted_initialize_probes_the_late_bound_shared_mutation_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, _config = _provisioned(tmp_path)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    events: list[str] = []

    @contextmanager
    def mutation_guard(vault_root: Path):
        events.append(f"enter:{vault_root.name}")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(hosted_runtime, "hosted_mutation_guard", mutation_guard)

    runtime = server_runtime.initialize_runtime(
        load_dotenv_func=lambda **_kwargs: pytest.fail("hosted startup loaded dotenv")
    )

    assert events == ["enter:vault", "exit"]
    assert runtime.hosted_lifecycle is not None
    assert runtime.hosted_lifecycle.readiness().ready is True
    assert runtime.hosted_lifecycle.readiness().write_admitted is True


def test_hosted_plaintext_roots_reject_all_group_or_world_permission_bits(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits are unavailable on Windows")
    values, config = _provisioned(tmp_path)
    config.vault_root.chmod(0o740)
    try:
        with pytest.raises(HostedConfigError) as error:
            HostedCellConfig.from_env(values, require_provisioned=True)
    finally:
        config.vault_root.chmod(0o700)

    assert error.value.code == "HOSTED_ROOT_PERMISSIONS"
    assert str(config.vault_root) not in str(error.value)


def test_hosted_initialize_starts_only_explicitly_granted_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, _config = _provisioned(tmp_path, grants="embeddings,media,file-watcher")
    values["EXOMEM_HOSTED_WORKER_LIMIT"] = "2"
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    calls: list[str] = []

    class Worker:
        def __init__(self, name: str) -> None:
            self.name = name

        def start(self) -> None:
            calls.append(f"restart:{self.name}")

        def stop(self) -> None:
            calls.append(f"stop:{self.name}")

    monkeypatch.setattr(
        server_runtime, "_start_compute_runtime", lambda _vault: calls.append("compute")
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_media_worker",
        lambda _vault: calls.append("media") or Worker("media"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda _vault: calls.append("watcher") or Worker("watcher"),
    )
    monkeypatch.setattr(
        server_runtime,
        "probe_hosted_mutation_authority",
        lambda _vault: (True, "HOSTED_READY"),
    )

    runtime = server_runtime.initialize_runtime(
        load_dotenv_func=lambda **_kwargs: pytest.fail("hosted startup loaded dotenv")
    )

    assert calls == ["compute", "media", "watcher"]
    assert runtime.hosted_lifecycle is not None
    runtime.hosted_lifecycle.set_mutation_authority(True)
    runtime.hosted_lifecycle.quiesce(timeout=1)
    assert calls[-2:] == ["stop:media", "stop:watcher"]
    runtime.hosted_lifecycle.resume()
    assert calls[-2:] == ["restart:media", "restart:watcher"]
    runtime.hosted_lifecycle.resume()
    assert calls.count("restart:media") == 1
    assert calls.count("restart:watcher") == 1


def test_zero_worker_limit_keeps_granted_background_features_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values, _config = _provisioned(tmp_path, grants="embeddings,media,file-watcher")
    values["EXOMEM_HOSTED_WORKER_LIMIT"] = "0"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        server_runtime,
        "probe_hosted_mutation_authority",
        lambda _vault_root: (True, "HOSTED_READY"),
    )

    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda _vault: pytest.fail("zero-limit compute worker started"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_media_worker",
        lambda _vault: pytest.fail("zero-limit media worker started"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_file_watcher",
        lambda _vault: pytest.fail("zero-limit file watcher started"),
    )

    runtime = server_runtime.initialize_runtime(
        load_dotenv_func=lambda **_kwargs: pytest.fail("hosted startup loaded dotenv")
    )

    assert runtime.hosted_lifecycle is not None
    degraded = runtime.hosted_lifecycle.readiness().as_dict()["degraded"]
    assert degraded == [
        {"check": "worker:embeddings", "reason_code": "HOSTED_WORKER_LIMIT_ZERO"},
        {"check": "worker:file-watcher", "reason_code": "HOSTED_WORKER_LIMIT_ZERO"},
        {"check": "worker:media", "reason_code": "HOSTED_WORKER_LIMIT_ZERO"},
    ]
    assert hosted_runtime.os.environ["EXOMEM_DISABLE_EMBEDDINGS"] == "1"
    assert hosted_runtime.os.environ["EXOMEM_DISABLE_MEDIA_EXTRACTION"] == "1"
    assert hosted_runtime.os.environ["EXOMEM_DISABLE_FILE_WATCHER"] == "1"


def test_lifecycle_reports_content_free_readiness_and_degradation(
    tmp_path: Path,
) -> None:
    _values, config = _provisioned(tmp_path, grants="media")
    lifecycle = HostedCellLifecycle(config)

    assert lifecycle.liveness().as_dict() == {
        "live": True,
        "cell_id": "cell-alpha",
        "protocol_version": hosted_runtime.HOSTED_PROTOCOL_VERSION,
    }
    assert lifecycle.readiness().reason_code == "HOSTED_STARTING"

    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    lifecycle.set_worker_status("media", ready=False, reason_code="HOSTED_WORKER_UNAVAILABLE")
    readiness = lifecycle.readiness().as_dict()
    assert readiness["ready"] is True
    assert readiness["reason_code"] == "HOSTED_READY"
    assert readiness["degraded"] == [
        {"check": "worker:media", "reason_code": "HOSTED_WORKER_UNAVAILABLE"}
    ]
    assert readiness["read_admitted"] is True
    assert readiness["write_admitted"] is True
    assert SECRET not in repr(readiness)
    assert str(config.vault_root) not in repr(readiness)

    lifecycle.set_mutation_authority(False, reason_code="HOSTED_MUTATION_LOCK_UNAVAILABLE")
    readiness = lifecycle.readiness()
    assert readiness.ready is False
    assert readiness.reason_code == "HOSTED_MUTATION_LOCK_UNAVAILABLE"


def test_deletion_sealing_waits_for_an_admitted_download(tmp_path: Path) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )

    with lifecycle.admit_transfer():
        quiesced = lifecycle.quiesce(timeout=1)
        assert quiesced.active_transfers == 1
        with pytest.raises(HostedLifecycleError) as error:
            lifecycle.seal_for_deletion()
        assert error.value.code == "HOSTED_TRANSFER_IN_FLIGHT"

    sealed = lifecycle.seal_for_deletion()
    assert sealed.phase == "sealed"
    assert sealed.active_transfers == 0


@pytest.mark.parametrize(
    ("vault_ready", "service_auth_ready", "reason_code"),
    [
        (False, True, "HOSTED_VAULT_UNAVAILABLE"),
        (True, False, "HOSTED_SERVICE_AUTH_UNAVAILABLE"),
    ],
)
def test_read_admission_requires_vault_and_service_auth_health(
    tmp_path: Path,
    vault_ready: bool,
    service_auth_ready: bool,
    reason_code: str,
) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=vault_ready,
        mutation_authority_ready=True,
        service_auth_ready=service_auth_ready,
    )

    readiness = lifecycle.readiness()
    assert readiness.ready is False
    assert readiness.reason_code == reason_code
    assert readiness.read_admitted is False
    with pytest.raises(HostedLifecycleError) as error:
        lifecycle.require_read_admission()
    assert error.value.code == "HOSTED_READ_NOT_ADMITTED"


def test_starting_cell_cannot_resume_into_active_state(tmp_path: Path) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = HostedCellLifecycle(config)

    with pytest.raises(HostedLifecycleError) as error:
        lifecycle.resume()

    assert error.value.code == "HOSTED_STARTUP_INCOMPLETE"
    assert lifecycle.readiness().reason_code == "HOSTED_STARTING"


def test_quiesce_drains_and_rejects_new_mutations_then_resume_is_idempotent(
    tmp_path: Path,
) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )

    entered = threading.Event()
    release = threading.Event()

    def admitted_write() -> None:
        with lifecycle.admit_mutation():
            entered.set()
            assert release.wait(2)

    writer = threading.Thread(target=admitted_write)
    writer.start()
    assert entered.wait(1)

    result: list[object] = []
    quiescer = threading.Thread(target=lambda: result.append(lifecycle.quiesce(timeout=2)))
    quiescer.start()
    deadline = time.monotonic() + 1
    while lifecycle.readiness().reason_code != "HOSTED_QUIESCING":
        assert time.monotonic() < deadline
        time.sleep(0.005)

    with pytest.raises(HostedLifecycleError) as error:
        with lifecycle.admit_mutation():
            pass
    assert error.value.code == "HOSTED_MUTATION_NOT_ADMITTED"

    release.set()
    writer.join(1)
    quiescer.join(1)
    assert result and result[0].phase == "quiesced"
    assert lifecycle.readiness().reason_code == "HOSTED_QUIESCED"
    assert lifecycle.readiness().read_admitted is True
    assert lifecycle.readiness().write_admitted is False
    with pytest.raises(HostedLifecycleError) as quiesced_error:
        with lifecycle.admit_mutation():
            pass
    assert quiesced_error.value.code == "HOSTED_MUTATION_NOT_ADMITTED"

    first = lifecycle.resume()
    second = lifecycle.resume()
    assert first.phase == second.phase == "active"
    assert lifecycle.readiness().ready is True


def test_deletion_seal_is_idempotent_and_rejects_reads_and_writes(
    tmp_path: Path,
) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    lifecycle.quiesce(timeout=1)

    first = lifecycle.seal_for_deletion()
    second = lifecycle.seal_for_deletion()
    assert first.phase == second.phase == "sealed"
    readiness = lifecycle.readiness()
    assert readiness.ready is False
    assert readiness.reason_code == "HOSTED_DELETION_SEALED"
    assert readiness.read_admitted is False
    assert readiness.write_admitted is False

    with pytest.raises(HostedLifecycleError) as read_error:
        lifecycle.require_read_admission()
    assert read_error.value.code == "HOSTED_READ_NOT_ADMITTED"
    with pytest.raises(HostedLifecycleError) as write_error:
        with lifecycle.admit_mutation():
            pass
    assert write_error.value.code == "HOSTED_MUTATION_NOT_ADMITTED"
    with pytest.raises(HostedLifecycleError) as resume_error:
        lifecycle.resume()
    assert resume_error.value.code == "HOSTED_DELETION_SEALED"


def test_quiesced_and_sealed_admission_survive_process_restart(tmp_path: Path) -> None:
    _values, config = _provisioned(tmp_path)
    first = HostedCellLifecycle(config)
    first.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    first.quiesce(timeout=1)

    restarted_quiesced = HostedCellLifecycle(config)
    snapshot = restarted_quiesced.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    assert snapshot.phase == "quiesced"
    assert restarted_quiesced.readiness().read_admitted is True
    assert restarted_quiesced.readiness().write_admitted is False

    restarted_quiesced.resume()
    restarted_quiesced.quiesce(timeout=1)
    restarted_quiesced.seal_for_deletion()

    restarted_sealed = HostedCellLifecycle(config)
    sealed = restarted_sealed.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    assert sealed.phase == "sealed"
    assert restarted_sealed.readiness().read_admitted is False
    assert restarted_sealed.readiness().write_admitted is False
    with pytest.raises(HostedLifecycleError) as error:
        restarted_sealed.resume()
    assert error.value.code == "HOSTED_DELETION_SEALED"


def test_provisioning_is_idempotent_machine_readable_and_non_destructive(
    tmp_path: Path,
) -> None:
    values = _env(tmp_path)
    config = HostedCellConfig.from_env(values, require_provisioned=False)

    first = provision_hosted_cell(config)
    log_text = (config.vault_root / "Knowledge Base" / "log.md").read_bytes()
    second = provision_hosted_cell(config)

    assert first.status == "provisioned"
    assert second.status == "existing"
    assert (config.vault_root / "Knowledge Base" / "_Schema" / "SKILL.md").is_file()
    assert (config.vault_root / "Knowledge Base" / "Sources" / "index.md").is_file()
    assert (config.vault_root / "Knowledge Base" / "Notes" / "index.md").is_file()
    assert (config.vault_root / "Knowledge Base" / "Entities" / "index.md").is_file()
    assert (config.vault_root / "Knowledge Base" / "log.md").read_bytes() == log_text
    assert first.as_dict()["cell_id"] == "cell-alpha"
    assert first.as_dict()["capabilities"] == []
    assert SECRET not in repr(first.as_dict())
    assert str(config.vault_root) not in repr(first.as_dict())

    conflict_root = tmp_path / "conflict" / "vault"
    conflict_root.mkdir(parents=True)
    sentinel = conflict_root / "do-not-touch.txt"
    sentinel.write_text("owned data", encoding="utf-8")
    conflict_values = _env(
        tmp_path / "conflict",
        cell_id="cell-conflict",
        vault_root=conflict_root,
    )
    conflict = HostedCellConfig.from_env(conflict_values, require_provisioned=False)
    with pytest.raises(HostedConfigError) as error:
        provision_hosted_cell(conflict)
    assert error.value.code == "HOSTED_PROVISIONING_CONFLICT"
    assert sentinel.read_text(encoding="utf-8") == "owned data"
    assert not conflict.state_root.exists()
    assert not conflict.log_root.exists()


def test_provisioning_converges_after_interrupted_staged_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = HostedCellConfig.from_env(_env(tmp_path), require_provisioned=False)
    real_promote = hosted_runtime._promote_staged_vault
    attempts = 0

    def fail_once(stage: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected publication crash")
        real_promote(stage, destination)

    monkeypatch.setattr(hosted_runtime, "_promote_staged_vault", fail_once)
    with pytest.raises(OSError, match="publication crash"):
        provision_hosted_cell(config)

    result = provision_hosted_cell(config)
    assert result.status == "provisioned"
    assert (
        HostedCellConfig.from_env(_env(tmp_path), require_provisioned=True).cell_id == "cell-alpha"
    )


def test_provisioning_recreates_missing_runtime_root_without_touching_vault(
    tmp_path: Path,
) -> None:
    values = _env(tmp_path)
    config = HostedCellConfig.from_env(values)
    provision_hosted_cell(config)
    canonical = config.vault_root / "Knowledge Base" / "index.md"
    before = canonical.read_bytes()
    shutil.rmtree(config.state_root)

    result = provision_hosted_cell(config)

    assert result.status == "existing"
    assert canonical.read_bytes() == before
    HostedCellConfig.from_env(values, require_provisioned=True)


def test_hosted_runtime_marker_is_not_a_user_addressable_vault_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / ".exomem-hosted-cell.json"
    marker.write_text('{"private":"binding"}', encoding="utf-8")
    monkeypatch.setenv(hosted_runtime.HOSTED_MODE_ENV, "true")
    with pytest.raises(vault.VaultPathError, match="reserved"):
        vault.resolve_under_vault(tmp_path, marker.name, must_be_file=True)

    monkeypatch.setenv(hosted_runtime.HOSTED_MODE_ENV, "false")
    resolved, relative = vault.resolve_under_vault(tmp_path, marker.name, must_be_file=True)
    assert resolved == marker
    assert relative == marker.name
