from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
import pytest

from exomem_provisioner.durability_actions import (
    DurabilityActionSettings,
    HelmRestoreCandidateController,
    HttpCandidateExportCheck,
    HttpCandidateProductProbe,
    RestoreWorkflowRouter,
    _ProviderMaximumFenceDriver,
)
from exomem_provisioner.kubernetes_restore import CandidateRestoreBinding, RestoreJobFailed
from exomem_provisioner.lifecycle import LifecycleConfig, OpaqueProviderMetadata


def _settings(**overrides: object) -> DurabilityActionSettings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://exomem_durability:secret@db.invalid/exomem",
        "database_schema": "exomem_provisioner",
        "database_role": "exomem_durability",
        "envelope_key": "e" * 32,
        "b2_endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "b2_region": "us-west-004",
        "recovery_bucket": "exomem-recovery-alpha",
        "user_export_bucket": "exomem-user-export-alpha",
        "recovery_restore_key_id": "recovery-restore-id",
        "recovery_restore_key": "recovery-restore-key",
        "user_export_upload_key_id": "export-upload-id",
        "user_export_upload_key": "export-upload-key",
        "user_export_restore_key_id": "export-restore-id",
        "user_export_restore_key": "export-restore-key",
        "user_export_delete_key_id": "export-delete-id",
        "user_export_delete_key": "export-delete-key",
        "user_export_delivery_key_id": "export-delivery-id",
        "user_export_delivery_key": "export-delivery-key",
        "provider_recovery_signing_key": "cnJycnJycnJycnJycnJycnJycnJycnJycnJycnJycnI",
        "release_manifest_path": "/opt/exomem/release/exomem-hosted-release-v1.json",
        "cell_chart_path": "/opt/exomem/charts/cell",
        "cell_chart_version": "0.1.0",
        "helm_binary": "/opt/exomem/bin/helm",
        "helm_version": "3.19.4",
        "control_hostname": "memory.example.test",
        "transfer_hostname": "transfer.example.test",
        "browser_origin": "https://app.example.test",
        "location": "fsn1",
        "provisioner_image": "ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
        "scratch_root": "/var/lib/exomem-scratch",
    }
    values.update(overrides)
    return DurabilityActionSettings(**values)


def test_action_settings_bind_dedicated_role_buckets_and_immutable_images() -> None:
    settings = _settings()

    assert settings.max_operations == 1
    assert settings.recovery_bucket != settings.user_export_bucket
    assert "@sha256:" in settings.provisioner_image
    with pytest.raises(ValueError):
        _settings(user_export_bucket="exomem-recovery-alpha")
    with pytest.raises(ValueError):
        _settings(database_role="another_role")
    with pytest.raises(ValueError):
        _settings(provisioner_image="ghcr.io/artexis10/exomem-provisioner:latest")


@pytest.mark.asyncio
async def test_action_fence_uses_the_maximum_database_or_authenticated_provider_value() -> None:
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def scalar(self, statement):
            return 7

    class Provider:
        async def observed_fence(self, tenant_id):
            assert tenant_id == "tenant-alpha"
            return 11

    driver = _ProviderMaximumFenceDriver(lambda: Session(), Provider())

    assert await driver.observed_fence("tenant-alpha") == 11


@pytest.mark.asyncio
async def test_restore_router_selects_only_the_opaque_reference_bucket_kind() -> None:
    events: list[str] = []

    class Workflow:
        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self, run, **arguments):
            events.append(f"{self.name}:{arguments['source_reference']}")
            return {"restored": True}

    router = RestoreWorkflowRouter(
        recovery=Workflow("recovery"),
        user_export=Workflow("export"),
    )
    assert await router.run(object(), source_reference="recovery_alpha") == {"restored": True}
    assert await router.run(object(), source_reference="export_alpha") == {"restored": True}
    with pytest.raises(ValueError, match="kind"):
        await router.run(object(), source_reference="unknown_alpha")
    assert events == ["recovery:recovery_alpha", "export:export_alpha"]


@pytest.mark.asyncio
async def test_candidate_product_probe_runs_real_commands_restart_then_publish() -> None:
    metadata = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "provision-alpha", 8)
    policy_digest = hashlib.sha256(b"policy").hexdigest()
    events: list[str] = []

    async def requester(method: str, url: str, **kwargs):
        path = url.split("/private/exomem/v1/", 1)[1]
        events.append(f"request:{method}:{path}")
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer " + "c" * 40
        assert headers["X-Exomem-Cell-Id"] == "cell-alpha"
        if path == "ready":
            data = {
                "cell_id": "cell-alpha",
                "vault_id": "tenant-alpha",
                "exomem_release": "0.22.0",
                "hosted_protocol": "exomem-hosted.v1",
                "authenticated_credential_version": "1",
                "security_revision": 1,
                "service_authenticated": True,
                "mutation_authority": True,
                "admission_phase": "ready",
                "read_admission": True,
                "write_admission": True,
                "worker_policy_digest": policy_digest,
            }
        elif path == "command/remember":
            data = {"path": "Knowledge Base/Notes/Insights/restore-self-test.md"}
        elif path == "command/ask_memory":
            data = [{"title": kwargs["json"]["query"]}]
        elif path == "command/review_memory":
            data = {}
        else:  # pragma: no cover - exact route allowlist
            raise AssertionError(path)
        return httpx.Response(200, json={"success": True, "data": data})

    class Controller:
        async def restart_and_verify(self):
            events.append("restart")

        async def publish(self):
            events.append("publish")

    class Export:
        async def verify_export(self, cell_id, operation_id):
            events.append(f"export:{cell_id}:{operation_id}")

    probe = HttpCandidateProductProbe(
        requester=requester,
        metadata=metadata,
        credential="c" * 40,
        credential_version="1",
        protocol_version="exomem-hosted.v1",
        release_version="0.22.0",
        worker_policy_digest=policy_digest,
        operation_id="restore-alpha",
        controller=Controller(),
        export_check=Export(),
    )

    checks = await probe.product_checks("cell-alpha")
    checks.update(await probe.finalize_candidate("cell-alpha"))

    assert set(checks) == probe.REQUIRED and all(checks.values())
    assert events[-3:] == ["restart", "request:GET:ready", "publish"]
    assert events.index("publish") > events.index("export:cell-alpha:restore-alpha:product-export")


def _metadata() -> OpaqueProviderMetadata:
    return OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "provision-alpha", 8)


def _binding() -> CandidateRestoreBinding:
    metadata = _metadata()
    return CandidateRestoreBinding(
        namespace=metadata.resource_name,
        service_account=metadata.resource_name,
        target_pvc=metadata.resource_name + "-data",
        credential_secret="exomem-cell-credentials",
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        source_vault_id=metadata.tenant_id,
        target_vault_id=metadata.tenant_id,
        target_vault_root="/var/lib/exomem/vault",
        target_state_root="/var/lib/exomem/state",
        target_log_root="/var/lib/exomem/logs",
        runtime_uid=10001,
        runtime_gid=10001,
        active_credential_version="1",
        expected_protocol="exomem-hosted.v1",
        workload_name=metadata.resource_name,
    )


@pytest.mark.asyncio
async def test_candidate_export_check_rejects_hosted_binding_state_and_always_releases(
    tmp_path,
) -> None:
    archive_path = tmp_path / "candidate.portable"
    archive_path.write_bytes(b"portable")
    events: list[str] = []

    class Runtime:
        async def quiesce(self, cell_id, operation_id, *, routing_stopped):
            events.append("quiesce")

        async def portable_export(self, cell_id, operation_id):
            events.append("export")
            return SimpleNamespace(
                archive_path=archive_path,
                archive_size=archive_path.stat().st_size,
                source_cell_id=cell_id,
                hosted_state_included=True,
            )

        async def release(self, cell_id, operation_id):
            events.append("release")

    with pytest.raises(RestoreJobFailed, match="hosted state"):
        await HttpCandidateExportCheck(Runtime()).verify_export("cell-alpha", "operation-alpha")

    assert events == ["quiesce", "export", "release"]


@pytest.mark.asyncio
async def test_candidate_controller_reconciles_restore_then_private_serve_then_routes() -> None:
    metadata = _metadata()
    annotations = {**metadata.kubernetes_annotations, "exomem.io/recovery-envelope": "opaque"}

    class Verifier:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def authenticate(self, envelope, *, provider, provider_reference, **identity):
            assert envelope == "opaque"
            self.calls.append((provider, provider_reference))

    class NotFound(RuntimeError):
        status = 404

    class Helm:
        def __init__(self):
            self.values: list[dict] = []

        async def ensure_release(self, owner, values):
            assert owner == metadata
            self.values.append(values)

    class Core:
        async def read_namespaced_service(self, name, namespace):
            raise NotFound()

        async def read_namespaced_persistent_volume_claim(self, name, namespace):
            return SimpleNamespace(status=SimpleNamespace(phase="Bound"))

    class Apps:
        async def read_namespaced_stateful_set(self, name, namespace):
            if helm.values[-1]["workloadMode"] == "restore":
                raise NotFound()
            return SimpleNamespace(
                metadata=SimpleNamespace(annotations=annotations),
                spec=SimpleNamespace(replicas=1),
            )

    class Custom:
        async def list_namespaced_custom_object(self, **kwargs):
            return {"items": []}

        async def get_namespaced_custom_object(self, **kwargs):
            return {"metadata": {"annotations": annotations}}

    helm = Helm()
    verifier = Verifier()
    controller = HelmRestoreCandidateController(
        metadata=metadata,
        request={
            "provisionMode": "restore-candidate",
            "workerPolicy": {},
            "_providerRecoveryEnvelopes": {
                name: "opaque"
                for name in (
                    "namespace",
                    "providerOperationConfigMap",
                    "persistentVolumeClaim",
                    "resourceQuota",
                    "limitRange",
                    "serviceAccount",
                    "defaultDenyNetworkPolicy",
                    "traefikIngressNetworkPolicy",
                    "statefulSet",
                    "service",
                    "middleware",
                    "controlRoute",
                    "transferRoute",
                    "credentialSecret",
                    "initRequestConfigMap",
                    "initJob",
                )
            },
        },
        config=LifecycleConfig(
            image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
            chart_path="/chart",
            chart_version="0.1.0",
            helm_version="3.19.4",
            control_hostname="memory.example.test",
            transfer_hostname="transfer.example.test",
            browser_origin="https://app.example.test",
            release_version="0.22.0",
            protocol_version="exomem-hosted.v1",
            operator_contract_digest="b" * 64,
            contract_digest="c" * 64,
            location="fsn1",
        ),
        identity_verifier=verifier,  # type: ignore[arg-type]
        helm=helm,  # type: ignore[arg-type]
        core_api=Core(),
        apps_api=Apps(),
        custom_objects=Custom(),
    )

    await controller.ensure_offline(_binding())
    await controller.promote(_binding())
    await controller.publish()

    assert [(value["workloadMode"], value["routes"]["enabled"]) for value in helm.values] == [
        ("restore", False),
        ("serve", False),
        ("serve", True),
    ]
    assert [value["provisionMode"] for value in helm.values] == [
        "restore-candidate",
        "restore-candidate",
        "restore-candidate",
    ]
    assert [provider for provider, _ in verifier.calls] == [
        "kubernetes",
        "traefik",
        "traefik",
        "traefik",
    ]
