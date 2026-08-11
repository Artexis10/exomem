from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from exomem_provisioner.capacity import CapacityConflict
from exomem_provisioner.config import (
    ProviderWorkerSettings,
    load_hosted_release_manifest,
)
from exomem_provisioner.driver import DriverPending, EffectContext
from exomem_provisioner.lifecycle import (
    CellLifecycleDriver,
    MetadataConflict,
    OpaqueProviderMetadata,
)
from exomem_provisioner.live import (
    KubernetesProviderRegistry,
    LiveLifecyclePlane,
)
from exomem_provisioner.models import CapacityReservationClass, ResourceKind
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


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _deployment_lock(tmp_path: Path) -> Path:
    path = tmp_path / "selected-deployment-lock.json"
    digest = "a" * 64
    commit = "b" * 40
    target = {
        "releaseVersion": "0.22.0",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "b" * 64,
        "commandFingerprint": "c" * 64,
        "schemaDigest": "d" * 64,
    }
    legacy_contract = {
        **target,
        "protocolVersion": "exomem-hosted.v1",
        "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{digest}",
        "sourceCommit": commit,
    }
    legacy_release_set = [
        {"releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1"}
    ]
    payload = {
        "artifact": "exomem-hosted-deployment-lock",
        "schemaVersion": 2,
        "admissionMode": "expand",
        "components": {
            "runtime": {"image": f"ghcr.io/artexis10/exomem@sha256:{digest}", "sourceCommit": commit, "candidateSha256": digest},
            "provisioner": {"image": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}", "sourceCommit": commit, "candidateSha256": "e" * 64, "wireProtocol": "exomem-cell-provisioner.v2"},
        },
        "runtimeTarget": target,
        "composition": {
            "commit": commit,
            "sourceClosure": {name: {"candidateCommit": commit, "compositionCommit": commit, "paths": ["src/**"]} for name in ("runtime", "provisioner")},
            "forwardContractSha256": digest,
            "authoritativeLegacyReleaseSetSha256": "f" * 64,
            "legacyCatalog": [{"releaseVersion": "0.22.0", "protocolVersion": "exomem-hosted.v1", "runtimeImage": f"ghcr.io/artexis10/exomem@sha256:{digest}", "sourceCommit": commit, "contractSha256": _canonical_sha256(legacy_contract), "contract": legacy_contract}],
            "legacyReleaseSetSha256": _canonical_sha256(legacy_release_set),
        },
        "rollback": {"provisionerImage": f"ghcr.io/artexis10/exomem-provisioner@sha256:{'e' * 64}", "provisionerSourceCommit": commit, "v1CorpusSha256": digest, "legacyManifestSha256": digest, "substrateV1ConsumerCommit": commit},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _capacity_contract(path: Path) -> Path:
    raw_key = base64.urlsafe_b64decode(IDENTITY_CODEC.public_key() + "=")
    contract = {
        "schema_version": 1,
        "receipt_authentication": {
            "algorithm": "ed25519",
            "capacity_domain": "exomem.capacity-live-receipt.v1",
            "capacity_ttl_seconds": 300,
            "capacity_public_key_id": hashlib.sha256(raw_key).hexdigest(),
        },
        "limits": {
            "active_user_cells": 4,
            "active_recovery_cells": 2,
            "maximum_potential_attachments": 6,
            "provider_volume_attachment_limit": 16,
            "minimum_unused_provider_headroom": 10,
        },
    }
    target = path / "private-alpha-capacity-v1.json"
    target.write_text(json.dumps(contract), encoding="utf-8")
    return target


def _metadata() -> OpaqueProviderMetadata:
    return OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)


def _settings(**overrides: object) -> ProviderWorkerSettings:
    values: dict[str, object] = {
        "deployment_lock_path": str(RELEASE_FIXTURE),
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
        "capacity_receipt_public_key": IDENTITY_CODEC.public_key(),
        "capacity_contract_path": str(RELEASE_FIXTURE.parent / "private-alpha-capacity-v1.json"),
        "capacity_receipt_namespace": "exomem-platform",
        "capacity_receipt_config_map": "exomem-capacity-receipt",
        "hcloud_server_id": 101,
    }
    values.update(overrides)
    return ProviderWorkerSettings(**values)  # type: ignore[arg-type]


def test_live_worker_settings_require_one_selected_lock_and_bound_internal_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings().deployment_lock_path == str(RELEASE_FIXTURE)
    with pytest.raises(ValidationError):
        _settings(cell_image="registry.invalid/exomem:latest")
    with pytest.raises(ValidationError):
        _settings(contract_digest="b" * 64)
    with pytest.raises(ValidationError):
        _settings(deployment_lock_path="relative-lock.json")
    with pytest.raises(ValidationError):
        _settings(release_manifest_path=str(RELEASE_FIXTURE))
    with pytest.raises(ValidationError):
        _settings(internal_origin="http://arbitrary-upstream.invalid")
    with pytest.raises(ValidationError):
        _settings(hcloud_server_id=0)
    with pytest.raises(ValidationError):
        _settings(capacity_contract_path="relative-capacity.json")
    monkeypatch.setenv("EXOMEM_PROVISIONER_CELL_IMAGE", "ignored-is-still-forbidden")
    with pytest.raises(ValidationError):
        _settings()


def test_live_worker_loads_the_selected_lock_and_rejects_an_unavailable_lock(tmp_path: Path) -> None:
    settings = _settings(
        capacity_contract_path=str(_capacity_contract(tmp_path)),
        deployment_lock_path=str(_deployment_lock(tmp_path)),
    )
    assert settings.deployment_lock.runtime_target.releaseVersion == "0.22.0"

    missing = _settings(
        capacity_contract_path=str(_capacity_contract(tmp_path)),
        deployment_lock_path=str(tmp_path / "missing-lock.json"),
    )
    with pytest.raises(ValueError, match="deployment lock is unavailable"):
        _ = missing.deployment_lock


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

    await registry.ensure_namespace(
        metadata,
        envelopes["namespace"],
        "serve",
        {
            "vaultId": metadata.tenant_id,
            "expectedRelease": "0.22.0",
            "workerPolicyDigest": "a" * 64,
            "browserOrigin": "https://substratesystems.io",
            "transferHostname": "transfer.example.invalid",
        },
    )
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


@pytest.mark.asyncio
async def test_live_plane_namespace_carries_the_fixed_helm_contract_annotations() -> None:
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
        namespace_body = None

        def create_namespace(self, body):
            self.namespace_body = body
            self.namespace = SimpleNamespace(metadata=SimpleNamespace(**body["metadata"]))

        def read_namespace(self, name):
            return self.namespace

        def create_namespaced_config_map(self, namespace, body):
            self.config_map = SimpleNamespace(metadata=SimpleNamespace(**body["metadata"]))

        def read_namespaced_persistent_volume_claim(self, name, namespace):
            raise _NotFound()

        def list_namespaced_config_map(self, namespace, *, label_selector):
            return SimpleNamespace(items=[])

    class Missing:
        def __getattr__(self, name):
            def missing(*args, **kwargs):
                raise _NotFound()

            return missing

    class Capacity:
        async def require_active(self, **kwargs):
            return None

    core = Core()
    registry = KubernetesProviderRegistry(
        core_v1=core,
        apps_v1=Missing(),
        batch_v1=Missing(),
        custom_objects=Missing(),
        identity_verifier=IDENTITY_CODEC.verifier(),
    )
    plane = LiveLifecyclePlane(
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        registry=registry,
        cell=SimpleNamespace(),  # type: ignore[arg-type]
        helm=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        maintenance=SimpleNamespace(),  # type: ignore[arg-type]
        capacity=Capacity(),  # type: ignore[arg-type]
        identity_verifier=IDENTITY_CODEC.verifier(),
        config=SimpleNamespace(
            image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
            browser_origin="https://substratesystems.io",
            control_hostname="control.example.invalid",
            transfer_hostname="transfer.example.invalid",
            protocol_version="1",
            release_version="0.22.0",
        ),  # type: ignore[arg-type]
    )
    key = plane._key(metadata)
    plane._operation_ids[key] = "internal-operation-alpha"
    plane._recovery_envelopes[key] = envelopes

    request = {
        "provisionMode": "serve",
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
    }
    await plane.ensure_namespace(metadata, request)

    expected = {
        "exomem.io/vault-id": "tenant-alpha",
        "exomem.io/expected-release": "0.22.0",
        "exomem.io/worker-policy-digest": hashlib.sha256(
            b'{"media":false,"semantic":true,"workerCount":2}'
        ).hexdigest(),
        "exomem.io/browser-origin": "https://substratesystems.io",
        "exomem.io/transfer-hostname": "transfer.example.invalid",
    }
    annotations = core.namespace_body["metadata"]["annotations"]
    assert {key: annotations.get(key) for key in expected} == expected


def test_production_factory_wires_the_live_plane_without_a_fake_selection_path(
    tmp_path: Path,
) -> None:
    async def requester(*args, **kwargs):  # pragma: no cover - construction only
        raise AssertionError

    async def probe(*args, **kwargs):  # pragma: no cover - construction only
        raise AssertionError

    components = build_live_provider_components(
        repository=SimpleNamespace(session_factory=SimpleNamespace()),  # type: ignore[arg-type]
        settings=_settings(
            capacity_contract_path=str(_capacity_contract(tmp_path)),
            deployment_lock_path=str(_deployment_lock(tmp_path)),
        ),
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
    assert components.lock.components.runtime.image.endswith("a" * 64)
    assert components.driver._config.release_version == "0.22.0"
    assert components.driver._config.protocol_version == "1"
    assert components.driver._config.contract_digest == "b" * 64
    assert components.driver._plane is components.plane
    assert components.driver._volumes is None
    assert components.capacity is components.plane._capacity


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
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
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

    await plane.enable_routes(metadata, request)

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
    calls: list[tuple[str, dict[str, object], str]] = []

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
            calls.append((command, dict(request), kwargs["protocol_version"]))
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
        config=SimpleNamespace(
            runtime_target_for=lambda _request, **_kwargs: {
                "releaseVersion": "0.22.0",
                "protocolVersion": "exomem-hosted.v1",
                "gatewayContractDigest": "b" * 64,
            }
        ),  # type: ignore[arg-type]
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
        protocol_version="exomem-hosted.v1",
    )
    accepted = await plane.credential_accepted(
        metadata,
        2,
        "credential-pending",
        {
            "releaseVersion": "0.22.0",
            "protocolVersion": "exomem-hosted.v1",
            "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
        },
        "rotate-alpha",
    )

    assert accepted is True
    assert [command for command, _, _ in calls] == ["credential", "probe"]
    assert all(request["cell_id"] == "cell-alpha" for _, request, _ in calls)
    assert all(request["vault_id"] == "tenant-alpha" for _, request, _ in calls)
    assert [protocol for _, _, protocol in calls] == ["exomem-hosted.v1"] * 2


@pytest.mark.asyncio
async def test_legacy_credential_promotion_uses_the_catalog_unit_after_mutation() -> None:
    metadata = _metadata()
    credentials = {"1": "credential-current", "2": "credential-pending"}
    annotations = {
        "exomem.io/active-credential-version": "1",
        "exomem.io/credential-phase": "proved",
        "exomem.io/security-revision": "2",
    }
    protocols: list[str] = []

    class Cell:
        async def read_credential_bundle(self, _metadata):
            return dict(credentials), dict(annotations)

        async def write_credential_bundle(self, _metadata, values, *, lifecycle_annotations):
            credentials.clear()
            credentials.update(values)
            annotations.clear()
            annotations.update(lifecycle_annotations)

    class Runtime:
        async def operator(self, _command, _metadata, request, *, protocol_version, **_kwargs):
            protocols.append(protocol_version)
            if request["action"] == "promote":
                return {"revision": 3, "phase": "promoted"}
            return {"revision": 4, "phase": "stable", "active_version": "2"}

        async def health(self, _metadata, *, protocol_version, expected_contract_digest, **_kwargs):
            protocols.append(protocol_version)
            assert expected_contract_digest == "e" * 64
            return SimpleNamespace(ready=True)

        async def credential_rejected(self, _metadata, *, protocol_version, **_kwargs):
            protocols.append(protocol_version)
            return True

    target = {
        "releaseVersion": "0.22.0",
        "protocolVersion": "exomem-hosted.v1",
        "gatewayContractDigest": "e" * 64,
    }
    plane = LiveLifecyclePlane(
        repository=SimpleNamespace(), registry=SimpleNamespace(), cell=Cell(), helm=SimpleNamespace(),
        runtime=Runtime(), routes=SimpleNamespace(), maintenance=SimpleNamespace(),
        capacity=SimpleNamespace(), identity_verifier=IDENTITY_CODEC.verifier(),
        config=SimpleNamespace(runtime_target_for=lambda _request, **_kwargs: target),  # type: ignore[arg-type]
    )
    request = {
        "releaseVersion": "0.22.0",
        "protocolVersion": "exomem-hosted.v1",
        "serviceCredential": "credential-current",
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
    }

    assert await plane.promote_credential(metadata, 2, request, "rotate-legacy") is True
    assert protocols == ["exomem-hosted.v1"] * 4


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
        await registry.ensure_namespace(
            metadata,
            "forged",
            "serve",
            {
                "vaultId": metadata.tenant_id,
                "expectedRelease": "0.22.0",
                "workerPolicyDigest": "a" * 64,
                "browserOrigin": "https://substratesystems.io",
                "transferHostname": "transfer.example.invalid",
            },
        )


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
@pytest.mark.parametrize(
    ("job", "present", "complete", "failed"),
    (
        (None, False, False, False),
        (SimpleNamespace(status=SimpleNamespace(conditions=(), failed=0)), True, False, False),
        (
            SimpleNamespace(
                status=SimpleNamespace(
                    conditions=(SimpleNamespace(type="Complete", status="True"),), failed=0
                )
            ),
            True,
            True,
            False,
        ),
        (
            SimpleNamespace(
                status=SimpleNamespace(
                    conditions=(SimpleNamespace(type="Failed", status="True"),), failed=1
                )
            ),
            True,
            False,
            True,
        ),
    ),
)
async def test_registry_distinguishes_absent_running_complete_and_failed_init_job(
    job, present: bool, complete: bool, failed: bool
) -> None:
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
            return SimpleNamespace(items=[object()])

    class Batch:
        def read_namespaced_job(self, name, namespace):
            if job is None:
                raise _NotFound()
            return job

    class Missing:
        def __getattr__(self, name):
            def missing(*args, **kwargs):
                raise _NotFound()

            return missing

    registry = KubernetesProviderRegistry(
        core_v1=Core(),
        apps_v1=Missing(),
        batch_v1=Batch(),
        custom_objects=Missing(),
        identity_verifier=IDENTITY_CODEC.verifier(),
    )

    snapshot = await registry.inspect(metadata, metadata)

    assert snapshot.init_job_present is present
    assert snapshot.init_complete is complete
    assert snapshot.init_failed is failed


def _init_snapshot(*, present: bool, complete: bool = False, failed: bool = False, serving: bool = False):
    return SimpleNamespace(
        namespace=True,
        release=True,
        init_job_present=present,
        init_complete=complete,
        init_failed=failed,
        serving=serving,
        runtime_admitted=False,
        routes=(False, False),
    )


def _init_recovery_config() -> SimpleNamespace:
    return SimpleNamespace(
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        browser_origin="https://substratesystems.io",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        protocol_version="1",
        release_version="0.22.0",
    )


def _init_recovery_request(*, envelope: str, worker_count: int = 2) -> dict[str, object]:
    return {
        "provisionMode": "serve",
        "workerPolicy": {"workerCount": worker_count, "semantic": True, "media": False},
        "_providerRecoveryEnvelopes": {"initJob": envelope},
    }


def _init_recovery_plane(snapshots: list[SimpleNamespace]):
    metadata = _metadata()
    original_owner = OpaqueProviderMetadata(
        metadata.tenant_id, metadata.subject_id, "original-operation", 6
    )
    helm_calls: list[tuple[OpaqueProviderMetadata, dict[str, object]]] = []

    class Registry:
        async def inspect(self, current, owner):
            assert current == metadata
            assert owner == original_owner
            return snapshots.pop(0)

        async def observed_fence(self, tenant_id):
            assert tenant_id == metadata.tenant_id
            return 0

        async def record_operation(self, current, envelope):
            assert current == metadata
            assert isinstance(envelope, str)

    class Helm:
        async def ensure_release(self, owner, values):
            helm_calls.append((owner, values))

    plane = LiveLifecyclePlane(
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        registry=Registry(),  # type: ignore[arg-type]
        cell=SimpleNamespace(),  # type: ignore[arg-type]
        helm=Helm(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        maintenance=SimpleNamespace(),  # type: ignore[arg-type]
        capacity=SimpleNamespace(),  # type: ignore[arg-type]
        identity_verifier=IDENTITY_CODEC.verifier(),
        config=_init_recovery_config(),  # type: ignore[arg-type]
    )
    key = plane._key(metadata)
    plane._owned[key] = original_owner
    plane._helm_requests[key] = _init_recovery_request(envelope="original-init-envelope")
    return plane, metadata, original_owner, helm_calls


@pytest.mark.asyncio
async def test_initialize_replays_absent_init_job_with_original_authenticated_release() -> None:
    plane, metadata, original_owner, helm_calls = _init_recovery_plane(
        [
            _init_snapshot(present=False),
            _init_snapshot(present=True, complete=True),
            _init_snapshot(present=False, serving=True),
        ]
    )

    initialized = await plane.initialize(
        metadata,
        _init_recovery_request(envelope="current-untrusted-envelope", worker_count=1),
        _init_recovery_config(),  # type: ignore[arg-type]
    )

    assert initialized is True
    assert [owner for owner, _values in helm_calls] == [original_owner, original_owner]
    assert [values["workloadMode"] for _owner, values in helm_calls] == ["initialize", "serve"]
    assert helm_calls[0][1]["workerPolicyDigest"] == helm_calls[1][1]["workerPolicyDigest"]
    assert helm_calls[0][1]["providerRecoveryEnvelopes"] == {
        "initJob": "original-init-envelope"
    }
    assert helm_calls[1][1]["providerRecoveryEnvelopes"] == {
        "initJob": "original-init-envelope"
    }


@pytest.mark.asyncio
async def test_initialize_keeps_present_running_init_job_pending_without_helm_replay() -> None:
    plane, metadata, _original_owner, helm_calls = _init_recovery_plane(
        [_init_snapshot(present=True)]
    )

    initialized = await plane.initialize(
        metadata, _init_recovery_request(envelope="current-envelope"), _init_recovery_config()  # type: ignore[arg-type]
    )

    assert initialized is False
    assert helm_calls == []


@pytest.mark.asyncio
async def test_initialize_keeps_replayed_pending_init_job_pending_without_serving() -> None:
    plane, metadata, _original_owner, helm_calls = _init_recovery_plane(
        [_init_snapshot(present=False), _init_snapshot(present=True)]
    )

    initialized = await plane.initialize(
        metadata, _init_recovery_request(envelope="current-envelope"), _init_recovery_config()  # type: ignore[arg-type]
    )

    assert initialized is False
    assert [values["workloadMode"] for _owner, values in helm_calls] == ["initialize"]


@pytest.mark.asyncio
async def test_initialize_raises_for_failed_replayed_init_job_without_serving() -> None:
    plane, metadata, _original_owner, helm_calls = _init_recovery_plane(
        [_init_snapshot(present=False), _init_snapshot(present=True, failed=True)]
    )

    with pytest.raises(MetadataConflict, match="cell storage initialization failed"):
        await plane.initialize(
            metadata, _init_recovery_request(envelope="current-envelope"), _init_recovery_config()  # type: ignore[arg-type]
        )

    assert [values["workloadMode"] for _owner, values in helm_calls] == ["initialize"]


@pytest.mark.asyncio
async def test_initializing_checkpoint_recovers_deleted_init_job_without_looping() -> None:
    plane, metadata, original_owner, helm_calls = _init_recovery_plane(
        [
            _init_snapshot(present=False),
            _init_snapshot(present=False),
            _init_snapshot(present=True, complete=True),
            _init_snapshot(present=False, serving=True),
        ]
    )
    envelopes = cell_provider_recovery_envelopes(
        IDENTITY_CODEC,
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
        resource_name=metadata.resource_name,
        operation_resource_name=provider_operation_resource_name(metadata.operation_id),
    )
    original_request = _init_recovery_request(envelope="original-init-envelope")
    original_request["_providerRecoveryEnvelopes"] = envelopes

    class Repository:
        async def list_resources(self, *, tenant_id, cell_id):
            assert (tenant_id, cell_id) == (metadata.tenant_id, metadata.subject_id)
            return [
                SimpleNamespace(
                    kind=ResourceKind.KUBERNETES_NAMESPACE,
                    operation_id="original-database-operation",
                    provider_operation_id=original_owner.operation_id,
                    provider_fence_generation=original_owner.fence_generation,
                )
            ]

        async def load_request(self, operation_id):
            assert operation_id == "original-database-operation"
            return original_request

    class Cell:
        async def volume_claim_bound(self, owner):
            assert owner == original_owner
            return True

    plane._repository = Repository()  # type: ignore[assignment]
    plane._cell = Cell()  # type: ignore[assignment]
    driver = CellLifecycleDriver(
        plane=plane, volume_worker=None, config=_init_recovery_config()  # type: ignore[arg-type]
    )

    outcome = await driver.execute(
        "provision",
        original_request,
        EffectContext(
            operation_id="current-database-operation",
            provider_operation_id=metadata.operation_id,
            tenant_id=metadata.tenant_id,
            cell_id=metadata.subject_id,
            fence_generation=metadata.fence_generation,
            checkpoint="initializing",
        ),
    )

    assert outcome == DriverPending("initialized", 1)
    assert [values["workloadMode"] for _owner, values in helm_calls] == ["initialize", "serve"]


@pytest.mark.asyncio
async def test_live_plane_requires_exact_reservation_before_namespace_or_release() -> None:
    metadata = _metadata()
    calls: list[tuple[str, object]] = []

    class Capacity:
        reject = False

        async def require_active(self, **identity):
            if self.reject:
                raise CapacityConflict("absent")
            calls.append(("capacity", identity))

    class Registry:
        async def ensure_namespace(self, owner, envelope, mode, helm_values):
            calls.append(("namespace", (owner, envelope, mode)))

        async def record_operation(self, owner, envelope):
            calls.append(("operation", (owner, envelope)))

        async def inspect(self, current, owner):
            return SimpleNamespace(
                namespace=True,
                release=False,
                init_complete=False,
                init_failed=False,
                serving=False,
                runtime_admitted=False,
                routes=(False, False),
            )

    capacity = Capacity()
    plane = LiveLifecyclePlane(
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        registry=Registry(),  # type: ignore[arg-type]
        cell=SimpleNamespace(),  # type: ignore[arg-type]
        helm=SimpleNamespace(),  # type: ignore[arg-type]
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        maintenance=SimpleNamespace(),  # type: ignore[arg-type]
        capacity=capacity,  # type: ignore[arg-type]
        identity_verifier=IDENTITY_CODEC.verifier(),
        config=SimpleNamespace(
            image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
            browser_origin="https://substratesystems.io",
            control_hostname="control.example.invalid",
            transfer_hostname="transfer.example.invalid",
            protocol_version="1",
            release_version="0.22.0",
        ),  # type: ignore[arg-type]
    )
    key = plane._key(metadata)
    plane._operation_ids[key] = "internal-operation-alpha"
    plane._recovery_envelopes[key] = {
        "namespace": "signed-namespace",
        "providerOperationConfigMap": "signed-operation",
    }

    await plane.ensure_namespace(
        metadata,
        {
            "provisionMode": "serve",
            "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
        },
    )

    assert calls[0] == (
        "capacity",
        {
            "internal_operation_id": "internal-operation-alpha",
            "tenant_id": "tenant-alpha",
            "cell_id": "cell-alpha",
            "provider_operation_id": "operation-alpha",
            "fence_generation": 7,
            "reservation_class": CapacityReservationClass.USER,
        },
    )
    assert calls[1] == (
        "namespace",
        (metadata, "signed-namespace", "serve"),
    )

    capacity.reject = True
    with pytest.raises(MetadataConflict, match="exact active capacity reservation"):
        await plane.install_release(metadata, {"provisionMode": "serve"}, {})
