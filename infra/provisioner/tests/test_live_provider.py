from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from exomem_provisioner.config import (
    ProviderWorkerSettings,
    load_hosted_release_manifest,
)
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.live import (
    KubernetesCapacityGate,
    KubernetesProviderRegistry,
    LiveLifecyclePlane,
)
from exomem_provisioner.production import build_live_provider_components
from exomem_provisioner.provider_identity import (
    ProviderRecoveryIdentityCodec,
    cell_provider_recovery_envelopes,
    provider_operation_resource_name,
)


class _NotFound(Exception):
    status = 404


RELEASE_FIXTURE = Path(__file__).parent / "fixtures/exomem-hosted-release-v1.json"
IDENTITY_CODEC = ProviderRecoveryIdentityCodec.from_secret("provider-recovery-root")


def _metadata() -> OpaqueProviderMetadata:
    return OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)


def _settings(**overrides: object) -> ProviderWorkerSettings:
    values: dict[str, object] = {
        "release_manifest_path": str(RELEASE_FIXTURE),
        "cell_chart_path": "/opt/exomem/charts/cell",
        "cell_chart_version": "0.1.0",
        "helm_binary": "/opt/exomem/bin/helm",
        "helm_version": "3.19.4",
        "control_hostname": "control.example.invalid",
        "transfer_hostname": "transfer.example.invalid",
        "browser_origin": "https://substratesystems.io",
        "location": "fsn1",
        "internal_origin": "http://{resource}.{namespace}.svc.cluster.local:8765",
        "worker_id": "worker-alpha",
        "provider_recovery_public_key": IDENTITY_CODEC.public_key(),
    }
    values.update(overrides)
    return ProviderWorkerSettings(**values)  # type: ignore[arg-type]


def test_live_worker_settings_require_one_release_manifest_and_bound_internal_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings().release_manifest_path == str(RELEASE_FIXTURE)
    with pytest.raises(ValidationError):
        _settings(cell_image="registry.invalid/exomem:latest")
    with pytest.raises(ValidationError):
        _settings(contract_digest="b" * 64)
    with pytest.raises(ValidationError):
        _settings(release_manifest_path="/tmp/partial-release.json")
    with pytest.raises(ValidationError):
        _settings(internal_origin="http://arbitrary-upstream.invalid")
    monkeypatch.setenv("EXOMEM_PROVISIONER_CELL_IMAGE", "ignored-is-still-forbidden")
    with pytest.raises(ValidationError):
        _settings()


def test_release_manifest_is_complete_strict_and_immutable(tmp_path: Path) -> None:
    manifest = load_hosted_release_manifest(RELEASE_FIXTURE)

    assert manifest.runtimeImage.endswith("a" * 64)
    assert manifest.gatewayContractSha256 == "b" * 64
    assert manifest.operatorContractSha256 == "c" * 64
    assert len(manifest.commandRegistry) == 21

    original = json.loads(RELEASE_FIXTURE.read_text(encoding="utf-8"))
    for name, mutate in (
        (
            "mutable-image",
            lambda value: value.update(runtimeImage="ghcr.io/artexis10/exomem:latest"),
        ),
        ("unknown-field", lambda value: value.update(independentOverride="forbidden")),
        (
            "partial-registry",
            lambda value: value.update(commandRegistry=value["commandRegistry"][:-1]),
        ),
        (
            "tag-drift",
            lambda value: value.update(publishedTag="ghcr.io/artexis10/exomem:hosted"),
        ),
    ):
        candidate = dict(original)
        candidate["commandRegistry"] = list(original["commandRegistry"])
        mutate(candidate)
        path = tmp_path / f"{name}-exomem-hosted-release-v1.json"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with pytest.raises((ValueError, ValidationError)):
            load_hosted_release_manifest(path)


@pytest.mark.asyncio
async def test_registry_creates_exact_helm_adoptable_namespace_and_operation_fence() -> None:
    metadata = _metadata()
    envelopes = cell_provider_recovery_envelopes(
        IDENTITY_CODEC,
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
        resource_name=metadata.resource_name,
        operation_resource_name=provider_operation_resource_name(metadata.operation_id),
    )

    class Core:
        namespace = None
        config_map = None
        namespace_selectors: list[str] = []

        def create_namespace(self, body):
            self.namespace = SimpleNamespace(metadata=SimpleNamespace(**body["metadata"]))

        def read_namespace(self, name):
            if self.namespace is None:
                raise _NotFound()
            return self.namespace

        def create_namespaced_config_map(self, namespace, body):
            self.config_map = SimpleNamespace(metadata=SimpleNamespace(**body["metadata"]))

        def read_namespaced_config_map(self, name, namespace):
            return self.config_map

        def list_namespace(self, *, label_selector):
            self.namespace_selectors.append(label_selector)
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(
                            name="default",
                            annotations={"kubernetes.io/metadata.name": "default"},
                        )
                    ),
                    self.namespace,
                ]
                if label_selector != "exomem.io/tenant-cell=true"
                else [self.namespace]
            )

        def list_config_map_for_all_namespaces(self, label_selector):
            assert label_selector == "exomem.io/provider-operation=true"
            return SimpleNamespace(items=[self.config_map])

    core = Core()
    registry = KubernetesProviderRegistry(
        core_v1=core,
        apps_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(),
        custom_objects=SimpleNamespace(),
        identity_verifier=IDENTITY_CODEC.verifier(),
    )

    await registry.ensure_namespace(metadata, envelopes["namespace"])
    await registry.record_operation(metadata, envelopes["providerOperationConfigMap"])

    assert core.namespace.metadata.labels["app.kubernetes.io/managed-by"] == "Helm"
    assert core.namespace.metadata.annotations["meta.helm.sh/release-name"] == (
        metadata.resource_name
    )
    assert core.config_map.metadata.annotations == {
        **metadata.kubernetes_annotations,
        "exomem.io/recovery-envelope": envelopes["providerOperationConfigMap"],
    }
    assert await registry.observed_fence("tenant-alpha") == 7
    assert core.namespace_selectors == ["exomem.io/tenant-cell=true"]


def test_production_factory_wires_the_live_plane_without_a_fake_selection_path() -> None:
    async def requester(*args, **kwargs):  # pragma: no cover - construction only
        raise AssertionError

    async def probe(*args, **kwargs):  # pragma: no cover - construction only
        raise AssertionError

    components = build_live_provider_components(
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        settings=_settings(),
        core_v1=SimpleNamespace(),
        apps_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(),
        coordination_v1=SimpleNamespace(),
        storage_v1=SimpleNamespace(),
        custom_objects=SimpleNamespace(),
        requester=requester,
        external_probe=probe,
    )

    assert isinstance(components.plane, LiveLifecyclePlane)
    assert components.release.runtimeImage.endswith("a" * 64)
    assert components.driver._config.release_version == "0.22.0"
    assert components.driver._config.protocol_version == "1"
    assert components.driver._config.contract_digest == "b" * 64
    assert components.driver._config.operator_contract_digest == "c" * 64
    assert components.driver._plane is components.plane
    assert components.driver._volumes is None


@pytest.mark.asyncio
async def test_live_route_enable_reconciles_the_original_authenticated_helm_release() -> None:
    metadata = _metadata()
    calls: list[dict[str, object]] = []

    class Helm:
        async def ensure_release(self, owner, values):
            assert owner == metadata
            calls.append(values)

    class Registry:
        async def inspect(self, current, owner):
            assert current == owner == metadata
            return SimpleNamespace(routes=(True, True))

    class Routes:
        async def enable(self, owner):  # pragma: no cover - must use Helm
            raise AssertionError("direct route writes lose provider recovery identity")

    config = SimpleNamespace(
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        browser_origin="https://substratesystems.io",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        protocol_version="1",
        release_version="0.22.0",
    )
    request = {
        "provisionMode": "serve",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
        "_providerRecoveryEnvelopes": {"controlRoute": "signed-control"},
    }
    plane = LiveLifecyclePlane(
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        registry=Registry(),  # type: ignore[arg-type]
        cell=SimpleNamespace(),  # type: ignore[arg-type]
        helm=Helm(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        routes=Routes(),  # type: ignore[arg-type]
        maintenance=SimpleNamespace(),  # type: ignore[arg-type]
        capacity=SimpleNamespace(),  # type: ignore[arg-type]
        identity_verifier=IDENTITY_CODEC.verifier(),
        config=config,  # type: ignore[arg-type]
    )
    plane._owned[plane._key(metadata)] = metadata
    plane._helm_requests[plane._key(metadata)] = request

    await plane.enable_routes(metadata)

    assert calls[0]["workloadMode"] == "serve"
    assert calls[0]["routes"] == {
        "controlHostname": "control.example.invalid",
        "enabled": True,
    }
    assert calls[0]["providerRecoveryEnvelopes"] == {"controlRoute": "signed-control"}


@pytest.mark.asyncio
async def test_credential_operator_requests_keep_physical_cell_and_stable_tenant_vault_ids() -> (
    None
):
    metadata = _metadata()
    calls: list[tuple[str, dict[str, object]]] = []

    class Cell:
        async def write_credential_bundle(self, *args, **kwargs):
            return None

        async def read_credential_bundle(self, _metadata):
            return (
                {"1": "credential-current", "2": "credential-pending"},
                {
                    "exomem.io/active-credential-version": "1",
                    "exomem.io/credential-phase": "staged",
                    "exomem.io/security-revision": "2",
                },
            )

    class Runtime:
        async def operator(self, command, _metadata, request, **kwargs):
            calls.append((command, dict(request)))
            if command == "credential":
                return {"revision": 2}
            return {
                "authenticated_credential_version": "2",
                "security_revision": 2,
                "proof_recorded": True,
            }

    plane = LiveLifecyclePlane(
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        registry=SimpleNamespace(),  # type: ignore[arg-type]
        cell=Cell(),  # type: ignore[arg-type]
        helm=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=Runtime(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        maintenance=SimpleNamespace(),  # type: ignore[arg-type]
        capacity=SimpleNamespace(),  # type: ignore[arg-type]
        identity_verifier=IDENTITY_CODEC.verifier(),
        config=SimpleNamespace(protocol_version="1"),  # type: ignore[arg-type]
    )

    await plane._credential_transition(
        metadata,
        credentials={"1": "credential-current", "2": "credential-pending"},
        annotations={
            "exomem.io/active-credential-version": "1",
            "exomem.io/security-revision": "1",
        },
        action="stage",
        operation_id="rotate-alpha",
        version="2",
    )
    accepted = await plane.credential_accepted(
        metadata,
        2,
        "credential-pending",
        {
            "releaseVersion": "0.22.0",
            "protocolVersion": "1",
            "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
        },
        "rotate-alpha",
    )

    assert accepted is True
    assert [command for command, _ in calls] == ["credential", "probe"]
    assert all(request["cell_id"] == "cell-alpha" for _, request in calls)
    assert all(request["vault_id"] == "tenant-alpha" for _, request in calls)


@pytest.mark.asyncio
async def test_registry_rejects_unowned_existing_namespace() -> None:
    metadata = _metadata()

    class Core:
        def create_namespace(self, body):
            raise type("Conflict", (Exception,), {"status": 409})()

        def read_namespace(self, name):
            return SimpleNamespace(metadata=SimpleNamespace(annotations={"exomem.io/fence": "7"}))

    registry = KubernetesProviderRegistry(
        core_v1=Core(),
        apps_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(),
        custom_objects=SimpleNamespace(),
        identity_verifier=IDENTITY_CODEC.verifier(),
    )
    with pytest.raises(MetadataConflict):
        await registry.ensure_namespace(metadata, "forged")


@pytest.mark.asyncio
async def test_registry_requires_deployed_helm_record_in_addition_to_pvc() -> None:
    metadata = _metadata()
    envelopes = cell_provider_recovery_envelopes(
        IDENTITY_CODEC,
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
        resource_name=metadata.resource_name,
        operation_resource_name=provider_operation_resource_name(metadata.operation_id),
    )

    class Core:
        releases: list[object] = []

        def read_namespace(self, name):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    annotations={
                        **metadata.kubernetes_annotations,
                        "exomem.io/recovery-envelope": envelopes["namespace"],
                    }
                )
            )

        def read_namespaced_persistent_volume_claim(self, name, namespace):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    annotations={
                        **metadata.kubernetes_annotations,
                        "exomem.io/recovery-envelope": envelopes["vaultPvc"],
                    }
                )
            )

        def list_namespaced_config_map(self, namespace, *, label_selector):
            assert label_selector == (f"owner=helm,name={metadata.resource_name},status=deployed")
            return SimpleNamespace(items=self.releases)

    class Missing:
        def __getattr__(self, name):
            def missing(*args, **kwargs):
                raise _NotFound()

            return missing

    core = Core()
    registry = KubernetesProviderRegistry(
        core_v1=core,
        apps_v1=Missing(),
        batch_v1=Missing(),
        custom_objects=Missing(),
        identity_verifier=IDENTITY_CODEC.verifier(),
    )

    snapshot = await registry.inspect(metadata, metadata)
    assert snapshot.release is False
    core.releases.append(object())
    snapshot = await registry.inspect(metadata, metadata)
    assert snapshot.release is True


@pytest.mark.asyncio
async def test_live_capacity_gate_uses_kubernetes_attachment_observations_only() -> None:
    metadata = _metadata()

    class Core:
        names = [f"exo-existing-{index}" for index in range(5)]

        def list_namespace(self, *, label_selector):
            assert label_selector == "exomem.io/tenant-cell=true"
            return SimpleNamespace(
                items=[SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in self.names]
            )

    class Storage:
        attached = 5

        def list_volume_attachment(self):
            return SimpleNamespace(
                items=[
                    SimpleNamespace(status=SimpleNamespace(attached=True))
                    for _ in range(self.attached)
                ]
            )

    core = Core()
    storage = Storage()
    gate = KubernetesCapacityGate(
        core_v1=core,
        storage_v1=storage,
    )

    assert await gate.block_reason(metadata) is None
    core.names.append("exo-existing-six")
    assert await gate.block_reason(metadata) == "active-user-cell-capacity-exhausted"
    core.names = ["exo-existing-one"]
    storage.attached = 6
    assert await gate.block_reason(metadata) == ("safe-volume-attachment-headroom-exhausted")
    core.names = [metadata.resource_name]
    assert await gate.block_reason(metadata) is None
