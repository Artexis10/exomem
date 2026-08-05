from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from exomem_provisioner.durability_actions import run_durability_actions
from exomem_provisioner.logging import ContentFreeFormatter
from exomem_provisioner.main import _create_app, create_app_from_env, run_api
from exomem_provisioner.production import run_worker
from exomem_provisioner.volume import run_volume_rebind, run_volume_worker


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _selected_lock(tmp_path: Path) -> Path:
    path = tmp_path / "selected-deployment-lock.json"
    digest = "a" * 64
    commit = "b" * 40
    target = {
        "releaseVersion": "0.35.1", "protocolVersion": "1", "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": digest, "commandFingerprint": "c" * 64, "schemaDigest": "d" * 64,
    }
    legacy = {**target, "releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1"}
    legacy_contract = {
        **legacy,
        "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{digest}",
        "sourceCommit": commit,
    }
    legacy_release_set = [
        {"releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1"}
    ]
    path.write_text(json.dumps({
        "artifact": "exomem-hosted-deployment-lock", "schemaVersion": 2, "admissionMode": "expand",
        "components": {
            "runtime": {"image": f"ghcr.io/artexis10/exomem@sha256:{digest}", "sourceCommit": commit, "candidateSha256": digest},
            "provisioner": {"image": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}", "sourceCommit": commit, "candidateSha256": "e" * 64, "wireProtocol": "exomem-cell-provisioner.v2"},
        },
        "runtimeTarget": target,
        "composition": {
            "commit": commit,
            "sourceClosure": {name: {"candidateCommit": commit, "compositionCommit": commit, "paths": ["src/**"]} for name in ("runtime", "provisioner")},
            "forwardContractSha256": digest, "authoritativeLegacyReleaseSetSha256": "f" * 64,
            "legacyCatalog": [{"releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1", "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{digest}", "sourceCommit": commit, "contractSha256": _canonical_sha256(legacy_contract), "contract": legacy_contract}],
            "legacyReleaseSetSha256": _canonical_sha256(legacy_release_set),
        },
        "rollback": {"provisionerImage": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}", "provisionerSourceCommit": commit, "v1CorpusSha256": digest, "legacyManifestSha256": digest, "substrateV1ConsumerCommit": commit},
    }), encoding="utf-8")
    return path


def _set_production_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_PROVISIONER_BEARER", "b" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_ENVELOPE_KEY", "k" * 32)
    monkeypatch.setenv(
        "EXOMEM_PROVISIONER_DATABASE_URL",
        "postgresql+asyncpg://exomem_provisioner_runtime:secret@database.invalid/exomem",
    )
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_SCHEMA", "exomem_provisioner")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ROLE", "exomem_provisioner_runtime")
    monkeypatch.setenv("EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS", "127.0.0.1")
    monkeypatch.setenv(
        "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
        "cnJycnJycnJycnJycnJycnJycnJycnJycnJycnJycnI",
    )
    monkeypatch.setenv("EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH", str(_selected_lock(tmp_path)))


def test_startup_loads_strict_environment_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_production_environment(monkeypatch, tmp_path)

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        app = create_app_from_env()
        assert any(isinstance(handler.formatter, ContentFreeFormatter) for handler in root.handlers)
    finally:
        root.handlers[:] = original_handlers

    assert app.state.database is not None
    assert app.state.repository is not None
    assert {route.path for route in app.routes} >= {
        "/health/live",
        "/health/ready",
        "/cells/provision",
        "/cells/destroy",
    }


def test_startup_loads_the_exact_handed_off_ed25519_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from exomem_provisioner.config import ProvisionerSettings
    from exomem_provisioner.provider_identity import ProviderRecoveryIdentityCodec

    _set_production_environment(monkeypatch, tmp_path)
    settings = ProvisionerSettings()  # type: ignore[call-arg]
    app = _create_app(settings)

    expected = ProviderRecoveryIdentityCodec(b"r" * 32).public_key()
    assert app.state.provider_identity_public_key == expected


def test_startup_fails_closed_without_selected_deployment_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_production_environment(monkeypatch, tmp_path)
    monkeypatch.delenv("EXOMEM_PROVISIONER_DEPLOYMENT_LOCK_PATH")

    with pytest.raises(ValueError, match="selected deployment lock is required"):
        create_app_from_env()


def test_startup_fails_closed_when_required_environment_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_production_environment(monkeypatch, tmp_path)
    for name in (
        "EXOMEM_PROVISIONER_BEARER",
        "EXOMEM_PROVISIONER_ENVELOPE_KEY",
        "EXOMEM_PROVISIONER_DATABASE_URL",
        "EXOMEM_PROVISIONER_DATABASE_SCHEMA",
        "EXOMEM_PROVISIONER_DATABASE_ROLE",
        "EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS",
        "EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        create_app_from_env()
def test_uvicorn_trusts_only_configured_proxy_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_production_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS",
        "127.0.0.1,10.42.7.9/16,fd42::7/64",
    )
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr("exomem_provisioner.main.uvicorn.run", fake_run)
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        run_api()
    finally:
        root.handlers[:] = original_handlers

    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "127.0.0.1/32,10.42.0.0/16,fd42::/64"
    assert captured["log_config"] is None


@pytest.mark.parametrize("entrypoint", [create_app_from_env, run_api])
def test_production_entrypoints_reject_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: object,
    tmp_path: Path,
) -> None:
    _set_production_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        "exomem_provisioner.main.uvicorn.run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not start with SQLite"),
    )

    with pytest.raises(RuntimeError, match="PostgreSQL is required"):
        entrypoint()  # type: ignore[operator]


def test_directly_injected_settings_keep_sqlite_available_for_tests(tmp_path: Path) -> None:
    from exomem_provisioner.config import ProvisionerSettings

    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url="sqlite+aiosqlite:///:memory:",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
        deployment_lock_path=str(_selected_lock(tmp_path)),
    )

    app = _create_app(settings)

    assert app.state.database is not None


@pytest.mark.parametrize(
    ("program", "entrypoint"),
    [
        ("exomem-provisioner-api", run_api),
        ("exomem-provisioner-worker", run_worker),
        ("exomem-durability-actions", run_durability_actions),
        ("exomem-volume-worker", run_volume_worker),
        ("exomem-provisioner-volume-rebind", run_volume_rebind),
    ],
)
def test_container_entrypoints_expose_environment_free_help_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    program: str,
    entrypoint: object,
) -> None:
    monkeypatch.setattr("sys.argv", [program, "--help"])

    entrypoint()  # type: ignore[operator]

    output = capsys.readouterr().out
    assert output.startswith(program)
    assert "environment" in output
