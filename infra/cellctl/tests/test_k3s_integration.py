"""Task 3.10: cellctl against a real, disposable K3s cluster.

Everything before this file exercises manifests.py/decide.py/reconcile.py
against in-memory fakes (test_reconcile.py) or a disposable Postgres alone
(test_db.py, test_fixture_schema.py). This file is the one place lane B
proves the imperative shell (k8s_client.py) actually talks to a real
Kubernetes API server the way the design assumes: real RBAC (as the actual
`cellctl` ServiceAccount, not cluster-admin), a real ValidatingAdmissionPolicy,
a real StatefulSet/PVC controller, real pod lifecycle, a real S3-compatible
backup/restore round trip, and real NetworkPolicy enforcement.

Skipped by default (like tests/test_hosted_k3s_admission.py's pattern in the
repository root): set RUN_CELLCTL_K3S_TEST=1 and have Docker + a `helm`
binary (HELM_BIN, or `helm` on PATH) available.

Deliberate stand-ins, each named where it matters below and in the delivery
report:

- Cell image: lane A's real image was not ready when this was written, so a
  minimal stand-in (serves /health, /health/ready, and test-only /write and
  /read endpoints round-tripping an owner-only file under the vault and
  under /data/host) is built and imported into the cluster, plus a second,
  deliberately-never-ready variant used only to force a canary failure. It
  implements no MCP protocol.
- Object storage: a real MinIO container (quay.io/minio/minio) stands in for
  B2's S3-compatible endpoint per the D8 amendment, so the backup/restore
  Jobs' restic calls are genuinely live. B2's own application-key management
  (create/delete/list, prefix restriction) stays on the already-tested
  in-process FakeB2, configured to hand out MinIO's one root credential as
  every "created" key's id/secret -- MinIO plays the network protocol, not
  B2's per-key security model, which stays covered by test_storage_fakes.py.
- Hetzner volumes: the already-tested in-process FakeHetznerVolumeProvider
  (no real Hetzner account is ever contacted).
- StorageClass: the platform chart's rendered `exomem-cloud-encrypted`
  StorageClass targets `csi.hetzner.cloud`, unusable here. A same-named
  StorageClass backed by k3s's built-in local-path provisioner is created
  instead, so real PVC binding still exercises the actual manifest (the
  PVC's `storageClassName` is unchanged) without a real cloud volume. One
  consequence: local-path PVs carry no `spec.csi` block, so
  `k8s_client.py`'s `pvc_volume_id` (read from `pv.spec.csi.volume_handle`)
  never populates -- a known, narrow gap in this stand-in's coverage, not a
  code defect (see the delivery report).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml
from kubernetes import client as k8s
from kubernetes import config as k8s_config

from cellctl.capacity import CapacityConfig
from cellctl.decide import ReconcileConfig
from cellctl.k8s_client import ClusterClient
from cellctl.manifests import (
    BACKUP_JOB_NAME,
    JOB_KIND_BACKUP,
    JOB_KIND_LABEL,
    JOB_KIND_RESTORE,
    ROW_GENERATION_ANNOTATION,
    hold_job_name,
    namespace_name,
)
from cellctl.reconcile import ClusterConfig, SecretsConfig, reconcile_once
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .conftest import CellDatabase

CELLCTL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CELLCTL_ROOT.parents[1]
PLATFORM_CHART = REPO_ROOT / "infra/helm/platform"
IMAGE_DIR = CELLCTL_ROOT / "tests" / "_k3s_standin_image"
BROKEN_IMAGE_DIR = CELLCTL_ROOT / "tests" / "_k3s_standin_image_broken"
K3S_GATE = REPO_ROOT / "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"

RUN_LIVE = os.environ.get("RUN_CELLCTL_K3S_TEST") == "1"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
K3S_IMAGE = json.loads(K3S_GATE.read_text(encoding="utf-8"))["k3sImage"]
STANDIN_REPOSITORY = "cellctl-standin.test/exomem-cell"
NAMESPACE_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
MINIO_IMAGE = "quay.io/minio/minio:latest"
MINIO_ACCESS_KEY = "cellctltest"
MINIO_SECRET_KEY = "cellctltestsecret1"
BUCKET_NAME = "exomem-cloud-backups"

# Short but real deadlines so the live test exercises the actual D6/D8 timeout
# branches without waiting on the 10/15-minute production defaults.
LIVE_CONFIG = ReconcileConfig(
    init_deadline=timedelta(seconds=90),
    # The StatefulSet's terminationGracePeriodSeconds is 30s, and the real
    # restic round trip against MinIO (repo init/snapshots + backup + forget
    # --prune) still has to fit inside this same deadline after that pod
    # actually terminates, so 60s cut it too close live; 180s leaves real
    # headroom while still exercising the real D8 timeout branch elsewhere.
    backup_deadline=timedelta(seconds=180),
    upgrade_ready_deadline=timedelta(seconds=20),
    backup_interval=timedelta(hours=24),  # only a never-backed-up cell's first backup fires
    # D8's production window is 02:00-05:00 UTC; the live run needs the nightly
    # path to be eligible whenever the suite happens to run.
    backup_window=(0, 24),
)


def _run(
    command: list[str], *, input_text: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=REPO_ROOT, input=input_text, text=True, capture_output=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{command}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def _cell_id() -> str:
    return "".join(random.choice(NAMESPACE_ALPHABET) for _ in range(16))


def _build_standin_image(image_dir: Path) -> str:
    tag = f"cellctl-standin/exomem-cell:{uuid.uuid4().hex[:12]}"
    _run(["docker", "build", "--tag", tag, str(image_dir)])
    return tag


def _import_image(k3s_name: str, image: str, *, repository: str) -> str:
    save = subprocess.Popen(["docker", "save", image], cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert save.stdout is not None
    imported = subprocess.run(
        ["docker", "exec", "--interactive", k3s_name, "ctr", "images", "import", "-"],
        cwd=REPO_ROOT,
        stdin=save.stdout,
        capture_output=True,
        check=False,
    )
    save.stdout.close()
    save_stderr = b"" if save.stderr is None else save.stderr.read()
    save_returncode = save.wait()
    assert imported.returncode == 0, (imported.stdout + imported.stderr).decode(errors="replace") + save_stderr.decode(errors="replace")
    assert save_returncode == 0, save_stderr.decode(errors="replace")

    images = _run(["docker", "exec", k3s_name, "ctr", "images", "ls"]).stdout
    manifest_digest = None
    for line in images.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0].endswith(image):
            manifest_digest = fields[2]
            break
    assert manifest_digest is not None, images
    digest_reference = f"{repository}@{manifest_digest}"
    _run(["docker", "exec", k3s_name, "ctr", "images", "tag", f"docker.io/library/{image}", digest_reference], check=False)
    verify = _run(["docker", "exec", k3s_name, "ctr", "images", "ls"]).stdout
    if digest_reference not in verify:
        imported_reference = None
        for line in images.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 3 and fields[0].endswith(image):
                imported_reference = fields[0]
                break
        assert imported_reference is not None, images
        _run(["docker", "exec", k3s_name, "ctr", "images", "tag", imported_reference, digest_reference])
    return digest_reference


def _kubectl(
    k3s_name: str, args: list[str], *, documents: list[dict[str, Any]] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    input_text = None if documents is None else "---\n".join(yaml.safe_dump(doc, sort_keys=False) for doc in documents)
    return _run(["docker", "exec", "--interactive", k3s_name, "kubectl", *args], input_text=input_text, check=check)


def _wait_for(predicate, *, timeout: float, interval: float = 1.0, description: str) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as error:  # noqa: BLE001 - retry until timeout, then raise
            last_error = error
        time.sleep(interval)
    detail = f" (last error: {last_error})" if last_error else ""
    raise AssertionError(f"timed out waiting for: {description}{detail}")


def _exec_py(k3s_name: str, namespace: str, pod: str, source: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _kubectl(k3s_name, ["exec", "--namespace", namespace, pod, "--", "python3", "-c", source], check=check)


def _print_job_diagnostics(k3s_name: str, namespace: str, job_kind: str) -> None:
    """Dumps a backup/restore Job's status and its pod's logs/termination
    message, for a fast root cause when a hold takes an unexpected branch --
    cheaper than re-running the whole live cluster to find out why."""

    # Job names are per hold (manifests.py hold_job_name), so list every Job
    # of this kind: the current hold's and any earlier one still inside its TTL.
    selector = f"{JOB_KIND_LABEL}={job_kind}"
    job = _kubectl(
        k3s_name,
        ["get", "jobs", "--namespace", namespace, f"--selector={selector}", "--output=json"],
        check=False,
    )
    print(f"[3.10] diagnostics: jobs {selector} status = {job.stdout or job.stderr}")
    logs = _kubectl(
        k3s_name,
        ["logs", "--namespace", namespace, f"--selector={selector}", "--all-containers", "--tail=200"],
        check=False,
    )
    print(f"[3.10] diagnostics: jobs {selector} pod logs =\n{logs.stdout}{logs.stderr}")
    pods = _kubectl(
        k3s_name,
        ["get", "pods", "--namespace", namespace, f"--selector={selector}", "--output=json"],
        check=False,
    )
    print(f"[3.10] diagnostics: jobs {selector} pod statuses = {pods.stdout or pods.stderr}")


@dataclass
class K3sCluster:
    name: str
    kubeconfig: Path
    host: str
    network: str
    minio_name: str
    minio_ip: str


def _bootstrap_backup_bucket(network: str, minio_ip: str) -> None:
    """Real B2 buckets are provisioned once by ops before any cell exists;
    this MinIO stand-in starts with none. Without this, the backup Job's
    `restic snapshots || restic init` (manifests.py) hits restic 0.17+'s S3
    backend retrying a missing-*bucket* error for minutes on end (found
    live: it fails fast on a missing *repository* in an existing bucket --
    the real first-backup shape -- but not on a missing bucket outright).
    One throwaway `restic init` creates the bucket as a side effect."""

    _run(
        [
            "docker", "run", "--rm", "--network", network,
            "-e", f"AWS_ACCESS_KEY_ID={MINIO_ACCESS_KEY}",
            "-e", f"AWS_SECRET_ACCESS_KEY={MINIO_SECRET_KEY}",
            "-e", "RESTIC_PASSWORD=bucket-bootstrap",
            "python:3.12-alpine", "sh", "-c",
            (
                "apk add --no-cache restic >/dev/null 2>&1 && "
                f"restic -r s3:http://{minio_ip}:443/{BUCKET_NAME}/cells/__bootstrap__ init"
            ),
        ]
    )


@pytest.fixture(scope="module")
def k3s(tmp_path_factory: pytest.TempPathFactory) -> Iterator[K3sCluster]:
    if not RUN_LIVE:
        pytest.skip("set RUN_CELLCTL_K3S_TEST=1 to run the real-cluster 3.10 integration test")
    if HELM is None:
        pytest.skip("set HELM_BIN (or put helm on PATH) to run the 3.10 integration test")
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for the 3.10 integration test")

    suffix = uuid.uuid4().hex[:12]
    name = f"cellctl-k3s-{suffix}"
    network = f"cellctl-net-{suffix}"
    minio_name = f"cellctl-minio-{suffix}"
    work = tmp_path_factory.mktemp("cellctl-k3s")

    _run(["docker", "network", "create", network])
    try:
        # k3s's embedded NetworkPolicy controller is on by default (D5/D11's
        # production k3s config in infra/ansible/roles/k3s/templates/
        # config.yaml.j2 disables only traefik, servicelb and local-storage,
        # never network-policy), so this container needs nothing extra to
        # match production's enforcement.
        # PodCertificateRequest is beta but off by default in v1.35 (locked
        # on from v1.36): with it off, the API server drops a podCertificate
        # projected source before admission, so NEW-1's denial of one could
        # never reach the ValidatingAdmissionPolicy it is meant to prove. The
        # gate needs certificates.k8s.io/v1beta1 served too, or the API
        # server's own PodCertificateRequest informer keeps it from ready.
        _run(
            [
                "docker", "run", "--privileged", "--detach", "--name", name,
                "--network", network,
                "--publish", "127.0.0.1::6443",
                K3S_IMAGE, "server",
                "--disable=traefik", "--disable=servicelb",
                "--write-kubeconfig-mode=600",
                "--kube-apiserver-arg=feature-gates=PodCertificateRequest=true",
                "--kube-apiserver-arg=runtime-config=certificates.k8s.io/v1beta1=true",
            ]
        )
        _run(
            [
                "docker", "run", "--detach", "--name", minio_name,
                "--network", network,
                "-e", f"MINIO_ROOT_USER={MINIO_ACCESS_KEY}",
                "-e", f"MINIO_ROOT_PASSWORD={MINIO_SECRET_KEY}",
                MINIO_IMAGE, "server", "/data", "--address", ":443",
            ]
        )
        try:
            _wait_for(
                lambda: _run(["docker", "exec", name, "kubectl", "get", "--raw=/readyz"], check=False).stdout.strip() == "ok",
                timeout=120, description="k3s API server readiness",
            )
            minio_ip = _run(
                [
                    "docker", "inspect", "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", minio_name,
                ]
            ).stdout.strip()
            assert minio_ip, "MinIO container has no IP on the shared docker network"
            _bootstrap_backup_bucket(network, minio_ip)

            published = _run(["docker", "port", name, "6443/tcp"]).stdout.strip()
            host_port = published.split(":")[-1]
            raw_kubeconfig = _run(["docker", "exec", name, "cat", "/etc/rancher/k3s/k3s.yaml"]).stdout
            raw_kubeconfig = raw_kubeconfig.replace("https://127.0.0.1:6443", f"https://127.0.0.1:{host_port}")
            kubeconfig_path = work / "kubeconfig.yaml"
            kubeconfig_path.write_text(raw_kubeconfig, encoding="utf-8")
            kubeconfig_path.chmod(0o600)
            yield K3sCluster(
                name=name,
                kubeconfig=kubeconfig_path,
                host=f"https://127.0.0.1:{host_port}",
                network=network,
                minio_name=minio_name,
                minio_ip=minio_ip,
            )
        finally:
            logs = _run(["docker", "logs", name], check=False)
            (work / "k3s.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
            minio_logs = _run(["docker", "logs", minio_name], check=False)
            (work / "minio.log").write_text(minio_logs.stdout + minio_logs.stderr, encoding="utf-8")
            _run(["docker", "rm", "--force", name], check=False)
            _run(["docker", "rm", "--force", minio_name], check=False)
    finally:
        _run(["docker", "network", "rm", network], check=False)


def _admin_api_client(k3s: K3sCluster) -> k8s.ApiClient:
    configuration = k8s.Configuration()
    k8s_config.load_kube_config(config_file=str(k3s.kubeconfig), client_configuration=configuration)
    return k8s.ApiClient(configuration)


def _sa_api_client(k3s: K3sCluster, *, token: str) -> k8s.ApiClient:
    configuration = k8s.Configuration()
    configuration.host = k3s.host
    configuration.verify_ssl = False
    return k8s.ApiClient(configuration, header_name="Authorization", header_value=f"Bearer {token}")


def _issue_sa_token(k3s: K3sCluster, *, name: str, namespace: str, duration: str = "60m") -> str:
    return _kubectl(
        k3s.name, ["create", "token", name, "--namespace", namespace, f"--duration={duration}"]
    ).stdout.strip()


def test_cellctl_against_a_real_k3s_cluster(k3s: K3sCluster, cell_db: CellDatabase) -> None:
    # === setup: images, RBAC/VAP/StorageClass, the real cellctl identity ===

    print("[3.10] building and importing the stand-in cell images (good + never-ready)")
    good_tag = _build_standin_image(IMAGE_DIR)
    broken_tag = _build_standin_image(BROKEN_IMAGE_DIR)
    good_image = _import_image(k3s.name, good_tag, repository=STANDIN_REPOSITORY)
    broken_image = _import_image(k3s.name, broken_tag, repository=STANDIN_REPOSITORY)
    repository, _, digest = good_image.partition("@")
    assert digest

    print("[3.10] rendering cellctl RBAC/ValidatingAdmissionPolicy/StorageClass from the platform chart")
    rendered = _run(
        [
            HELM, "template", "cellctl-k3s-test", str(PLATFORM_CHART),
            "--namespace", "exomem-platform",
            "--values", str(PLATFORM_CHART / "values.validation.yaml"),
            "--set", f"cellctl.cellImageRepository={repository}",
            # This cellctl renders job-egress with no except list, so MinIO on
            # the docker network stays reachable; the admission policy pins
            # job-egress to the chart's list, which must match.
            "--set-json", "cells.jobEgressExcept=[]",
            "--set", "cloudGateway.enabled=false",
            "--set", "cloudIngress.enabled=false",
            "--set", "cert-manager.enabled=false",
            "--show-only", "templates/cellctl.yaml",
            "--show-only", "templates/cloud-storage-class.yaml",
        ]
    ).stdout
    documents = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
    kinds = {doc["kind"] for doc in documents}
    assert {"Namespace", "ServiceAccount", "ClusterRole", "ClusterRoleBinding", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding", "StorageClass"} <= kinds

    cluster_role = next(doc for doc in documents if doc["kind"] == "ClusterRole")
    namespaces_rule = next(r for r in cluster_role["rules"] if r["resources"] == ["namespaces"])
    assert set(namespaces_rule["verbs"]) == {"get", "list", "watch", "create", "patch", "delete"}, (
        "D4 amendment: namespaces rule must be exactly get/list/watch/create/patch/delete"
    )

    non_storage_docs = [doc for doc in documents if doc["kind"] != "StorageClass"]
    storage_doc = next(doc for doc in documents if doc["kind"] == "StorageClass")
    _kubectl(k3s.name, ["apply", "--filename=-"], documents=non_storage_docs)
    # exomem-cloud is default-deny (cellctl.yaml), so the gateway-labelled
    # probe below reaches a cell only through the chart's own gateway
    # NetworkPolicy. Apply that policy alone; the gateway Deployment itself
    # is not part of this test.
    gateway_rendered = _run(
        [
            HELM, "template", "cellctl-k3s-test", str(PLATFORM_CHART),
            "--namespace", "exomem-platform",
            "--values", str(PLATFORM_CHART / "values.validation.yaml"),
            "--set", f"cellctl.cellImageRepository={repository}",
            # This cellctl renders job-egress with no except list, so MinIO on
            # the docker network stays reachable; the admission policy pins
            # job-egress to the chart's list, which must match.
            "--set-json", "cells.jobEgressExcept=[]",
            "--set", "cloudIngress.enabled=false",
            "--set", "cert-manager.enabled=false",
            "--show-only", "templates/cloud-gateway.yaml",
        ]
    ).stdout
    gateway_policy = next(
        doc
        for doc in yaml.safe_load_all(gateway_rendered)
        if isinstance(doc, dict) and doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "exomem-cloud-gateway"
    )
    _kubectl(k3s.name, ["apply", "--filename=-"], documents=[gateway_policy])
    local_path_class = copy.deepcopy(storage_doc)
    local_path_class["provisioner"] = "rancher.io/local-path"
    local_path_class.pop("parameters", None)
    local_path_class["reclaimPolicy"] = "Delete"
    local_path_class["volumeBindingMode"] = "WaitForFirstConsumer"
    _kubectl(k3s.name, ["apply", "--filename=-"], documents=[local_path_class])

    def _policy_type_checked() -> bool:
        status = json.loads(
            _kubectl(k3s.name, ["get", "validatingadmissionpolicy", "exomem-cellctl-scope", "--output=json"]).stdout
        ).get("status", {})
        if "typeChecking" not in status:
            return False
        assert status["typeChecking"].get("expressionWarnings", []) == [], status["typeChecking"]
        return True

    _wait_for(_policy_type_checked, timeout=30, description="ValidatingAdmissionPolicy exomem-cellctl-scope to type-check")

    # === the real cellctl ServiceAccount is the cluster client for every
    # functional scenario below (ruling: rerun under the real SA's token,
    # not an admin kubeconfig). Built from an issued TokenRequest, i.e. "a
    # kubeconfig built from a token issued for that ServiceAccount". ===

    token = _issue_sa_token(k3s, name="cellctl", namespace="exomem-cloud", duration="60m")
    sa_client = _sa_api_client(k3s, token=token)
    cluster = ClusterClient(sa_client)

    print("[3.10] scenario: the real cellctl ServiceAccount can now read a Namespace (D4 amendment)")
    # A namespace that does not exist yet must come back as "does not exist",
    # not a 403 -- proves the widened RBAC actually took effect, in contrast
    # to the earlier round's reproduced 403 under the old rule.
    probe_observation = cluster.observe("nonexistent-probe", "exo-cell-" + _cell_id())
    assert probe_observation.namespace_exists is False

    cell_id = _cell_id()
    namespace = namespace_name(cell_id)
    tenant_id = uuid.uuid4()
    admin_dsn = cell_db.dsn(role="substrate_owner")
    app_dsn = cell_db.dsn(role="exomem_cellctl")

    async def _exec_admin(sql: str, *args: object) -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(sql, *args)
        finally:
            await connection.close()

    async def _fetchval(sql: str, *args: object) -> object:
        connection = await asyncpg.connect(admin_dsn)
        try:
            return await connection.fetchval(sql, *args)
        finally:
            await connection.close()

    asyncio.run(
        _exec_admin("INSERT INTO exomem_tenants (id) VALUES ($1)", tenant_id)
    )
    asyncio.run(
        _exec_admin(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state, desired_image) "
            "VALUES ($1, $2, 'running', NULL)",
            cell_id,
            tenant_id,
        )
    )
    asyncio.run(
        _exec_admin("INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", json.dumps(good_image))
    )

    secrets_config = SecretsConfig(
        cell_token_key_current=b"k3s-integration-test-current-key-32b!",
        cell_token_key_previous=None,
        cell_token_key_version=1,
        cell_token_key_previous_version=None,
        backup_master_keys={1: b"0" * 32},
        backup_master_key_current_version=1,
    )
    cluster_config = ClusterConfig(
        object_storage_bucket=BUCKET_NAME,
        object_storage_endpoint=f"http://{k3s.minio_ip}:443",
        # K3s here has no Hetzner CSI driver, so its CSINode carries no
        # allocatable count: D9's fallback is how a rehearsal publishes slots.
        capacity=CapacityConfig(attachments_limit_fallback=100, headroom=5),
    )
    # D8: B2's own key-management stays faked; every "created" per-cell key
    # hands out MinIO's one root credential so restic can actually
    # authenticate against the live S3 double.
    fake_b2 = FakeB2(fixed_credentials=(MINIO_ACCESS_KEY, MINIO_SECRET_KEY))
    fake_hetzner = FakeHetznerVolumeProvider()

    async def _pass() -> None:
        connection = await asyncpg.connect(app_dsn)
        try:
            await reconcile_once(connection, cluster, fake_b2, fake_hetzner, secrets_config, cluster_config, config=LIVE_CONFIG)
        finally:
            await connection.close()

    async def _row() -> asyncpg.Record:
        connection = await asyncpg.connect(app_dsn)
        try:
            return await connection.fetchrow("SELECT * FROM exomem_cloud_cells WHERE cell_id = $1", cell_id)
        finally:
            await connection.close()

    async def _rollout_row() -> asyncpg.Record:
        connection = await asyncpg.connect(app_dsn)
        try:
            return await connection.fetchrow("SELECT * FROM exomem_cloud_rollout WHERE id = 1")
        finally:
            await connection.close()

    def _reconciled_to(*states: str) -> bool:
        asyncio.run(_pass())
        return asyncio.run(_row())["observed_state"] in states

    def _pod_name() -> str:
        # Excludes backup/restore Job pods (same label k8s_client.py uses to
        # tell them apart from the StatefulSet's own pod): a Job's pod can
        # still be around under its ttlSecondsAfterFinished window, and
        # counting it here would make this assert exactly the wrong thing.
        pods = json.loads(
            _kubectl(
                k3s.name,
                ["get", "pods", "--namespace", namespace, f"--selector=!{JOB_KIND_LABEL}", "--output=json"],
            ).stdout
        )["items"]
        assert len(pods) == 1, pods
        return pods[0]["metadata"]["name"]

    def _assert_owner_only_modes_intact(pod: str) -> None:
        source = (
            "import os, stat, json; "
            "d = oct(stat.S_IMODE(os.stat('/data/host').st_mode)); "
            "f = oct(stat.S_IMODE(os.stat('/data/host/canary.txt').st_mode)); "
            "print(json.dumps({'dir': d, 'file': f}))"
        )
        result = json.loads(_exec_py(k3s.name, namespace, pod, source).stdout)
        assert result["dir"] == "0o700", result
        assert result["file"] == "0o600", result

    # === scenario: create + converge to running on the real cluster, under
    # the real cellctl ServiceAccount's own RBAC. ===

    print("[3.10] scenario: create + converge to running under the real cellctl ServiceAccount")

    def _converged() -> bool:
        asyncio.run(_pass())
        row = asyncio.run(_row())
        return row["observed_state"] == "running" and row["ready"]

    _wait_for(_converged, timeout=180, interval=3, description="cell to converge to observed_state=running, ready=true")

    pvcs = json.loads(_kubectl(k3s.name, ["get", "pvc", "--namespace", namespace, "--output=json"]).stdout)["items"]
    assert len(pvcs) == 1
    assert pvcs[0]["status"]["phase"] == "Bound", pvcs[0]["status"]

    pod_name = _pod_name()
    write_source = (
        "import urllib.request; "
        "urllib.request.urlopen(urllib.request.Request("
        "'http://127.0.0.1:8765/write', data=b'k3s-integration-canary', method='POST')).read()"
    )
    _exec_py(k3s.name, namespace, pod_name, write_source)
    print("[3.10] scenario: owner-only modes (0700/0600) under /data/host after first start")
    _assert_owner_only_modes_intact(pod_name)

    # === scenario: governed write persists across a pod kill (real
    # StatefulSet/PVC recreation, not cellctl's doing). ===

    print("[3.10] scenario: governed write persists across a pod kill")
    original_uid = json.loads(
        _kubectl(k3s.name, ["get", "pod", "--namespace", namespace, pod_name, "--output=json"]).stdout
    )["metadata"]["uid"]
    _kubectl(k3s.name, ["delete", "pod", "--namespace", namespace, pod_name])

    def _recreated() -> bool:
        current = _kubectl(k3s.name, ["get", "pod", "--namespace", namespace, pod_name, "--output=json"], check=False)
        if current.returncode != 0:
            return False
        return json.loads(current.stdout)["metadata"]["uid"] != original_uid

    _wait_for(_recreated, timeout=120, description="StatefulSet to recreate the killed pod with a new UID")
    _kubectl(k3s.name, ["wait", "--namespace", namespace, "--for=condition=Ready", "pod", "-l", "app.kubernetes.io/name=exomem-cell", "--timeout=120s"])
    recreated_pod_name = _pod_name()
    read_source = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/read').read().decode())"
    read_result = _exec_py(k3s.name, namespace, recreated_pod_name, read_source).stdout.strip()
    assert read_result == "k3s-integration-canary", read_result
    print("[3.10] scenario: owner-only modes (0700/0600) under /data/host after pod replacement")
    _assert_owner_only_modes_intact(recreated_pod_name)

    # === scenario: a nightly backup under a real backup hold, with a real
    # restic snapshot id recorded, and a desired-state flip to read_only
    # honored only once the hold ends. ===

    print("[3.10] scenario: nightly backup hold begins; flip desired_state to read_only mid-hold")
    asyncio.run(_pass())  # last_backup_at IS NULL -> nightly-due fires now
    row = asyncio.run(_row())
    assert row["hold_kind"] == "backup", dict(row)
    asyncio.run(_exec_admin("UPDATE exomem_cloud_cells SET desired_state = 'read_only' WHERE cell_id = $1", cell_id))

    def _backup_hold_cleared_as_read_only() -> bool:
        asyncio.run(_pass())
        row = asyncio.run(_row())
        return row["hold_kind"] is None and row["observed_state"] == "read_only"

    try:
        _wait_for(_backup_hold_cleared_as_read_only, timeout=240, interval=2, description="backup hold to clear and resume as read_only")
    finally:
        _print_job_diagnostics(k3s.name, namespace, JOB_KIND_BACKUP)
    row = asyncio.run(_row())
    print(
        f"[3.10] backup hold cleared: hold_kind={row['hold_kind']!r} observed_state={row['observed_state']!r} "
        f"last_error_code={row['last_error_code']!r} last_backup_snapshot={row['last_backup_snapshot']!r}"
    )
    assert row["last_error_code"] is None, row["last_error_code"]
    snapshot_id = row["last_backup_snapshot"]
    assert snapshot_id and snapshot_id != "latest" and all(c in "0123456789abcdef" for c in snapshot_id), snapshot_id
    stateful_set = json.loads(_kubectl(k3s.name, ["get", "statefulset", "cell", "--namespace", namespace, "--output=json"]).stdout)
    env = {e["name"]: e.get("value") for e in stateful_set["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env.get("EXOMEM_CLOUD_READ_ONLY") == "1", env

    asyncio.run(_exec_admin("UPDATE exomem_cloud_cells SET desired_state = 'running' WHERE cell_id = $1", cell_id))
    _wait_for(
        lambda: _reconciled_to("running"),
        timeout=60, interval=2, description="cell to resume running before the upgrade scenarios",
    )

    # === B1/H9 regression, at the live-cluster level: this row already
    # carries a `last_backup_snapshot` from the nightly backup above (this
    # is deliberate -- both this scenario and the outage scenario below
    # used to NULL it out first, which is exactly the condition that masked
    # B1/H9: a stale row-level nightly snapshot standing in for an upgrade
    # attempt's own pre-upgrade backup). A governed write made *after* that
    # nightly snapshot, and captured by no snapshot yet, must survive the
    # restore below -- it can only do so if the restore used a fresh
    # snapshot taken by this attempt's own pre-upgrade backup, never the
    # older nightly one this row already has on it. ===

    print("[3.10] scenario: governed write after the nightly backup, before the upgrade attempt")
    post_nightly_pod = _pod_name()
    post_nightly_payload = "k3s-post-nightly-write"
    post_nightly_write_source = (
        "import urllib.request; "
        "urllib.request.urlopen(urllib.request.Request("
        f"'http://127.0.0.1:8765/write', data=b'{post_nightly_payload}', method='POST')).read()"
    )
    _exec_py(k3s.name, namespace, post_nightly_pod, post_nightly_write_source)
    post_nightly_confirm = _exec_py(k3s.name, namespace, post_nightly_pod, read_source).stdout.strip()
    assert post_nightly_confirm == post_nightly_payload, post_nightly_confirm

    # === scenario: a forced canary failure restores the pre-upgrade
    # snapshot with --delete and returns to the previous (good) image. ===

    print("[3.10] scenario: upgrade to a never-ready canary forces a restore back to the previous image")
    previous_image = good_image
    asyncio.run(_exec_admin("INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", json.dumps(broken_image)))

    _restore_check_count = 0

    def _restore_completed_back_to_previous() -> bool:
        nonlocal _restore_check_count
        _restore_check_count += 1
        asyncio.run(_pass())
        row = asyncio.run(_row())
        if _restore_check_count <= 3 or _restore_check_count % 10 == 0:
            sts = json.loads(
                _kubectl(k3s.name, ["get", "statefulset", "cell", "--namespace", namespace, "--output=json"], check=False).stdout or "{}"
            )
            sts_image = (sts.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or [{}])[0].get("image")
            sts_replicas = sts.get("spec", {}).get("replicas")
            sts_hold = (sts.get("metadata", {}).get("annotations") or {}).get("exomem.io/hold")
            sts_hold_started = (sts.get("metadata", {}).get("annotations") or {}).get("exomem.io/hold-started-at")
            print(
                f"[3.10] trace #{_restore_check_count}: row hold_kind={row['hold_kind']!r} observed_state={row['observed_state']!r} "
                f"observed_image={row['observed_image']!r} last_backup_snapshot={row['last_backup_snapshot']!r} "
                f"hold_started_at={row['hold_started_at']!r} | sts image={sts_image!r} replicas={sts_replicas!r} "
                f"hold={sts_hold!r} hold_started={sts_hold_started!r}"
            )
        return row["hold_kind"] is None and row["observed_image"] == previous_image and row["ready"]

    try:
        _wait_for(
            _restore_completed_back_to_previous, timeout=360, interval=2,
            description="forced-canary-failure restore to return to the previous image",
        )
    finally:
        _print_job_diagnostics(k3s.name, namespace, JOB_KIND_BACKUP)
        _print_job_diagnostics(k3s.name, namespace, JOB_KIND_RESTORE)
        diag_row = asyncio.run(_row())
        print(
            f"[3.10] diagnostics: cell row now hold_kind={diag_row['hold_kind']!r} "
            f"observed_state={diag_row['observed_state']!r} observed_image={diag_row['observed_image']!r} "
            f"ready={diag_row['ready']!r} last_error_code={diag_row['last_error_code']!r}"
        )
    row = asyncio.run(_row())
    rollout_row = asyncio.run(_rollout_row())
    print(
        f"[3.10] canary-failure resolved: hold_kind={row['hold_kind']!r} observed_image={row['observed_image']!r} "
        f"ready={row['ready']!r} last_error_code={row['last_error_code']!r} last_backup_snapshot={row['last_backup_snapshot']!r} | "
        f"rollout paused={rollout_row['paused']!r} error_code={rollout_row['error_code']!r} held_cell_id={rollout_row['held_cell_id']!r}"
    )
    assert rollout_row["paused"] is True, dict(rollout_row)
    assert rollout_row["error_code"] == "UPGRADE_READINESS_TIMEOUT"
    assert rollout_row["held_cell_id"] == cell_id
    # D6 step 2: the attempt's pre-upgrade backup is a real backup, so it
    # also moved `last_backup_snapshot` on from the nightly snapshot. The
    # restore itself reads only the exomem.io/pre-upgrade-snapshot
    # annotation. The read-back below is the real proof: it can equal the
    # post-nightly write only if the restore used this attempt's own
    # snapshot (taken after that write), never the older nightly one.
    attempt_snapshot = row["last_backup_snapshot"]
    assert attempt_snapshot != snapshot_id, (attempt_snapshot, snapshot_id)
    assert len(attempt_snapshot) == 64 and all(c in "0123456789abcdef" for c in attempt_snapshot), attempt_snapshot

    restored_pod = _pod_name()
    read_after_restore = _exec_py(k3s.name, namespace, restored_pod, read_source).stdout.strip()
    assert read_after_restore == post_nightly_payload, read_after_restore
    print("[3.10] scenario: owner-only modes (0700/0600) under /data/host after the restore")
    _assert_owner_only_modes_intact(restored_pod)

    # Un-pause the rollout for the next upgrade attempt.
    asyncio.run(_exec_admin("UPDATE exomem_cloud_rollout SET paused = false, error_code = NULL, held_cell_id = NULL WHERE id = 1"))

    # === scenario: an object-storage outage during the pre-upgrade backup
    # gives BACKUP_FAILED, and the cell restarts on its current image. ===

    print("[3.10] scenario: object-storage outage during a pre-upgrade backup -> BACKUP_FAILED, restart on current image")
    _run(["docker", "stop", k3s.minio_name])
    try:
        # No last_backup_snapshot reset here either (B1/H9): this row still
        # carries the previous attempt's snapshot, and the outage must still
        # fail this attempt's own fresh backup regardless of what that
        # column holds.
        asyncio.run(_exec_admin("INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", json.dumps(broken_image)))

        _outage_check_count = 0
        outage_hold_started_at = None
        outage_job_seen: dict | None = None
        outage_job_ran_seen = False
        outage_job_failed_seen = False

        def _backup_failed_and_restarted() -> bool:
            nonlocal _outage_check_count, outage_hold_started_at, outage_job_seen
            nonlocal outage_job_ran_seen, outage_job_failed_seen
            _outage_check_count += 1
            asyncio.run(_pass())
            row = asyncio.run(_row())
            if outage_hold_started_at is None and row["hold_kind"] == "upgrade":
                outage_hold_started_at = row["hold_started_at"]
            if row["hold_kind"] == "upgrade" and outage_hold_started_at is not None:
                # Read this hold's own backup Job while the hold still runs:
                # the BACKUP_FAILED exit deletes it before the cell restarts.
                job_name = hold_job_name(BACKUP_JOB_NAME, outage_hold_started_at.isoformat())
                found = _kubectl(k3s.name, ["get", "job", job_name, "--namespace", namespace, "--output=json"], check=False)
                if found.returncode == 0:
                    outage_job_seen = json.loads(found.stdout)
                    if outage_job_seen["status"].get("failed"):
                        outage_job_failed_seen = True
                    job_pods = json.loads(
                        _kubectl(
                            k3s.name,
                            ["get", "pods", "--namespace", namespace,
                             f"--selector=batch.kubernetes.io/job-name={job_name}", "--output=json"],
                        ).stdout
                    )["items"]
                    for job_pod in job_pods:
                        for status in (job_pod.get("status") or {}).get("containerStatuses") or []:
                            state = status.get("state") or {}
                            if state.get("running") or state.get("terminated"):
                                outage_job_ran_seen = True
                            if (state.get("terminated") or {}).get("exitCode", 0) != 0:
                                outage_job_failed_seen = True
            if _outage_check_count <= 3 or _outage_check_count % 10 == 0:
                print(
                    f"[3.10] outage-trace #{_outage_check_count}: hold_kind={row['hold_kind']!r} "
                    f"observed_state={row['observed_state']!r} observed_image={row['observed_image']!r} "
                    f"last_error_code={row['last_error_code']!r} hold_started_at={row['hold_started_at']!r}"
                )
            return row["hold_kind"] is None and row["last_error_code"] == "BACKUP_FAILED"

        # LIVE_CONFIG.backup_deadline is itself 180s; waiting exactly that
        # long left no margin for the pod-scale-down before hold_started_at
        # is even set, or for the per-pass kubectl/docker overhead, so the
        # test's own clock (which starts first) lost the race against
        # cellctl's deadline-driven transition almost every time.
        try:
            _wait_for(
                _backup_failed_and_restarted, timeout=280, interval=2,
                description="backup Job to fail against a stopped object store and restart the cell",
            )
        finally:
            _print_job_diagnostics(k3s.name, namespace, JOB_KIND_BACKUP)
            diag_row = asyncio.run(_row())
            print(
                f"[3.10] diagnostics: cell row now hold_kind={diag_row['hold_kind']!r} "
                f"observed_state={diag_row['observed_state']!r} last_error_code={diag_row['last_error_code']!r}"
            )
        row = asyncio.run(_row())
        assert row["observed_image"] == previous_image, "expected the cell to restart on its CURRENT image, not the failed upgrade target"

        # The BACKUP_FAILED came from this hold's own backup Job, which really
        # ran against the stopped object store -- not from a finished Job of
        # the canary scenario's hold reused under a shared name.
        assert outage_hold_started_at is not None, "the outage upgrade hold was never observed"
        outage_job_name = hold_job_name(BACKUP_JOB_NAME, outage_hold_started_at.isoformat())
        assert outage_job_seen is not None, f"the outage hold's own backup Job {outage_job_name} was never observed"
        print(f"[3.10] outage hold's own backup Job {outage_job_name}: status={outage_job_seen['status']!r}")
        assert not outage_job_seen["status"].get("succeeded"), outage_job_seen["status"]
        # ...and it actually ran against the stopped store: at least one
        # capture showed its own pod's restic container started. restic
        # retries an unreachable endpoint for longer than the live 180 s
        # backup deadline, so the hold ends on that deadline while the Job is
        # still active; status.failed only shows when it gave up first.
        print(f"[3.10] outage Job ran={outage_job_ran_seen} failed={outage_job_failed_seen}")
        assert outage_job_ran_seen, f"no capture of {outage_job_name} showed its container started: {outage_job_seen['status']!r}"
        # D4/D8: BACKUP_FAILED deletes the hold's backup Job before the cell
        # restarts, so the ResourceQuota never holds the restart back.
        gone = _kubectl(k3s.name, ["get", "job", outage_job_name, "--namespace", namespace], check=False)
        assert gone.returncode != 0 and "NotFound" in gone.stderr, gone.stderr
    finally:
        _run(["docker", "start", k3s.minio_name])
        _wait_for(
            lambda: _run(["docker", "exec", k3s.minio_name, "true"], check=False).returncode == 0,
            timeout=30, description="MinIO to be running again",
        )

    asyncio.run(_exec_admin("INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", json.dumps(good_image)))
    _wait_for(
        lambda: _reconciled_to("running", "read_only"),
        timeout=60, interval=2, description="cell to settle after the outage scenario",
    )

    # === scenario: D4 per-object apply. The real API server refuses a bound
    # PVC's shrink (422); that refuses the PVC alone, and the StatefulSet,
    # which carries replicas and hold state, still gets its own apply. ===

    print("[3.10] scenario: a refused PVC shrink does not stop the StatefulSet's own apply")

    def _pvc_request() -> str:
        pvc = json.loads(_kubectl(k3s.name, ["get", "pvc", "cell-data", "--namespace", namespace, "--output=json"]).stdout)
        return pvc["spec"]["resources"]["requests"]["storage"]

    pvc_request_before = _pvc_request()
    asyncio.run(
        _exec_admin("UPDATE exomem_cloud_cells SET storage_gib = 1, desired_state = 'read_only' WHERE cell_id = $1", cell_id)
    )
    asyncio.run(_pass())
    refused_row = asyncio.run(_row())
    refused_statefulset = json.loads(
        _kubectl(k3s.name, ["get", "statefulset", "cell", "--namespace", namespace, "--output=json"]).stdout
    )
    assert refused_row["last_error_code"] == "MANIFEST_IMMUTABLE", dict(refused_row)
    assert refused_row["observed_generation"] != refused_row["generation"], dict(refused_row)
    assert refused_statefulset["metadata"]["annotations"][ROW_GENERATION_ANNOTATION] == str(refused_row["generation"])
    assert {"name": "EXOMEM_CLOUD_READ_ONLY", "value": "1"} in refused_statefulset["spec"]["template"]["spec"]["containers"][0]["env"]
    assert _pvc_request() == pvc_request_before
    asyncio.run(
        _exec_admin("UPDATE exomem_cloud_cells SET storage_gib = 10, desired_state = 'running' WHERE cell_id = $1", cell_id)
    )
    _wait_for(
        lambda: _reconciled_to("running") and asyncio.run(_row())["last_error_code"] is None,
        timeout=180, interval=2, description="cell to converge once the shrink is undone",
    )

    # === scenario: real ValidatingAdmissionPolicy denies an out-of-scope
    # cellctl write (wrong namespace shape, and a non-digest-pinned image). =

    print("[3.10] scenario: admission denies an out-of-scope namespace write")
    cellctl_username = "system:serviceaccount:exomem-cloud:cellctl"
    forged_namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "attacker-namespace",
            "labels": {
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "v1.35",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/audit-version": "v1.35",
                "pod-security.kubernetes.io/warn": "restricted",
                "pod-security.kubernetes.io/warn-version": "v1.35",
            },
        },
    }
    # `apply` first GETs the existing object to compute a merge patch; even
    # though the RBAC gap is fixed now, `create` is still the right verb to
    # exercise here (it is what cellctl itself actually issues for a brand
    # new namespace), so this stays on `create --dry-run=server`.
    denied = _kubectl(
        k3s.name, ["create", "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
        documents=[forged_namespace], check=False,
    )
    assert denied.returncode != 0
    assert "cellctl may write only inside a cell namespace" in denied.stderr, denied.stderr

    print("[3.10] scenario: admission denies a non-digest-pinned image")
    stateful_set = json.loads(_kubectl(k3s.name, ["get", "statefulset", "cell", "--namespace", namespace, "--output=json"]).stdout)
    forged_statefulset = copy.deepcopy(stateful_set)
    forged_statefulset["spec"]["template"]["spec"]["containers"][0]["image"] = "docker.io/library/alpine:latest"
    denied_image = _kubectl(
        k3s.name, ["apply", "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
        documents=[forged_statefulset], check=False,
    )
    assert denied_image.returncode != 0
    assert "digest-pinned images from the configured cell repository" in denied_image.stderr, denied_image.stderr

    # NEW-1: projected sources are an allowlist, so a podCertificate source
    # (which a serviceAccountToken denylist let through) is denied, and so is
    # a CSI inline volume.
    forged_volumes = {
        "a projected serviceAccountToken volume": {
            "name": "token", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]},
        },
        "a projected podCertificate volume": {
            "name": "certificate",
            "projected": {
                "sources": [
                    {
                        "podCertificate": {
                            "signerName": "example.com/signer",
                            "keyType": "ED25519",
                            "credentialBundlePath": "credentials.pem",
                        }
                    }
                ]
            },
        },
        "a CSI inline volume": {"name": "inline", "csi": {"driver": "secrets-store.csi.k8s.io"}},
    }
    for described, volume in forged_volumes.items():
        print(f"[3.10] scenario: admission denies {described}")
        forged_mount = copy.deepcopy(stateful_set)
        forged_mount["spec"]["template"]["spec"].setdefault("volumes", []).append(volume)
        denied_mount = _kubectl(
            k3s.name, ["apply", "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
            documents=[forged_mount], check=False,
        )
        assert denied_mount.returncode != 0, described
        assert "projected volume sources are configMap, secret or downwardAPI" in denied_mount.stderr, (
            described, denied_mount.stderr
        )

    # Security MED-1: with a stolen cellctl token, a NetworkPolicy that
    # opens egress, a service-account-token Secret, a PVC off the encrypted
    # class, a widened quota, an ephemeral volume or a pod that picks its
    # node or runtime must all be denied, and so must deleting default-deny.
    def _live(kind: str, name: str) -> dict[str, Any]:
        live = json.loads(_kubectl(k3s.name, ["get", kind, name, "--namespace", namespace, "--output=json"]).stdout)
        return {
            "apiVersion": live["apiVersion"],
            "kind": live["kind"],
            "metadata": {"name": live["metadata"]["name"], "namespace": namespace},
            "spec": live["spec"],
        }

    forged_writes: dict[str, dict[str, Any]] = {}
    allow_all = _live("networkpolicy", "job-egress")
    allow_all["metadata"]["name"] = "allow-all"
    allow_all["spec"] = {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}
    forged_writes["an extra allow-all NetworkPolicy"] = allow_all
    open_default = _live("networkpolicy", "default-deny")
    open_default["spec"] = {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}
    forged_writes["default-deny rewritten to allow egress"] = open_default
    job_open = _live("networkpolicy", "job-egress")
    job_open["spec"]["egress"][0]["ports"] = [{"protocol": "TCP", "port": 5432}]
    forged_writes["job-egress widened to 5432"] = job_open
    ingress_from_all = _live("networkpolicy", "runtime-ingress")
    ingress_from_all["spec"]["ingress"][0]["from"] = [{"namespaceSelector": {}}]
    forged_writes["runtime-ingress from every namespace"] = ingress_from_all
    forged_writes["a service-account-token Secret"] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "stolen-token",
            "namespace": namespace,
            "annotations": {"kubernetes.io/service-account.name": "default"},
        },
        "type": "kubernetes.io/service-account-token",
    }
    quota = _live("resourcequota", "cell-quota")
    quota["spec"]["hard"]["pods"] = "50"
    forged_writes["a quota with 50 pods"] = quota
    for described, mutate in {
        "an ephemeral volume": lambda pod: pod.setdefault("volumes", []).append(
            {
                "name": "eph",
                "ephemeral": {
                    "volumeClaimTemplate": {
                        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}
                    }
                },
            }
        ),
        "a nodeName": lambda pod: pod.update(nodeName="elsewhere"),
        "a runtimeClassName": lambda pod: pod.update(runtimeClassName="runc"),
        "hostAliases": lambda pod: pod.update(hostAliases=[{"ip": "10.0.0.1", "hostnames": ["object-storage"]}]),
    }.items():
        forged_pod = copy.deepcopy(stateful_set)
        mutate(forged_pod["spec"]["template"]["spec"])
        forged_writes[f"a StatefulSet with {described}"] = forged_pod
    for described, document in forged_writes.items():
        print(f"[3.10] scenario: admission denies {described}")
        verb = "create" if document["kind"] == "Secret" else "apply"
        denied_write = _kubectl(
            k3s.name, [verb, "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
            documents=[document], check=False,
        )
        assert denied_write.returncode != 0, described
        assert "exomem-cellctl-scope" in denied_write.stderr, (described, denied_write.stderr)
    print("[3.10] scenario: admission denies deleting default-deny")
    denied_delete = _kubectl(
        k3s.name,
        ["delete", "networkpolicy", "default-deny", "--namespace", namespace, "--dry-run=server", f"--as={cellctl_username}"],
        check=False,
    )
    assert denied_delete.returncode != 0
    assert "exomem-cellctl-scope" in denied_delete.stderr, denied_delete.stderr

    # === scenario: live NetworkPolicy enforcement (not just admission). ===

    print("[3.10] scenario: a second cell converges; cross-cell and DNS/egress denial, gateway-can-reach")
    cell_id_2 = _cell_id()
    namespace_2 = namespace_name(cell_id_2)
    tenant_id_2 = uuid.uuid4()
    asyncio.run(_exec_admin("INSERT INTO exomem_tenants (id) VALUES ($1)", tenant_id_2))
    asyncio.run(
        _exec_admin(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state, desired_image) VALUES ($1, $2, 'running', NULL)",
            cell_id_2,
            tenant_id_2,
        )
    )
    def _second_cell_pod_running() -> bool:
        asyncio.run(_pass())
        pods = json.loads(
            _kubectl(k3s.name, ["get", "pods", "--namespace", namespace_2, "--output=json"], check=False).stdout or "{}"
        ).get("items", [])
        return bool(pods) and pods[0].get("status", {}).get("phase") == "Running"

    _wait_for(_second_cell_pod_running, timeout=180, interval=3, description="second cell's pod to be Running")
    _kubectl(k3s.name, ["wait", "--namespace", namespace_2, "--for=condition=Ready", "pod", "-l", "app.kubernetes.io/name=exomem-cell", "--timeout=120s"])
    pod_2 = json.loads(_kubectl(k3s.name, ["get", "pods", "--namespace", namespace_2, "--output=json"]).stdout)["items"][0]["metadata"]["name"]
    cell_1_service_ip = json.loads(
        _kubectl(k3s.name, ["get", "service", "cell", "--namespace", namespace, "--output=json"]).stdout
    )["spec"]["clusterIP"]

    print("[3.10] scenario: a pod in the second cell's namespace cannot reach the first cell's Service")
    cross_cell_probe = (
        "import socket; s = socket.socket(); s.settimeout(3); "
        f"import sys; sys.exit(0) if s.connect_ex(('{cell_1_service_ip}', 8765)) == 0 else sys.exit(1)"
    )
    cross_cell_result = _exec_py(k3s.name, namespace_2, pod_2, cross_cell_probe, check=False)
    assert cross_cell_result.returncode != 0, "cross-cell connection unexpectedly succeeded"

    print("[3.10] scenario: the runtime pod cannot reach anything outbound, including DNS")
    dns_probe = "import socket; socket.setdefaulttimeout(3); socket.gethostbyname('kubernetes.default.svc.cluster.local')"
    dns_result = _exec_py(k3s.name, namespace_2, pod_2, dns_probe, check=False)
    assert dns_result.returncode != 0, "DNS resolution from a fully-egress-denied runtime pod unexpectedly succeeded"

    print("[3.10] scenario: a pod labelled as the gateway in exomem-cloud can reach the cell")
    gateway_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "gateway-probe",
            "namespace": "exomem-cloud",
            "labels": {"app.kubernetes.io/name": "exomem-cloud-gateway"},
        },
        "spec": {
            # exomem-cloud (cellctl.yaml) carries the same restricted
            # Pod Security labels as a cell namespace, so this ad-hoc probe
            # needs the same admission-satisfying securityContext the real
            # StatefulSet gets from manifests.py, not just a bare pod spec.
            "securityContext": {
                "runAsNonRoot": True,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": good_image,
                    "command": ["python3", "-c", "import time; time.sleep(3600)"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
        },
    }
    _kubectl(k3s.name, ["apply", "--filename=-"], documents=[gateway_pod])
    _kubectl(k3s.name, ["wait", "--namespace", "exomem-cloud", "--for=condition=Ready", "pod/gateway-probe", "--timeout=60s"])
    gateway_probe_source = (
        "import socket; s = socket.socket(); s.settimeout(3); "
        f"import sys; sys.exit(0) if s.connect_ex(('{cell_1_service_ip}', 8765)) == 0 else sys.exit(1)"
    )
    gateway_result = _exec_py(k3s.name, "exomem-cloud", "gateway-probe", gateway_probe_source, check=False)
    assert gateway_result.returncode == 0, "the gateway-labelled pod could not reach the cell's Service on 8765"
    _kubectl(k3s.name, ["delete", "pod", "--namespace", "exomem-cloud", "gateway-probe", "--ignore-not-found"])

    # === scenario: deletion runs to completion with real namespace absence,
    # on top of the already-tested fake-backed absence proofs for backup
    # objects / B2 key, plus the D10 amendment's real PV-claimRef check. ===

    print("[3.10] scenario: deletion removes the real namespace and reaches observed_state=deleted")
    asyncio.run(_exec_admin("UPDATE exomem_cloud_cells SET desired_state = 'deleted' WHERE cell_id = $1", cell_id))

    def _deletion_progressed() -> bool:
        asyncio.run(_pass())
        row = asyncio.run(_row())
        return row["observed_state"] == "deleted"

    _wait_for(_deletion_progressed, timeout=180, interval=3, description="cell to reach observed_state=deleted")
    namespace_gone = _kubectl(k3s.name, ["get", "namespace", namespace], check=False)
    assert namespace_gone.returncode != 0, "expected the real cell namespace to have been deleted"

    _kubectl(k3s.name, ["delete", "namespace", namespace_2, "--ignore-not-found"])

    print("[3.10] all scenarios passed")
