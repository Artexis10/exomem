from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from exomem_provisioner.config import (
    PROVISIONER_PROTOCOL,
    ProvisionerSettings,
    VolumeWorkerSettings,
    load_deployment_lock,
)
from exomem_provisioner.logging import ContentFreeFormatter


def _settings(**overrides: object) -> ProvisionerSettings:
    values: dict[str, object] = {
        "bearer": "b" * 32,
        "envelope_key": "k" * 32,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "database_schema": "exomem_provisioner",
        "database_role": "exomem_provisioner_runtime",
        "trusted_proxy_ips": "127.0.0.1",
    }
    values.update(overrides)
    return ProvisionerSettings(**values)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _deployment_lock(*, admission_mode: str = "expand") -> dict[str, object]:
    digest = "a" * 64
    commit = "b" * 40
    target = {
        "releaseVersion": "0.35.1",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": digest,
        "commandFingerprint": "c" * 64,
        "schemaDigest": "d" * 64,
    }
    legacy_target = {**target, "releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1"}
    legacy_contract = {
        **legacy_target,
        "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{digest}",
        "sourceCommit": commit,
    }
    legacy_release_set = [{"releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1"}]
    return {
        "artifact": "exomem-hosted-deployment-lock",
        "schemaVersion": 2,
        "admissionMode": admission_mode,
        "components": {
            "runtime": {
                "image": f"ghcr.io/artexis10/exomem@sha256:{digest}",
                "sourceCommit": commit,
                "candidateSha256": digest,
            },
            "provisioner": {
                "image": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}",
                "sourceCommit": commit,
                "candidateSha256": "e" * 64,
                "wireProtocol": "exomem-cell-provisioner.v2",
            },
        },
        "runtimeTarget": target,
        "composition": {
            "commit": commit,
            "sourceClosure": {
                name: {"candidateCommit": commit, "compositionCommit": commit, "paths": ["src/**"]}
                for name in ("runtime", "provisioner")
            },
            "forwardContractSha256": digest,
            "authoritativeLegacyReleaseSetSha256": "f" * 64,
            "legacyCatalog": [
                {
                    "releaseVersion": "0.22.0",
                    "protocolVersion": "exomem-hosted.v1",
                    "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{digest}",
                    "sourceCommit": commit,
                    "contractSha256": _canonical_sha256(legacy_contract),
                    "contract": legacy_contract,
                }
            ],
            "legacyReleaseSetSha256": _canonical_sha256(legacy_release_set),
        },
        "rollback": {
            "provisionerImage": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}",
            "provisionerSourceCommit": commit,
            "v1CorpusSha256": digest,
            "legacyManifestSha256": digest,
            "substrateV1ConsumerCommit": commit,
        },
    }


def test_selected_deployment_lock_is_strict_and_exposes_admission_inputs(tmp_path: Path) -> None:
    path = tmp_path / "selected-lock.json"
    path.write_text(json.dumps(_deployment_lock()), encoding="utf-8")

    lock = load_deployment_lock(path)

    assert lock.admission_mode == "expand"
    assert lock.runtime_target.releaseVersion == "0.35.1"
    assert lock.legacy_catalog == frozenset({("0.22.0", "exomem-hosted.v1")})
    assert lock.authoritative_legacy_release_set_sha256 == "f" * 64
    assert _settings(deployment_lock_path=str(path)).deployment_lock == lock
    assert lock.matches_runtime_request(
        {"releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1"},
        wire_protocol="exomem-cell-provisioner.v1",
    )
    assert not lock.matches_runtime_request(
        {"releaseVersion": "0.22.1", "protocolVersion": "exomem-hosted.v1"},
        wire_protocol="exomem-cell-provisioner.v1",
    )

    path.write_text(json.dumps({"artifact": "exomem-hosted-deployment-lock-pair", "schemaVersion": 2, "locks": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_deployment_lock(path)


@pytest.mark.parametrize("tamper", ("contract", "release_set", "catalog_order"))
def test_selected_lock_rejects_forged_or_noncanonical_legacy_admission_evidence(
    tmp_path: Path, tamper: str
) -> None:
    lock = _deployment_lock()
    composition = lock["composition"]
    assert isinstance(composition, dict)
    if tamper == "contract":
        composition["legacyCatalog"][0]["contract"]["schemaDigest"] = "e" * 64  # type: ignore[index]
    elif tamper == "release_set":
        composition["legacyReleaseSetSha256"] = "f" * 64
    else:
        composition["legacyCatalog"] *= 2
    path = tmp_path / "selected-lock.json"
    path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(ValueError):
        load_deployment_lock(path)


def test_settings_require_independent_long_secrets_and_exact_protocol() -> None:
    settings = _settings()

    assert settings.protocol == PROVISIONER_PROTOCOL
    assert settings.bearer.get_secret_value() == "b" * 32
    assert settings.envelope_key.get_secret_value() == "k" * 32

    for field in ("bearer", "envelope_key"):
        with pytest.raises(ValidationError):
            _settings(**{field: "too-short"})
    with pytest.raises(ValidationError):
        _settings(protocol="exomem-cell-provisioner.v2")
    with pytest.raises(ValidationError):
        _settings(bearer="s" * 32, envelope_key="s" * 32)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_schema", "public; drop schema public"),
        ("database_schema", "public"),
        ("database_role", "postgres"),
        ("database_role", "role with spaces"),
    ],
)
def test_settings_reject_unsafe_or_shared_database_identifiers(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_content_free_formatter_never_renders_sensitive_values() -> None:
    formatter = ContentFreeFormatter()
    record = logging.LogRecord(
        "provisioner",
        logging.INFO,
        __file__,
        1,
        "operation",
        (),
        None,
    )
    record.event = "operation_submitted"  # type: ignore[attr-defined]
    record.action = "provision"  # type: ignore[attr-defined]
    record.operation_id = "operation-1"  # type: ignore[attr-defined]
    record.authorization = "Bearer secret-sentinel"  # type: ignore[attr-defined]
    record.serviceCredential = "credential-sentinel"  # type: ignore[attr-defined]
    record.note = "private note sentinel"  # type: ignore[attr-defined]

    rendered = formatter.format(record)

    assert "operation_submitted" in rendered
    assert "operation-1" in rendered
    assert "secret-sentinel" not in rendered
    assert "credential-sentinel" not in rendered
    assert "private note sentinel" not in rendered
    assert "authorization" not in rendered


def test_settings_repr_redacts_database_credentials() -> None:
    settings = _settings(
        database_url=(
            "postgresql+asyncpg://exomem_provisioner_runtime:database-password-sentinel@database.invalid/exomem"
        )
    )

    assert "database-password-sentinel" not in repr(settings)
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")


def test_postgres_url_role_must_match_dedicated_runtime_role() -> None:
    with pytest.raises(ValidationError):
        _settings(database_url="postgresql+asyncpg://wrong_role:secret@database.invalid/exomem")


def test_settings_require_bounded_failure_ceiling_and_private_trusted_proxies() -> None:
    settings = _settings(
        max_failure_attempts=4,
        trusted_proxy_ips="127.0.0.1,10.42.7.9/16,127.0.0.1/32,fd42::7/64,::1",
    )
    assert settings.max_failure_attempts == 4
    assert settings.trusted_proxy_ips == ("127.0.0.1/32,10.42.0.0/16,fd42::/64,::1/128")

    for invalid in (
        "*",
        "0.0.0.0/0",
        "8.8.8.8",
        "192.0.2.1",
        "169.254.1.1",
        "fe80::1",
        "2001:db8::1",
        "::ffff:8.8.8.8",
        "not-an-address",
    ):
        with pytest.raises(ValidationError):
            _settings(trusted_proxy_ips=invalid)
    for invalid in (0, 101):
        with pytest.raises(ValidationError):
            _settings(max_failure_attempts=invalid)


def test_volume_worker_requires_public_capacity_verification_settings() -> None:
    values = {
        "hcloud_token": "h" * 32,
        "provider_recovery_signing_key": "a" * 43,
        "volume_encryption_secret_name": "volume-encryption",
        "volume_encryption_secret_namespace": "exomem-platform",
        "location": "fsn1",
        "worker_id": "volume-worker",
        "capacity_receipt_public_key": "b" * 43,
        "capacity_contract_path": "/etc/exomem/capacity/private-alpha-capacity-v1.json",
        "capacity_receipt_namespace": "exomem-platform",
        "capacity_receipt_config_map": "exomem-capacity-receipt",
        "hcloud_server_id": 101,
    }

    settings = VolumeWorkerSettings(**values)

    assert settings.hcloud_server_id == 101
    for field in (
        "capacity_receipt_public_key",
        "capacity_contract_path",
        "capacity_receipt_namespace",
        "capacity_receipt_config_map",
        "hcloud_server_id",
    ):
        invalid = dict(values)
        invalid.pop(field)
        with pytest.raises(ValidationError):
            VolumeWorkerSettings(**invalid)
    with pytest.raises(ValidationError):
        VolumeWorkerSettings(**{**values, "capacity_contract_path": "relative.json"})
