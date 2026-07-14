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
    KubernetesHostedOperatorAdapter,
    KubernetesMaintenanceLeaseAdapter,
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
async def test_kubernetes_adapter_discovers_csi_handle_tags_pv_and_rebinds_original() -> None:
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
    assert core.pv.metadata.annotations == metadata.kubernetes_annotations

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
async def test_cell_adapter_creates_external_secret_then_reads_the_exact_bundle() -> None:
    metadata = _metadata()

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
    adapter = KubernetesCellAdapter(core_v1=core, apps_v1=SimpleNamespace())
    await adapter.write_credential_bundle(
        metadata,
        {"1": _credential()},
        lifecycle_annotations={"exomem.io/security-revision": "1"},
    )

    credentials, annotations = await adapter.read_credential_bundle(metadata)
    assert credentials == {"1": _credential()}
    assert annotations["exomem.io/security-revision"] == "1"


@pytest.mark.asyncio
async def test_hosted_operator_uses_exact_exec_stdin_contract_and_validates_envelope() -> None:
    metadata = _metadata()
    calls: list[tuple[str, str, tuple[str, ...], bytes]] = []
    request = {
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "operation_id": "rotation-alpha:stage",
        "cell_id": "cell-alpha",
        "vault_id": "cell-alpha",
        "state_root": "/var/lib/exomem/state",
        "action": "stage",
        "expected_revision": 1,
        "pending_version": "2",
    }

    async def runner(namespace, pod, command, payload):
        calls.append((namespace, pod, command, payload))
        return (
            0,
            json.dumps(
                {
                    "contract_version": 1,
                    "ok": True,
                    "command": "credential",
                    "request_id": request["request_id"],
                    "code": "HOSTED_CREDENTIAL_STAGED",
                    "data": {"revision": 2},
                }
            ),
            "",
        )

    core = SimpleNamespace(
        list_namespaced_pod=lambda namespace, label_selector: SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="cell-pod-0"),
                    status=SimpleNamespace(phase="Running"),
                )
            ]
        )
    )
    adapter = KubernetesHostedOperatorAdapter(core_v1=core, runner=runner)

    assert await adapter.execute("credential", metadata, request) == {"revision": 2}
    assert calls[0][0:2] == (metadata.resource_name, "cell-pod-0")
    assert calls[0][2] == (
        "exomem",
        "hosted",
        "credential",
        "--contract-version",
        "1",
        "--request-file",
        "-",
    )
    assert json.loads(calls[0][3]) == request


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

        def delete_namespaced_lease(self, name, namespace, body):
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
        )
        is True
    )
    assert len(probes) == 2
    assert probes[0][0] == "OPTIONS"
    assert probes[0][1].endswith("/public/exomem/v2/transfers/download")
    assert probes[0][2] == {
        "Access-Control-Request-Headers": "X-Exomem-Transfer-Grant",
        "Access-Control-Request-Method": "GET",
        "Origin": "https://substratesystems.io",
    }
    assert probes[1][0] == "GET"
    assert probes[1][1] == probes[0][1]
    assert "ticket-unused" in probes[1][2].values()
    assert probes[1][2]["Origin"] == "https://substratesystems.io"
    assert not any(key.startswith("CF-Access-") for key in probes[1][2])

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
    worker_policy = {"workerCount": 0, "semantic": False, "media": False}
    ready = {
        "cell_id": "cell-alpha",
        "vault_id": "cell-alpha",
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
        operator_contract_digest="c" * 64,
        contract_digest="b" * 64,
        location="fsn1",
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
    )
    await adapter.quiesce(
        _metadata(),
        credential=_credential(),
        protocol_version="1",
        operation_id="quiesce-alpha",
    )
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
        worker_policy={"workerCount": 0, "semantic": False, "media": False},
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
