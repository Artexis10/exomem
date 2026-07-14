from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from exomem_provisioner.models import OperationAction
from exomem_provisioner.provider_recovery import ProviderRecoveryIdentityCodec
from exomem_provisioner.vault_backup import (
    HttpPortableRuntimePort,
    LiveBackupTarget,
    LiveBackupTargetRegistry,
    OpaqueProviderMetadata,
    VaultBackupSettings,
    VerifiedRouteMaintenancePort,
    _recover_active_credential,
)


def _seed(value: bytes = b"s" * 32) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _settings(tmp_path: Path, **overrides: object) -> VaultBackupSettings:
    seed = _seed()
    values: dict[str, object] = {
        "database_url": ("postgresql+asyncpg://exomem_provisioner_runtime:secret@db.invalid/app"),
        "database_schema": "exomem_provisioner",
        "database_role": "exomem_provisioner_runtime",
        "envelope_key": "wrapping-key-material-which-is-long-enough",
        "provider_recovery_signing_key": seed,
        "b2_endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "b2_region": "us-west-004",
        "recovery_bucket": "exomem-private-alpha-recovery-deadbeef",
        "recovery_upload_key_id": "upload-key-id",
        "recovery_upload_key": "upload-key-secret",
        "release_manifest_path": tmp_path / "exomem-hosted-release-v1.json",
        "scratch_root": tmp_path / "scratch",
    }
    values.update(overrides)
    return VaultBackupSettings(**values)


def test_vault_backup_settings_bind_one_provider_identity_trust_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.max_concurrency == 4
    assert settings.scratch_root.is_absolute()
    assert "public" not in " ".join(type(settings).model_fields)
    assert ProviderRecoveryIdentityCodec.from_encoded_seed(
        settings.provider_recovery_signing_key.get_secret_value()
    ).public_key()


@pytest.mark.asyncio
async def test_active_credential_recovery_matches_the_ledger_version() -> None:
    operations = [
        SimpleNamespace(id="pending-v3", action=OperationAction.ROTATE_CREDENTIAL),
        SimpleNamespace(id="active-v2", action=OperationAction.ROTATE_CREDENTIAL),
        SimpleNamespace(id="initial-v1", action=OperationAction.PROVISION),
    ]

    class Repository:
        async def load_request(self, operation_id: str):
            return {
                "pending-v3": {"credentialVersion": 3, "nextCredential": "credential-v3"},
                "active-v2": {"credentialVersion": 2, "nextCredential": "credential-v2"},
                "initial-v1": {"serviceCredential": "credential-v1"},
            }[operation_id]

    assert await _recover_active_credential(operations, Repository(), 2) == "credential-v2"
    assert await _recover_active_credential(operations, Repository(), 1) == "credential-v1"


def _target() -> LiveBackupTarget:
    metadata = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)
    routes = tuple(
        {
            "metadata": {"name": f"{metadata.resource_name}-{kind}"},
            "spec": {
                "routes": [
                    {
                        "kind": "Rule",
                        "match": f"Host(`{kind}.example.test`) && PathPrefix(`/cells`)",
                    }
                ]
            },
        }
        for kind in ("control", "transfer")
    )
    return LiveBackupTarget(
        metadata=metadata,
        credential=_seed(bytes(range(32))),
        credential_version="1",
        protocol_version="1",
        release_version="0.22.0",
        browser_origin="https://app.example.test",
        control_hostname="control.example.test",
        transfer_hostname="transfer.example.test",
        routes=routes,
    )


@pytest.mark.asyncio
async def test_route_close_failure_restores_exact_specs_and_releases_lease() -> None:
    target = _target()
    registry = LiveBackupTargetRegistry()
    registry.replace({target.metadata.subject_id: target})

    class Custom:
        calls: list[dict[str, object]] = []

        def patch_namespaced_custom_object(self, **arguments: object) -> None:
            self.calls.append(arguments)

    class Maintenance:
        released = False

        async def acquire(self, metadata: object, operation_id: str) -> bool:
            return True

        async def release(self, metadata: object, operation_id: str) -> None:
            self.released = True

    class Http:
        async def get(self, *arguments: object, **keywords: object) -> httpx.Response:
            return httpx.Response(200)

        async def options(self, *arguments: object, **keywords: object) -> httpx.Response:
            return httpx.Response(200)

    custom = Custom()
    maintenance = Maintenance()
    port = VerifiedRouteMaintenancePort(
        custom_objects=custom,
        coordination_v1=object(),
        http=Http(),  # type: ignore[arg-type]
        registry=registry,
        maintenance=maintenance,
    )

    with pytest.raises(RuntimeError, match="remained reachable"):
        await port.close_and_verify(target.metadata.subject_id, "backup-operation")

    assert maintenance.released is True
    assert [call["body"] for call in custom.calls[:2]] == [
        {"spec": {"routes": []}},
        {"spec": {"routes": []}},
    ]
    assert [call["body"] for call in custom.calls[2:]] == [
        {"spec": route["spec"]} for route in target.routes
    ]


@pytest.mark.asyncio
async def test_http_runtime_streams_verified_archive_and_cleans_local_artifacts(
    tmp_path: Path,
) -> None:
    target = _target()
    registry = LiveBackupTargetRegistry()
    registry.replace({target.metadata.subject_id: target})
    archive = b"portable archive\n" * 200
    manifest = (
        json.dumps(
            {
                "hostedStateIncluded": False,
                "releaseVersion": target.release_version,
                "sourceCellId": target.metadata.subject_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lifecycle/export"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "downloadPath": "artifact",
                        "archiveSha256": hashlib.sha256(archive).hexdigest(),
                        "manifestSha256": hashlib.sha256(manifest.encode()).hexdigest(),
                        "archiveSize": len(archive),
                        "sourceCellId": target.metadata.subject_id,
                        "releaseVersion": target.release_version,
                        "hostedStateIncluded": False,
                        "manifestJson": manifest,
                    },
                },
            )
        if request.url.path.endswith("/artifact"):
            return httpx.Response(200, content=archive)
        if request.url.path.endswith(("/lifecycle/quiesce", "/lifecycle/resume")):
            return httpx.Response(200, json={"success": True, "data": {}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        port = HttpPortableRuntimePort(
            http=http,
            registry=registry,
            scratch_root=tmp_path,
        )
        await port.quiesce(
            target.metadata.subject_id,
            "backup-operation",
            routing_stopped=True,
        )
        result = await port.portable_export(
            target.metadata.subject_id,
            "backup-operation",
        )

        assert result.archive_path.read_bytes() == archive
        assert result.manifest_path.read_text(encoding="utf-8") == manifest
        await port.release(target.metadata.subject_id, "backup-operation")
        assert not result.archive_path.exists()
        assert not result.manifest_path.exists()
