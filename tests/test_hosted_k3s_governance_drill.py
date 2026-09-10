"""Agent-run disposable governance v3-to-v4 migration and recovery drill.

The fake-adapter suites prove the coordinator's decisions. This module proves
the same decisions against a real Kubernetes API, a real PersistentVolume, a
real Secret CAS and the real target-image runner reporting through
``/dev/termination-log``. It runs the shipped provisioner composition --
``build_live_provider_components`` fed by a deployment lock that selects
``migrationMode: governance-v3-to-v4`` -- and substitutes only a disposable
SQLite ``OperationRepository`` for the production database.

Nothing here edits provisioner or runtime source. A boundary that cannot be
resumed with the shipped coordinator is a defect to report, not to patch.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

RUN_DRILL = os.environ.get("RUN_K3S_GOVERNANCE_DRILL_TEST") == "1"
if not RUN_DRILL:  # pragma: no cover - the gate is the point
    pytest.skip(
        "set RUN_K3S_GOVERNANCE_DRILL_TEST=1 to run the disposable governance drill",
        allow_module_level=True,
    )

# Core CI shards run the runtime distribution without the separately packaged
# provisioner. Skip the whole module rather than failing collection.
pytest.importorskip("exomem_provisioner", reason="requires the provisioner package")
pytest.importorskip("kubernetes", reason="requires the provisioner Kubernetes SDK")

# The adapter refuses any other Helm CLI version; read the pin the workflow
# installs rather than restating it here.
_TOOL_VERSIONS = Path(__file__).resolve().parents[1] / "infra" / "tool-versions.env"
_HELM_VERSION = next(
    line.split("=", 1)[1].strip()
    for line in _TOOL_VERSIONS.read_text(encoding="utf-8").splitlines()
    if line.startswith("HELM_VERSION=")
)

from exomem_provisioner.adapters import KubernetesCellAdapter  # noqa: E402
from exomem_provisioner.authorization_membership import (  # noqa: E402
    build_initial_hosted_authorization_bundle,
    inspect_hosted_authorization_bundle,
    transition_hosted_authorization_bundle,
)
from exomem_provisioner.driver import DriverPending, EffectContext  # noqa: E402
from exomem_provisioner.governance_migration_checkpoint import (  # noqa: E402
    MigrationCheckpoint,
)
from exomem_provisioner.governance_migration_coordinator import (  # noqa: E402
    HostedGovernanceMigrationCoordinator,
)
from exomem_provisioner.governance_migration_job import (  # noqa: E402
    KubernetesGovernanceMigrationAdapter,
)
from exomem_provisioner.lifecycle import (  # noqa: E402
    LifecycleConfig,
    OpaqueProviderMetadata,
    _fixed_helm_values,
)
from exomem_provisioner.provider_identity import (  # noqa: E402
    ProviderRecoveryIdentityCodec,
    cell_provider_recovery_envelopes,
    provider_operation_resource_name,
)
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2  # noqa: E402
from test_hosted_k3s_admission import (  # noqa: E402
    CELL,
    HELM,
    PLATFORM,
    ROOT,
    _build_current_runtime_image,
    _import_runtime_image,
    _pod_logs,
    _run,
    _successful_job_logs,
    _yaml,
)
from test_hosted_k3s_admission import k3s as k3s  # noqa: E402,F401 - reuse the exact harness

CODEC = ProviderRecoveryIdentityCodec.from_secret("k3s-governance-drill-provider-root")
CUSTODY_SECRET = "exomem-authorization-session"
STORAGE_CLASS = "exomem-hcloud-encrypted-retain"
SOURCE_SOFTWARE_VERSION = "0.48.0"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class DrillCell:
    """One disposable cell, already stopped, drained and seeded on the cluster."""

    owner: OpaqueProviderMetadata
    envelopes: dict[str, str]
    pvc_uid: str
    runtime_image: str
    release: str
    kubeconfig: Path
    core_v1: Any
    apps_v1: Any
    batch_v1: Any

    @property
    def namespace(self) -> str:
        return self.owner.resource_name

    @property
    def custody_envelope(self) -> str:
        return self.envelopes["authorizationSessionSecret"]

    @property
    def job_envelope(self) -> str:
        return self.envelopes["initJob"]


def _lifecycle_config(*, image: str, release: str) -> LifecycleConfig:
    target = {
        "releaseVersion": release,
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v1",
        "gatewayContractDigest": "b" * 64,
        "commandFingerprint": "c" * 64,
        "schemaDigest": "d" * 64,
    }
    return LifecycleConfig(
        image=image,
        chart_path=str(CELL),
        chart_version=yaml.safe_load((CELL / "Chart.yaml").read_text(encoding="utf-8"))["version"],
        helm_version=_HELM_VERSION,
        control_hostname="control.drill.invalid",
        transfer_hostname="transfer.drill.invalid",
        browser_origin="https://app.drill.invalid",
        release_version=release,
        protocol_version="1",
        contract_digest="b" * 64,
        location="fsn1",
        runtime_target=target,
        legacy_runtime_units={(release, "1"): target},
        records_reader_version=2,
        lifecycle_actions_enabled=False,
        migration_mode="governance-v3-to-v4",
    )


def _cell_request(*, config: LifecycleConfig, envelopes: dict[str, str]) -> dict[str, Any]:
    return {
        "provisionMode": "serve",
        "runtimeTarget": dict(config.runtime_target),
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
        "serviceCredential": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode(),
        "_providerRecoveryEnvelopes": dict(envelopes),
    }


def _kubernetes_clients(kubeconfig: Path) -> tuple[Any, Any, Any, Any]:
    from kubernetes import client
    from kubernetes import config as kube_config

    loaded = kube_config.new_client_from_config(config_file=str(kubeconfig))
    return (
        client.CoreV1Api(loaded),
        client.AppsV1Api(loaded),
        client.BatchV1Api(loaded),
        loaded,
    )


def _helm(kubeconfig: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return _run(
        [str(HELM), "--kubeconfig", str(kubeconfig), *arguments],
        env=os.environ | {"HELM_DRIVER": "configmap"},
    )


def _values_file(directory: Path, name: str, values: dict[str, Any]) -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(values, sort_keys=True), encoding="utf-8")
    return path


def _host_kubectl(
    kubeconfig: Path,
    arguments: list[str],
    *,
    documents: list[dict[str, Any]] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["kubectl", "--kubeconfig", str(kubeconfig), *arguments],
        input_text=None if documents is None else _yaml(documents),
        check=check,
    )


def _schema_three_draining_bundle(
    owner: OpaqueProviderMetadata, envelope: str, *, now: int, claimed_schema: int = 3
):
    """Mint the custody a deployed pre-migration cell carries.

    ``claimed_schema=4`` reproduces the deployed legacy defect: a signed
    membership claiming schema 4 over a store that is actually v3, which the
    coordinator's inspect phase must repair before it may prepare.
    """

    replica = owner.resource_name + "-0"
    initial = build_initial_hosted_authorization_bundle(
        cell_id=owner.subject_id,
        logical_vault_id=owner.tenant_id,
        replica_id=replica,
        software_version=SOURCE_SOFTWARE_VERSION,
        schema_version=claimed_schema,
        recovery_envelope=envelope,
        now=now,
    )
    return transition_hosted_authorization_bundle(
        initial.files,
        expected_cell_id=owner.subject_id,
        expected_logical_vault_id=owner.tenant_id,
        expected_replica_id=replica,
        expected_software_version=SOURCE_SOFTWARE_VERSION,
        expected_schema_version=claimed_schema,
        expected_recovery_envelope=envelope,
        target_state="DRAINING",
        target_no_in_flight=True,
        now=now,
    )


@pytest.fixture(scope="module")
def runtime_image(k3s: str) -> Iterator[tuple[str, str]]:
    """Build the current runtime once and import it under its digest reference."""

    image, gate = _build_current_runtime_image()
    try:
        yield _import_runtime_image(k3s, image), gate["release"]
    finally:
        _run(["docker", "image", "rm", "--force", image], check=False)


def _seed_cell(
    k3s: str,
    *,
    scratch: Path,
    runtime: tuple[str, str],
    cell_id: str,
    operation_id: str,
    fence: int,
    volume: str,
    claimed_schema: int = 3,
) -> tuple[DrillCell, Any]:
    """Install the shipped cell chart, run its initializer, then stop the cell."""

    from test_hosted_k3s_admission import _host_kubeconfig, _render

    image, release = runtime
    owner = OpaqueProviderMetadata("tenant-drill", cell_id, operation_id, fence)
    resource = owner.resource_name
    envelopes = cell_provider_recovery_envelopes(
        CODEC,
        tenant_id=owner.tenant_id,
        cell_id=owner.subject_id,
        operation_id=owner.operation_id,
        fence_generation=owner.fence_generation,
        resource_name=resource,
        operation_resource_name=provider_operation_resource_name(owner.operation_id),
    )
    config = _lifecycle_config(image=image, release=release)
    request = _cell_request(config=config, envelopes=envelopes)
    values = _fixed_helm_values(owner, request, config)
    kubeconfig = _host_kubeconfig(k3s, scratch / f"kubeconfig-{cell_id}")

    # The shipped order publishes custody before Helm can start anything, so the
    # chart's required revision names an authenticated bundle that already exists.
    source_bundle = _schema_three_draining_bundle(
        owner,
        envelopes["authorizationSessionSecret"],
        now=int(time.time()),
        claimed_schema=claimed_schema,
    )
    values["authorizationSessionRevision"] = source_bundle.revision
    bootstrap = _values_file(scratch, f"{cell_id}-initialize.yaml", values)
    namespace_document = next(
        item for item in _render(CELL, bootstrap, resource) if item.get("kind") == "Namespace"
    )
    namespace_document["metadata"].setdefault("labels", {})[
        "app.kubernetes.io/managed-by"
    ] = "Helm"
    namespace_document["metadata"]["annotations"].update(
        {"meta.helm.sh/release-name": resource, "meta.helm.sh/release-namespace": resource}
    )

    credential = base64.urlsafe_b64encode(hashlib.sha256(cell_id.encode()).digest())
    credential = credential.rstrip(b"=").decode("ascii")
    _host_kubectl(
        kubeconfig,
        ["apply", "--filename=-"],
        documents=[
            namespace_document,
            {
                "apiVersion": "node.k8s.io/v1",
                "kind": "RuntimeClass",
                "metadata": {"name": "exomem-storage-init"},
                "handler": "runc",
            },
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": volume},
                "spec": {
                    "capacity": {"storage": "10Gi"},
                    "accessModes": ["ReadWriteOnce"],
                    "volumeMode": "Filesystem",
                    "persistentVolumeReclaimPolicy": "Retain",
                    "storageClassName": STORAGE_CLASS,
                    "claimRef": {"namespace": resource, "name": resource + "-data"},
                    "hostPath": {"path": "/var/lib/" + volume, "type": "DirectoryOrCreate"},
                },
            },
        ],
    )
    core_v1, apps_v1, batch_v1, _client = _kubernetes_clients(kubeconfig)
    cell = DrillCell(
        owner=owner,
        envelopes=envelopes,
        pvc_uid="",
        runtime_image=image,
        release=release,
        kubeconfig=kubeconfig,
        core_v1=core_v1,
        apps_v1=apps_v1,
        batch_v1=batch_v1,
    )
    _publish_source_custody(cell, source_bundle)
    _host_kubectl(
        kubeconfig,
        ["apply", "--filename=-"],
        documents=[
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "exomem-cell-credentials", "namespace": resource},
                "type": "Opaque",
                "stringData": {
                    "credentials.json": json.dumps(
                        {"schema_version": 1, "credentials": {"1": credential}},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                },
            }
        ],
    )
    _helm(
        kubeconfig,
        [
            "upgrade",
            "--install",
            resource,
            str(CELL),
            "--namespace",
            resource,
            "--values",
            str(bootstrap),
            "--wait",
            "--wait-for-jobs",
            "--timeout",
            "300s",
        ],
    )
    envelope = json.loads(_successful_job_logs(k3s, resource, resource + "-init"))
    assert envelope["ok"] is True, envelope

    # The migration Job reuses this exact fixed slot. Prove the initializer is
    # gone -- Job and pods -- before anything else claims it.
    stopped = dict(values, workloadMode="restore")
    _helm(
        kubeconfig,
        [
            "upgrade",
            "--install",
            resource,
            str(CELL),
            "--namespace",
            resource,
            "--values",
            str(_values_file(scratch, f"{cell_id}-restore.yaml", stopped)),
            "--wait",
            "--timeout",
            "120s",
        ],
    )
    for _ in range(60):
        remaining = json.loads(
            _host_kubectl(
                kubeconfig,
                ["get", "pods", "--namespace", resource, "--output=json"],
            ).stdout
        )["items"]
        if not remaining:
            break
        time.sleep(1)
    else:  # pragma: no cover - diagnostic
        raise AssertionError(_pod_logs(k3s, resource, "--all"))

    pvc = json.loads(
        _host_kubectl(
            kubeconfig,
            ["get", "pvc", resource + "-data", "--namespace", resource, "--output=json"],
        ).stdout
    )
    assert pvc["status"]["phase"] == "Bound"
    return replace(cell, pvc_uid=pvc["metadata"]["uid"]), source_bundle


def _publish_source_custody(cell: DrillCell, bundle: Any) -> Any:
    """Create the pre-migration Secret through the shipped adapter, not kubectl."""

    adapter = KubernetesCellAdapter(
        core_v1=cell.core_v1, apps_v1=cell.apps_v1, identity_verifier=CODEC.verifier()
    )

    async def guard() -> None:
        return None

    asyncio.run(
        adapter.write_authorization_session_bundle(
            cell.owner,
            bundle.files,
            recovery_envelope=cell.custody_envelope,
            membership_epoch=bundle.epoch,
            membership_digest=bundle.membership_digest,
            revision=bundle.revision,
            create_only=True,
            effect_guard=guard,
        )
    )
    return bundle


def _identity(cell: DrillCell) -> dict[str, Any]:
    return {
        "expected_cell_id": cell.owner.subject_id,
        "expected_logical_vault_id": cell.owner.tenant_id,
        "expected_replica_id": cell.owner.resource_name + "-0",
        "expected_software_version": None,
        "expected_schema_version": None,
        "expected_recovery_envelope": cell.custody_envelope,
    }


def _observe_custody(cell: DrillCell, *, now: int) -> Any:
    adapter = KubernetesCellAdapter(
        core_v1=cell.core_v1, apps_v1=cell.apps_v1, identity_verifier=CODEC.verifier()
    )
    files = asyncio.run(adapter.read_authorization_session_bundle(cell.owner))
    assert files is not None
    return inspect_hosted_authorization_bundle(
        files, **_identity(cell), now=now, _require_fresh=False
    )


def _migration_context(cell: DrillCell, current: OpaqueProviderMetadata, checkpoint: str, guard):
    return EffectContext(
        operation_id="internal-" + current.operation_id,
        provider_operation_id=current.operation_id,
        tenant_id=current.tenant_id,
        cell_id=current.subject_id,
        fence_generation=current.fence_generation,
        wire_protocol=WIRE_PROTOCOL_V2,
        checkpoint=checkpoint,
        effect_guard=guard,
    )


@dataclass
class DrillTrace:
    """Content-safe evidence: identities, digests and phases -- never custody bytes."""

    scenario: str
    directory: Path
    started: float
    passes: list[dict[str, Any]]
    faults: list[str]

    def record(self, cell: DrillCell, checkpoint: str, *, terminal: dict[str, Any] | None) -> None:
        decoded = MigrationCheckpoint.decode(checkpoint)
        observed = _observe_custody(cell, now=int(time.time()))
        self.passes.append(
            {
                "phase": decoded.phase,
                "custodyRevision": observed.revision,
                "governanceEnrolled": bool(observed.governance_enrolled),
                "membershipSchema": observed.membership_schema_version,
                "replicaState": observed.replica_state,
                "actualSchema": None if terminal is None else terminal.get("actualSchema"),
                "runnerCode": None if terminal is None else terminal.get("code"),
                "activationTupleDigest": hashlib.sha256(
                    _canonical(
                        [
                            observed.activation_store_id,
                            observed.activation_epoch,
                            observed.activation_state_digest,
                        ]
                    )
                ).hexdigest(),
                "elapsedSeconds": round(time.monotonic() - self.started, 2),
            }
        )

    def write(self) -> Path:
        path = self.directory / f"{self.scenario}.json"
        path.write_text(
            json.dumps(
                {
                    "artifact": "exomem-hosted-governance-drill-evidence",
                    "schemaVersion": 1,
                    "scenario": self.scenario,
                    "faults": self.faults,
                    "passes": self.passes,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\n--- {self.scenario} evidence ---\n{path.read_text(encoding='utf-8')}")
        return path


_OBSERVE_SOURCE_STORE = """
import json, os, traceback
from pathlib import Path
from exomem.governance import store
vault = Path("/var/lib/exomem/vault")
state = Path("/var/lib/exomem/state")
result = {
    "sidecarPresent": store.sidecar_path(vault).exists(),
    "vaultWritable": os.access(vault, os.W_OK),
    "stateWritable": os.access(state, os.W_OK),
}
try:
    result["schema"] = store.authorization_session_schema_version(vault)
except Exception:
    result["schema"] = traceback.format_exc()[-600:]
try:
    connection = store.open_readonly_connection(vault)
    result["readonlyOpen"] = "ok" if connection is not None else "absent"
    if connection is not None:
        connection.close()
except Exception:
    result["readonlyOpen"] = traceback.format_exc()[-600:]
print("EXOMEM-DRILL " + json.dumps(result, sort_keys=True))
"""


_CLASSIFY_SOURCE_STORE = """
import json
from pathlib import Path
from exomem.governance import store
vault = Path("/var/lib/exomem/vault")
result = {"sidecarPresent": store.sidecar_path(vault).exists()}
try:
    result["schema"] = store.authorization_session_schema_version(vault)
except store.GovernanceStoreUnreadable:
    result["schema"] = "unreadable"
print("EXOMEM-DRILL " + json.dumps(result, sort_keys=True))
"""


def _drill_json(output: str) -> dict[str, Any]:
    line = next(
        item for item in reversed(output.splitlines()) if item.startswith("EXOMEM-DRILL ")
    )
    return json.loads(line.removeprefix("EXOMEM-DRILL "))


def _run_target_image_pod(
    cell: DrillCell,
    current: OpaqueProviderMetadata,
    *,
    name: str,
    script: str,
    phase: str = "prepare",
) -> str:
    """Run one short-lived pod with the migration Job's exact mounts and identity."""

    from exomem_provisioner.governance_migration_job import (
        MigrationJobRequest,
        build_governance_migration_job,
    )

    revision = _observe_custody(cell, now=int(time.time())).revision
    request = MigrationJobRequest(
        current,
        current.tenant_id,
        cell.pvc_uid,
        cell.runtime_image,
        revision,
        phase,
        None if phase == "inspect" else "a" * 64,
        "b" * 64 if phase == "commit" else None,
    )
    job = build_governance_migration_job(request, recovery_envelope=cell.job_envelope)
    template = copy.deepcopy(job["spec"]["template"])
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": cell.namespace,
            "labels": template["metadata"]["labels"],
            "annotations": template["metadata"]["annotations"],
        },
        "spec": template["spec"],
    }
    pod["spec"]["containers"][0]["args"] = ["-c", script]
    _host_kubectl(cell.kubeconfig, ["apply", "--filename=-"], documents=[pod])
    try:
        _host_kubectl(
            cell.kubeconfig,
            [
                "wait",
                "--namespace",
                cell.namespace,
                "--for=jsonpath={.status.phase}=Succeeded",
                "pod/" + name,
                "--timeout=180s",
            ],
            check=False,
        )
        logs = _host_kubectl(
            cell.kubeconfig,
            ["logs", "--namespace", cell.namespace, name, "-c", "exomem"],
            check=False,
        )
        return logs.stdout + logs.stderr
    finally:
        _host_kubectl(
            cell.kubeconfig,
            ["delete", "pod", name, "--namespace", cell.namespace, "--wait=true", "--timeout=120s"],
            check=False,
        )


def _classify_source_store(cell: DrillCell, current: OpaqueProviderMetadata) -> dict[str, Any]:
    """Read the store the shipped initializer left behind, through the runner's reader.

    Hosted initialization creates the never-served schema-3 genesis sidecar, so
    the coordinator can tell "never opened" apart from "could not read". The
    drill no longer seeds a store of its own: it asserts the shipped one.
    """

    return _drill_json(
        _run_target_image_pod(
            cell, current, name=cell.namespace + "-classify", script=_CLASSIFY_SOURCE_STORE
        )
    )


class LostAcknowledgement(Exception):
    """A real effect that landed and whose provider acknowledgement never arrived."""

    status = 503


_MIGRATION_PHASE_ANNOTATION = "exomem.io/governance-migration-phase"
_INTERCEPTED = frozenset(
    {
        "patch_namespaced_secret",
        "list_namespaced_pod",
        "read_namespaced_job",
        "create_namespaced_job",
        "delete_namespaced_job",
    }
)


class _Interceptor:
    """Observe real API calls and lose exactly one acknowledgement after its effect."""

    def __init__(self, delegate: Any, *, state: dict[str, Any]) -> None:
        self._delegate = delegate
        self._state = state

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if name not in _INTERCEPTED:
            return attribute

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            if name == "list_namespaced_pod":
                self._observe_pods(result)
                return result
            if name in {"read_namespaced_job", "create_namespaced_job"}:
                # The Job the coordinator actually submits names its own phase.
                # A checkpoint does not: an `e` replay runs prepare or commit.
                annotations = getattr(getattr(result, "metadata", None), "annotations", None) or {}
                self._state["jobPhase"] = annotations.get(_MIGRATION_PHASE_ANNOTATION)
                if name == "read_namespaced_job":
                    return result
            boundary = (
                f"{self._state['jobPhase']}-delete-ack"
                if name == "delete_namespaced_job"
                else f"{self._state['jobPhase']}-create-ack"
                if name == "create_namespaced_job"
                else _publication_boundary(
                    self._state, args[2] if len(args) > 2 else kwargs["body"]
                )
            )
            self._state["boundaries"].append(boundary)
            if boundary == self._state["interruption"] and not self._state["faults"]:
                self._state["faults"].append(boundary)
                raise LostAcknowledgement(boundary)
            return result

        return wrapped

    def _observe_pods(self, result: Any) -> None:
        for item in getattr(result, "items", []):
            for status in getattr(item.status, "container_statuses", None) or []:
                terminated = getattr(status.state, "terminated", None)
                message = getattr(terminated, "message", None)
                if status.name == "exomem" and message:
                    self._state["terminal"] = json.loads(message)


def _publication_boundary(state: dict[str, Any], body: dict[str, Any]) -> str:
    """Name the custody publication from the successor the adapter is patching.

    Classify it with the shipped inspector rather than by reading raw signed
    documents, so the drill cannot drift from the custody format.
    """

    successor = inspect_hosted_authorization_bundle(
        {name: value.encode("utf-8") for name, value in body["stringData"].items()},
        **state["identity"],
        now=int(time.time()),
        _require_fresh=False,
    )
    if successor.membership_schema_version == 4:
        return "completion-ack"
    return "enrollment-ack" if successor.governance_enrolled else "repair-ack"


def _drive_migration(
    cell: DrillCell,
    current: OpaqueProviderMetadata,
    *,
    trace: DrillTrace,
    checkpoint: str,
    interruption: str | None = None,
    lose_checkpoint_at: str | None = None,
    max_passes: int = 30,
) -> str:
    """Replay the shipped coordinator until it reaches `complete`, as a worker would."""

    guarded: list[int] = []
    state: dict[str, Any] = {
        "phase": "inspect",
        "interruption": interruption,
        "faults": trace.faults,
        "terminal": None,
        "jobPhase": None,
        "boundaries": [],
        "identity": _identity(cell),
        "kubeconfig": cell.kubeconfig,
    }
    lost: list[str] = []

    async def guard() -> None:
        guarded.append(1)

    for _ in range(max_passes):
        state["phase"] = (
            MigrationCheckpoint.decode(checkpoint).phase
            if checkpoint.startswith("gm1:")
            else "inspect"
        )
        core = _Interceptor(cell.core_v1, state=state)
        batch = _Interceptor(cell.batch_v1, state=state)
        coordinator = HostedGovernanceMigrationCoordinator(
            cell=KubernetesCellAdapter(
                core_v1=core, apps_v1=cell.apps_v1, identity_verifier=CODEC.verifier()
            ),
            jobs=KubernetesGovernanceMigrationAdapter(
                core_v1=core, apps_v1=cell.apps_v1, batch_v1=batch
            ),
        )
        pending = asyncio.run(
            coordinator.advance(
                context=_migration_context(cell, current, checkpoint, guard),
                metadata=current,
                owner=cell.owner,
                pvc_uid=cell.pvc_uid,
                runtime_image=cell.runtime_image,
                job_recovery_envelope=cell.job_envelope,
                custody_recovery_envelope=cell.custody_envelope,
            )
        )
        assert isinstance(pending, DriverPending)
        trace.record(cell, pending.checkpoint, terminal=state["terminal"])
        print("drill pass:", json.dumps(trace.passes[-1], sort_keys=True))
        terminal = state["terminal"]
        if terminal is not None and "actualSchema" not in terminal:
            # A content-free refusal is not a diagnosis. Observe the same store
            # through the runner's own reader, in the mounts of both an
            # inspecting and a preparing Job, so the failure names what the
            # runner actually saw instead of only its code.
            observed = {
                phase: _drill_json(
                    _run_target_image_pod(
                        cell,
                        current,
                        name=f"{cell.namespace}-observe-{phase}",
                        script=_OBSERVE_SOURCE_STORE,
                        phase=phase,
                    )
                )
                for phase in ("inspect", "prepare")
            }
            trace.passes[-1]["storeObservation"] = observed
            trace.write()
            raise AssertionError(
                f"the {state['phase']} migration Job refused: {terminal}\n"
                + json.dumps(observed, indent=2, sort_keys=True)
            )
        if lose_checkpoint_at is not None and state["phase"] == lose_checkpoint_at and not lost:
            # The worker died between the driver's success and its checkpoint
            # commit. The next claim replays the exact previous checkpoint.
            lost.append(state["phase"])
            trace.passes[-1]["checkpointLost"] = True
            continue
        checkpoint = pending.checkpoint
        if MigrationCheckpoint.decode(checkpoint).phase == "complete":
            assert guarded
            return checkpoint
    raise AssertionError("coordinator did not reach migration completion")


def _phases(trace: DrillTrace) -> list[str]:
    ordered: list[str] = []
    for entry in trace.passes:
        if not ordered or ordered[-1] != entry["phase"]:
            ordered.append(entry["phase"])
    return ordered


def _assert_migrated(cell: DrillCell, source: Any, trace: DrillTrace) -> Any:
    """Every completed drill must land on the same content-safe end state."""

    final = _observe_custody(cell, now=int(time.time()))
    assert _phases(trace) == ["inspect", "prepare", "enroll", "commit", "complete"]
    assert final.replica_state == "DRAINING" and final.no_in_flight
    assert final.governance_enrolled is True
    assert final.membership_schema_version == 4
    assert final.keyring == source.keyring, "migration must never rewrite the keyring"
    schemas = [entry["actualSchema"] for entry in trace.passes if entry["actualSchema"] is not None]
    assert schemas[0] == 3 and schemas[-1] == 4
    # One cutover, in one direction: a replayed commit reconciles the committed
    # store, and no pass may ever observe v3 again once v4 is durable.
    assert schemas == sorted(schemas), schemas
    assert {entry["actualSchema"] for entry in trace.passes if entry["phase"] == "complete"} == {4}
    remaining = json.loads(
        _host_kubectl(
            cell.kubeconfig,
            ["get", "jobs,pods", "--namespace", cell.namespace, "--output=json"],
        ).stdout
    )["items"]
    assert remaining == [], remaining
    return final


@pytest.mark.timeout(2700)
def test_governance_drill_migrates_a_seeded_schema_three_cell(
    k3s: str, runtime_image: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    scratch = tmp_path_factory.mktemp("governance-drill-baseline")
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-baseline",
        operation_id="provision-baseline",
        fence=3,
        volume="exomem-drill-baseline-pv",
    )
    assert source.membership_schema_version == 3
    assert source.governance_enrolled is not True
    current = OpaqueProviderMetadata(
        cell.owner.tenant_id, cell.owner.subject_id, "rollforward-baseline", 4
    )
    # Hosted initialization leaves a real never-served schema-3 sidecar, which
    # is exactly what the migration's inspect phase must classify.
    assert _classify_source_store(cell, current) == {"sidecarPresent": True, "schema": 3}
    trace = DrillTrace("baseline-uninterrupted", scratch, time.monotonic(), [], [])
    _drive_migration(
        cell, current, trace=trace, checkpoint="vault-fingerprinted-" + "0" * 64
    )
    final = _assert_migrated(cell, source, trace)
    assert trace.faults == []
    assert final.revision != source.revision
    trace.write()


@pytest.mark.timeout(2700)
@pytest.mark.parametrize(
    "interruption",
    [
        # Custody publications, in the order the coordinator reaches them.
        "repair-ack",
        "enrollment-ack",
        "completion-ack",
        # Job submission and exact cleanup, per phase.
        "inspect-create-ack",
        "inspect-delete-ack",
        "prepare-create-ack",
        "prepare-delete-ack",
        "commit-create-ack",
        "commit-delete-ack",
    ],
)
def test_governance_drill_resumes_the_same_operation_after_a_lost_acknowledgement(
    k3s: str,
    runtime_image: tuple[str, str],
    tmp_path_factory: pytest.TempPathFactory,
    interruption: str,
) -> None:
    """Lose one real acknowledgement per effect boundary; the same operation finishes."""

    scenario = "interrupted-" + interruption
    scratch = tmp_path_factory.mktemp("governance-drill-" + interruption)
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-" + interruption,
        operation_id="provision-" + interruption,
        fence=3,
        volume="exomem-drill-" + interruption + "-pv",
        # Only a cell whose signed membership wrongly claims schema 4 reaches
        # the one-time schema-claim repair publication.
        claimed_schema=4 if interruption == "repair-ack" else 3,
    )
    current = OpaqueProviderMetadata(
        cell.owner.tenant_id, cell.owner.subject_id, "rollforward-" + interruption, 4
    )
    assert _classify_source_store(cell, current)["schema"] == 3
    trace = DrillTrace(scenario, scratch, time.monotonic(), [], [])
    _drive_migration(
        cell,
        current,
        trace=trace,
        checkpoint="vault-fingerprinted-" + "0" * 64,
        interruption=interruption,
    )
    _assert_migrated(cell, source, trace)
    assert trace.faults == [interruption], trace.faults
    trace.write()


@pytest.mark.timeout(2700)
def test_governance_drill_replays_a_commit_whose_worker_died_before_its_checkpoint(
    k3s: str, runtime_image: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The commit Job lands a v4 transaction the worker never recorded.

    Exact replay under the retained checkpoint must reconcile the committed
    store rather than start a second cutover or restore the v3 backup.
    """

    scratch = tmp_path_factory.mktemp("governance-drill-commit-crash")
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-commit-crash",
        operation_id="provision-commit-crash",
        fence=3,
        volume="exomem-drill-commit-crash-pv",
    )
    current = OpaqueProviderMetadata(
        cell.owner.tenant_id, cell.owner.subject_id, "rollforward-commit-crash", 4
    )
    assert _classify_source_store(cell, current)["schema"] == 3
    trace = DrillTrace("interrupted-commit-checkpoint", scratch, time.monotonic(), [], [])
    _drive_migration(
        cell,
        current,
        trace=trace,
        checkpoint="vault-fingerprinted-" + "0" * 64,
        lose_checkpoint_at="commit",
    )
    final = _assert_migrated(cell, source, trace)
    replays = [entry for entry in trace.passes if entry.get("checkpointLost")]
    assert len(replays) == 1 and replays[0]["phase"] == "complete"
    # The dropped completion and its exact replay both report the committed v4
    # store; the replay never re-ran the cutover or restored the v3 backup.
    completions = [entry for entry in trace.passes if entry["phase"] == "complete"]
    assert len(completions) == 2
    assert completions[0]["actualSchema"] == completions[1]["actualSchema"] == 4
    # An exact replay republishes nothing: the custody revision is identical.
    assert completions[0]["custodyRevision"] == completions[1]["custodyRevision"]
    assert final.governance_enrolled is True
    trace.write()


@pytest.mark.timeout(1800)
def test_governance_drill_custody_cas_refuses_a_real_concurrent_writer(
    k3s: str, runtime_image: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A real API-server resourceVersion conflict is uncertainty, not a foreign successor.

    The fake adapters assert the precondition the coordinator sends. Only a real
    API server decides the conflict, so this drives the shipped Secret writer
    against one, with a concurrent writer landing inside the effect guard.
    """

    from exomem_provisioner.conflict_reason import ConflictReason
    from exomem_provisioner.driver import DriverRetryable
    from exomem_provisioner.lifecycle import MetadataConflict

    scratch = tmp_path_factory.mktemp("governance-drill-cas")
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-cas",
        operation_id="provision-cas",
        fence=3,
        volume="exomem-drill-cas-pv",
    )
    adapter = KubernetesCellAdapter(
        core_v1=cell.core_v1, apps_v1=cell.apps_v1, identity_verifier=CODEC.verifier()
    )
    published = _observe_custody(cell, now=int(time.time()))
    assert published.revision == source.revision

    async def concurrent_writer() -> None:
        # The guard runs after the predecessor read and immediately before the
        # patch, so this write is the exact race the precondition exists for.
        _host_kubectl(
            cell.kubeconfig,
            [
                "annotate",
                "secret",
                CUSTODY_SECRET,
                "--namespace",
                cell.namespace,
                f"exomem.io/drill-concurrent-writer={uuid.uuid4().hex}",
                "--overwrite",
            ],
        )

    def publish(*, expected_revision: str, guard: Any) -> None:
        asyncio.run(
            adapter.write_authorization_session_bundle(
                cell.owner,
                source.files,
                recovery_envelope=cell.custody_envelope,
                membership_epoch=source.epoch,
                membership_digest=source.membership_digest,
                revision=source.revision,
                expected_revision=expected_revision,
                effect_guard=guard,
            )
        )

    # A guarded write loses the race: the coordinator must keep its checkpoint
    # and reread, so the adapter reports uncertainty rather than a conflict.
    with pytest.raises(DriverRetryable):
        publish(expected_revision=source.revision, guard=concurrent_writer)
    assert _observe_custody(cell, now=int(time.time())).revision == source.revision

    # A stale predecessor is decided before any effect, and stays a conflict.
    async def guard() -> None:
        return None

    with pytest.raises(MetadataConflict) as refused:
        publish(expected_revision="f" * 64, guard=guard)
    assert (
        refused.value.reason
        is ConflictReason.AUTHORIZATION_SESSION_BUNDLE_PREDECESSOR_DIFFERS
    )
    assert _observe_custody(cell, now=int(time.time())).files == source.files


_BREAK_SOURCE_STORE = """
import json
import shutil
from pathlib import Path
from exomem.governance import store
sidecar = store.sidecar_path(Path("/var/lib/exomem/vault"))
if sidecar.is_dir():
    shutil.rmtree(sidecar)
elif sidecar.exists():
    sidecar.unlink()
sidecar.mkdir()
print("EXOMEM-DRILL " + json.dumps({"sidecarIsDirectory": sidecar.is_dir()}, sort_keys=True))
"""


@pytest.mark.timeout(1800)
def test_governance_drill_names_an_unreadable_store_and_keeps_its_checkpoint(
    k3s: str, runtime_image: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """An unreadable store is named as such and never advances the migration.

    Reporting it as "no schema" is what previously made a read-only mount look
    identical to a cell that had never opened its store. The runner must now
    say which one it is, and the coordinator must hold its exact checkpoint.
    """

    scratch = tmp_path_factory.mktemp("governance-drill-unreadable")
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-unreadable",
        operation_id="provision-unreadable",
        fence=3,
        volume="exomem-drill-unreadable-pv",
    )
    current = OpaqueProviderMetadata(
        cell.owner.tenant_id, cell.owner.subject_id, "rollforward-unreadable", 4
    )
    assert _classify_source_store(cell, current)["schema"] == 3
    broken = _drill_json(
        _run_target_image_pod(
            cell, current, name=cell.namespace + "-break", script=_BREAK_SOURCE_STORE
        )
    )
    assert broken == {"sidecarIsDirectory": True}
    assert _classify_source_store(cell, current) == {
        "sidecarPresent": True,
        "schema": "unreadable",
    }

    state: dict[str, Any] = {
        "phase": "inspect",
        "interruption": None,
        "faults": [],
        "terminal": None,
        "jobPhase": None,
        "boundaries": [],
        "identity": _identity(cell),
        "kubeconfig": cell.kubeconfig,
    }

    async def guard() -> None:
        return None

    checkpoint = "vault-fingerprinted-" + "0" * 64
    checkpoints = []
    for _ in range(3):
        core = _Interceptor(cell.core_v1, state=state)
        batch = _Interceptor(cell.batch_v1, state=state)
        coordinator = HostedGovernanceMigrationCoordinator(
            cell=KubernetesCellAdapter(
                core_v1=core, apps_v1=cell.apps_v1, identity_verifier=CODEC.verifier()
            ),
            jobs=KubernetesGovernanceMigrationAdapter(
                core_v1=core, apps_v1=cell.apps_v1, batch_v1=batch
            ),
        )
        pending = asyncio.run(
            coordinator.advance(
                context=_migration_context(cell, current, checkpoint, guard),
                metadata=current,
                owner=cell.owner,
                pvc_uid=cell.pvc_uid,
                runtime_image=cell.runtime_image,
                job_recovery_envelope=cell.job_envelope,
                custody_recovery_envelope=cell.custody_envelope,
            )
        )
        assert isinstance(pending, DriverPending)
        checkpoint = pending.checkpoint
        checkpoints.append(MigrationCheckpoint.decode(checkpoint))

    assert state["terminal"] == {"code": "HOSTED_GOVERNANCE_STORE_UNREADABLE"}
    # The barrier holds at inspect: no phase advance, no custody publication,
    # and the fixed Job slot is released after every refused attempt.
    assert {item.phase for item in checkpoints} == {"inspect"}
    assert _observe_custody(cell, now=int(time.time())).files == source.files
    remaining = json.loads(
        _host_kubectl(
            cell.kubeconfig,
            ["get", "jobs,pods", "--namespace", cell.namespace, "--output=json"],
        ).stdout
    )["items"]
    assert remaining == [], remaining
    DrillTrace(
        "negative-store-unreadable",
        scratch,
        time.monotonic(),
        [
            {
                "phase": item.phase,
                "runnerCode": state["terminal"]["code"],
                "custodyRevision": source.revision,
            }
            for item in checkpoints
        ],
        [],
    ).write()


def _pending_release(cell: DrillCell) -> dict[str, Any] | None:
    """Find the abandoned Helm release record exactly as an operator would."""

    items = json.loads(
        _host_kubectl(
            cell.kubeconfig,
            [
                "get",
                "configmap",
                "--namespace",
                cell.namespace,
                "--selector",
                f"owner=helm,name={cell.namespace},status=pending-upgrade",
                "--output=json",
            ],
        ).stdout
    )["items"]
    return items[0] if items else None


@pytest.mark.timeout(1800)
def test_governance_drill_reconciles_only_its_own_abandoned_helm_release(
    k3s: str, runtime_image: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Kill a real `helm upgrade --wait` mid-flight and reconcile what it left.

    Helm refuses every later attempt at the same target while a pending record
    stands, so target start cannot resume without clearing it. Only a real Helm
    writes that record; the fake runners in the provisioner suite cannot.
    """

    from exomem_provisioner.adapters import HelmCliAdapter, _subprocess_runner
    from exomem_provisioner.conflict_reason import ConflictReason
    from exomem_provisioner.driver import DriverRetryable
    from exomem_provisioner.lifecycle import MetadataConflict

    scratch = tmp_path_factory.mktemp("governance-drill-helm")
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-helm",
        operation_id="provision-helm",
        fence=3,
        volume="exomem-drill-helm-pv",
    )
    config = _lifecycle_config(image=cell.runtime_image, release=cell.release)
    request = _cell_request(config=config, envelopes=cell.envelopes)
    values = _fixed_helm_values(cell.owner, request, config)
    values["authorizationSessionRevision"] = source.revision
    killed: list[int | None] = []
    attempted: list[tuple[str, ...]] = []
    # The shipped adapter never passes --kubeconfig: in the cluster it uses the
    # ambient configuration. Point the pinned CLI at the disposable cluster the
    # same way, through the environment it already merges.
    cluster = {"KUBECONFIG": str(cell.kubeconfig)}

    async def plain_runner(argv: tuple[str, ...], environment: dict[str, str]) -> Any:
        return await _subprocess_runner(argv, dict(environment) | cluster)

    async def abandoning_runner(argv: tuple[str, ...], environment: dict[str, str]) -> Any:
        """Run the shipped command, then die mid `--wait` exactly once."""

        if "upgrade" not in argv or killed:
            return await plain_runner(argv, environment)
        attempted.append(argv)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(os.environ) | environment | cluster,
        )
        for _ in range(600):
            if process.returncode is not None:
                break
            if _pending_release(cell) is not None:
                break
            await asyncio.sleep(0.2)
        try:
            process.kill()
        except ProcessLookupError:
            # Helm returned on its own; the record it left is whatever it left.
            pass
        await process.communicate()
        killed.append(process.returncode)
        return type("Killed", (), {"returncode": -9, "stdout": "", "stderr": ""})()

    def adapter(runner: Any) -> Any:
        assert HELM is not None
        return HelmCliAdapter(
            binary=str(HELM),
            expected_version=config.helm_version,
            chart_path=str(CELL),
            chart_version=config.chart_version,
            runner=runner,
            core_v1=cell.core_v1,
        )

    async def guard() -> None:
        return None

    def apply(runner: Any, applied: dict[str, Any]) -> None:
        asyncio.run(
            adapter(runner).ensure_release(
                cell.owner, applied, rollback_on_failure=False, effect_guard=guard
            )
        )

    def history() -> list[dict[str, Any]]:
        assert HELM is not None
        return json.loads(
            _run(
                [
                    str(HELM),
                    "--kubeconfig",
                    str(cell.kubeconfig),
                    "history",
                    cell.namespace,
                    "--namespace",
                    cell.namespace,
                    "--output",
                    "json",
                ],
                env=os.environ | {"HELM_DRIVER": "configmap"},
            ).stdout
        )

    seeded = history()
    assert _pending_release(cell) is None, seeded
    assert all(item["status"] == "deployed" for item in seeded[-1:]), seeded

    started = time.monotonic()
    # An abandoned upgrade is an uncertain outcome, never a committed one.
    with pytest.raises(DriverRetryable) as abandoned_error:
        apply(abandoning_runner, values)
    assert len(killed) == 1, (
        f"{abandoned_error.value}; attempted={attempted}; history={history()}"
    )
    assert killed[0] in (-9, None), f"helm exited on its own with {killed[0]}"
    abandoned = _pending_release(cell)
    assert abandoned is not None, "killing helm mid --wait left no pending record"
    revision = abandoned["metadata"]["labels"]["version"]
    assert abandoned["metadata"]["name"] == f"sh.helm.release.v1.{cell.namespace}.v{revision}"

    # A record whose saved values are not this call's own stays fenced.
    with pytest.raises(MetadataConflict) as foreign:
        apply(plain_runner, values | {"browserOrigin": "https://other.drill.invalid"})
    assert foreign.value.reason is ConflictReason.HELM_PENDING_RELEASE_IS_FOREIGN
    still_there = _pending_release(cell)
    assert still_there is not None
    assert still_there["metadata"]["uid"] == abandoned["metadata"]["uid"]

    # Its own record is cleared under UID/resourceVersion preconditions, proven
    # gone, and only then does the exact target proceed in the same call.
    apply(plain_runner, values)
    assert _pending_release(cell) is None
    final = history()
    assert final[-1]["status"] == "deployed"
    assert not any(item["status"].startswith("pending-") for item in final)
    (scratch / "helm-pending-release.json").write_text(
        json.dumps(
            {
                "artifact": "exomem-hosted-governance-drill-evidence",
                "schemaVersion": 1,
                "scenario": "helm-pending-release-reconciliation",
                "abandonedRevision": int(revision),
                "abandonedRecord": abandoned["metadata"]["name"],
                "foreignAttemptRefused": foreign.value.reason.value,
                "finalHistory": [
                    {"revision": item["revision"], "status": item["status"]} for item in final
                ],
                "elapsedSeconds": round(time.monotonic() - started, 2),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _drill_capacity_contract(scratch: Path) -> tuple[Path, str]:
    """Pin the shipped capacity contract to a disposable verifier key.

    The composition authenticates its capacity contract against the public key
    it was given, so the production contract cannot be paired with anything but
    the production collector key. The drill exercises no capacity decision; it
    only needs a pair the shipped verifier accepts, so it re-pins the shipped
    contract bytes to a key generated for this run.
    """

    import hashlib as _hashlib

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(
        _hashlib.sha256(b"k3s-governance-drill-capacity").digest()
    )
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    contract = json.loads(
        (ROOT / "infra/helm/platform/files/private-alpha-capacity-v1.json").read_text("utf-8")
    )
    contract["receipt_authentication"]["capacity_public_key_id"] = _hashlib.sha256(raw).hexdigest()
    directory = scratch / "capacity"
    directory.mkdir(exist_ok=True)
    path = directory / "private-alpha-capacity-v1.json"
    path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _governance_deployment_lock(scratch: Path, cell: DrillCell) -> Path:
    """Write a real lock that selects the governance mode and the imported image."""

    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text(encoding="utf-8"))
    lock = json.loads(values["provisioner"]["deploymentLockJson"])
    lock["components"]["runtime"]["image"] = cell.runtime_image
    lock["runtimeTarget"]["releaseVersion"] = cell.release
    lock["runtimeUpgrade"] = {
        "compatibilityDigest": "a" * 64,
        "migrationMode": "governance-v3-to-v4",
        "substrateConsumerCommit": "b" * 40,
        "substrateTrustSha256": "c" * 64,
    }
    path = scratch / "deployment-lock.json"
    path.write_text(json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n", "utf-8")
    return path


async def _disposable_repository(scratch: Path) -> tuple[Any, Any]:
    """One disposable SQLite operation store with the shipped schema and codec."""

    from exomem_provisioner.config import ProvisionerSettings
    from exomem_provisioner.crypto import AesGcmEnvelopeCodec
    from exomem_provisioner.database import ProvisionerDatabase
    from exomem_provisioner.repository import OperationRepository

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
        max_failure_attempts=2,
    )


def _live_components(cell: DrillCell, repository: Any, scratch: Path) -> Any:
    """Build the SHIPPED provider composition; substitute only the repository."""

    from exomem_provisioner.config import ProviderWorkerSettings
    from exomem_provisioner.production import build_live_provider_components
    from kubernetes import client as kube_client

    api = _kubernetes_clients(cell.kubeconfig)[3]
    capacity_contract, capacity_key = _drill_capacity_contract(scratch)
    assert HELM is not None
    settings = ProviderWorkerSettings(
        deployment_lock_path=str(_governance_deployment_lock(scratch, cell)),
        runtime_selection="active",
        cell_chart_path=str(CELL),
        cell_chart_version=yaml.safe_load(
            (CELL / "Chart.yaml").read_text(encoding="utf-8")
        )["version"],
        helm_binary=str(HELM),
        helm_version=_HELM_VERSION,
        control_hostname="control.drill.invalid",
        transfer_hostname="transfer.drill.invalid",
        browser_origin="https://app.drill.invalid",
        location="fsn1",
        internal_origin="http://{resource}.{namespace}.svc.cluster.local:8765",
        worker_id="drill-worker",
        provider_recovery_public_key=CODEC.public_key(),
        capacity_receipt_public_key=capacity_key,
        capacity_contract_path=str(capacity_contract),
        capacity_receipt_namespace="exomem-platform",
        capacity_receipt_config_map="exomem-capacity-receipt",
        hcloud_server_id=1,
    )

    async def requester(method: str, url: str, **kwargs: Any) -> Any:
        raise AssertionError(f"the drill reached the private cell API: {method} {url}")

    async def external_probe(method: str, url: str, headers: dict[str, str]) -> int:
        raise AssertionError(f"the drill reached the public edge: {method} {url}")

    return build_live_provider_components(
        repository=repository,
        settings=settings,
        core_v1=kube_client.CoreV1Api(api),
        apps_v1=kube_client.AppsV1Api(api),
        batch_v1=kube_client.BatchV1Api(api),
        coordination_v1=kube_client.CoordinationV1Api(api),
        storage_v1=kube_client.StorageV1Api(api),
        custom_objects=kube_client.CustomObjectsApi(api),
        requester=requester,
        external_probe=external_probe,
    )


def _rollforward_request(cell: DrillCell, current: OpaqueProviderMetadata) -> dict[str, Any]:
    """The v2 request shape the worker stores, with this operation's own envelopes."""

    config = _lifecycle_config(image=cell.runtime_image, release=cell.release)
    envelopes = cell_provider_recovery_envelopes(
        CODEC,
        tenant_id=current.tenant_id,
        cell_id=current.subject_id,
        operation_id=current.operation_id,
        fence_generation=current.fence_generation,
        resource_name=current.resource_name,
        operation_resource_name=provider_operation_resource_name(current.operation_id),
    )
    return {
        "operationId": current.operation_id,
        "checkpoint": "requested",
        "fenceGeneration": current.fence_generation,
        "tenantId": current.tenant_id,
        "cellId": current.subject_id,
        "provisionMode": "serve",
        "runtimeTarget": dict(config.runtime_target),
        "serviceCredential": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode(),
        "workerPolicy": {"workerCount": 2, "semantic": True, "media": False},
        "_providerRecoveryEnvelopes": envelopes,
    }


@pytest.mark.timeout(2700)
def test_governance_drill_requeues_a_terminally_failed_migration_under_its_own_digest(
    k3s: str, runtime_image: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Compose the shipped provider, fail a real migration row terminally, requeue it.

    The composition here is `build_live_provider_components` fed by a real
    deployment lock that selects `governance-v3-to-v4`; only the operation store
    is disposable. The checkpoint the requeue must preserve is the one an actual
    cluster migration produced earlier in this test, not a synthetic string.
    """

    from exomem_provisioner.models import OperationAction, OperationState
    from exomem_provisioner.repository import RepositoryConflict
    from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2
    from exomem_provisioner.worker import ProvisionerWorker

    scratch = tmp_path_factory.mktemp("governance-drill-requeue")
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-requeue",
        operation_id="provision-requeue",
        fence=3,
        volume="exomem-drill-requeue-pv",
    )
    current = OpaqueProviderMetadata(
        cell.owner.tenant_id, cell.owner.subject_id, "rollforward-requeue", 4
    )
    assert _classify_source_store(cell, current)["schema"] == 3
    trace = DrillTrace("requeue-after-terminal-failure", scratch, time.monotonic(), [], [])
    migrated = _drive_migration(
        cell, current, trace=trace, checkpoint="vault-fingerprinted-" + "0" * 64
    )
    _assert_migrated(cell, source, trace)
    assert MigrationCheckpoint.decode(migrated).phase == "complete"

    async def drive() -> dict[str, Any]:
        database, repository = await _disposable_repository(scratch)
        try:
            components = _live_components(cell, repository, scratch)
            # Stop condition (b) is closed here: the shipped composition takes a
            # disposable repository and a lock JSON with no code change.
            assert components.driver is not None
            selected = components.lock.selected_runtime("active")
            assert selected.migrationMode == "governance-v3-to-v4"
            assert selected.image == cell.runtime_image

            request = _rollforward_request(cell, current)
            operation = await repository.submit(
                "rollforward", "drill-requeue", request, wire_protocol=WIRE_PROTOCOL_V2
            )
            claim = await repository.claim_next("drill-worker")
            assert claim is not None and claim.id == operation.id
            # Persist the checkpoint the real migration produced, through the
            # same repository call the worker itself uses.
            await repository.mark_pending(
                claim.id,
                "drill-worker",
                claim_token=claim.claim_token,
                claim_generation=claim.claim_generation,
                checkpoint=migrated,
                retry_after_seconds=1,
            )
            successor = await repository.submit(
                "resume",
                "drill-successor",
                _rollforward_request(
                    cell, replace(current, operation_id="successor-requeue", fence_generation=4)
                )
                | {"operationId": "successor-requeue"},
                wire_protocol=WIRE_PROTOCOL_V2,
            )

            worker = ProvisionerWorker(
                repository,
                components.driver,
                worker_id="drill-worker",
                allowed_actions=frozenset({OperationAction.ROLLFORWARD}),
            )
            attempts = 0
            while attempts < 12:
                attempts += 1
                stored = await repository.get_by_id(operation.id)
                if stored.state is OperationState.ERROR:
                    break
                if not await worker.run_once(
                    now=datetime.now(UTC) + timedelta(seconds=120 * attempts)
                ):
                    break
            exhausted = await repository.get_by_id(operation.id)
            assert exhausted.state is OperationState.ERROR, exhausted.state
            assert exhausted.checkpoint == migrated
            # The composed driver refuses the drill's request at the release-unit
            # gate on its first attempt; that terminal failure, not a retry
            # exhaustion, is the ERROR row the recovery must requeue.
            assert exhausted.error_code == "PROVISIONER_RELEASE_UNIT_MISMATCH"

            # The retained gm1 progress is the denial barrier; it outlives the
            # failure and keeps every successor on this cell out of the queue.
            assert await repository.claim_next("other-worker") is None
            assert (await repository.get_by_id(successor.id)).state is OperationState.PENDING

            before = await repository.get_by_id(operation.id)
            digest = await repository.preflight_governance_recovery(operation.id)
            assert await repository.get_by_id(operation.id) == before, "preflight wrote"

            with pytest.raises(RepositoryConflict):
                await repository.resume_governance_recovery(
                    operation.id, expected_digest="0" * 64
                )

            assert await repository.resume_governance_recovery(
                operation.id, expected_digest=digest
            ) == "queued"
            requeued = await repository.get_by_id(operation.id)
            assert requeued.state is OperationState.PENDING
            assert requeued.checkpoint == migrated
            assert requeued.error_code is None
            assert requeued.progress.get("failure_attempts") == 0

            # Replaying the acknowledged digest is idempotent, not a second write.
            assert await repository.resume_governance_recovery(
                operation.id, expected_digest=digest
            ) == "already-queued"

            resumed = await repository.claim_next(
                "drill-worker",
                allowed_actions=frozenset({OperationAction.ROLLFORWARD}),
            )
            assert resumed is not None
            assert resumed.id == operation.id, "the requeue resumed a different operation"
            assert resumed.checkpoint == migrated
            assert (await repository.get_by_id(successor.id)).state is OperationState.PENDING
            return {
                "checkpointPhase": MigrationCheckpoint.decode(migrated).phase,
                "errorCode": exhausted.error_code,
                "failureAttemptsBefore": before.progress.get("failure_attempts"),
                "resumedOperation": resumed.id,
                "successorBlocked": True,
            }
        finally:
            await database.dispose()

    recovery = asyncio.run(drive())
    trace.passes.append({"phase": "requeue", **recovery})
    trace.write()


_PRIVATE_READINESS = """
import base64, hashlib, json, urllib.error, urllib.request, uuid
scope = base64.urlsafe_b64encode(
    hashlib.sha256(b"exomem-provisioner-private-cell-control").digest()
).rstrip(b"=").decode("ascii")
bundle = json.load(open("/run/exomem/credentials/credentials.json"))
credential = bundle["credentials"]["1"]
import os
request = urllib.request.Request(
    "http://127.0.0.1:8765/private/exomem/v1/ready",
    headers={
        "Authorization": "Bearer " + credential,
        "X-Exomem-Cell-Id": os.environ["EXOMEM_HOSTED_CELL_ID"],
        "X-Exomem-Protocol-Version": "1",
        "X-Exomem-Request-Id": str(uuid.uuid4()),
        "X-Exomem-Principal-Scope": scope,
    },
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read())
except urllib.error.HTTPError as error:
    body = {"status": error.code, "body": error.read().decode("utf-8", "replace")[:600]}
print("EXOMEM-DRILL " + json.dumps(body, sort_keys=True))
"""


def _serve_schema_four(cell: DrillCell, scratch: Path, values: dict[str, Any], revision: str):
    """Start the migrated target with the shipped chart, exactly as rollforward does."""

    serving = dict(values, workloadMode="serve", authorizationSessionRevision=revision)
    serving["routes"] = dict(values["routes"], enabled=False)
    _helm(
        cell.kubeconfig,
        [
            "upgrade",
            "--install",
            cell.namespace,
            str(CELL),
            "--namespace",
            cell.namespace,
            "--values",
            str(_values_file(scratch, "serve.yaml", serving)),
            "--wait",
            "--timeout",
            "300s",
        ],
    )
    return serving


@pytest.mark.timeout(2700)
def test_governance_drill_serves_schema_four_and_verifies_its_private_proof(
    k3s: str, runtime_image: tuple[str, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Migrate, publish the schema-4 SERVING successor, start it, verify the proof.

    The producer is the real runtime's authenticated private readiness route and
    the consumer is the shipped provisioner verifier. Only a real cell running
    the migrated store can put those two on the same evidence.
    """

    from exomem_provisioner.authorization_membership import (
        DEFAULT_ATTESTATION_TTL_SECONDS,
    )
    from exomem_provisioner.governance_readiness import verify_governance_readiness

    scratch = tmp_path_factory.mktemp("governance-drill-serving")
    cell, source = _seed_cell(
        k3s,
        scratch=scratch,
        runtime=runtime_image,
        cell_id="drill-serving",
        operation_id="provision-serving",
        fence=3,
        volume="exomem-drill-serving-pv",
    )
    current = OpaqueProviderMetadata(
        cell.owner.tenant_id, cell.owner.subject_id, "rollforward-serving", 4
    )
    assert _classify_source_store(cell, current)["schema"] == 3
    trace = DrillTrace("serving-readiness-proof", scratch, time.monotonic(), [], [])
    _drive_migration(cell, current, trace=trace, checkpoint="vault-fingerprinted-" + "0" * 64)
    migrated = _assert_migrated(cell, source, trace)

    # The complete phase's own publication: schema 4, enrolled, DRAINING to
    # SERVING, capped by the signing key's remaining validity.
    identity = _identity(cell) | {"expected_schema_version": 4}
    now = int(time.time())
    keyring = json.loads(migrated.keyring)["accepted_keys"][0]
    ttl = min(DEFAULT_ATTESTATION_TTL_SECONDS, keyring["not_after"] - now)
    assert ttl > 300, "the drill's signing key has too little life left to serve"
    publication = transition_hosted_authorization_bundle(
        migrated.files,
        **(identity | {"expected_software_version": None}),
        target_state="SERVING",
        target_no_in_flight=False,
        target_software_version=cell.release,
        now=now,
        ttl_seconds=ttl,
    )
    adapter = KubernetesCellAdapter(
        core_v1=cell.core_v1, apps_v1=cell.apps_v1, identity_verifier=CODEC.verifier()
    )

    async def guard() -> None:
        return None

    asyncio.run(
        adapter.write_authorization_session_bundle(
            cell.owner,
            publication.files,
            recovery_envelope=cell.custody_envelope,
            membership_epoch=publication.epoch,
            membership_digest=publication.membership_digest,
            revision=publication.revision,
            expected_revision=migrated.revision,
            effect_guard=guard,
        )
    )

    config = _lifecycle_config(image=cell.runtime_image, release=cell.release)
    values = _fixed_helm_values(
        cell.owner, _cell_request(config=config, envelopes=cell.envelopes), config
    )
    started = time.monotonic()
    _serve_schema_four(cell, scratch, values, publication.revision)
    pod = cell.namespace + "-0"
    _host_kubectl(
        cell.kubeconfig,
        [
            "wait",
            "--namespace",
            cell.namespace,
            "--for=condition=Ready",
            f"pod/{pod}",
            "--timeout=300s",
        ],
    )
    ready = _drill_json(
        _host_kubectl(
            cell.kubeconfig,
            ["exec", "--namespace", cell.namespace, pod, "--", "python", "-c", _PRIVATE_READINESS],
        ).stdout
    )
    assert ready.get("success") is True, ready
    proof = ready["data"]["governance"]

    # The shipped verifier decides; the drill only supplies the two artifacts.
    verify_governance_readiness(
        proof,
        bundle=publication,
        cell_id=cell.owner.subject_id,
        vault_id=cell.owner.tenant_id,
        replica_id=cell.owner.resource_name + "-0",
        software_version=cell.release,
    )
    assert proof["actualSchema"] == 4
    assert proof["governanceEnrolled"] is True
    assert proof["storeAgreement"] is True
    assert proof["custodyRevision"] == publication.revision
    trace.passes.append(
        {
            "phase": "serving",
            "actualSchema": proof["actualSchema"],
            "governanceEnrolled": proof["governanceEnrolled"],
            "storeAgreement": proof["storeAgreement"],
            "custodyRevision": proof["custodyRevision"],
            "membershipEpoch": proof["membershipEpoch"],
            "activationTupleDigest": hashlib.sha256(
                _canonical(
                    [
                        proof["activationStoreId"],
                        proof["activationEpoch"],
                        proof["activationStateDigest"],
                    ]
                )
            ).hexdigest(),
            "elapsedSeconds": round(time.monotonic() - started, 2),
        }
    )
    trace.write()
