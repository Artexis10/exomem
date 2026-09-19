"""Agent-run disposable first hosted provision drill.

The governance drill seeds a stopped, initialized cell by hand and starts at the
migration. Production has failed at seams that seeding skips: a
Kubernetes-defaulted Job field, the attestation window, the capacity gate, the
initializer-to-migration hand-off and the migration Job's own window check.
This module produces that state through the shipped path instead. It submits
the PROVISION a claimed invite submits, then drives the shipped routine and
volume-registration workers over ``build_live_provider_components`` against
exact K3s.

Only external seams are substituted:

- a disposable SQLite ``OperationRepository`` for the production database;
- a recording stand-in for the Hetzner volume API, plus a read-only view that
  presents a local-path PV the way the Hetzner CSI driver provisions one;
- a capacity receipt minted before every pass from the shipped observer, in
  place of the live collector;
- host-reachable Kubernetes port-forwards for the private service and the
  pinned Traefik controller that production routes through.

The node carries the ``hcloud://`` provider ID the Hetzner cloud controller
would stamp. Nothing here edits provisioner or runtime source: a step the
shipped path cannot take is a production defect to report, not to patch.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import select
import socket
import subprocess
import time
import traceback
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

RUN_DRILL = os.environ.get("RUN_K3S_FIRST_PROVISION_DRILL_TEST") == "1"
if not RUN_DRILL:  # pragma: no cover - the gate is the point
    pytest.skip(
        "set RUN_K3S_FIRST_PROVISION_DRILL_TEST=1 to run the disposable first-provision drill",
        allow_module_level=True,
    )
if os.environ.get("RUN_K3S_GOVERNANCE_DRILL_TEST") != "1":  # pragma: no cover
    pytest.skip(
        "also set RUN_K3S_GOVERNANCE_DRILL_TEST=1: the reused drill helpers live behind it",
        allow_module_level=True,
    )

# Core CI shards run the runtime distribution without the separately packaged
# provisioner. Skip the whole module rather than failing collection.
pytest.importorskip("exomem_provisioner", reason="requires the provisioner package")
pytest.importorskip("kubernetes", reason="requires the provisioner Kubernetes SDK")

from exomem_provisioner.adapters import (  # noqa: E402
    KubernetesCellAdapter,
    KubernetesVolumeAdapter,
)
from exomem_provisioner.capacity import (  # noqa: E402
    KubernetesCapacityObserver,
    canonical_contract_digest,
)
from exomem_provisioner.config import ProviderWorkerSettings, ProvisionerSettings  # noqa: E402
from exomem_provisioner.crypto import AesGcmEnvelopeCodec  # noqa: E402
from exomem_provisioner.database import ProvisionerDatabase  # noqa: E402
from exomem_provisioner.driver import DriverTerminal  # noqa: E402
from exomem_provisioner.governance_migration_checkpoint import (  # noqa: E402
    CHECKPOINT_VERSION,
    MigrationCheckpoint,
)
from exomem_provisioner.lifecycle import (  # noqa: E402
    OpaqueProviderMetadata,
    VolumeLifecycleWorker,
    VolumeRegistrationDriver,
)
from exomem_provisioner.models import OperationState, ResourceKind  # noqa: E402
from exomem_provisioner.production import (  # noqa: E402
    build_live_provider_components,
    build_routine_operation_worker,
)
from exomem_provisioner.provider_identity import (  # noqa: E402
    cell_provider_recovery_envelopes,
    cell_resource_name,
    provider_operation_resource_name,
)
from exomem_provisioner.repository import OperationRepository  # noqa: E402
from exomem_provisioner.schemas import request_plaintext  # noqa: E402
from exomem_provisioner.volume import build_volume_registration_worker  # noqa: E402
from exomem_provisioner.wire_protocol import (  # noqa: E402
    REQUEST_MODELS_BY_PROTOCOL,
    WIRE_PROTOCOL_V2,
)
from test_hosted_k3s_admission import (  # noqa: E402
    ADMISSION_CONFIG,
    CELL,
    HELM,
    K3S_IMAGE,
    _build_provisioner_fingerprint_image,
    _host_kubeconfig,
    _import_provisioner_image,
    _retain_k3s_logs,
    _run,
    shutil_which,
)
from test_hosted_k3s_governance_drill import (  # noqa: E402
    _HELM_VERSION,
    _OBSERVE_SOURCE_STORE,
    CODEC,
    STORAGE_CLASS,
    DrillCell,
    _drill_capacity_contract,
    _drill_json,
    _governance_deployment_lock,
    _host_kubectl,
    _kubernetes_clients,
    _observe_custody,
    _run_target_image_pod,
)

HCLOUD_SERVER_ID = 1
PROVIDER_ID = f"hcloud://{HCLOUD_SERVER_ID}"
LOCATION = "fsn1"
RECEIPT_NAMESPACE = "exomem-platform"
RECEIPT_CONFIG_MAP = "exomem-capacity-receipt"
RECEIPT_DOMAIN = b"exomem.capacity-live-receipt.v1\0"
RUNTIME_TARGET_FIELDS = (
    "releaseVersion",
    "protocolVersion",
    "agentProfile",
    "gatewayContractDigest",
    "commandFingerprint",
    "schemaDigest",
)
OWNER = OpaqueProviderMetadata(
    "tenant-first-provision", "cell-first-provision", "provision-first-provision", 1
)
RUNTIME_IMAGE = (
    "ghcr.io/artexis10/exomem@"
    "sha256:73ab2439e653d490b800eb810c370e297da0efcad176557756b46b89b5c82172"
)
RUNTIME_RELEASE = "0.77.0"
RUNTIME_TARGET = {
    "releaseVersion": RUNTIME_RELEASE,
    "protocolVersion": "1",
    "agentProfile": "hosted-alpha-agent-v4",
    "gatewayContractDigest": "fa554d6c379d0c98aa8bf9cb31525d1b5be6ff19647afedba35f3e2058b85be1",
    "commandFingerprint": "4b4b71280fec7915042483207b1ab0e15e916148ac1b88ef965e03671de80968",
    "schemaDigest": "60b5aec6f872874234a214e778e26ce57fa5805af8ce744bdd68efe8ca0fcb26",
}
COMPATIBILITY_DIGEST = "320e75168c5f72b73551e56f43a82b8d3ee77bf39158ae42ef3292a25b576ec6"
TRAEFIK_CHART_VERSION = "41.0.2"
# The live admission requires the pass clock to sit 0-30 s after its own
# observation, so every pass runs slightly ahead of the wall clock.
CLOCK_LEAD = timedelta(seconds=10)
CLOCK_LEAD_LIMIT = timedelta(seconds=20)
MAX_PASSES = 240
STALL_PASSES = 40

# The first provision, checkpoint by checkpoint. gpi1 entries drop their PVC
# binding digest and gm1 entries are decoded to their phase.
EXPECTED_THROUGH_MIGRATION = [
    "queued",
    "namespace-ready",
    "release-applied",
    "gpi1:binding",
    "gpi1:registering",
    "gpi1:registered",
    "gpi1:initializing",
    "gpi1:complete",
    "gpi1:drained",
    "gm1:inspect",
    "gm1:prepare",
    "gm1:enroll",
    "gm1:commit",
    "gm1:complete",
]
EXPECTED_THROUGH_SERVING = [*EXPECTED_THROUGH_MIGRATION, "gm1:confirmed", "complete"]


@dataclass(frozen=True, slots=True)
class DrillImages:
    runtime: str
    provisioner: str
    release: str


@pytest.fixture(scope="module")
def first_provision_k3s(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, Path]]:
    """Exact K3s as the admission harness starts it, plus what a first provision needs.

    The bundled local-path provisioner stays enabled so the first consumer binds
    a real dynamic volume. The embedded cloud controller is disabled so the
    kubelet's ``hcloud://`` provider ID, which the capacity observer requires,
    is the one the node registers with.
    """

    if HELM is None:
        pytest.skip("set HELM_BIN to run the first-provision drill")
    if not shutil_which("docker"):
        pytest.skip("Docker is required for the first-provision drill")

    name = f"exomem-first-provision-{uuid.uuid4().hex[:12]}"
    scratch = tmp_path_factory.mktemp("k3s-first-provision")
    log_path = scratch / f"{name}.log"
    failures_before = request.session.testsfailed
    logs_retained = False
    _run(
        [
            "docker",
            "run",
            "--privileged",
            "--detach",
            "--name",
            name,
            "--publish",
            "127.0.0.1::6443",
            "--volume",
            f"{ADMISSION_CONFIG}:/etc/rancher/k3s/admission-config.yaml:ro",
            K3S_IMAGE,
            "server",
            "--disable=traefik",
            "--disable=servicelb",
            "--disable-cloud-controller",
            f"--kubelet-arg=provider-id={PROVIDER_ID}",
            "--write-kubeconfig-mode=600",
            "--kube-apiserver-arg=admission-control-config-file=/etc/rancher/k3s/admission-config.yaml",
        ]
    )
    try:
        consecutive_ready = 0
        for _ in range(90):
            ready = _run(["docker", "exec", name, "kubectl", "get", "--raw=/readyz"], check=False)
            if ready.returncode == 0 and ready.stdout.strip() == "ok":
                consecutive_ready += 1
                if consecutive_ready == 3:
                    break
            else:
                consecutive_ready = 0
            time.sleep(1)
        else:
            raise AssertionError("K3s did not become ready")
        nodes = None
        for _ in range(120):
            nodes = _run(
                ["docker", "exec", name, "kubectl", "get", "nodes", "--output=json"], check=False
            )
            items = json.loads(nodes.stdout)["items"] if nodes.returncode == 0 else []
            if (
                len(items) == 1
                and items[0]["spec"].get("providerID") == PROVIDER_ID
                and any(
                    condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in items[0].get("status", {}).get("conditions", [])
                )
            ):
                break
            time.sleep(1)
        else:
            raise AssertionError(
                f"the K3s node never became Ready as {PROVIDER_ID}: "
                + ("" if nodes is None else nodes.stdout + nodes.stderr)
            )
        for _ in range(120):
            provisioner = _run(
                [
                    "docker",
                    "exec",
                    name,
                    "kubectl",
                    "get",
                    "deployment/local-path-provisioner",
                    "--namespace=kube-system",
                ],
                check=False,
            )
            if provisioner.returncode == 0:
                break
            time.sleep(1)
        else:
            raise AssertionError("the bundled local-path provisioner was not installed")
        _run(
            [
                "docker",
                "exec",
                name,
                "kubectl",
                "rollout",
                "status",
                "deployment/local-path-provisioner",
                "--namespace=kube-system",
                "--timeout=180s",
            ]
        )
        yield name, _host_kubeconfig(name, scratch / "kubeconfig")
    except BaseException:
        _retain_k3s_logs(name, log_path)
        logs_retained = True
        raise
    finally:
        if not logs_retained and request.session.testsfailed > failures_before:
            _retain_k3s_logs(name, log_path)
        _run(["docker", "rm", "--force", name], check=False)


@pytest.fixture(scope="module")
def drill_images(first_provision_k3s: tuple[str, Path]) -> Iterator[DrillImages]:
    """Import immutable runtime 0.77.0 and build the current provisioner image."""

    k3s, _kubeconfig = first_provision_k3s
    provisioner: str | None = None
    try:
        inspected = json.loads(_run(["docker", "image", "inspect", RUNTIME_IMAGE]).stdout)[0]
        assert RUNTIME_IMAGE in inspected.get("RepoDigests", ())
        _run(
            [
                "docker",
                "exec",
                k3s,
                "ctr",
                "images",
                "pull",
                "--platform=linux/amd64",
                RUNTIME_IMAGE,
            ]
        )
        image_rows = _run(["docker", "exec", k3s, "ctr", "images", "ls"]).stdout.splitlines()
        runtime_row = next(
            line.split() for line in image_rows[1:] if line.split()[0] == RUNTIME_IMAGE
        )
        assert runtime_row[2] == RUNTIME_IMAGE.rsplit("@", 1)[1]
        # The vault fingerprint Job runs the provisioner image the lock names.
        provisioner = _build_provisioner_fingerprint_image()
        yield DrillImages(
            runtime=RUNTIME_IMAGE,
            provisioner=_import_provisioner_image(k3s, provisioner),
            release=RUNTIME_RELEASE,
        )
    finally:
        if provisioner is not None:
            _run(["docker", "image", "rm", "--force", provisioner], check=False)


def _install_platform_prerequisites(kubeconfig: Path, scratch: Path) -> None:
    """What the platform chart and the Hetzner CSI driver provide in production."""

    _host_kubectl(
        kubeconfig,
        ["apply", "--filename=-"],
        documents=[
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": STORAGE_CLASS},
                "provisioner": "rancher.io/local-path",
                "reclaimPolicy": "Retain",
                "volumeBindingMode": "WaitForFirstConsumer",
            },
            {
                "apiVersion": "node.k8s.io/v1",
                "kind": "RuntimeClass",
                "metadata": {"name": "exomem-storage-init"},
                "handler": "runc",
            },
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": RECEIPT_NAMESPACE}},
        ],
    )
    assert HELM is not None
    values = yaml.safe_load(
        (Path(__file__).parents[1] / "infra/helm/platform/values.yaml").read_text("utf-8")
    )["traefik"]
    values["ports"]["web"]["forwardedHeaders"]["trustedIPs"].append("127.0.0.0/8")
    values_path = scratch / "traefik.yaml"
    values_path.write_text(yaml.safe_dump(values, sort_keys=True), encoding="utf-8")
    _run(
        [
            str(HELM),
            "upgrade",
            "--install",
            "exomem-platform",
            "traefik",
            "--repo",
            "https://traefik.github.io/charts",
            "--version",
            TRAEFIK_CHART_VERSION,
            "--namespace",
            RECEIPT_NAMESPACE,
            "--values",
            str(values_path),
            "--wait",
            "--timeout",
            "300s",
        ]
    )


@dataclass
class _PortForward:
    kubeconfig: Path
    namespace: str
    service: str
    remote_port: int
    process: subprocess.Popen[str] | None = None
    local_port: int | None = None

    def start(self) -> str:
        if self.process is not None and self.process.poll() is None and self.local_port is not None:
            return f"http://127.0.0.1:{self.local_port}"
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            self.local_port = int(available.getsockname()[1])
        self.process = subprocess.Popen(
            [
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "port-forward",
                "--address=127.0.0.1",
                "--namespace",
                self.namespace,
                f"service/{self.service}",
                f"{self.local_port}:{self.remote_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert self.process.stdout is not None
        output: list[str] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output.extend(self.process.stdout.readlines())
                raise AssertionError(
                    f"kubectl port-forward for {self.namespace}/{self.service} exited: "
                    + "".join(output)
                )
            ready, _, _ = select.select([self.process.stdout], [], [], 0.1)
            if ready:
                line = self.process.stdout.readline()
                output.append(line)
                if "Forwarding from 127.0.0.1:" in line:
                    return f"http://127.0.0.1:{self.local_port}"
        raise AssertionError(
            f"kubectl port-forward for {self.namespace}/{self.service} did not start: "
            + "".join(output)
        )

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


@dataclass
class DrillNetwork:
    """Host-reachable transports to real cell and Traefik services."""

    kubeconfig: Path
    traefik: _PortForward
    cells: dict[tuple[str, str], _PortForward] = field(default_factory=dict)

    @property
    def traefik_origin(self) -> str:
        return self.traefik.start()

    def _cell_origin(self, resource: str, namespace: str) -> str:
        key = (namespace, resource)
        forward = self.cells.get(key)
        if forward is None:
            forward = _PortForward(self.kubeconfig, namespace, resource, 8765)
            self.cells[key] = forward
        return forward.start()

    async def private_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        parsed = urlsplit(url)
        labels = (parsed.hostname or "").split(".")
        assert len(labels) >= 2, f"private cell URL did not name a service and namespace: {url}"
        origin = await asyncio.to_thread(self._cell_origin, labels[0], labels[1])
        async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
            return await client.request(
                method,
                origin + parsed.path + ("?" + parsed.query if parsed.query else ""),
                **kwargs,
            )

    async def ingress_request(
        self, method: str, url: str, headers: dict[str, str]
    ) -> httpx.Response:
        parsed = urlsplit(url)
        forwarded = dict(headers)
        forwarded["Host"] = parsed.netloc
        forwarded["X-Forwarded-Proto"] = "https"
        origin = await asyncio.to_thread(lambda: self.traefik_origin)
        async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
            return await client.request(
                method,
                origin + parsed.path + ("?" + parsed.query if parsed.query else ""),
                headers=forwarded,
            )

    async def external_probe(self, method: str, url: str, headers: dict[str, str]) -> int:
        return (await self.ingress_request(method, url, headers)).status_code

    def close(self) -> None:
        for forward in self.cells.values():
            forward.close()
        self.traefik.close()


def _start_network(kubeconfig: Path) -> DrillNetwork:
    network = DrillNetwork(
        kubeconfig,
        _PortForward(kubeconfig, RECEIPT_NAMESPACE, "exomem-platform-traefik", 80),
    )
    network.traefik.start()
    return network


def _deployment_lock(scratch: Path, images: DrillImages) -> Path:
    """The governance drill's real lock, also naming the imported provisioner image."""

    path = _governance_deployment_lock(
        scratch, SimpleNamespace(runtime_image=images.runtime, release=images.release)
    )
    lock = json.loads(path.read_text(encoding="utf-8"))
    lock["components"]["provisioner"]["image"] = images.provisioner
    lock["runtimeTarget"] = dict(RUNTIME_TARGET)
    lock["runtimeUpgrade"]["compatibilityDigest"] = COMPATIBILITY_DIGEST
    path.write_text(json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n", "utf-8")
    return path


def _provider_settings(scratch: Path, images: DrillImages) -> ProviderWorkerSettings:
    capacity_contract, capacity_key = _drill_capacity_contract(scratch)
    assert HELM is not None
    return ProviderWorkerSettings(
        deployment_lock_path=str(_deployment_lock(scratch, images)),
        runtime_selection="active",
        cell_chart_path=str(CELL),
        cell_chart_version=yaml.safe_load((CELL / "Chart.yaml").read_text(encoding="utf-8"))[
            "version"
        ],
        helm_binary=str(HELM),
        helm_version=_HELM_VERSION,
        control_hostname="control.drill.invalid",
        transfer_hostname="transfer.drill.invalid",
        browser_origin="https://app.drill.invalid",
        location=LOCATION,
        internal_origin="http://{resource}.{namespace}.svc.cluster.local:8765",
        worker_id="drill-routine-worker",
        provider_recovery_public_key=CODEC.public_key(),
        capacity_receipt_public_key=capacity_key,
        capacity_contract_path=str(capacity_contract),
        capacity_receipt_namespace=RECEIPT_NAMESPACE,
        capacity_receipt_config_map=RECEIPT_CONFIG_MAP,
        hcloud_server_id=HCLOUD_SERVER_ID,
    )


def _first_provision_request(
    settings: ProviderWorkerSettings, owner: OpaqueProviderMetadata = OWNER
) -> dict[str, Any]:
    """The stored request: the v2 wire model plus what the API adds before submit."""

    selected = settings.deployment_lock.selected_runtime(settings.runtime_selection)
    lock_target = selected.runtimeTarget.model_dump(mode="json")
    target = {name: lock_target[name] for name in RUNTIME_TARGET_FIELDS}
    # The API's forward target carries the selected unit's compatibility digest,
    # so the control plane's PROVISION names it too.
    if selected.compatibilityDigest is not None:
        target["compatibilityDigest"] = selected.compatibilityDigest
    wire = {
        "operationId": owner.operation_id,
        "checkpoint": "requested",
        "fenceGeneration": owner.fence_generation,
        "tenantId": owner.tenant_id,
        "cellId": owner.subject_id,
        "provisionMode": "serve",
        "runtimeTarget": target,
        "serviceCredential": base64.urlsafe_b64encode(
            hashlib.sha256(b"first-provision-drill-service-credential").digest()
        )
        .rstrip(b"=")
        .decode("ascii"),
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
    }
    model = REQUEST_MODELS_BY_PROTOCOL[WIRE_PROTOCOL_V2]["provision"].model_validate(wire)
    request = request_plaintext(model)
    request["_providerRecoveryEnvelopes"] = cell_provider_recovery_envelopes(
        CODEC,
        tenant_id=owner.tenant_id,
        cell_id=owner.subject_id,
        operation_id=owner.operation_id,
        fence_generation=owner.fence_generation,
        resource_name=cell_resource_name(owner.subject_id),
        operation_resource_name=provider_operation_resource_name(owner.operation_id),
    )
    return request


async def _operation_store(scratch: Path) -> tuple[ProvisionerDatabase, OperationRepository]:
    """A disposable SQLite store with the shipped schema and production failure budget."""

    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{scratch / 'provisioner.sqlite'}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    return database, OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
        max_failure_attempts=settings.max_failure_attempts,
    )


class _TerminationWitness:
    """Pass every call through; remember each terminated container a pod read returns."""

    _OBSERVED = frozenset({"list_namespaced_pod", "read_namespaced_pod"})

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.terminations: dict[tuple[str, str, str], dict[str, Any]] = {}

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if name not in self._OBSERVED:
            return attribute

        def observed(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            pods = getattr(result, "items", None) if name == "list_namespaced_pod" else [result]
            for pod in pods or ():
                self._remember(pod)
            return result

        return observed

    def _remember(self, pod: Any) -> None:
        metadata = getattr(pod, "metadata", None)
        status = getattr(pod, "status", None)
        labels = dict(getattr(metadata, "labels", None) or {})
        statuses = [
            *(getattr(status, "init_container_statuses", None) or ()),
            *(getattr(status, "container_statuses", None) or ()),
        ]
        for container in statuses:
            terminated = getattr(getattr(container, "state", None), "terminated", None)
            if terminated is None:
                continue
            pod_name = str(getattr(metadata, "name", ""))
            key = (pod_name, str(container.name), str(getattr(terminated, "finished_at", "")))
            self.terminations.setdefault(
                key,
                {
                    "pod": pod_name,
                    "purpose": _pod_purpose(labels),
                    "job": labels.get("batch.kubernetes.io/job-name") or labels.get("job-name"),
                    "jobUid": labels.get("batch.kubernetes.io/controller-uid")
                    or labels.get("controller-uid"),
                    "container": container.name,
                    "exitCode": getattr(terminated, "exit_code", None),
                    "reason": getattr(terminated, "reason", None),
                    "message": getattr(terminated, "message", None),
                },
            )


class _ObservedDriver:
    """Delegate to a shipped driver; keep the cause of every terminal refusal it raises.

    The worker logs only a code and a closed-set reason. The chained cause is
    what names the refusing call, so the drill records it before re-raising.
    """

    def __init__(self, delegate: Any, failures: list[dict[str, Any]], *, lane: str) -> None:
        self._delegate = delegate
        self._failures = failures
        self._lane = lane

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def execute(self, action: str, request: dict[str, Any], context: Any) -> Any:
        try:
            return await self._delegate.execute(action, request, context)
        except DriverTerminal as error:
            reason = error.reason
            self._failures.append(
                {
                    "lane": self._lane,
                    "checkpoint": _checkpoint_label(context.checkpoint),
                    "code": error.code,
                    "reason": None if reason is None else str(getattr(reason, "value", reason)),
                    "traceback": "".join(traceback.format_exception(error)),
                }
            )
            raise


def _pod_purpose(labels: dict[str, str]) -> str:
    if labels.get("exomem.io/governance-migration") == "true":
        return "governance-migration"
    if labels.get("exomem.io/storage-binding") == "true":
        return "storage-binding"
    if labels.get("exomem.io/vault-fingerprint") == "true":
        return "vault-fingerprint"
    if "exomem.io/storage-init" in labels:
        return "storage-init"
    return labels.get("app.kubernetes.io/name", "unlabelled")


class _HetznerProvisionedReads:
    """Read local-path PVs the way the Hetzner CSI driver provisions them.

    Volume registration records the PV's CSI volume handle and its Hetzner
    location topology. A local-path PV carries neither, so only the results of
    ``read_persistent_volume`` gain them. Every write still reaches the real PV.
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if name != "read_persistent_volume":
            return attribute

        def read(*args: Any, **kwargs: Any) -> Any:
            from kubernetes import client

            volume = attribute(*args, **kwargs)
            if volume.spec.csi is not None:
                return volume
            digest = hashlib.sha256(str(volume.metadata.uid).encode("utf-8")).hexdigest()
            volume.spec.csi = client.V1CSIPersistentVolumeSource(
                driver="csi.hetzner.cloud", volume_handle=str(int(digest[:10], 16) + 1)
            )
            location = client.V1NodeSelectorTerm(
                match_expressions=[
                    client.V1NodeSelectorRequirement(
                        key="csi.hetzner.cloud/location", operator="In", values=[LOCATION]
                    )
                ]
            )
            affinity = volume.spec.node_affinity
            if affinity is None or affinity.required is None:
                volume.spec.node_affinity = client.V1VolumeNodeAffinity(
                    required=client.V1NodeSelector(node_selector_terms=[location])
                )
            else:
                affinity.required.node_selector_terms = [
                    *(affinity.required.node_selector_terms or ()),
                    location,
                ]
            return volume

        return read


@dataclass
class _RecordingHCloudVolumes:
    """The Hetzner volume API: the only real Hetzner call a first provision makes."""

    labelled: dict[str, OpaqueProviderMetadata] = field(default_factory=dict)

    async def label_volume(
        self, handle: str, metadata: OpaqueProviderMetadata, recovery_envelope: str | None = None
    ) -> None:
        assert recovery_envelope, "a registered volume must carry its recovery envelope"
        self.labelled[handle] = metadata

    async def verify_volume(
        self, handle: str, metadata: OpaqueProviderMetadata, location: str
    ) -> bool:
        return True

    async def observed_fence(self, tenant_id: str) -> int:
        return max(
            (
                item.fence_generation
                for item in self.labelled.values()
                if item.tenant_id == tenant_id
            ),
            default=0,
        )

    async def delete_volume(self, handle: str) -> None:
        raise AssertionError("volume deletion is not on the first-provision path")

    async def volume_absent(self, handle: str) -> bool:
        raise AssertionError("volume absence is not on the first-provision path")

    async def discover_tenant_volumes(self, tenant_id: str) -> tuple[str, ...]:
        raise AssertionError("tenant volume discovery is not on the first-provision path")

    async def quarantine_volume(self, handle: str) -> None:
        raise AssertionError("volume quarantine is not on the first-provision path")


def _volume_registration_driver(
    core_v1: Any, *, runtime_image: str, hcloud: _RecordingHCloudVolumes
) -> VolumeRegistrationDriver:
    """``build_volume_provider_components`` with the Hetzner client substituted."""

    verifier = CODEC.verifier()
    kubernetes = KubernetesVolumeAdapter(
        core_v1=_HetznerProvisionedReads(core_v1),
        storage_class_name=STORAGE_CLASS,
        encryption_secret_name="exomem-hcloud-volume-encryption",
        encryption_secret_namespace="kube-system",
        identity_verifier=verifier,
    )
    return VolumeRegistrationDriver(
        VolumeLifecycleWorker(kubernetes, hcloud, identity_codec=CODEC),
        identity_verifier=verifier,
        binding_observer=KubernetesCellAdapter(
            core_v1=core_v1, apps_v1=None, identity_verifier=verifier
        ),
        runtime_image=runtime_image,
    )


def _capacity_signing_key() -> Any:
    """The private half of the key ``_drill_capacity_contract`` pins the contract to."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"k3s-governance-drill-capacity").digest()
    )


@dataclass
class _CapacityCollector:
    """Stand in for the live collector: observe with the shipped observer, sign, publish."""

    core_v1: Any
    storage_v1: Any
    contract: dict[str, Any]
    sequence: int = 0
    published: list[dict[str, int]] = field(default_factory=list)

    async def publish(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from kubernetes.client.exceptions import ApiException

        observation = await KubernetesCapacityObserver(
            core_v1=self.core_v1,
            storage_v1=self.storage_v1,
            expected_server_id=HCLOUD_SERVER_ID,
            expected_location=LOCATION,
        ).observe()
        self.sequence += 1
        observed = datetime.now(UTC).replace(microsecond=0)
        unsigned = {
            "schema_version": 1,
            "issuer": "exomem-live-kubernetes-hcloud-v1",
            "contract_sha256": canonical_contract_digest(self.contract),
            "receipt_id": str(uuid.uuid4()),
            "sequence": self.sequence,
            "cluster_uid": observation.cluster_uid,
            "hcloud_server_id": HCLOUD_SERVER_ID,
            "hcloud_location": LOCATION,
            "observed_at": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (observed + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "active_user_cells": len(observation.user_resource_names),
            "active_recovery_cells": len(observation.recovery_resource_names),
            "attached_volumes": observation.attached_hcloud_volumes,
        }
        key = _capacity_signing_key()
        raw_key = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        receipt = {
            **unsigned,
            "authentication": {
                "algorithm": "ed25519",
                "key_id": hashlib.sha256(raw_key).hexdigest(),
                "signature": key.sign(RECEIPT_DOMAIN + canonical).hex(),
            },
        }
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": RECEIPT_CONFIG_MAP, "namespace": RECEIPT_NAMESPACE},
            "data": {"receipt.json": json.dumps(receipt, sort_keys=True, separators=(",", ":"))},
        }
        try:
            await asyncio.to_thread(
                self.core_v1.replace_namespaced_config_map,
                RECEIPT_CONFIG_MAP,
                RECEIPT_NAMESPACE,
                body,
            )
        except ApiException as error:
            if error.status != 404:
                raise
            await asyncio.to_thread(
                self.core_v1.create_namespaced_config_map, RECEIPT_NAMESPACE, body
            )
        self.published.append(
            {
                "sequence": self.sequence,
                "activeUserCells": unsigned["active_user_cells"],
                "activeRecoveryCells": unsigned["active_recovery_cells"],
                "attachedVolumes": unsigned["attached_volumes"],
            }
        )


def _checkpoint_label(checkpoint: str) -> str:
    if checkpoint.startswith(CHECKPOINT_VERSION + ":"):
        return f"{CHECKPOINT_VERSION}:{MigrationCheckpoint.decode(checkpoint).phase}"
    if checkpoint.startswith("gpi1:"):
        return ":".join(checkpoint.split(":")[:2])
    return checkpoint


def _migration_complete(checkpoint: str) -> bool:
    return checkpoint.startswith(CHECKPOINT_VERSION + ":") and (
        MigrationCheckpoint.decode(checkpoint).phase == "complete"
    )


def _volume_lane(checkpoint: str) -> bool:
    return checkpoint == "volume-registration-required" or checkpoint.startswith(
        "gpi1:registering:"
    )


def _terminal_json(message: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(message or "")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _migration_refusal(terminations: list[dict[str, Any]]) -> str | None:
    """A migration runner terminal that names no schema, read as the governance drill reads it."""

    for item in terminations:
        terminal = _terminal_json(item["message"])
        if item["purpose"] == "governance-migration" and terminal is not None:
            if "actualSchema" not in terminal:
                return f"the migration Job refused: {terminal}"
    return None


def _failed_attempts(terminations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-zero container exits whose Job outcome is still to be decided.

    A Job retries a failed pod within its backoff limit, so an exit alone is not
    a refusal. Only a Job that reaches ``Failed`` is.
    """

    return [
        item
        for item in terminations
        if item["exitCode"] not in (0, None)
        and not (
            item["purpose"] == "governance-migration"
            and _terminal_json(item["message"]) is not None
        )
    ]


async def _pass_clock(earliest: datetime) -> datetime:
    wall = datetime.now(UTC)
    if earliest - wall > CLOCK_LEAD_LIMIT:
        await asyncio.sleep((earliest - wall - CLOCK_LEAD_LIMIT).total_seconds())
        wall = datetime.now(UTC)
    return max(wall + CLOCK_LEAD, earliest)


def _namespace_evidence(kubeconfig: Path, namespace: str) -> str:
    """Names, phases, events and logs only; custody Secrets are never read."""

    sections = []
    for arguments in (
        ["get", "jobs,pods,pvc,leases", "--output=wide", "--namespace", namespace],
        ["get", "pv", "--output=wide"],
        ["get", "events", "--sort-by=.lastTimestamp", "--namespace", namespace],
        ["describe", "pods", "--namespace", namespace],
        [
            "logs",
            "--namespace",
            namespace,
            "--all-containers=true",
            "--prefix",
            "--tail=200",
            "--selector=app.kubernetes.io/name",
        ],
    ):
        result = _host_kubectl(kubeconfig, arguments, check=False)
        sections.append(f"--- kubectl {' '.join(arguments)} ---\n{result.stdout}{result.stderr}")
    return "\n".join(sections)


@dataclass
class FirstProvisionTrace:
    """Content-safe evidence: checkpoint labels, lanes, timings and runner codes."""

    directory: Path
    started: float = field(default_factory=time.monotonic)
    checkpoints: list[str] = field(default_factory=list)
    passes: list[dict[str, Any]] = field(default_factory=list)
    terminations: list[dict[str, Any]] = field(default_factory=list)
    # Failed pod attempts the owning Job retried past: findings, not refusals.
    retried: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, checkpoint: str) -> None:
        if not self.checkpoints or self.checkpoints[-1] != checkpoint:
            self.checkpoints.append(checkpoint)

    def phases(self) -> list[str]:
        ordered: list[str] = []
        for checkpoint in self.checkpoints:
            label = _checkpoint_label(checkpoint)
            if not ordered or ordered[-1] != label:
                ordered.append(label)
        return ordered

    def write(self, scenario: str, **extra: Any) -> Path:
        path = self.directory / f"{scenario}.json"
        path.write_text(
            json.dumps(
                {
                    "artifact": "exomem-hosted-first-provision-drill-evidence",
                    "schemaVersion": 1,
                    "scenario": scenario,
                    "checkpoints": self.phases(),
                    "passes": self.passes,
                    "terminations": self.terminations,
                    "retriedTerminations": self.retried,
                    **extra,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\n--- {scenario} evidence ---\n{path.read_text(encoding='utf-8')}", flush=True)
        return path


@dataclass
class _Drill:
    """One composed first provision: the shipped workers over one disposable cluster."""

    scratch: Path
    kubeconfig: Path
    images: DrillImages
    owner: OpaqueProviderMetadata
    repository: OperationRepository
    routine: Any
    volume: Any
    plane: Any
    network: DrillNetwork
    collector: _CapacityCollector
    witness: _TerminationWitness
    hcloud: _RecordingHCloudVolumes
    probes: list[tuple[str, str, int]]
    clients: tuple[Any, Any, Any, Any]
    request: dict[str, Any]
    trace: FirstProvisionTrace
    operation_id: str = ""
    failures: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)

    def cell(self, pvc_uid: str) -> DrillCell:
        core_v1, apps_v1, batch_v1, _api = self.clients
        return DrillCell(
            owner=self.owner,
            envelopes=dict(self.request["_providerRecoveryEnvelopes"]),
            pvc_uid=pvc_uid,
            runtime_image=self.images.runtime,
            release=self.images.release,
            kubeconfig=self.kubeconfig,
            core_v1=core_v1,
            apps_v1=apps_v1,
            batch_v1=batch_v1,
        )

    async def pvc_uid(self) -> str:
        core_v1, apps_v1, _batch_v1, _api = self.clients
        adapter = KubernetesCellAdapter(
            core_v1=core_v1, apps_v1=apps_v1, identity_verifier=CODEC.verifier()
        )
        return await adapter.authenticated_volume_uid(self.owner)

    async def refusal(self, reason: str, *, observe_store: bool = False) -> AssertionError:
        """Write the evidence and return the failure a reader can act on."""

        detail: dict[str, Any] = {"reason": reason}
        stored = await self.repository.get_by_id(self.operation_id)
        detail["operation"] = {
            "state": stored.state.value,
            "checkpoint": _checkpoint_label(stored.checkpoint),
            "errorCode": stored.error_code,
            "progress": stored.progress,
        }
        detail["receipts"] = self.collector.published[-3:]
        detail["unsettledFailedAttempts"] = self.unresolved
        detail["driverRefusals"] = [
            {key: value for key, value in item.items() if key != "traceback"}
            for item in self.failures
        ]
        if observe_store:
            # A content-free refusal is not a diagnosis. Observe the store
            # through the runner's own reader in both Job mounts, as the
            # governance drill does, so the failure names what the runner saw.
            cell = self.cell(await self.pvc_uid())
            detail["storeObservation"] = {
                phase: await asyncio.to_thread(
                    lambda phase=phase: _drill_json(
                        _run_target_image_pod(
                            cell,
                            self.owner,
                            name=f"{self.owner.resource_name}-observe-{phase}",
                            script=_OBSERVE_SOURCE_STORE,
                            phase=phase,
                        )
                    )
                )
                for phase in ("inspect", "prepare")
            }
        self.trace.write("first-provision-refused", refusal=detail)
        return AssertionError(
            f"first provision stopped at {_checkpoint_label(stored.checkpoint)}: {reason}\n"
            + json.dumps(detail, indent=2, sort_keys=True, default=str)
            + "\n"
            + "".join(
                "--- driver refusal traceback ---\n" + item["traceback"] for item in self.failures
            )
            + await asyncio.to_thread(
                _namespace_evidence, self.kubeconfig, self.owner.resource_name
            )
        )

    def _log_tail(self, item: dict[str, Any]) -> list[str]:
        result = _host_kubectl(
            self.kubeconfig,
            [
                "logs",
                "--namespace",
                self.owner.resource_name,
                item["pod"],
                "--container",
                item["container"],
                "--tail=20",
            ],
            check=False,
        )
        return (result.stdout + result.stderr).splitlines()[-20:]

    async def _job_outcome(self, item: dict[str, Any]) -> str:
        from kubernetes.client.exceptions import ApiException

        if not item.get("job"):
            # A bare pod has no retry: its failed exit is final.
            return "failed"
        _core_v1, _apps_v1, batch_v1, _api = self.clients
        try:
            job = await asyncio.to_thread(
                batch_v1.read_namespaced_job, item["job"], self.owner.resource_name
            )
        except ApiException as error:
            if error.status == 404:
                return "absent"
            raise
        if job.metadata.uid != item.get("jobUid"):
            return "replaced"
        conditions = {
            condition.type: condition.status for condition in (job.status.conditions or ())
        }
        if conditions.get("Failed") == "True":
            return "failed"
        if conditions.get("Complete") == "True":
            return "complete"
        return "running"

    async def settle_failed_attempts(self, fresh: list[dict[str, Any]]) -> None:
        """Keep attempts a Job retried past as findings; refuse only a Job that failed."""

        for item in fresh:
            # Capture the attempt's own output before TTL cleanup removes the pod.
            item["logTail"] = await asyncio.to_thread(self._log_tail, item)
            self.unresolved.append(item)
        waiting: list[dict[str, Any]] = []
        for item in self.unresolved:
            outcome = await self._job_outcome(item)
            if outcome == "failed":
                raise await self.refusal(
                    f"the {item['purpose']} Job {item['job']} failed: container "
                    f"{item['container']} in {item['pod']} exited {item['exitCode']} "
                    f"({item['reason']}): {item['message']}"
                )
            if outcome == "running":
                waiting.append(item)
                continue
            settled = item | {"jobOutcome": outcome}
            self.trace.retried.append(settled)
            print(
                "first-provision retried attempt:",
                json.dumps(settled, sort_keys=True, default=str),
                flush=True,
            )
        self.unresolved = waiting

    async def drive_until(self, done: Any) -> str:
        """Run whichever shipped worker owns the checkpoint until ``done`` holds."""

        earliest = datetime.now(UTC)
        unchanged = 0
        for index in range(MAX_PASSES):
            before = await self.repository.get_by_id(self.operation_id)
            self.trace.observe(before.checkpoint)
            if before.state is not OperationState.PENDING or done(before.checkpoint):
                return before.checkpoint
            clock = await _pass_clock(earliest)
            await self.collector.publish()
            lane = "volume" if _volume_lane(before.checkpoint) else "routine"
            worker = self.volume if lane == "volume" else self.routine
            seen = set(self.witness.terminations)
            try:
                claimed = await worker.run_once(now=clock)
            except Exception as error:  # noqa: BLE001 - every escape is a finding
                raise await self.refusal(
                    f"the {lane} worker raised {type(error).__name__}: {error}\n"
                    + traceback.format_exc()
                ) from error
            after = await self.repository.get_by_id(self.operation_id)
            fresh = [
                self.witness.terminations[key] for key in self.witness.terminations.keys() - seen
            ]
            self.trace.terminations.extend(fresh)
            record = {
                "pass": index,
                "lane": lane,
                "claimed": claimed,
                "from": _checkpoint_label(before.checkpoint),
                "to": _checkpoint_label(after.checkpoint),
                "state": after.state.value,
                "errorCode": after.error_code,
                "retryAfterSeconds": after.retry_after_seconds,
                "clockLeadSeconds": round((clock - datetime.now(UTC)).total_seconds(), 2),
                "elapsedSeconds": round(time.monotonic() - self.trace.started, 2),
            }
            self.trace.passes.append(record)
            print("first-provision pass:", json.dumps(record, sort_keys=True), flush=True)
            if after.state is OperationState.ERROR:
                raise await self.refusal(f"the {lane} worker failed the operation")
            if (
                claimed
                and after.retry_after_seconds == 300
                and (reason := after.progress.get("last_capacity_wait_reason"))
            ):
                raise await self.refusal(f"capacity admission held the pass: {reason}")
            migration_refused = _migration_refusal(fresh)
            if migration_refused is not None:
                raise await self.refusal(migration_refused, observe_store=True)
            await self.settle_failed_attempts(_failed_attempts(fresh))
            earliest = clock + timedelta(seconds=after.retry_after_seconds if claimed else 1)
            unchanged = unchanged + 1 if after.checkpoint == before.checkpoint else 0
            if unchanged >= STALL_PASSES:
                raise await self.refusal(f"no checkpoint progress in {unchanged} passes")
        raise await self.refusal(f"the drill did not finish within {MAX_PASSES} passes")


async def _compose(
    scratch: Path,
    kubeconfig: Path,
    images: DrillImages,
    *,
    owner: OpaqueProviderMetadata = OWNER,
    database_repository: tuple[ProvisionerDatabase, OperationRepository] | None = None,
    settings: ProviderWorkerSettings | None = None,
    request: dict[str, Any] | None = None,
    network: DrillNetwork | None = None,
) -> tuple[_Drill, Any]:
    """Compose the shipped workers; substitute only the seams the module docstring names."""

    from kubernetes import client as kube_client

    settings = settings or _provider_settings(scratch, images)
    selected = settings.deployment_lock.selected_runtime(settings.runtime_selection)
    assert selected.migrationMode == "governance-v3-to-v4"
    assert selected.image == images.runtime
    assert selected.runtimeTarget.model_dump(mode="json") == RUNTIME_TARGET
    assert selected.compatibilityDigest == COMPATIBILITY_DIGEST
    assert settings.deployment_lock.components.provisioner.image == images.provisioner

    contract = json.loads(Path(settings.capacity_contract_path).read_text(encoding="utf-8"))
    from cryptography.hazmat.primitives import serialization

    signing = (
        _capacity_signing_key()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    assert (
        base64.urlsafe_b64encode(signing).rstrip(b"=").decode("ascii")
        == settings.capacity_receipt_public_key
    ), "the drill signs receipts with a key the contract does not pin"

    database, repository = database_repository or await _operation_store(scratch)
    clients = _kubernetes_clients(kubeconfig)
    core_v1, apps_v1, batch_v1, api = clients
    witness = _TerminationWitness(core_v1)
    probes: list[tuple[str, str, int]] = []
    network = network or _start_network(kubeconfig)

    async def requester(method: str, url: str, **kwargs: Any) -> Any:
        return await network.private_request(method, url, **kwargs)

    async def external_probe(method: str, url: str, headers: dict[str, str]) -> int:
        status = await network.external_probe(method, url, headers)
        probes.append((method, url, status))
        return status

    components = build_live_provider_components(
        repository=repository,
        settings=settings,
        core_v1=witness,
        apps_v1=apps_v1,
        batch_v1=batch_v1,
        coordination_v1=kube_client.CoordinationV1Api(api),
        storage_v1=kube_client.StorageV1Api(api),
        custom_objects=kube_client.CustomObjectsApi(api),
        requester=requester,
        external_probe=external_probe,
    )
    hcloud = _RecordingHCloudVolumes()
    failures: list[dict[str, Any]] = []
    drill = _Drill(
        scratch=scratch,
        kubeconfig=kubeconfig,
        images=images,
        owner=owner,
        repository=repository,
        routine=build_routine_operation_worker(
            repository=repository,
            driver=_ObservedDriver(components.driver, failures, lane="routine"),
            worker_id=settings.worker_id,
            capacity_admission=components.capacity,
        ),
        volume=build_volume_registration_worker(
            repository=repository,
            driver=_ObservedDriver(
                _volume_registration_driver(core_v1, runtime_image=images.runtime, hcloud=hcloud),
                failures,
                lane="volume",
            ),
            worker_id="drill-volume-worker",
            capacity_admission=components.capacity,
        ),
        plane=components.plane,
        network=network,
        collector=_CapacityCollector(
            core_v1=core_v1, storage_v1=kube_client.StorageV1Api(api), contract=contract
        ),
        witness=witness,
        hcloud=hcloud,
        probes=probes,
        clients=clients,
        request=request or _first_provision_request(settings, owner),
        trace=FirstProvisionTrace(scratch),
        failures=failures,
    )
    return drill, database


@pytest.mark.timeout(3600)
def test_first_provision_reaches_final_serving_through_the_shipped_workers(
    first_provision_k3s: tuple[str, Path],
    drill_images: DrillImages,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit one PROVISION and let the shipped workers take it to FINAL/SERVING.

    Every checkpoint is produced by the real path: namespace, storage shell,
    first-consumer binding, volume registration, offline initialization,
    custody drain, vault fingerprint, governance migration, private readiness,
    and real Traefik route publication.
    """

    _k3s, kubeconfig = first_provision_k3s
    scratch = tmp_path_factory.mktemp("first-provision")
    # The shipped Helm adapter uses the ambient cluster configuration, as it
    # does in-cluster.
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    _install_platform_prerequisites(kubeconfig, scratch)

    async def drive() -> dict[str, Any]:
        drill, database = await _compose(scratch, kubeconfig, drill_images)
        try:
            operation = await drill.repository.submit(
                "provision",
                "first-provision-drill",
                drill.request,
                wire_protocol=WIRE_PROTOCOL_V2,
            )
            drill.operation_id = operation.id
            final = await drill.drive_until(lambda _checkpoint: False)
            stored = await drill.repository.get_by_id(operation.id)
            if stored.state is not OperationState.FINAL or final != "complete":
                raise await drill.refusal("the operation did not reach FINAL")

            cell = drill.cell(await drill.pvc_uid())
            custody = await asyncio.to_thread(_observe_custody, cell, now=int(time.time()))
            health = await drill.plane.health(drill.owner, drill.request, v2=True)
            readiness_url = (
                f"https://control.drill.invalid/cells/{drill.owner.subject_id}"
                "/private/exomem/v1/ready"
            )
            ingress_headers = {
                "Authorization": f"Bearer {drill.request['serviceCredential']}",
                "X-Exomem-Cell-Id": drill.owner.subject_id,
                "X-Exomem-Protocol-Version": RUNTIME_TARGET["protocolVersion"],
                "X-Exomem-Request-Id": str(uuid.uuid4()),
                "X-Exomem-Principal-Scope": base64.urlsafe_b64encode(
                    hashlib.sha256(b"exomem-provisioner-private-cell-control").digest()
                )
                .rstrip(b"=")
                .decode("ascii"),
            }
            ingress_statuses: list[int] = []
            deadline = time.monotonic() + 60
            while True:
                ingress = await drill.network.ingress_request("GET", readiness_url, ingress_headers)
                ingress_statuses.append(ingress.status_code)
                if ingress.status_code == 200 or time.monotonic() >= deadline:
                    break
                assert ingress.status_code in {404, 503}, ingress.text
                await asyncio.sleep(1)
            assert ingress.status_code == 200, (
                f"real ingress did not converge: statuses={ingress_statuses}; body={ingress.text}"
            )
            ingress_body = ingress.json()
            assert ingress_body.get("success") is True, ingress_body
            governance = ingress_body["data"]["governance"]
            resources = await drill.repository.list_resources(
                tenant_id=drill.owner.tenant_id, cell_id=drill.owner.subject_id
            )
            bindings = {
                checkpoint.split(":")[2]
                for checkpoint in drill.trace.checkpoints
                if checkpoint.startswith("gpi1:")
            }
            migration_checkpoint = next(
                checkpoint
                for checkpoint in reversed(drill.trace.checkpoints)
                if _migration_complete(checkpoint)
            )
            evidence = {
                "phases": drill.trace.phases(),
                "gpi1Bindings": len(bindings),
                "migrationBindingMatches": MigrationCheckpoint.decode(migration_checkpoint).binding
                in bindings,
                "resourceKinds": sorted(item.kind.value for item in resources),
                "hcloudLabelled": [item.resource_name for item in drill.hcloud.labelled.values()],
                "externalProbes": len(drill.probes),
                "externalProbeStatuses": [status for _method, _url, status in drill.probes],
                "receiptsPublished": len(drill.collector.published),
                "retriedTerminations": len(drill.trace.retried),
                "operationState": stored.state.value,
                "privateReady": health.ready,
                "routes": list(drill.plane.routes_enabled(drill.owner)),
                "ingress": {
                    "status": ingress.status_code,
                    "statuses": ingress_statuses,
                    "actualSchema": governance["actualSchema"],
                    "governanceEnrolled": governance["governanceEnrolled"],
                    "storeAgreement": governance["storeAgreement"],
                    "custodyRevision": governance["custodyRevision"],
                },
                "custody": {
                    "governanceEnrolled": custody.governance_enrolled,
                    "membershipSchema": custody.membership_schema_version,
                    "replicaState": custody.replica_state,
                },
            }
            drill.trace.write("first-provision-through-serving", summary=evidence)
            return evidence
        finally:
            drill.network.close()
            await database.dispose()

    evidence = asyncio.run(drive())

    assert evidence["phases"] == EXPECTED_THROUGH_SERVING
    # One PVC binding carries from first consumer through the migration.
    assert evidence["gpi1Bindings"] == 1 and evidence["migrationBindingMatches"]
    assert {
        ResourceKind.KUBERNETES_NAMESPACE.value,
        ResourceKind.HELM_RELEASE.value,
        ResourceKind.PVC.value,
        ResourceKind.VOLUME.value,
    } <= set(evidence["resourceKinds"])
    assert evidence["hcloudLabelled"] == [OWNER.resource_name]
    assert evidence["externalProbes"] > 0, "route closure was never proven at the edge seam"
    assert set(evidence["externalProbeStatuses"]) <= {401, 403, 404, 503}
    assert evidence["operationState"] == "final"
    assert evidence["privateReady"] is True
    assert evidence["routes"] == [True, True]
    assert evidence["ingress"]["status"] == 200
    assert evidence["ingress"]["actualSchema"] == 4
    assert evidence["ingress"]["governanceEnrolled"] is True
    assert evidence["ingress"]["storeAgreement"] is True
    assert evidence["custody"] == {
        "governanceEnrolled": True,
        "membershipSchema": 4,
        "replicaState": "SERVING",
    }
