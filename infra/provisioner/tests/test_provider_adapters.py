from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem_provisioner.adapters import (
    HCloudVolumeAdapter,
    HelmCliAdapter,
    KubernetesCellAdapter,
    KubernetesMaintenanceLeaseAdapter,
    KubernetesVaultFingerprintAdapter,
    KubernetesVolumeAdapter,
    PrivateCellApiAdapter,
    TraefikRoutingAdapter,
)
from exomem_provisioner.lifecycle import (
    HealthObservation,
    LifecycleConfig,
    MetadataConflict,
    OpaqueProviderMetadata,
    RecordedVolume,
)
from exomem_provisioner.provider_identity import (
    ProviderRecoveryIdentityCodec,
    ProviderReference,
    cell_provider_recovery_envelopes,
    chunk_hcloud_identity_envelope,
)


def _metadata(**overrides: object) -> OpaqueProviderMetadata:
    values: dict[str, object] = {
        "tenant_id": "tenant-alpha",
        "subject_id": "cell-alpha",
        "operation_id": "operation-alpha",
        "fence_generation": 7,
    }
    values.update(overrides)
    return OpaqueProviderMetadata(**values)  # type: ignore[arg-type]


def _credential(offset: int = 0) -> str:
    raw = bytes((index + offset) % 256 for index in range(32))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class _KubernetesCore:
    def __init__(self, metadata: OpaqueProviderMetadata) -> None:
        annotations = metadata.kubernetes_annotations
        self.pvc = SimpleNamespace(
            metadata=SimpleNamespace(annotations=annotations),
            spec=SimpleNamespace(volume_name="pv-alpha"),
        )
        self.pv = SimpleNamespace(
            metadata=SimpleNamespace(name="pv-alpha", annotations={}),
            spec=SimpleNamespace(
                csi=SimpleNamespace(volume_handle="42"),
                node_affinity=SimpleNamespace(
                    required=SimpleNamespace(
                        node_selector_terms=[
                            SimpleNamespace(
                                match_expressions=[
                                    SimpleNamespace(
                                        key="topology.kubernetes.io/zone",
                                        values=["fsn1"],
                                    )
                                ]
                            )
                        ]
                    )
                ),
            ),
        )
        self.created_pvs: list[dict[str, object]] = []
        self.created_pvcs: list[tuple[str, dict[str, object]]] = []
        self.deleted_pvcs: list[tuple[str, str]] = []
        self.deleted_pvs: list[str] = []
        self.persistent_volume_patches: list[tuple[str, dict[str, object]]] = []

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str):
        assert name.endswith("-data")
        assert namespace.startswith("exo-")
        if (name, namespace) in self.deleted_pvcs:
            raise _ApiNotFound()
        return self.pvc

    def read_persistent_volume(self, name: str):
        if name in self.deleted_pvs:
            raise _ApiNotFound()
        assert name == "pv-alpha"
        return self.pv

    def patch_persistent_volume(self, name: str, body: dict[str, object]):
        assert name == "pv-alpha"
        self.persistent_volume_patches.append((name, body))
        self.pv.metadata.annotations.update(body["metadata"]["annotations"])

    def create_persistent_volume(self, body: dict[str, object]):
        self.created_pvs.append(body)

    def create_namespaced_persistent_volume_claim(self, namespace: str, body: dict[str, object]):
        self.created_pvcs.append((namespace, body))

    def delete_persistent_volume(self, name: str, body: dict[str, object]):
        assert body["propagationPolicy"] == "Foreground"
        self.deleted_pvs.append(name)

    def delete_namespaced_persistent_volume_claim(
        self, name: str, namespace: str, body: dict[str, object]
    ) -> None:
        assert body["propagationPolicy"] == "Foreground"
        self.deleted_pvcs.append((name, namespace))


class _ApiNotFound(Exception):
    status = 404


class _ApiConflict(Exception):
    status = 409


@pytest.mark.asyncio
async def test_kubernetes_adapter_discovers_csi_handle_then_labels_with_full_identity() -> None:
    metadata = _metadata()
    core = _KubernetesCore(metadata)
    adapter = KubernetesVolumeAdapter(
        core_v1=core,
        storage_class_name="encrypted-retain",
        encryption_secret_name="volume-encryption",
        encryption_secret_namespace="exomem-platform",
    )

    recorded = await adapter.discover_bound_volume(metadata)

    assert recorded == RecordedVolume("42", "pv-alpha", "fsn1", metadata)
    assert core.pv.metadata.annotations == {}
    assert core.persistent_volume_patches == []

    await adapter.label_bound_volume(recorded, "sealed-pv-envelope")
    assert core.persistent_volume_patches == [
        (
            "pv-alpha",
            {
                "metadata": {
                    "labels": {
                        "exomem.io/resource-name": metadata.resource_name,
                    },
                    "annotations": {
                        **metadata.kubernetes_annotations,
                        "exomem.io/recovery-envelope": "sealed-pv-envelope",
                    },
                }
            },
        )
    ]

    await adapter.create_static_binding(recorded)
    assert core.created_pvs[0]["spec"]["csi"]["volumeHandle"] == "42"
    assert core.created_pvs[0]["spec"]["csi"]["nodePublishSecretRef"] == {
        "name": "volume-encryption",
        "namespace": "exomem-platform",
    }
    assert core.created_pvs[0]["spec"]["claimRef"] == {
        "name": _metadata().resource_name + "-data",
        "namespace": _metadata().resource_name,
    }
    assert core.created_pvs[0]["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert core.created_pvcs[0][1]["spec"]["volumeName"] == "pv-alpha"

    await adapter.delete_claim(recorded)
    assert await adapter.claim_absent(recorded) is True
    await adapter.delete_pv("pv-alpha")
    assert await adapter.pv_absent("pv-alpha") is True


@pytest.mark.asyncio
async def test_kubernetes_adapter_rejects_pvc_identity_or_location_without_mutation() -> None:
    metadata = _metadata()
    core = _KubernetesCore(metadata)
    core.pvc.metadata.annotations["exomem.io/fence"] = "8"
    adapter = KubernetesVolumeAdapter(
        core_v1=core,
        storage_class_name="encrypted-retain",
        encryption_secret_name="volume-encryption",
        encryption_secret_namespace="exomem-platform",
    )

    with pytest.raises(MetadataConflict):
        await adapter.discover_bound_volume(metadata)
    assert core.pv.metadata.annotations == {}


@pytest.mark.asyncio
async def test_recovery_bound_volume_digest_changes_for_pv_replacement() -> None:
    metadata = _metadata()
    core = _KubernetesCore(metadata)
    core.pv.metadata.annotations = {
        **metadata.kubernetes_annotations,
        "exomem.io/recovery-envelope": "sealed-pv-envelope",
    }
    core.pv.metadata.uid = "pv-uid-one"
    core.pv.metadata.resource_version = "11"
    adapter = KubernetesVolumeAdapter(
        core_v1=core,
        storage_class_name="encrypted-retain",
        encryption_secret_name="volume-encryption",
        encryption_secret_namespace="exomem-platform",
    )

    first = await adapter.observe_recovery_bound_volume(metadata)
    assert first is not None
    core.pv.metadata.uid = "pv-uid-two"
    core.pv.metadata.resource_version = "12"
    second = await adapter.observe_recovery_bound_volume(metadata)

    assert second is not None
    assert first.stability_digest != second.stability_digest


@pytest.mark.asyncio
async def test_cell_adapter_creates_external_secret_then_reads_the_exact_bundle() -> None:
    metadata = _metadata()
    identity = ProviderRecoveryIdentityCodec.from_secret("credential-secret-recovery")
    envelope = identity.seal(
        provider="kubernetes",
        provider_reference=ProviderReference.kubernetes(
            provider="kubernetes",
            api_version="v1",
            kind="Secret",
            namespace=metadata.resource_name,
            name="exomem-cell-credentials",
        ),
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
    )

    class Core:
        secret = None

        def patch_namespaced_secret(self, name, namespace, body):
            if self.secret is None:
                raise _ApiNotFound()
            self.secret = SimpleNamespace(
                metadata=SimpleNamespace(annotations=body["metadata"]["annotations"]),
                data={
                    "credentials.json": base64.b64encode(
                        body["stringData"]["credentials.json"].encode()
                    ).decode()
                },
            )

        def create_namespaced_secret(self, namespace, body):
            assert namespace == metadata.resource_name
            assert body["metadata"]["name"] == "exomem-cell-credentials"
            assert body["metadata"]["labels"] == {
                "exomem.io/cell": metadata.resource_name,
                "exomem.io/resource-name": metadata.resource_name,
            }
            self.patch_namespaced_secret = lambda name, namespace, update: setattr(
                self,
                "secret",
                SimpleNamespace(
                    metadata=SimpleNamespace(annotations=update["metadata"]["annotations"]),
                    data={
                        "credentials.json": base64.b64encode(
                            update["stringData"]["credentials.json"].encode()
                        ).decode()
                    },
                ),
            )
            self.secret = SimpleNamespace(
                metadata=SimpleNamespace(annotations=body["metadata"]["annotations"]),
                data={
                    "credentials.json": base64.b64encode(
                        body["stringData"]["credentials.json"].encode()
                    ).decode()
                },
            )

        def read_namespaced_secret(self, name, namespace):
            return self.secret

    core = Core()
    adapter = KubernetesCellAdapter(
        core_v1=core,
        apps_v1=SimpleNamespace(),
        identity_verifier=identity.verifier(),
    )
    await adapter.write_credential_bundle(
        metadata,
        {"1": _credential()},
        lifecycle_annotations={
            "exomem.io/security-revision": "1",
            "exomem.io/recovery-envelope": envelope,
        },
    )

    credentials, annotations = await adapter.read_credential_bundle(metadata)
    assert credentials == {"1": _credential()}
    assert annotations["exomem.io/security-revision"] == "1"
    core.secret.metadata.annotations["exomem.io/recovery-envelope"] = "forged"
    with pytest.raises(MetadataConflict, match="did not authenticate"):
        await adapter.read_credential_bundle(metadata)


@pytest.mark.asyncio
async def test_maintenance_lease_rejects_concurrent_owner_and_releases_with_precondition() -> None:
    metadata = _metadata()
    now = datetime(2030, 1, 1, tzinfo=UTC)

    class Coordination:
        lease = None
        deleted = None

        def read_namespaced_lease(self, name, namespace):
            if self.lease is None:
                raise _ApiNotFound()
            return self.lease

        def create_namespaced_lease(self, namespace, body):
            if self.lease is not None:
                raise _ApiConflict()
            self.lease = SimpleNamespace(
                metadata=SimpleNamespace(
                    annotations=body["metadata"]["annotations"],
                    resource_version="1",
                    uid="lease-uid",
                ),
                spec=SimpleNamespace(
                    holder_identity=body["spec"]["holderIdentity"],
                    renew_time=body["spec"]["renewTime"],
                    lease_duration_seconds=body["spec"]["leaseDurationSeconds"],
                ),
            )

        def replace_namespaced_lease(self, name, namespace, body):
            self.lease.metadata.annotations = body["metadata"]["annotations"]
            self.lease.spec.holder_identity = body["spec"]["holderIdentity"]
            self.lease.spec.renew_time = body["spec"]["renewTime"]

        def delete_namespaced_lease(self, name, namespace, *, body):
            self.deleted = body
            self.lease = None

    coordination = Coordination()
    adapter = KubernetesMaintenanceLeaseAdapter(coordination_v1=coordination, now=lambda: now)
    assert await adapter.acquire(metadata, "operation-one") is True
    assert await adapter.acquire(metadata, "operation-two") is False
    await adapter.release(metadata, "operation-two")
    assert coordination.lease is not None
    await adapter.release(metadata, "operation-one")
    assert coordination.lease is None
    assert coordination.deleted == {"preconditions": {"uid": "lease-uid", "resourceVersion": "1"}}


@pytest.mark.asyncio
async def test_maintenance_lease_allows_expired_owner_takeover_with_new_fence() -> None:
    original = _metadata(operation_id="operation-one", fence_generation=7)
    replacement = _metadata(operation_id="operation-two", fence_generation=8)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    lease = SimpleNamespace(
        metadata=SimpleNamespace(
            annotations=original.kubernetes_annotations,
            resource_version="3",
            uid="lease-uid",
        ),
        spec=SimpleNamespace(
            holder_identity="operation-one",
            renew_time=now - timedelta(seconds=121),
            lease_duration_seconds=120,
        ),
    )

    class Coordination:
        def read_namespaced_lease(self, name, namespace):
            return lease

        def replace_namespaced_lease(self, name, namespace, body):
            lease.metadata.annotations = body["metadata"]["annotations"]
            lease.spec.holder_identity = body["spec"]["holderIdentity"]
            lease.spec.renew_time = body["spec"]["renewTime"]

    adapter = KubernetesMaintenanceLeaseAdapter(coordination_v1=Coordination(), now=lambda: now)
    assert await adapter.acquire(replacement, "operation-two") is True
    assert lease.spec.holder_identity == "operation-two"
    assert lease.metadata.annotations == replacement.kubernetes_annotations


class _HCloudVolumes:
    def __init__(self) -> None:
        self.volume = SimpleNamespace(
            id=42,
            labels={},
            location=SimpleNamespace(name="fsn1"),
        )
        self.deleted = False

    def get_by_id(self, volume_id: int):
        if self.deleted or volume_id != 42:
            return None
        return self.volume

    def update(self, volume, *, labels: dict[str, str]):
        assert volume is self.volume
        volume.labels = dict(labels)
        return volume

    def delete(self, volume):
        assert volume is self.volume
        self.deleted = True

    def get_all(self, *, label_selector: str):
        key, expected = label_selector.split("=")
        if not self.deleted and self.volume.labels.get(key) == expected:
            return [self.volume]
        return []


@pytest.mark.asyncio
async def test_hcloud_adapter_applies_exact_immutable_labels_and_proves_absence() -> None:
    volumes = _HCloudVolumes()
    adapter = HCloudVolumeAdapter(client=SimpleNamespace(volumes=volumes))
    metadata = _metadata()

    await adapter.label_volume("42", metadata)
    assert await adapter.verify_volume("42", metadata, "fsn1") is True
    assert await adapter.discover_tenant_volumes("tenant-alpha") == ("42",)
    assert "tenant-alpha" not in repr(adapter)

    with pytest.raises(MetadataConflict):
        await adapter.label_volume("42", _metadata(operation_id="operation-other"))

    await adapter.quarantine_volume("42")
    assert volumes.volume.labels["exomem_quarantine"] == "true"
    await adapter.delete_volume("42")
    assert await adapter.volume_absent("42") is True


@pytest.mark.asyncio
async def test_hcloud_adapter_rejects_replacing_an_unauthenticated_recovery_identity() -> None:
    volumes = _HCloudVolumes()
    metadata = _metadata()
    trusted = ProviderRecoveryIdentityCodec.from_secret("trusted-provider-recovery-root")
    forged = ProviderRecoveryIdentityCodec.from_secret("forged-provider-recovery-root")
    reference = ProviderReference.hcloud(kind="volume", resource_id=42)
    forged_envelope = forged.seal(
        provider="hcloud",
        provider_reference=reference,
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
    )
    volumes.volume.labels = {
        **metadata.hcloud_labels,
        **chunk_hcloud_identity_envelope(forged_envelope),
    }
    adapter = HCloudVolumeAdapter(
        client=SimpleNamespace(volumes=volumes),
        identity_verifier=trusted.verifier(),
    )
    trusted_envelope = trusted.seal(
        provider="hcloud",
        provider_reference=reference,
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
    )

    with pytest.raises(MetadataConflict, match="recovery identity"):
        await adapter.label_volume("42", metadata, trusted_envelope)

    assert volumes.volume.labels == {
        **metadata.hcloud_labels,
        **chunk_hcloud_identity_envelope(forged_envelope),
    }


@pytest.mark.asyncio
async def test_helm_cli_checks_pinned_version_and_uses_private_values_file(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    rendered_values: list[dict[str, object]] = []

    async def runner(argv: tuple[str, ...], environment: dict[str, str]) -> SimpleNamespace:
        calls.append(argv)
        assert environment == {"HELM_DRIVER": "configmap"}
        if argv[1] == "version":
            return SimpleNamespace(returncode=0, stdout="v3.19.4\n", stderr="")
        values_path = Path(argv[argv.index("--values") + 1])
        assert values_path.stat().st_mode & 0o777 == 0o600
        rendered_values.append(json.loads(values_path.read_text()))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    adapter = HelmCliAdapter(
        binary="/opt/hosted/bin/helm",
        expected_version="3.19.4",
        chart_path="/opt/hosted/charts/cell",
        chart_version="0.1.0",
        runner=runner,
        temporary_directory=tmp_path,
    )
    values = {"resourceName": _metadata().resource_name, "image": "repo@sha256:" + "a" * 64}

    await adapter.ensure_release(_metadata(), values)

    assert calls[0] == (
        "/opt/hosted/bin/helm",
        "version",
        "--template",
        "{{.Version}}",
    )
    assert "--version" in calls[1] and calls[1][calls[1].index("--version") + 1] == "0.1.0"
    labels = calls[1][calls[1].index("--labels") + 1]
    assert labels == ",".join(
        f"{key}={value}" for key, value in sorted(_metadata().hcloud_labels.items())
    )
    assert rendered_values == [values]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_helm_cli_rejects_secret_values_and_version_drift(tmp_path: Path) -> None:
    async def wrong_version(_argv: tuple[str, ...], environment: dict[str, str]) -> SimpleNamespace:
        assert environment == {"HELM_DRIVER": "configmap"}
        return SimpleNamespace(returncode=0, stdout="v3.18.0\n", stderr="")

    adapter = HelmCliAdapter(
        binary="helm",
        expected_version="3.19.4",
        chart_path="chart",
        chart_version="0.1.0",
        runner=wrong_version,
        temporary_directory=tmp_path,
    )
    with pytest.raises(MetadataConflict):
        await adapter.ensure_release(_metadata(), {"serviceCredential": "secret-sentinel"})
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_helm_runtime_transition_replays_and_rolls_back_the_original_revision(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    current_values: dict[str, object] = {"workloadMode": "serve", "image": "old"}
    revision_values: dict[int, dict[str, object]] = {1: dict(current_values)}
    current_revision = 1

    async def runner(argv: tuple[str, ...], environment: dict[str, str]) -> SimpleNamespace:
        nonlocal current_revision, current_values
        calls.append(argv)
        assert environment == {"HELM_DRIVER": "configmap"}
        if argv[1] == "version":
            return SimpleNamespace(returncode=0, stdout="v3.19.4\n", stderr="")
        if argv[1:3] == ("get", "values"):
            selected = (
                revision_values[int(argv[argv.index("--revision") + 1])]
                if "--revision" in argv
                else current_values
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(selected), stderr="")
        if argv[1] == "history":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "revision": revision,
                            "status": "deployed" if revision == current_revision else "superseded",
                        }
                        for revision in sorted(revision_values)
                    ]
                ),
                stderr="",
            )
        if argv[1] == "upgrade":
            values_path = Path(argv[argv.index("--values") + 1])
            current_values = json.loads(values_path.read_text(encoding="utf-8"))
            current_revision += 1
            revision_values[current_revision] = dict(current_values)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "rollback":
            assert argv[3] == "1"
            current_values = dict(revision_values[1])
            current_revision += 1
            revision_values[current_revision] = dict(current_values)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    adapter = HelmCliAdapter(
        binary="helm",
        expected_version="3.19.4",
        chart_path="chart",
        chart_version="0.1.0",
        runner=runner,
        temporary_directory=tmp_path,
    )
    desired = {"workloadMode": "serve", "image": "target"}

    await adapter.transition_release(_metadata(), desired, operation_id="rollforward-alpha")
    await adapter.transition_release(_metadata(), desired, operation_id="rollforward-alpha")

    assert current_values["runtimeUpgrade"] == {
        "schemaVersion": 1,
        "operationDigest": hashlib.sha256(b"rollforward-alpha").hexdigest(),
        "priorRevision": 1,
    }
    route_reopened = dict(current_values)
    route_reopened["routeReopened"] = True
    await adapter.ensure_release(_metadata(), route_reopened)
    assert sum(call[1] == "upgrade" for call in calls) == 2

    await adapter.rollback_release(_metadata(), operation_id="rollforward-alpha")

    assert current_values == {"workloadMode": "serve", "image": "old"}
    rollback = next(call for call in calls if call[1] == "rollback")
    assert rollback[2:4] == (_metadata().resource_name, "1")
    assert "--wait" in rollback and "--cleanup-on-fail" in rollback

    await adapter.rollback_release(
        _metadata(), operation_id="rollforward-alpha", require_marker=True
    )
    assert sum(call[1] == "rollback" for call in calls) == 1

    with pytest.raises(MetadataConflict, match="rollback authority is absent"):
        await adapter.rollback_release(
            _metadata(), operation_id="different-operation", require_marker=True
        )


class _CustomObjects:
    def __init__(self) -> None:
        self.applied: list[dict[str, object]] = []
        self.deleted: list[str] = []

    def patch_namespaced_custom_object(self, **kwargs: object):
        self.applied.append(kwargs["body"])  # type: ignore[arg-type]

    def delete_namespaced_custom_object(self, **kwargs: object):
        self.deleted.append(str(kwargs["name"]))


@pytest.mark.asyncio
async def test_traefik_adapter_closes_and_reopens_only_exact_routes() -> None:
    custom = _CustomObjects()
    probes: list[tuple[str, str, dict[str, str]]] = []

    async def probe(method: str, url: str, headers: dict[str, str]) -> int:
        probes.append((method, url, headers))
        return 404

    adapter = TraefikRoutingAdapter(
        custom_objects=custom,
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=probe,
    )

    await adapter.disable(_metadata())
    assert sorted(custom.deleted) == [
        _metadata().resource_name + "-control",
        _metadata().resource_name + "-transfer",
    ]
    assert (
        await adapter.prove_rejected(
            _metadata(),
            unused_ticket="ticket-unused",
            browser_origin="https://substratesystems.io",
            control_credential="credential-current",
            protocol_version="1",
        )
        is True
    )
    assert len(probes) == 3
    assert probes[0][0:2] == (
        "GET",
        "https://control.example.invalid/cells/cell-alpha/private/exomem/v1/ready",
    )
    assert probes[0][2]["Authorization"] == "Bearer credential-current"
    assert probes[0][2]["X-Exomem-Cell-Id"] == "cell-alpha"
    assert probes[0][2]["X-Exomem-Protocol-Version"] == "1"
    assert probes[0][2]["X-Exomem-Principal-Scope"]
    assert probes[0][2]["X-Exomem-Request-Id"].count("-") == 4
    assert probes[1][0] == "OPTIONS"
    assert probes[1][1].endswith("/public/exomem/v2/transfers/download")
    assert probes[1][2] == {
        "Access-Control-Request-Headers": "X-Exomem-Transfer-Grant",
        "Access-Control-Request-Method": "GET",
        "Origin": "https://substratesystems.io",
    }
    assert probes[2][0] == "GET"
    assert probes[2][1] == probes[1][1]
    assert "ticket-unused" in probes[2][2].values()
    assert probes[2][2]["Origin"] == "https://substratesystems.io"
    assert not any(key.startswith("CF-Access-") for key in probes[2][2])

    await adapter.enable(_metadata())
    rendered = json.dumps(custom.applied, sort_keys=True)
    assert '"stripPrefix": {"prefixes": ["/cells/cell-alpha"]}' in rendered
    assert f"/cells/{_metadata().subject_id}/private/exomem/v1" in rendered
    assert f"/cells/{_metadata().subject_id}/public/exomem/v2/transfers/upload" in rendered
    assert "upstream" not in rendered.lower()
    assert "namespaceSelector" not in rendered


@pytest.mark.asyncio
async def test_traefik_rejection_proof_rejects_a_missing_target_on_an_open_route() -> None:
    async def probe(method: str, _url: str, _headers: dict[str, str]) -> int:
        return 204 if method == "OPTIONS" else 404

    adapter = TraefikRoutingAdapter(
        custom_objects=_CustomObjects(),
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=probe,
    )

    assert (
        await adapter.prove_rejected(
            _metadata(),
            unused_ticket="ticket-unused",
            browser_origin="https://substratesystems.io",
            control_credential="credential-current",
            protocol_version="1",
        )
        is False
    )


@pytest.mark.asyncio
async def test_traefik_rejection_proof_rejects_ticket_failures_on_an_open_route() -> None:
    async def probe(method: str, _url: str, _headers: dict[str, str]) -> int:
        return 204 if method == "OPTIONS" else 401

    adapter = TraefikRoutingAdapter(
        custom_objects=_CustomObjects(),
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=probe,
    )

    assert (
        await adapter.prove_rejected(
            _metadata(),
            unused_ticket="ticket-unused",
            browser_origin="https://substratesystems.io",
            control_credential="credential-current",
            protocol_version="1",
        )
        is False
    )


@pytest.mark.asyncio
async def test_kubernetes_cell_adapter_scales_and_writes_atomic_bundle_shape() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Core:
        def patch_namespaced_secret(
            self, name: str, namespace: str, body: dict[str, object]
        ) -> None:
            calls.append((name, namespace, body))

    class Apps:
        def patch_namespaced_stateful_set_scale(
            self, name: str, namespace: str, body: dict[str, object]
        ) -> None:
            calls.append((name, namespace, body))

    adapter = KubernetesCellAdapter(core_v1=Core(), apps_v1=Apps())
    await adapter.write_credential_bundle(
        _metadata(),
        {"1": _credential(), "2": _credential(32)},
    )
    await adapter.scale(_metadata(), 0)

    secret = calls[0][2]
    assert calls[0][0] == "exomem-cell-credentials"
    bundle = json.loads(secret["stringData"]["credentials.json"])  # type: ignore[index]
    assert bundle == {
        "credentials": {"1": _credential(), "2": _credential(32)},
        "schema_version": 1,
    }
    assert calls[1][2] == {"spec": {"replicas": 0}}
    assert _credential() not in repr(adapter)

    with pytest.raises(MetadataConflict):
        await adapter.write_credential_bundle(_metadata(), {"3": "a" * 43})


@pytest.mark.asyncio
async def test_kubernetes_cell_adapter_persists_one_authenticated_authorization_bundle() -> None:
    metadata = _metadata()
    identity = ProviderRecoveryIdentityCodec.from_secret("provider-root")
    envelopes = cell_provider_recovery_envelopes(
        identity,
        tenant_id=metadata.tenant_id,
        cell_id=metadata.subject_id,
        operation_id=metadata.operation_id,
        fence_generation=metadata.fence_generation,
        resource_name=metadata.resource_name,
        operation_resource_name="exo-op-0123456789abcdef0123",
    )

    class Core:
        secret = None

        def patch_namespaced_secret(self, name, namespace, body):
            if self.secret is None:
                raise _ApiNotFound()
            if "resourceVersion" in body["metadata"]:
                assert body["metadata"]["resourceVersion"] == "7"
            self.secret = _secret(body)

        def create_namespaced_secret(self, namespace, body):
            self.secret = _secret(body)

        def read_namespaced_secret(self, name, namespace):
            if self.secret is None:
                raise _ApiNotFound()
            return self.secret

    def _secret(body):
        assert body["metadata"]["name"] == "exomem-authorization-session"
        assert set(body["stringData"]) == {
            "keyring.json",
            "control.json",
            "serving-membership.json",
        }
        return SimpleNamespace(
            metadata=SimpleNamespace(
                annotations=body["metadata"]["annotations"],
                resource_version="7",
            ),
            data={
                key: base64.b64encode(value.encode()).decode()
                for key, value in body["stringData"].items()
            },
        )

    class Apps:
        staged = None

        def patch_namespaced_stateful_set(self, name, namespace, body):
            self.staged = (name, namespace, body)

    core = Core()
    apps = Apps()
    adapter = KubernetesCellAdapter(
        core_v1=core,
        apps_v1=apps,
        identity_verifier=identity.verifier(),
    )
    files = {
        "keyring.json": b'{"keyring":"value"}',
        "control.json": b'{"control":"value"}',
        "serving-membership.json": b'{"membership":"value"}',
    }
    revision = hashlib.sha256(
        files["keyring.json"] + files["control.json"] + files["serving-membership.json"]
    ).hexdigest()
    assert await adapter.read_authorization_session_bundle(metadata) is None
    await adapter.write_authorization_session_bundle(
        metadata,
        files,
        recovery_envelope=envelopes["authorizationSessionSecret"],
        membership_epoch=1,
        membership_digest="a" * 64,
        revision=revision,
    )

    stored = await adapter.read_authorization_session_bundle(metadata)
    assert stored == files
    assert (
        core.secret.metadata.annotations["exomem.io/recovery-envelope"]
        == envelopes["authorizationSessionSecret"]
    )

    successor_files = {
        **files,
        "serving-membership.json": b'{"membership":"successor"}',
    }
    successor_revision = hashlib.sha256(
        successor_files["keyring.json"]
        + successor_files["control.json"]
        + successor_files["serving-membership.json"]
    ).hexdigest()
    await adapter.write_authorization_session_bundle(
        metadata,
        successor_files,
        recovery_envelope=envelopes["authorizationSessionSecret"],
        membership_epoch=2,
        membership_digest="c" * 64,
        revision=successor_revision,
        expected_revision=revision,
    )
    await adapter.stage_authorization_session_revision(metadata, successor_revision)
    assert apps.staged == (
        metadata.resource_name,
        metadata.resource_name,
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "exomem.io/authorization-session-revision": successor_revision
                        }
                    }
                }
            }
        },
    )

    with pytest.raises(MetadataConflict, match="predecessor differs"):
        await adapter.write_authorization_session_bundle(
            metadata,
            successor_files,
            recovery_envelope=envelopes["authorizationSessionSecret"],
            membership_epoch=3,
            membership_digest="e" * 64,
            revision=successor_revision,
            expected_revision=revision,
        )

    core.secret.data["extra.json"] = base64.b64encode(b"{}").decode()
    with pytest.raises(MetadataConflict, match="bundle shape"):
        await adapter.read_authorization_session_bundle(metadata)


@pytest.mark.asyncio
async def test_kubernetes_fingerprint_job_is_read_only_bounded_and_content_free() -> None:
    metadata = _metadata()
    image = "registry.example/exomem-provisioner@sha256:" + "a" * 64
    created: list[dict[str, object]] = []

    class Batch:
        def read_namespaced_job(self, name: str, namespace: str):
            if not created:
                raise _ApiNotFound()
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    annotations=created[0]["metadata"]["annotations"],  # type: ignore[index]
                    deletion_timestamp=None,
                ),
                spec=SimpleNamespace(
                    template=SimpleNamespace(
                        spec=SimpleNamespace(
                            containers=[
                                SimpleNamespace(
                                    image=image,
                                    args=["exomem-provisioner-vault-fingerprint"],
                                )
                            ]
                        )
                    )
                ),
                status=SimpleNamespace(succeeded=1, failed=0),
            )

        def create_namespaced_job(self, namespace: str, body: dict[str, object]) -> None:
            assert namespace == metadata.resource_name
            created.append(body)

        def delete_namespaced_job(self, name: str, namespace: str, *, body: object) -> None:
            assert name == metadata.resource_name + "-init"
            assert namespace == metadata.resource_name
            assert body == {"propagationPolicy": "Foreground"}

    record = json.dumps(
        {
            "artifact": "exomem-hosted-vault-fingerprint",
            "schemaVersion": 1,
            "sha256": "b" * 64,
        },
        separators=(",", ":"),
    )

    class Core:
        def list_namespaced_pod(self, namespace: str, *, label_selector: str):
            assert namespace == metadata.resource_name
            assert label_selector == f"job-name={metadata.resource_name}-init"
            terminated = SimpleNamespace(exit_code=0, message=record)
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        status=SimpleNamespace(
                            container_statuses=[
                                SimpleNamespace(state=SimpleNamespace(terminated=terminated))
                            ]
                        )
                    )
                ]
            )

    adapter = KubernetesVaultFingerprintAdapter(
        core_v1=Core(),
        batch_v1=Batch(),
        image=image,
        sleep=lambda _seconds: None,
    )

    digest = await adapter.fingerprint(
        metadata,
        operation_id="rollforward-alpha",
        phase="before",
        recovery_envelope="signed-init-job-envelope",
    )

    assert digest == "b" * 64
    job = created[0]
    assert job["metadata"]["name"] == metadata.resource_name + "-init"  # type: ignore[index]
    assert job["metadata"]["annotations"]["exomem.io/recovery-envelope"] == (  # type: ignore[index]
        "signed-init-job-envelope"
    )
    pod = job["spec"]["template"]["spec"]  # type: ignore[index]
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert container["image"] == image
    assert container["args"] == ["exomem-provisioner-vault-fingerprint"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["volumeMounts"] == [
        {
            "name": "data",
            "mountPath": "/var/lib/exomem/vault",
            "subPath": "vault",
            "readOnly": True,
        }
    ]
    assert pod["volumes"] == [
        {
            "name": "data",
            "persistentVolumeClaim": {"claimName": metadata.resource_name + "-data"},
        }
    ]


@pytest.mark.asyncio
async def test_kubernetes_fingerprint_rejects_untrusted_or_leaky_result() -> None:
    metadata = _metadata()
    image = "registry.example/exomem-provisioner@sha256:" + "a" * 64

    class Batch:
        def read_namespaced_job(self, name: str, namespace: str):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    annotations={
                        **metadata.kubernetes_annotations,
                        "exomem.io/recovery-envelope": "signed",
                        "exomem.io/vault-fingerprint-phase": "before",
                        "exomem.io/vault-fingerprint-operation": hashlib.sha256(
                            b"rollforward-alpha"
                        ).hexdigest(),
                    },
                    deletion_timestamp=None,
                ),
                spec=SimpleNamespace(
                    template=SimpleNamespace(
                        spec=SimpleNamespace(
                            containers=[
                                SimpleNamespace(
                                    image=image,
                                    args=["exomem-provisioner-vault-fingerprint"],
                                )
                            ]
                        )
                    )
                ),
                status=SimpleNamespace(succeeded=1, failed=0),
            )

        def delete_namespaced_job(self, name: str, namespace: str, body: object) -> None:
            raise AssertionError("invalid result must not be accepted or deleted")

    class Core:
        def list_namespaced_pod(self, namespace: str, *, label_selector: str):
            leaked = json.dumps(
                {
                    "artifact": "exomem-hosted-vault-fingerprint",
                    "schemaVersion": 1,
                    "sha256": "b" * 64,
                    "path": "private.md",
                }
            )
            terminated = SimpleNamespace(exit_code=0, message=leaked)
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        status=SimpleNamespace(
                            container_statuses=[
                                SimpleNamespace(state=SimpleNamespace(terminated=terminated))
                            ]
                        )
                    )
                ]
            )

    adapter = KubernetesVaultFingerprintAdapter(
        core_v1=Core(), batch_v1=Batch(), image=image, sleep=lambda _seconds: None
    )
    with pytest.raises(MetadataConflict, match="fingerprint result"):
        await adapter.fingerprint(
            metadata,
            operation_id="rollforward-alpha",
            phase="before",
            recovery_envelope="signed",
        )


class _Response:
    def __init__(self, status_code: int, data: dict[str, object], *, raw: bool = False) -> None:
        self.status_code = status_code
        self._data = data
        self._raw = raw

    def json(self) -> dict[str, object]:
        return self._data if self._raw else {"success": True, "data": self._data}


@pytest.mark.asyncio
async def test_private_cell_api_uses_fresh_identity_and_exact_lifecycle_routes() -> None:
    calls: list[tuple[str, str, dict[str, str], object]] = []
    worker_policy = {"workerCount": 2, "semantic": True, "media": False}
    ready = {
        "cell_id": "cell-alpha",
        "vault_id": "tenant-alpha",
        "exomem_release": "0.22.0",
        "hosted_protocol": "1",
        "authenticated_credential_version": "1",
        "security_revision": 1,
        "service_authenticated": True,
        "mutation_authority": True,
        "admission_phase": "active",
        "read_admission": True,
        "write_admission": True,
        "worker_policy_digest": hashlib.sha256(
            json.dumps(worker_policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }

    async def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: object = None,
    ) -> _Response:
        calls.append((method, url, headers, json))
        if url.endswith("/contract"):
            return _Response(
                200,
                {"digest": {"algorithm": "sha256", "value": "b" * 64}},
                raw=True,
            )
        if url.endswith("/ready"):
            return _Response(200, ready)
        if url.endswith("/lifecycle/quiesce"):
            return _Response(
                200,
                {
                    "phase": "quiesced",
                    "active_reads": 0,
                    "active_mutations": 0,
                    "active_transfers": 0,
                    "reason_code": "HOSTED_QUIESCED",
                },
            )
        return _Response(
            200,
            {"live": True, "cell_id": "cell-alpha", "protocol_version": "1"},
        )

    config = LifecycleConfig(
        image="repo@sha256:" + "a" * 64,
        chart_path="chart",
        chart_version="0.1.0",
        helm_version="3.19.4",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        browser_origin="https://substratesystems.io",
        release_version="0.22.0",
        protocol_version="1",
        contract_digest="e" * 64,
        location="fsn1",
        runtime_target={
            "releaseVersion": "0.22.0",
            "protocolVersion": "1",
            "agentProfile": "hosted-alpha-agent-v1",
            "gatewayContractDigest": "b" * 64,
            "commandFingerprint": "c" * 64,
            "schemaDigest": "d" * 64,
        },
        legacy_runtime_units={
            ("0.22.0", "1"): {
                "releaseVersion": "0.22.0",
                "protocolVersion": "1",
                "agentProfile": "hosted-alpha-agent-v1",
                "gatewayContractDigest": "b" * 64,
                "commandFingerprint": "c" * 64,
                "schemaDigest": "d" * 64,
                "runtimeImage": "repo@sha256:" + "a" * 64,
                "sourceCommit": "a" * 40,
            }
        },
    )
    adapter = PrivateCellApiAdapter(
        request=request,
        internal_origin="http://traefik.exomem-platform.svc.cluster.local",
    )
    health = await adapter.health(
        _metadata(),
        credential=_credential(),
        protocol_version="1",
        config=config,
        expected_release="0.22.0",
        expected_worker_policy=worker_policy,
        expected_contract_digest="b" * 64,
    )
    quiesced = await adapter.quiesce(
        _metadata(),
        credential=_credential(),
        protocol_version="1",
        operation_id="quiesce-alpha",
    )
    assert quiesced == {
        "phase": "quiesced",
        "active_reads": 0,
        "active_mutations": 0,
        "active_transfers": 0,
        "reason_code": "HOSTED_QUIESCED",
    }
    await adapter.resume(
        _metadata(),
        credential=_credential(),
        protocol_version="1",
        operation_id="resume-alpha",
    )
    await adapter.seal(
        _metadata(),
        credential=_credential(),
        protocol_version="1",
        operation_id="seal-alpha",
        created_at="2030-01-01T00:00:00Z",
    )

    assert health == HealthObservation(
        live=True,
        ready=True,
        cell_id="cell-alpha",
        protocol_version="1",
        release_version="0.22.0",
        service_authenticated=True,
        mutation_authority=True,
        read_admission=True,
        write_admission=True,
        worker_policy={"workerCount": 2, "semantic": True, "media": False},
        code="CELL_READY",
        contract_digest="b" * 64,
        policy_admitted=True,
        admission_admitted=True,
    )
    request_ids = [call[2]["X-Exomem-Request-Id"] for call in calls]
    assert len(request_ids) == len(set(request_ids))
    assert all(call[2]["Authorization"] == "Bearer " + _credential() for call in calls)
    assert calls[-3][1].endswith("/private/exomem/v1/lifecycle/quiesce")
    assert calls[-3][2]["X-Exomem-Routing-Stopped"] == "true"
    assert calls[-2][1].endswith("/private/exomem/v1/lifecycle/resume")
    assert calls[-1][1].endswith("/private/exomem/v1/lifecycle/seal")
    assert calls[-1][2]["X-Exomem-Routing-Stopped"] == "true"
    assert calls[-1][3]["created_at"] == "2030-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_private_cell_api_derives_release_independent_schema_digest_from_agent_contract() -> (
    None
):
    calls: list[str] = []
    agent_contract_base = {
        "schema_version": 1,
        "protocol_version": "1",
        "exomem_release": "0.22.0",
        "agent_profile": {
            "profile": "hosted-alpha-agent-v1",
            "active_capability_sha256": "c" * 64,
        },
        "commands": [],
    }
    runtime_digest = hashlib.sha256(
        json.dumps(agent_contract_base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    published_schema_digest = hashlib.sha256(
        json.dumps(
            {key: value for key, value in agent_contract_base.items() if key != "exomem_release"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert runtime_digest != published_schema_digest

    async def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: object = None,
    ) -> _Response:
        del method, headers, json
        calls.append(url)
        if url.endswith("/agent/hosted-alpha-agent-v1/contract"):
            return _Response(
                200,
                {
                    **agent_contract_base,
                    "digest": {"algorithm": "sha256", "value": runtime_digest},
                },
                raw=True,
            )
        if url.endswith("/agent/hosted-alpha-agent-v1/reader-status"):
            return _Response(
                200,
                {
                    "records_reader_version": 2,
                    "lifecycle_actions_enabled": False,
                },
            )
        if url.endswith("/contract"):
            return _Response(200, {"digest": {"algorithm": "sha256", "value": "b" * 64}}, raw=True)
        if url.endswith("/ready"):
            return _Response(
                200,
                {
                    "cell_id": "cell-alpha",
                    "vault_id": "tenant-alpha",
                    "exomem_release": "0.22.0",
                    "hosted_protocol": "1",
                    "authenticated_credential_version": "1",
                    "security_revision": 1,
                    "service_authenticated": True,
                    "mutation_authority": True,
                    "admission_phase": "active",
                    "read_admission": True,
                    "write_admission": True,
                    "worker_policy_digest": hashlib.sha256(
                        b'{"media":false,"semantic":true,"workerCount":2}'
                    ).hexdigest(),
                },
            )
        return _Response(200, {"live": True, "cell_id": "cell-alpha", "protocol_version": "1"})

    config = LifecycleConfig(
        image="repo@sha256:" + "a" * 64,
        chart_path="chart",
        chart_version="0.1.0",
        helm_version="3.19.4",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        browser_origin="https://substratesystems.io",
        release_version="0.22.0",
        protocol_version="1",
        contract_digest="b" * 64,
        location="fsn1",
        runtime_target={
            "releaseVersion": "0.22.0",
            "protocolVersion": "1",
            "agentProfile": "hosted-alpha-agent-v1",
            "gatewayContractDigest": "b" * 64,
            "commandFingerprint": "c" * 64,
            "schemaDigest": published_schema_digest,
        },
        records_reader_version=2,
        lifecycle_actions_enabled=True,
    )
    adapter = PrivateCellApiAdapter(request=request, internal_origin="http://cells.invalid")

    health = await adapter.health(
        _metadata(),
        credential=_credential(),
        protocol_version="1",
        config=config,
        expected_release="0.22.0",
        expected_worker_policy={"workerCount": 2, "semantic": True, "media": False},
        require_runtime_identity=True,
    )

    assert health.agent_profile == "hosted-alpha-agent-v1"
    assert health.command_fingerprint == "c" * 64
    assert health.schema_digest == published_schema_digest
    assert health.records_reader_version == 2
    assert health.lifecycle_actions_enabled is False
    assert calls == [
        "http://cells.invalid/private/exomem/v1/live",
        "http://cells.invalid/private/exomem/v1/ready",
        "http://cells.invalid/private/exomem/v1/contract",
        "http://cells.invalid/private/exomem/v1/agent/hosted-alpha-agent-v1/contract",
        "http://cells.invalid/private/exomem/v1/agent/hosted-alpha-agent-v1/reader-status",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["digest", "body"])
async def test_private_cell_api_rejects_agent_contract_with_unverified_content(tamper: str) -> None:
    agent_contract_base = {
        "schema_version": 1,
        "protocol_version": "1",
        "exomem_release": "0.22.0",
        "agent_profile": {
            "profile": "hosted-alpha-agent-v1",
            "active_capability_sha256": "c" * 64,
        },
        "commands": [],
    }
    runtime_digest = hashlib.sha256(
        json.dumps(agent_contract_base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    agent_contract: dict[str, object] = {
        **agent_contract_base,
        "digest": {"algorithm": "sha256", "value": runtime_digest},
    }
    if tamper == "digest":
        agent_contract["digest"] = {"algorithm": "sha256", "value": "d" * 64}
    else:
        agent_contract["unexpected"] = True

    async def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: object = None,
    ) -> _Response:
        del method, headers, json
        if url.endswith("/agent/hosted-alpha-agent-v1/contract"):
            return _Response(200, agent_contract, raw=True)
        if url.endswith("/contract"):
            return _Response(200, {"digest": {"algorithm": "sha256", "value": "b" * 64}}, raw=True)
        if url.endswith("/ready"):
            return _Response(
                200,
                {
                    "cell_id": "cell-alpha",
                    "vault_id": "tenant-alpha",
                    "exomem_release": "0.22.0",
                    "hosted_protocol": "1",
                    "authenticated_credential_version": "1",
                    "security_revision": 1,
                    "service_authenticated": True,
                    "mutation_authority": True,
                    "admission_phase": "active",
                    "read_admission": True,
                    "write_admission": True,
                    "worker_policy_digest": hashlib.sha256(
                        b'{"media":false,"semantic":true,"workerCount":2}'
                    ).hexdigest(),
                },
            )
        return _Response(200, {"live": True, "cell_id": "cell-alpha", "protocol_version": "1"})

    adapter = PrivateCellApiAdapter(request=request, internal_origin="http://cells.invalid")
    config = LifecycleConfig(
        image="repo@sha256:" + "a" * 64,
        chart_path="chart",
        chart_version="0.1.0",
        helm_version="3.19.4",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        browser_origin="https://substratesystems.io",
        release_version="0.22.0",
        protocol_version="1",
        contract_digest="b" * 64,
        location="fsn1",
        runtime_target={
            "releaseVersion": "0.22.0",
            "protocolVersion": "1",
            "agentProfile": "hosted-alpha-agent-v1",
            "gatewayContractDigest": "b" * 64,
            "commandFingerprint": "c" * 64,
            "schemaDigest": "d" * 64,
        },
    )

    with pytest.raises(MetadataConflict, match="agent contract digest differs"):
        await adapter.health(
            _metadata(),
            credential=_credential(),
            protocol_version="1",
            config=config,
            expected_release="0.22.0",
            expected_worker_policy={"workerCount": 2, "semantic": True, "media": False},
            require_runtime_identity=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("exomem_release", "0.22.1", "release differs"),
        ("protocol_version", "2", "protocol differs"),
    ),
)
async def test_private_cell_api_rejects_validly_redigested_wrong_agent_contract_identity(
    field: str,
    replacement: str,
    message: str,
) -> None:
    agent_contract_base: dict[str, object] = {
        "schema_version": 1,
        "protocol_version": "1",
        "exomem_release": "0.22.0",
        "agent_profile": {
            "profile": "hosted-alpha-agent-v1",
            "active_capability_sha256": "c" * 64,
        },
        "commands": [],
    }
    agent_contract_base[field] = replacement
    runtime_digest = hashlib.sha256(
        json.dumps(agent_contract_base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    agent_contract = {
        **agent_contract_base,
        "digest": {"algorithm": "sha256", "value": runtime_digest},
    }

    async def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: object = None,
    ) -> _Response:
        del method, headers, json
        if url.endswith("/agent/hosted-alpha-agent-v1/contract"):
            return _Response(200, agent_contract, raw=True)
        if url.endswith("/contract"):
            return _Response(200, {"digest": {"algorithm": "sha256", "value": "b" * 64}}, raw=True)
        if url.endswith("/ready"):
            return _Response(
                200,
                {
                    "cell_id": "cell-alpha",
                    "vault_id": "tenant-alpha",
                    "exomem_release": "0.22.0",
                    "hosted_protocol": "1",
                    "authenticated_credential_version": "1",
                    "security_revision": 1,
                    "service_authenticated": True,
                    "mutation_authority": True,
                    "admission_phase": "active",
                    "read_admission": True,
                    "write_admission": True,
                    "worker_policy_digest": hashlib.sha256(
                        b'{"media":false,"semantic":true,"workerCount":2}'
                    ).hexdigest(),
                },
            )
        return _Response(200, {"live": True, "cell_id": "cell-alpha", "protocol_version": "1"})

    adapter = PrivateCellApiAdapter(request=request, internal_origin="http://cells.invalid")
    config = LifecycleConfig(
        image="repo@sha256:" + "a" * 64,
        chart_path="chart",
        chart_version="0.1.0",
        helm_version="3.19.4",
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        browser_origin="https://substratesystems.io",
        release_version="0.22.0",
        protocol_version="1",
        contract_digest="b" * 64,
        location="fsn1",
        runtime_target={
            "releaseVersion": "0.22.0",
            "protocolVersion": "1",
            "agentProfile": "hosted-alpha-agent-v1",
            "gatewayContractDigest": "b" * 64,
            "commandFingerprint": "c" * 64,
            "schemaDigest": "d" * 64,
        },
    )

    with pytest.raises(MetadataConflict, match=message):
        await adapter.health(
            _metadata(),
            credential=_credential(),
            protocol_version="1",
            config=config,
            expected_release="0.22.0",
            expected_worker_policy={"workerCount": 2, "semantic": True, "media": False},
            require_runtime_identity=True,
        )
