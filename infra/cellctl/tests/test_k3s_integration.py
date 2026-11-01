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

- Cell image: backup/restore scenarios use a minimal stand-in (serves
  /health, /health/ready, and test-only /write and
  /read endpoints round-tripping an owner-only file under the vault and
  under /data/host) is built and imported into the cluster, plus a second,
  deliberately-never-ready variant used only to force a canary failure. It
  implements no MCP protocol. The protected-import case uses a supplied,
  source-built Cloud image for the runtime and every Job, with only the
  disposable storage CA added to its certificate bundle.
- Object storage: a real MinIO container (Chainguard's build) stands in for
  B2's S3-compatible endpoint per the D8 amendment, so the backup/restore
  Jobs' restic calls are genuinely live. MinIO's one root credential plays
  cellctl's staging key and, in the cell's `cell-object-store-key` Secret
  (written under the real key custodian service account, not by cellctl),
  the cell's backup key -- MinIO plays the network protocol, not B2's
  per-key security model, which stays covered by test_storage_fakes.py. The
  custodian's own B2 key calls are covered by test_custodian.py; cellctl's
  remaining B2 calls (an old cell's object versions) stay on the in-process
  FakeB2.
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
import base64
import copy
import hashlib
import io
import ipaddress
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import tarfile
import time
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import asyncpg
import boto3
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from kubernetes import client as k8s
from kubernetes import config as k8s_config

from cellctl.capacity import CapacityConfig
from cellctl.decide import ReconcileConfig
from cellctl.imports import PHASES, job_name
from cellctl.k8s_client import ClusterClient
from cellctl.manifests import (
    BACKUP_JOB_NAME,
    JOB_KIND_BACKUP,
    JOB_KIND_LABEL,
    JOB_KIND_RESTORE,
    OBJECT_STORE_KEY_ID_FIELD,
    OBJECT_STORE_KEY_SECRET,
    OBJECT_STORE_KEY_SECRET_FIELD,
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
AUDIT_POLICY = REPO_ROOT / "infra/ansible/roles/k3s/files/audit-policy.yaml"
K3S_AUDIT_LOG = "/var/lib/rancher/k3s/server/logs/audit.log"

RUN_LIVE = os.environ.get("RUN_CELLCTL_K3S_TEST") == "1"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
K3S_IMAGE = json.loads(K3S_GATE.read_text(encoding="utf-8"))["k3sImage"]
STANDIN_REPOSITORY = "cellctl-standin.test/exomem-cell"
NAMESPACE_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
# MinIO's own community images (quay.io/minio/minio, minio/minio) are no
# longer pullable; Chainguard's digest-pinned build stands in. It runs as
# root here on a named volume: the image's non-root user cannot write the
# container's /data, and MinIO refuses the overlay root filesystem ("Rename
# across devices"). The volume also keeps the bucket across the outage
# scenario's docker stop/start.
MINIO_IMAGE = "chainguard/minio@sha256:bd014394a80898e68c149f2311fdf8d5a2c2f3bb2c33b9327ae6d02b4b065ae1"
MINIO_ACCESS_KEY = "cellctltest"
MINIO_SECRET_KEY = "cellctltestsecret1"
BUCKET_NAME = "exomem-cloud-backups"
# The identity the platform chart gives the key custodian (templates/key-custodian.yaml).
CUSTODIAN_USERNAME = "system:serviceaccount:exomem-key-custody:key-custodian"

# Short but real deadlines so the live test exercises the actual D6/D8 timeout
# branches without waiting on the 30/15-minute readiness and backup production defaults.
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
    return _kubectl(k3s_name, [*BREAK_GLASS, "exec", "--namespace", namespace, pod, "--", "python3", "-c", source], check=check)


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
    # A parallel session's own prefix keeps its containers apart from others'.
    prefix = os.environ.get("CELLCTL_K3S_NAME_PREFIX", "cellctl")
    name = f"{prefix}-k3s-{suffix}"
    network = f"{prefix}-net-{suffix}"
    minio_name = f"{prefix}-minio-{suffix}"
    minio_volume = f"{prefix}-minio-data-{suffix}"
    work = tmp_path_factory.mktemp("cellctl-k3s")

    network_args = ["docker", "network", "create"]
    if subnet := os.environ.get("CELLCTL_K3S_NETWORK_SUBNET"):
        network_args += ["--subnet", subnet]
    _run([*network_args, network])
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
        # The node's own audit policy (harden-exomem-cloud-operator-access
        # D7), so the operator-access runbook's audit-log steps are
        # rehearsed against the same policy the node runs.
        _run(
            [
                "docker", "run", "--privileged", "--detach", "--name", name,
                "--network", network,
                "--publish", "127.0.0.1::6443",
                "--mount", f"type=bind,src={AUDIT_POLICY},dst=/etc/rancher/k3s/audit-policy.yaml,readonly",
                K3S_IMAGE, "server",
                "--disable=traefik", "--disable=servicelb",
                "--write-kubeconfig-mode=600",
                "--kube-apiserver-arg=feature-gates=PodCertificateRequest=true",
                "--kube-apiserver-arg=runtime-config=certificates.k8s.io/v1beta1=true",
                "--kube-apiserver-arg=audit-policy-file=/etc/rancher/k3s/audit-policy.yaml",
                f"--kube-apiserver-arg=audit-log-path={K3S_AUDIT_LOG}",
            ]
        )
        _run(
            [
                "docker", "run", "--detach", "--name", minio_name,
                "--network", network,
                "--user", "0:0",
                "--mount", f"type=volume,src={minio_volume},dst=/data",
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
            # Helm installs the platform into its release namespace, so it
            # always exists in production; the chart puts namespaced objects
            # there (cellctl's alert-delivery Role and RoleBinding).
            _run(["docker", "exec", name, "kubectl", "create", "namespace", "exomem-platform"])
            # Positive exec probes need normal break-glass RBAC even when selected alone.
            _kubectl(name, ["apply", "--filename=-"], documents=_render_platform("templates/operator-access.yaml"))
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
            _run(["docker", "rm", "--force", "--volumes", name], check=False)
            _run(["docker", "rm", "--force", minio_name], check=False)
            _run(["docker", "volume", "rm", "--force", minio_volume], check=False)
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


@pytest.fixture
def import_storage(k3s: K3sCluster, tmp_path: Path) -> Iterator[dict[str, Any]]:
    source_image = os.environ.get("CELLCTL_K3S_IMPORT_IMAGE")
    if not source_image:
        pytest.skip("set CELLCTL_K3S_IMPORT_IMAGE to the source-built Cloud image")
    work = tmp_path / "import-storage"
    certificates = work / "certificates"
    certificates.mkdir(parents=True)
    suffix = uuid.uuid4().hex[:12]
    name = f"{k3s.name}-import-storage"
    volume = f"{name}-data"
    trusted_image = f"cellctl-import-tls:{suffix}"
    try:
        _run([
            "docker", "run", "--detach", "--name", name, "--network", k3s.network,
            "--user", "0:0", "--mount", f"type=volume,src={volume},dst=/data",
            "--mount", f"type=bind,src={certificates},dst=/certificates,readonly",
            "-e", f"MINIO_ROOT_USER={MINIO_ACCESS_KEY}",
            "-e", f"MINIO_ROOT_PASSWORD={MINIO_SECRET_KEY}",
            # C6 exports ask for encryption at rest (SSE-S3), which MinIO serves only with a KMS key.
            "-e", "MINIO_KMS_SECRET_KEY=export-proof:" + base64.b64encode(os.urandom(32)).decode("ascii"),
            MINIO_IMAGE, "server", "/data", "--address", ":443", "--certs-dir", "/certificates",
        ])
        address = _container_ip(name)
        # Docker assigns the address at start; restart this owned fixture after issuing its IP certificate.
        _run(["docker", "stop", name])
        now = datetime.now(UTC)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable import CA")])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                             key_encipherment=False, data_encipherment=False, key_agreement=False,
                             key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
              .sign(ca_key, hashes.SHA256()))
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, "Disposable import storage")]))
                .issuer_name(ca_name).public_key(leaf_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(ca_key, hashes.SHA256()))
        ca_path = work / "ca.crt"
        ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        (certificates / "public.crt").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        private_key = certificates / "private.key"
        private_key.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        private_key.chmod(0o600)
        _run(["docker", "start", name])
        assert _container_ip(name) == address
        # Omit the default port so the S3 signature and urllib's Host header agree.
        endpoint = f"https://{address}"
        storage = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                               aws_access_key_id=MINIO_ACCESS_KEY, aws_secret_access_key=MINIO_SECRET_KEY,
                               verify=str(ca_path))
        _wait_for(lambda: storage.list_buckets() is not None, timeout=60,
                  description="TLS import storage with verified CA and IP SAN")
        storage.create_bucket(Bucket=BUCKET_NAME)
        storage.put_bucket_versioning(Bucket=BUCKET_NAME, VersioningConfiguration={"Status": "Enabled"})
        assert storage.get_bucket_versioning(Bucket=BUCKET_NAME)["Status"] == "Enabled"
        # Trust is the sole image change; the product and baked model remain the source image's bytes.
        (work / "Dockerfile").write_text(
            f"FROM {source_image}\nUSER 0:0\nCOPY ca.crt /tmp/import-ca.crt\n"
            "RUN cat /tmp/import-ca.crt >> /etc/ssl/certs/ca-certificates.crt && rm /tmp/import-ca.crt\n"
            "USER 10001:10001\n", encoding="utf-8")
        _run(["docker", "build", "--tag", trusted_image, str(work)])
        product_hash = (
            "import hashlib,json; from pathlib import Path; "
            "roots=[Path('/app/.venv'),Path('/opt/exomem-models')]; "
            "digest=hashlib.sha256(); "
            "[(digest.update(str(p).encode()),digest.update(p.read_bytes())) "
            "for root in roots for p in sorted(root.rglob('*')) if p.is_file()]; "
            "print(json.dumps({'sha256':digest.hexdigest()}))"
        )
        identities = []
        for image in (source_image, trusted_image):
            identities.append(json.loads(_run(["docker", "run", "--rm", "--network=none",
                                              "--entrypoint=python3", image, "-c", product_hash]).stdout))
        assert identities[0] == identities[1]
        image = _import_image(k3s.name, trusted_image, repository="cellctl-import.test/exomem-cell")
        source_id = _run(["docker", "image", "inspect", "--format={{.Id}}", source_image]).stdout.strip()
        print(f"[import] source image {source_id}; trusted image {image}; product/model {identities[0]['sha256']}")
        yield {"endpoint": endpoint, "ca": ca_path, "tag": trusted_image, "image": image, "client": storage}
    finally:
        logs = _run(["docker", "logs", name], check=False)
        print(f"[import] TLS storage log:\n{logs.stdout}{logs.stderr}")
        _run(["docker", "rm", "--force", name], check=False)
        _run(["docker", "volume", "rm", "--force", volume], check=False)
        _run(["docker", "image", "rm", trusted_image], check=False)
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not RUN_LIVE, reason="set RUN_CELLCTL_K3S_TEST=1 for the real import workflow")
# These fixed protocol cases cover acceptance and the three C5 refusal boundaries.
@pytest.mark.parametrize(("case", "expected_error"), [
    pytest.param("accepted", None, id="accepted"),
    pytest.param("unsafe-member-after-valid", "ARCHIVE_MEMBER_REFUSED", id="unsafe-member-after-valid"),
    pytest.param("wrong-age-recipient", "ARCHIVE_UNDECRYPTABLE", id="wrong-age-recipient"),
    pytest.param("substituted-ciphertext", "ARCHIVE_DIGEST_MISMATCH", id="substituted-ciphertext"),
])
def test_protected_import_waits_for_real_cleanup_pod_gc(
    k3s: K3sCluster, cell_db: CellDatabase, import_storage: dict[str, Any],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected_error: str | None,
) -> None:
    """A deleted Job with a terminating Pod cannot publish done or release import admission."""
    image = import_storage["image"]
    documents = _render_platform("templates/cellctl.yaml", "templates/cloud-storage-class.yaml", settings=(
                                     "--set", "cellctl.cellImageRepository=cellctl-import.test/exomem-cell",
                                     "--set-json", "cells.jobEgressExcept=[]"))
    storage_class = next(document for document in documents if document["kind"] == "StorageClass")
    storage_class.update(provisioner="rancher.io/local-path", reclaimPolicy="Delete",
                         volumeBindingMode="WaitForFirstConsumer")
    storage_class.pop("parameters", None)
    _kubectl(k3s.name, ["apply", "--filename=-"], documents=documents)
    _wait_for(lambda: all(
        "typeChecking" in (status := json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy",
                          policy, "--output=json"]).stdout).get("status", {}))
        and not status["typeChecking"].get("expressionWarnings")
        for policy in ("exomem-cellctl-scope", "exomem-cell-connect-guard")),
        timeout=30, description="normal controller and operator policies to type-check")
    token = _issue_sa_token(k3s, name="cellctl", namespace="exomem-cloud")
    # Load only the API endpoint and CA, then replace the admin credential with the actual SA token.
    configuration = k8s.Configuration()
    k8s_config.load_kube_config(config_file=str(k3s.kubeconfig), client_configuration=configuration)
    configuration.cert_file = configuration.key_file = None
    configuration.api_key = {"authorization": f"Bearer {token}"}
    sa_client = k8s.ApiClient(configuration)
    cluster = ClusterClient(sa_client)
    monkeypatch.setenv("AWS_CA_BUNDLE", str(import_storage["ca"]))
    cell_id, tenant_id = _cell_id(), uuid.uuid4()
    namespace = namespace_name(cell_id)
    sources = {"metrics.json": b'{"value":23,"unit":"sample"}\n',
               "notes/readme.txt": b"Neutral archive fixture.\n"}
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path, content in sources.items():
            zipped.writestr(path, content)
    expected_archive = {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "bytes": archive.stat().st_size}
    expected_members = {path: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                        for path, content in sources.items()}
    envelope_source = (
        "import json,tarfile; from pathlib import Path; "
        "from exomem.cloud_import_envelope import Envelope,write; "
        "archive=Path('/fixture/export.zip'); "
        "envelope=Envelope(scope='Workspace',category='Exports',filename='export.zip'); "
        "stream=Path('/fixture/envelope.tar').open('wb'); write(stream,archive,envelope); stream.close(); "
        "members=tarfile.open('/fixture/envelope.tar').getmembers(); "
        "print(json.dumps({'bytes':sum(m.size for m in members),'files':len(members)}))"
    )
    declared = json.loads(_run(["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
                               "--mount", f"type=bind,src={tmp_path},dst=/fixture", "--entrypoint=python3",
                               import_storage["tag"], "-c", envelope_source]).stdout)
    if case == "unsafe-member-after-valid":
        with tarfile.open(tmp_path / "envelope.tar", "a", format=tarfile.USTAR_FORMAT) as envelope:
            content = b"Refused archive member.\n"
            member = tarfile.TarInfo("../refused.txt")
            member.size = len(content)
            envelope.addfile(member, io.BytesIO(content))
        with tarfile.open(tmp_path / "envelope.tar") as envelope:
            members = envelope.getmembers()
        declared = {"bytes": sum(member.size for member in members), "files": len(members)}

    async def sql(query: str, *args: object, role: str = "substrate_owner"):
        connection = await asyncpg.connect(cell_db.dsn(role=role))
        try:
            return await connection.fetchrow(query, *args)
        finally:
            await connection.close()

    asyncio.run(sql("INSERT INTO exomem_tenants(id) VALUES($1)", tenant_id))
    asyncio.run(sql("INSERT INTO exomem_cloud_cells(cell_id,tenant_id,desired_state) VALUES($1,$2,'running')",
                    cell_id, tenant_id))
    asyncio.run(sql("INSERT INTO exomem_cloud_settings(key,value) VALUES('cell_image',$1) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", json.dumps(image)))
    # MinIO's one root credential plays the cell-data staging key cellctl holds for exports and imports.
    secrets = SecretsConfig(cell_token_key_current=b"k3s-integration-test-current-key-32b!",
                            cell_token_key_previous=None, cell_token_key_version=1,
                            cell_token_key_previous_version=None, backup_master_keys={1: b"0" * 32},
                            backup_master_key_current_version=1,
                            staging_key_id=MINIO_ACCESS_KEY, staging_application_key=MINIO_SECRET_KEY)
    cluster_config = ClusterConfig(object_storage_bucket=BUCKET_NAME,
                                    object_storage_endpoint=import_storage["endpoint"],
                                    cell_data_backup_bucket=BUCKET_NAME, cell_data_staging_bucket=BUCKET_NAME,
                                    cell_data_endpoint=import_storage["endpoint"],
                                    capacity=CapacityConfig(attachments_limit_fallback=100, headroom=5))
    fake_b2 = FakeB2()
    volumes = FakeHetznerVolumeProvider()

    async def step():
        connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
        try:
            await reconcile_once(connection, cluster, fake_b2, volumes, secrets, cluster_config,
                                 config=LIVE_CONFIG)
        finally:
            await connection.close()

    def ready():
        asyncio.run(step())
        row = asyncio.run(sql("SELECT observed_state,ready FROM exomem_cloud_cells WHERE cell_id=$1", cell_id))
        observed = cluster.observe(cell_id, namespace)
        return row["observed_state"] == "running" and row["ready"] and observed.pod_ready

    def import_row():
        return asyncio.run(sql("SELECT * FROM exomem_cloud_imports WHERE import_id=$1", import_id))

    def reconciled_key():
        asyncio.run(step())
        return import_row()["recipient"] is not None

    def vault_manifest():
        # The owned local-path fixture exposes its PV; reading it adds no inspection Pod.
        result = subprocess.run(["docker", "exec", k3s.name, "tar", "cf", "-", "-C", f"{pvc_path}/vault", "."],
                                check=True, capture_output=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as snapshot:
            return {member.name: {
                "type": member.type.decode("ascii"), "mode": member.mode, "uid": member.uid,
                "gid": member.gid, "size": member.size, "link": member.linkname,
                "sha256": hashlib.sha256(snapshot.extractfile(member).read()).hexdigest() if member.isfile() else None,
            } for member in snapshot}

    try:
        _wait_for(ready, timeout=360, description="actual cell-init and Cloud runtime Ready")
        statefulset = json.loads(_kubectl(k3s.name, ["get", "statefulsets", "--namespace", namespace,
                                                   "--output=json"]).stdout)["items"][0]
        runtime = statefulset["spec"]["template"]["spec"]["containers"][0]
        assert runtime["image"] == image and runtime["securityContext"]["runAsUser"] == 10001
        assert runtime["securityContext"]["readOnlyRootFilesystem"] is True
        assert runtime["resources"]["requests"]["memory"] == "512Mi"
        assert runtime["resources"]["limits"]["memory"] == "3Gi"
        assert runtime["startupProbe"]["periodSeconds"] * runtime["startupProbe"]["failureThreshold"] == 300
        pvc = json.loads(_kubectl(k3s.name, ["get", "pvc", "cell-data", "--namespace", namespace, "--output=json"]).stdout)
        pv = json.loads(_kubectl(k3s.name, ["get", "pv", pvc["spec"]["volumeName"], "--output=json"]).stdout)
        assert pv["spec"]["claimRef"]["namespace"] == namespace
        assert pv["spec"]["claimRef"]["name"] == pvc["metadata"]["name"] == "cell-data"
        assert pv["spec"]["claimRef"]["uid"] == pvc["metadata"]["uid"]
        assert pv["metadata"]["annotations"]["pv.kubernetes.io/provisioned-by"] == "rancher.io/local-path"
        pvc_path = pv["spec"]["hostPath"]["path"]
        # Suppress the unrelated scheduled backup only after the real runtime has become ready.
        asyncio.run(sql("UPDATE exomem_cloud_cells SET last_backup_at=now() WHERE cell_id=$1", cell_id))
        import_id = asyncio.run(sql("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                                     "VALUES($1,'archive_members',$2,$3) RETURNING import_id", cell_id,
                                     declared["bytes"], declared["files"], role="substrate_app"))["import_id"]
        _wait_for(reconciled_key, timeout=120, description="actual prepare Job recipient")
        recipient = import_row()["recipient"]
        _wait_for(ready, timeout=360, description="C1 and runtime Ready at key_ready before the digest")
        assert import_row()["observed_state"] == "key_ready"
        print(f"[import] {case}: C1 running/ready and Pod Ready at key_ready before the digest")
        if case == "wrong-age-recipient":
            _run(["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
                  "--mount", f"type=bind,src={tmp_path},dst=/fixture", "--entrypoint=age-keygen",
                  import_storage["tag"], "-o", "/fixture/other.age"])
            other_recipient = _run(["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
                                   "--mount", f"type=bind,src={tmp_path},dst=/fixture", "--entrypoint=age-keygen",
                                   import_storage["tag"], "-y", "/fixture/other.age"]).stdout.strip()
            assert other_recipient != recipient
            recipient = other_recipient
        _run(["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
              "--mount", f"type=bind,src={tmp_path},dst=/fixture", "--entrypoint=age",
              import_storage["tag"], "-r", recipient, "-o", "/fixture/upload.age", "/fixture/envelope.tar"])
        ciphertext = (tmp_path / "upload.age").read_bytes()
        cipher_hash = hashlib.sha256(ciphertext).hexdigest()
        uploaded = ciphertext
        if case == "substituted-ciphertext":
            uploaded = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
            assert len(uploaded) == len(ciphertext) and hashlib.sha256(uploaded).hexdigest() != cipher_hash
        asyncio.run(sql("UPDATE exomem_cloud_imports SET ciphertext_sha256=$2,ciphertext_bytes=$3 WHERE import_id=$1",
                        import_id, cipher_hash, len(ciphertext), role="substrate_app"))
        _wait_for(lambda: (asyncio.run(step()), import_row()["part_urls"] is not None)[1],
                  timeout=120, description="controller-issued multipart upload URLs")
        parts, offset = [], 0
        for part in json.loads(import_row()["part_urls"]):
            size = part["part_size_bytes"]
            request = Request(part["url"], data=uploaded[offset:offset + size], method="PUT")
            with urlopen(request, context=ssl.create_default_context(cafile=str(import_storage["ca"])), timeout=30) as response:
                assert response.status == 200
                parts.append({"part_number": part["part_number"], "etag": response.headers["ETag"]})
            offset += size
        assert offset == len(ciphertext)
        _wait_for(ready, timeout=360, description="C1 and runtime Ready after actual PUT before completion")
        assert import_row()["observed_state"] == "key_ready"
        print(f"[import] {case}: C1 running/ready and Pod Ready after actual PUT before completion")
        before = vault_manifest()
        asyncio.run(sql("UPDATE exomem_cloud_imports SET requested_state='uploaded',part_etags=$2 WHERE import_id=$1",
                        import_id, json.dumps(parts), role="substrate_app"))

        def import_launched():
            if not cluster.observe_import_jobs(namespace, str(import_id))["import"].exists:
                asyncio.run(step())
            return cluster.observe_import_jobs(namespace, str(import_id))["import"].exists

        _wait_for(import_launched, timeout=120, description="controller-launched protected import Job")
        # Observe without reconciliation so cleanup cannot delete a failed Job's termination report.
        _wait_for(lambda: (job := cluster.observe_import_jobs(namespace, str(import_id))["import"]).succeeded or job.failed,
                  timeout=180, description="actual protected import termination report")
        completed = cluster.observe_import_jobs(namespace, str(import_id))["import"]
        if expected_error is None:
            assert completed.succeeded and completed.report == {"ok": True, "files": declared["files"], "bytes": declared["bytes"]}
        else:
            assert completed.failed and completed.report == {"ok": False, "error": expected_error}
            assert vault_manifest() == before
        print(f"[import] {case}: actual termination {json.dumps(completed.report, sort_keys=True)}")
        assert cluster.observe(cell_id, namespace).statefulset_replicas == 0
        held = asyncio.run(sql("SELECT hold_kind,ready FROM exomem_cloud_cells WHERE cell_id=$1", cell_id))
        assert held["hold_kind"] == "import" and held["ready"] is False
        cleanup_name = job_name(str(import_id), "cleanup")

        def cleanup_launched():
            if not cluster.observe_import_jobs(namespace, str(import_id))["cleanup"].exists:
                asyncio.run(step())
            return cluster.observe_import_jobs(namespace, str(import_id))["cleanup"].exists

        _wait_for(cleanup_launched, timeout=60, description="controller-launched local cleanup Job")
        _wait_for(lambda: cluster.observe_import_jobs(namespace, str(import_id))["cleanup"].succeeded,
                  timeout=120, description="actual local cleanup Job success")
        if expected_error is not None:
            assert vault_manifest() == before
        cleanup_pod = json.loads(_kubectl(k3s.name, ["get", "pods", "--namespace", namespace,
                                  f"--selector=batch.kubernetes.io/job-name={cleanup_name}", "--output=json"]).stdout)["items"][0]
        pod_name = cleanup_pod["metadata"]["name"]
        _kubectl(k3s.name, ["patch", "pod", pod_name, "--namespace", namespace, "--type=merge",
                          "--patch", json.dumps({"metadata": {"finalizers": ["proof.example/retain-cleanup"]}})])
        asyncio.run(step())
        _wait_for(lambda: not cluster.observe_import_jobs(namespace, str(import_id))["cleanup"].exists,
                  timeout=60, description="cleanup Job controller deletion")
        _wait_for(lambda: json.loads(_kubectl(k3s.name, ["get", "pod", pod_name, "--namespace", namespace,
                                   "--output=json"]).stdout)["metadata"].get("deletionTimestamp") is not None,
                  timeout=60, description="retained cleanup Pod entering termination")
        asyncio.run(step())
        assert import_row()["observed_state"] == "cleanup_stopping"
        observed = cluster.observe(cell_id, namespace)
        assert observed.statefulset_replicas == 0
        held = asyncio.run(sql("SELECT hold_kind,ready FROM exomem_cloud_cells WHERE cell_id=$1", cell_id))
        assert held["hold_kind"] == "import" and held["ready"] is False
        with pytest.raises(asyncpg.UniqueViolationError):
            asyncio.run(sql("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                            "VALUES($1,'archive_members',$2,$3)", cell_id, declared["bytes"],
                            declared["files"], role="substrate_app"))
        print("[import] cleanup Job absent, terminating Pod present: cleanup_stopping, replicas=0, admission occupied")
        _kubectl(k3s.name, ["patch", "pod", pod_name, "--namespace", namespace, "--type=merge",
                          "--patch", '{"metadata":{"finalizers":[]}}'])
        _wait_for(lambda: all(not job.exists and not job.pods_present
                  for job in cluster.observe_import_jobs(namespace, str(import_id)).values()),
                  timeout=60, description="every import phase Job and Pod absent")
        asyncio.run(step())
        terminal = "done" if expected_error is None else "failed"
        assert import_row()["observed_state"] == terminal and import_row()["error_code"] == expected_error
        _wait_for(ready, timeout=360, description="terminal import and actual Cloud runtime Ready again")
        assert asyncio.run(sql("SELECT hold_kind FROM exomem_cloud_cells WHERE cell_id=$1", cell_id))["hold_kind"] is None
        # C5 must admit a successor only after cleanup; roll back this admission probe.
        async def successor_admitted():
            connection = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
            try:
                transaction = connection.transaction()
                await transaction.start()
                successor = await connection.fetchval(
                    "INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                    "VALUES($1,'archive_members',$2,$3) RETURNING import_id", cell_id,
                    declared["bytes"], declared["files"])
                await transaction.rollback()
                return successor is not None
            finally:
                await connection.close()

        assert asyncio.run(successor_admitted())
        for phase in PHASES:
            observed_job = cluster.observe_import_jobs(namespace, str(import_id))[phase]
            assert not observed_job.exists and not observed_job.pods_present
        key = f"cells/{cell_id}/imports/{import_id}.age"
        storage = import_storage["client"]
        assert not storage.list_multipart_uploads(Bucket=BUCKET_NAME, Prefix=key).get("Uploads")
        versions = storage.list_object_versions(Bucket=BUCKET_NAME, Prefix=key)
        assert not versions.get("Versions") and not versions.get("DeleteMarkers")
        inspect = f"""
import gzip, hashlib, json, os, stat
from pathlib import Path
from exomem import archive_members, state_paths
from exomem.governance.principal import OWNER_AUDIENCE, RequestPrincipal, request_scope
vault = Path('/data/vault')
receipt_path = state_paths.vault_state_dir(vault) / 'cloud-imports' / '{import_id}.json'
assert Path('/data/host').is_dir() and os.getuid() == 10001
assert not (vault / '_Imports' / '{import_id}').exists()
assert not (vault.parent / '.import-keys' / '{import_id}').exists()
assert not (vault.parent / '.import-{import_id}').exists()
"""
        if expected_error is None:
            inspect += f"""
receipt = json.loads(receipt_path.read_text())
with request_scope(RequestPrincipal(audience_id=OWNER_AUDIENCE, surface='proof', issuer_family='proof')):
    manifest = archive_members.read_manifest(vault, receipt['manifest'])
    assert manifest is not None
    members = {{member['path']: {{'sha256': hashlib.sha256(gzip.decompress((vault / Path(receipt['manifest']).parent / member['blob']).read_bytes())).hexdigest(), 'bytes': len(gzip.decompress((vault / Path(receipt['manifest']).parent / member['blob']).read_bytes()))}} for member in manifest.members}}
    assert members == {expected_members!r}
    assert {{member['path']: {{key: member[key] for key in ('sha256', 'bytes')}} for member in manifest.members}} == {expected_members!r}
    assert {{key: manifest.archive[key] for key in ('sha256', 'bytes')}} == {expected_archive!r}
assert receipt['request'] == {{'import_id': '{import_id}', 'cell_id': '{cell_id}', 'kind': 'archive_members', 'ciphertext_sha256': '{cipher_hash}', 'ciphertext_bytes': {len(ciphertext)}, 'declared_bytes': {declared['bytes']}, 'declared_files': {declared['files']}}}
assert receipt['result'] == {{'ok': True, 'files': {declared['files']}, 'bytes': {declared['bytes']}}}
assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
print(json.dumps({{'receipt': True, 'raw_manifest': True, 'archive': manifest.archive, 'members': members, 'identity_staging_adoption_absent': True}}, sort_keys=True))
"""
        else:
            inspect += "assert not receipt_path.exists()\nprint('refusal receipt and import paths absent')\n"
            assert vault_manifest() == before
            print(f"[import] {case}: all {len(before)} canonical vault entries unchanged before/refusal/cleanup/restart")
        runtime_pod = json.loads(_kubectl(k3s.name, ["get", "pods", "--namespace", namespace,
                                      f"--selector=!{JOB_KIND_LABEL}", "--output=json"]).stdout)["items"][0]
        inspected = _exec_py(k3s.name, namespace, runtime_pod["metadata"]["name"], inspect)
        print(f"[import] final PVC proof: {inspected.stdout.strip()}")
    except BaseException:
        _print_job_diagnostics(k3s.name, namespace, "import")
        for args in (["describe", "pods"], ["logs", "statefulset/cell", "--all-containers", "--tail=100"]):
            diagnostics = _kubectl(k3s.name, [*args, "--namespace", namespace], check=False)
            print(f"[import] runtime diagnostics:\n{diagnostics.stdout}{diagnostics.stderr}")
        raise
    finally:
        sa_client.close()
        _kubectl(k3s.name, ["delete", "namespace", namespace, "--ignore-not-found", "--wait=false"], check=False)
        for path in (archive, tmp_path / "envelope.tar", tmp_path / "upload.age", tmp_path / "other.age"):
            path.unlink(missing_ok=True)


@pytest.mark.skipif(not RUN_LIVE, reason="set RUN_CELLCTL_K3S_TEST=1 for the real export workflow")
# C6 branches on whether the hold must stop a runtime or keep a stopped cell stopped.
@pytest.mark.parametrize("desired_state", ["running", "stopped"])
def test_owner_export_packs_the_real_volume_and_serves_one_verified_archive(
    k3s: K3sCluster, cell_db: CellDatabase, import_storage: dict[str, Any],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desired_state: str,
) -> None:
    """C6: the real export Job reads the cell's volume under the export hold and
    uploads through the real job egress; only the verified version is served,
    a stopped cell's runtime never starts, and expiry deletes every version."""
    image = import_storage["image"]
    documents = _render_platform("templates/cellctl.yaml", "templates/cloud-storage-class.yaml", settings=(
                                     "--set", "cellctl.cellImageRepository=cellctl-import.test/exomem-cell",
                                     "--set-json", "cells.jobEgressExcept=[]"))
    storage_class = next(document for document in documents if document["kind"] == "StorageClass")
    storage_class.update(provisioner="rancher.io/local-path", reclaimPolicy="Delete",
                         volumeBindingMode="WaitForFirstConsumer")
    storage_class.pop("parameters", None)
    _kubectl(k3s.name, ["apply", "--filename=-"], documents=documents)
    _wait_for(lambda: all(
        "typeChecking" in (status := json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy",
                          policy, "--output=json"]).stdout).get("status", {}))
        and not status["typeChecking"].get("expressionWarnings")
        for policy in ("exomem-cellctl-scope", "exomem-cell-connect-guard")),
        timeout=30, description="normal controller and operator policies to type-check")
    token = _issue_sa_token(k3s, name="cellctl", namespace="exomem-cloud")
    configuration = k8s.Configuration()
    k8s_config.load_kube_config(config_file=str(k3s.kubeconfig), client_configuration=configuration)
    configuration.cert_file = configuration.key_file = None
    configuration.api_key = {"authorization": f"Bearer {token}"}
    sa_client = k8s.ApiClient(configuration)
    cluster = ClusterClient(sa_client)
    monkeypatch.setenv("AWS_CA_BUNDLE", str(import_storage["ca"]))
    cell_id, tenant_id = _cell_id(), uuid.uuid4()
    namespace = namespace_name(cell_id)
    storage = import_storage["client"]

    async def sql(query: str, *args: object, role: str = "substrate_owner"):
        connection = await asyncpg.connect(cell_db.dsn(role=role))
        try:
            return await connection.fetchrow(query, *args)
        finally:
            await connection.close()

    asyncio.run(sql("INSERT INTO exomem_tenants(id) VALUES($1)", tenant_id))
    asyncio.run(sql("INSERT INTO exomem_cloud_cells(cell_id,tenant_id,desired_state) VALUES($1,$2,'running')",
                    cell_id, tenant_id))
    asyncio.run(sql("INSERT INTO exomem_cloud_settings(key,value) VALUES('cell_image',$1) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", json.dumps(image)))
    # MinIO's one root credential plays the cell-data staging key cellctl holds for exports and imports.
    secrets = SecretsConfig(cell_token_key_current=b"k3s-integration-test-current-key-32b!",
                            cell_token_key_previous=None, cell_token_key_version=1,
                            cell_token_key_previous_version=None, backup_master_keys={1: b"0" * 32},
                            backup_master_key_current_version=1,
                            staging_key_id=MINIO_ACCESS_KEY, staging_application_key=MINIO_SECRET_KEY)
    cluster_config = ClusterConfig(object_storage_bucket=BUCKET_NAME,
                                    object_storage_endpoint=import_storage["endpoint"],
                                    cell_data_backup_bucket=BUCKET_NAME, cell_data_staging_bucket=BUCKET_NAME,
                                    cell_data_endpoint=import_storage["endpoint"],
                                    capacity=CapacityConfig(attachments_limit_fallback=100, headroom=5))
    fake_b2 = FakeB2()
    volumes = FakeHetznerVolumeProvider()

    async def step():
        connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
        try:
            await reconcile_once(connection, cluster, fake_b2, volumes, secrets, cluster_config, config=LIVE_CONFIG)
        finally:
            await connection.close()

    def cell():
        return asyncio.run(sql("SELECT * FROM exomem_cloud_cells WHERE cell_id=$1", cell_id))

    def settled(state: str):
        def check():
            asyncio.run(step())
            observed = cluster.observe(cell_id, namespace)
            row = cell()
            serving = row["ready"] and observed.pod_ready if state == "running" else not observed.pod_exists
            return row["observed_state"] == state and row["hold_kind"] is None and serving
        return check

    try:
        _wait_for(settled("running"), timeout=360, description="actual cell-init and Cloud runtime Ready")
        # Suppress the unrelated scheduled backup only after the real runtime has become ready.
        asyncio.run(sql("UPDATE exomem_cloud_cells SET last_backup_at=now() WHERE cell_id=$1", cell_id))
        if desired_state == "stopped":
            asyncio.run(sql("UPDATE exomem_cloud_cells SET desired_state='stopped' WHERE cell_id=$1", cell_id,
                            role="substrate_app"))
            _wait_for(settled("stopped"), timeout=180, description="runtime stopped before the export")
        export_id = asyncio.run(sql("INSERT INTO exomem_cloud_exports(cell_id) VALUES($1) RETURNING export_id",
                                    cell_id, role="substrate_app"))["export_id"]
        seen: list[tuple[int | None, bool]] = []

        def export_row():
            return asyncio.run(sql("SELECT * FROM exomem_cloud_exports WHERE export_id=$1", export_id))

        def packed():
            asyncio.run(step())
            observed = cluster.observe(cell_id, namespace)
            seen.append((observed.statefulset_replicas, observed.pod_exists))
            return export_row()["observed_state"] in ("ready", "failed")

        _wait_for(packed, timeout=360, interval=2, description="actual export Job and verified completion")
        job = cluster.observe_export_job(namespace, str(export_id), verify=True)
        print(f"[export] {desired_state}: actual termination {json.dumps(job.report, sort_keys=True)}")
        ready = export_row()
        assert (ready["observed_state"], ready["error_code"]) == ("ready", None)
        assert job.succeeded and job.report["bytes"] == ready["archive_bytes"] == storage.head_object(
            Bucket=BUCKET_NAME, Key=f"cells/{cell_id}/exports/{export_id}.zip",
            VersionId=ready["archive_version_id"])["ContentLength"]
        assert job.report["verified"] is True
        assert job.report["version"] == ready["archive_version_id"]
        assert job.report["sha256"] == ready["archive_sha256"]
        measured = cell()
        assert 0 <= measured["used_bytes"] <= measured["filesystem_bytes"] and measured["usage_observed_at"]
        _wait_for(settled(desired_state), timeout=360, description="export hold released, desired state again")
        assert not cluster.observe_export_job(namespace, str(export_id)).present
        assert not cluster.observe_export_job(namespace, str(export_id), verify=True).present
        if desired_state == "stopped":
            assert {replicas for replicas, _pod in seen} == {0} and not any(pod for _replicas, pod in seen)
            print(f"[export] stopped: {len(seen)} passes, StatefulSet replicas always 0, no runtime pod")

        asyncio.run(sql("UPDATE exomem_cloud_exports SET link_requested_at=now() WHERE export_id=$1", export_id,
                        role="substrate_app"))
        asyncio.run(step())
        linked = export_row()
        with urlopen(linked["download_url"], context=ssl.create_default_context(cafile=str(import_storage["ca"])),
                     timeout=60) as response:
            archive = response.read()
            assert response.headers["Content-Disposition"].startswith("attachment;")
        assert hashlib.sha256(archive).hexdigest() == ready["archive_sha256"]
        (tmp_path / "export.zip").write_bytes(archive)
        verify = (
            "import json; from exomem.vault_portability import verify_export_archive; "
            f"v=verify_export_archive('/fixture/export.zip', expected_cell_id='{cell_id}'); "
            "print(json.dumps({'files': len(v.manifest['files']), 'sha256': v.archive_sha256}))"
        )
        verified = json.loads(_run(["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
                                    "--mount", f"type=bind,src={tmp_path},dst=/fixture", "--entrypoint=python3",
                                    import_storage["tag"], "-c", verify]).stdout)
        assert verified["sha256"] == ready["archive_sha256"] and verified["files"] > 0
        print(f"[export] {desired_state}: downloaded archive verifies: {json.dumps(verified, sort_keys=True)}")

        # Stands in for the TTL passing: cellctl reads expires_at from the row.
        asyncio.run(sql("UPDATE exomem_cloud_exports SET expires_at=now() - interval '1 second' WHERE export_id=$1",
                        export_id))
        asyncio.run(step())
        expired = export_row()
        assert expired["observed_state"] == "expired" and expired["download_url"] is None
        key = f"cells/{cell_id}/exports/{export_id}.zip"
        versions = storage.list_object_versions(Bucket=BUCKET_NAME, Prefix=key)
        assert not versions.get("Versions") and not versions.get("DeleteMarkers")
        assert not storage.list_multipart_uploads(Bucket=BUCKET_NAME, Prefix=key).get("Uploads")
        print(f"[export] {desired_state}: expired, every version of {key} deleted")
    except BaseException:
        _print_job_diagnostics(k3s.name, namespace, "export")
        for args in (["describe", "pods"], ["logs", "statefulset/cell", "--all-containers", "--tail=100"]):
            diagnostics = _kubectl(k3s.name, [*args, "--namespace", namespace], check=False)
            print(f"[export] runtime diagnostics:\n{diagnostics.stdout}{diagnostics.stderr}")
        raise
    finally:
        sa_client.close()
        _kubectl(k3s.name, ["delete", "namespace", namespace, "--ignore-not-found", "--wait=false"], check=False)
        (tmp_path / "export.zip").unlink(missing_ok=True)


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

    # D9: the key custodian's identity, RBAC and admission policy are applied as the chart renders them.
    # Only its Deployment and NetworkPolicies are left out: there is no custodian image to run here, and
    # these cases act through its service account by impersonation. The default private-range egress
    # exclusions stay, because the custodian template refuses to render without them.
    custodian_documents = [
        document for document in _render_platform("templates/key-custodian.yaml",
                                                  settings=("--set", "keyCustodian.enabled=true"))
        if document["kind"] not in {"Deployment", "NetworkPolicy"}
    ]
    assert {"Namespace", "ServiceAccount", "ClusterRole", "ClusterRoleBinding", "ValidatingAdmissionPolicy",
            "ValidatingAdmissionPolicyBinding"} == {document["kind"] for document in custodian_documents}
    _kubectl(k3s.name, ["apply", "--filename=-"], documents=custodian_documents)
    _wait_for(lambda: "typeChecking" in json.loads(_kubectl(
        k3s.name, ["get", "validatingadmissionpolicy", "exomem-key-custodian-scope", "--output=json"]
    ).stdout).get("status", {}), timeout=30, description="ValidatingAdmissionPolicy exomem-key-custodian-scope to type-check")
    custodian_status = json.loads(_kubectl(
        k3s.name, ["get", "validatingadmissionpolicy", "exomem-key-custodian-scope", "--output=json"]
    ).stdout)["status"]["typeChecking"]
    assert custodian_status.get("expressionWarnings", []) == [], custodian_status

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
        staging_key_id=MINIO_ACCESS_KEY,
        staging_application_key=MINIO_SECRET_KEY,
    )
    cluster_config = ClusterConfig(
        object_storage_bucket=BUCKET_NAME,
        object_storage_endpoint=f"http://{k3s.minio_ip}:443",
        cell_data_backup_bucket=BUCKET_NAME,
        cell_data_staging_bucket=BUCKET_NAME,
        cell_data_endpoint=f"http://{k3s.minio_ip}:443",
        # K3s here has no Hetzner CSI driver, so its CSINode carries no
        # allocatable count: D9's fallback is how a rehearsal publishes slots.
        capacity=CapacityConfig(attachments_limit_fallback=100, headroom=5),
    )
    # D9: these cells are new, so deletion lists nothing in the founder account; the cell's backup key
    # is the Secret the key custodian's identity creates below, holding MinIO's one root credential.
    fake_b2 = FakeB2()
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

    # D9: the key custodian, never cellctl, writes a cell's object-store key. This is the Secret the
    # custodian's identity writes for a new cell, in the one shape admission allows; its stringData
    # reaches admission as data, which is what the policy checks. Every backup and restore below
    # reads it, so a refusal here fails the whole scenario at its source.
    print("[3.10] scenario: the key custodian's identity writes the cell's object-store key Secret")
    _kubectl(k3s.name, ["create", "--filename=-", f"--as={CUSTODIAN_USERNAME}"], documents=[{
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": OBJECT_STORE_KEY_SECRET, "namespace": namespace},
        "type": "Opaque",
        "stringData": {OBJECT_STORE_KEY_ID_FIELD: MINIO_ACCESS_KEY, OBJECT_STORE_KEY_SECRET_FIELD: MINIO_SECRET_KEY},
    }])

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

    print("[3.10] scenario: explicit stop and resume preserve the volume and stored data")
    pvc_before = json.loads(
        _kubectl(k3s.name, ["get", "pvc", "cell-data", "--namespace", namespace, "--output=json"]).stdout
    )["metadata"]["uid"]
    # C1 fixes these desired states; the fixture owner supplies controller inputs.
    asyncio.run(_exec_admin("UPDATE exomem_cloud_cells SET desired_state = 'stopped' WHERE cell_id = $1", cell_id))
    _wait_for(
        lambda: _reconciled_to("stopped"),
        timeout=120, interval=2, description="the explicit stopped input to stop the runtime",
    )
    assert asyncio.run(_row())["ready"] is False
    runtime_pods = json.loads(
        _kubectl(k3s.name, ["get", "pods", "--namespace", namespace, f"--selector=!{JOB_KIND_LABEL}", "--output=json"]).stdout
    )["items"]
    assert not runtime_pods, runtime_pods
    asyncio.run(_exec_admin("UPDATE exomem_cloud_cells SET desired_state = 'running' WHERE cell_id = $1", cell_id))
    _wait_for(
        _converged, timeout=180, interval=2, description="the stopped cell to resume Ready",
    )
    resumed_pod = _pod_name()
    assert _exec_py(k3s.name, namespace, resumed_pod, read_source).stdout.strip() == "k3s-integration-canary"
    _assert_owner_only_modes_intact(resumed_pod)
    pvc_after = json.loads(
        _kubectl(k3s.name, ["get", "pvc", "cell-data", "--namespace", namespace, "--output=json"]).stdout
    )["metadata"]["uid"]
    assert pvc_after == pvc_before

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
    # cellctl derives every cell bearer: it may admit itself to the
    # health-only port, never to the cell port, and never admit another peer.
    rendered_ingress = _live("networkpolicy", "runtime-ingress")
    print("[3.10] scenario: admission admits the rendered gateway and cellctl health-port rules")
    assert [rule["ports"] for rule in rendered_ingress["spec"]["ingress"]] == [
        [{"protocol": "TCP", "port": 8765}], [{"protocol": "TCP", "port": 8766}]
    ]
    admitted_ingress = _kubectl(
        k3s.name, ["apply", "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
        documents=[rendered_ingress], check=False,
    )
    assert admitted_ingress.returncode == 0, admitted_ingress.stderr
    cellctl_on_cell_port = copy.deepcopy(rendered_ingress)
    cellctl_on_cell_port["spec"]["ingress"][1]["ports"] = [{"protocol": "TCP", "port": 8765}]
    forged_writes["runtime-ingress admitting cellctl to the cell port"] = cellctl_on_cell_port
    third_peer = copy.deepcopy(rendered_ingress)
    third_peer["spec"]["ingress"].append(copy.deepcopy(third_peer["spec"]["ingress"][1]))
    third_peer["spec"]["ingress"][2]["from"][0]["podSelector"]["matchLabels"] = {
        "app.kubernetes.io/name": "exomem-media-edge"
    }
    forged_writes["runtime-ingress admitting a third peer to the health port"] = third_peer
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
    # D9: the Secret that holds a cell's backup key is the custodian's alone to write.
    forged_writes["the key custodian's Secret, cell-object-store-key"] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": OBJECT_STORE_KEY_SECRET, "namespace": namespace},
        "type": "Opaque",
        "stringData": {OBJECT_STORE_KEY_ID_FIELD: "forged", OBJECT_STORE_KEY_SECRET_FIELD: "forged"},
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
        "tolerations": lambda pod: pod.update(tolerations=[{"operator": "Exists"}]),
        "a nodeSelector": lambda pod: pod.update(nodeSelector={"node-role.kubernetes.io/control-plane": "true"}),
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
    # Security MEDIUM (recheck): a fresh cell namespace has no NetworkPolicy
    # yet, so the isolation policy must keep any pod out of it until the
    # deny-all default-deny exists.
    print("[3.10] scenario: admission denies a Job in a fresh cell namespace with no default-deny")
    fresh_namespace = namespace_name(_cell_id())
    fresh = copy.deepcopy(forged_namespace)
    fresh["metadata"]["name"] = fresh_namespace
    # The scope policy requires the cell label, which is also what puts the
    # namespace inside the isolation policy's namespaceSelector.
    fresh["metadata"]["labels"]["exomem.io/cloud-cell"] = fresh_namespace.removeprefix("exo-cell-")
    _kubectl(k3s.name, ["create", "--filename=-", f"--as={cellctl_username}"], documents=[fresh])
    fresh_job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "probe", "namespace": fresh_namespace},
        "spec": {
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {"name": "probe", "image": good_image, "command": ["python3", "-c", "pass"]}
                    ],
                }
            }
        },
    }
    denied_fresh = _kubectl(
        k3s.name, ["create", "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
        documents=[fresh_job], check=False,
    )
    assert denied_fresh.returncode != 0
    assert "exomem-cellctl-isolation" in denied_fresh.stderr, denied_fresh.stderr
    print("[3.10] scenario: admission denies a StatefulSet in a fresh cell namespace with no default-deny")
    fresh_statefulset = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "cell", "namespace": fresh_namespace},
        "spec": copy.deepcopy(stateful_set["spec"]),
    }
    denied_fresh_statefulset = _kubectl(
        k3s.name, ["create", "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
        documents=[fresh_statefulset], check=False,
    )
    assert denied_fresh_statefulset.returncode != 0
    assert "exomem-cellctl-isolation" in denied_fresh_statefulset.stderr, denied_fresh_statefulset.stderr
    _kubectl(k3s.name, ["delete", "namespace", fresh_namespace, "--wait=false"])

    # Security LOW: an exo-cell-* namespace someone else made without the cell
    # label is outside the isolation selector, so cellctl may not run a pod there.
    print("[3.10] scenario: admission denies a Job in an unlabelled exo-cell namespace another party made")
    stray_namespace = namespace_name(_cell_id())
    stray = copy.deepcopy(forged_namespace)
    stray["metadata"]["name"] = stray_namespace
    _kubectl(k3s.name, ["create", "--filename=-"], documents=[stray])
    stray_job = copy.deepcopy(fresh_job)
    stray_job["metadata"]["namespace"] = stray_namespace
    denied_stray = _kubectl(
        k3s.name, ["create", "--dry-run=server", "--filename=-", f"--as={cellctl_username}"],
        documents=[stray_job], check=False,
    )
    assert denied_stray.returncode != 0
    assert "exomem.io/cloud-cell label names its own cell" in denied_stray.stderr, denied_stray.stderr
    _kubectl(k3s.name, ["delete", "namespace", stray_namespace, "--wait=false"])

    print("[3.10] scenario: admission denies deleting default-deny")
    denied_delete = _kubectl(
        k3s.name,
        ["delete", "networkpolicy", "default-deny", "--namespace", namespace, "--dry-run=server", f"--as={cellctl_username}"],
        check=False,
    )
    assert denied_delete.returncode != 0
    assert "exomem-cellctl-scope" in denied_delete.stderr, denied_delete.stderr

    # === D9 key custody: with a stolen cellctl token, a cell's object-store key can be neither
    # overwritten nor read back; with the custodian's token, nothing but that one Secret is writable. ===

    def _can_i(identity: str, *words: str) -> str:
        return _kubectl(k3s.name, ["auth", "can-i", *words, f"--as={identity}"], check=False).stdout.strip()

    print("[3.10] scenario: cellctl can neither patch nor read the cell's object-store key")
    patched = _kubectl(
        k3s.name,
        ["patch", "secret", OBJECT_STORE_KEY_SECRET, "--namespace", namespace, "--type=merge", "--dry-run=server",
         f"--patch={json.dumps({'stringData': {OBJECT_STORE_KEY_ID_FIELD: 'forged'}})}", f"--as={cellctl_username}"],
        check=False,
    )
    assert patched.returncode != 0
    assert "exomem-cellctl-scope" in patched.stderr, patched.stderr
    assert _can_i(cellctl_username, "get", f"secrets/{OBJECT_STORE_KEY_SECRET}", "--namespace", namespace) == "no"
    assert _can_i(cellctl_username, "list", "secrets", "--namespace", namespace) == "no"

    print("[3.10] scenario: the key custodian can read its own Secret name and write nothing but that Secret")
    assert _can_i(CUSTODIAN_USERNAME, "get", f"secrets/{OBJECT_STORE_KEY_SECRET}", "--namespace", namespace) == "yes"
    for verb, resource in (("get", "secrets/cell-credentials"), ("list", "secrets"), ("patch", "secrets"),
                           ("update", "secrets"), ("delete", "secrets"), ("create", "configmaps"),
                           ("create", "pods"), ("create", "namespaces")):
        assert _can_i(CUSTODIAN_USERNAME, verb, resource, "--namespace", namespace) == "no", (verb, resource)

    def _custodian_secret(name: str, secret_namespace: str, data: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": secret_namespace},
            "stringData": data or {OBJECT_STORE_KEY_ID_FIELD: "forged", OBJECT_STORE_KEY_SECRET_FIELD: "forged"},
        }

    # RBAC lets the custodian create any Secret anywhere; admission is what narrows that to one name in cells.
    for described, document in {
        "the cell's cell-credentials Secret": _custodian_secret("cell-credentials", namespace),
        "another Secret name in a cell namespace": _custodian_secret("unrelated-key", namespace),
        "cell-object-store-key outside a cell namespace": _custodian_secret(OBJECT_STORE_KEY_SECRET, "exomem-cloud"),
        "cell-object-store-key with an extra field": _custodian_secret(OBJECT_STORE_KEY_SECRET, namespace, {
            OBJECT_STORE_KEY_ID_FIELD: "forged", OBJECT_STORE_KEY_SECRET_FIELD: "forged", "extra": "forged"}),
    }.items():
        print(f"[3.10] scenario: admission denies the key custodian writing {described}")
        denied_custodian = _kubectl(
            k3s.name, ["create", "--dry-run=server", "--filename=-", f"--as={CUSTODIAN_USERNAME}"],
            documents=[document], check=False,
        )
        assert denied_custodian.returncode != 0, described
        assert "exomem-key-custodian-scope" in denied_custodian.stderr, (described, denied_custodian.stderr)

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


@pytest.mark.parametrize("placement_mode", ["dedicated", "selected", "all-shared"])
def test_dedicated_cell_admission_allows_only_own_exact_pair(k3s: K3sCluster, placement_mode: str) -> None:
    from cellctl.manifests import (
        CellManifestSpec,
        render_backup_job,
        render_namespace,
        render_network_policies,
        render_restore_job,
        render_statefulset,
    )

    selected_id, other_id = _cell_id(), _cell_id()
    placement = {}
    settings = ("--set-json", f'cellctl.dedicatedCellIds=["{selected_id}"]')
    if placement_mode != "dedicated":
        from dataclasses import asdict

        from cellctl.capacity import SharedWorkerPolicy
        from cellctl.manifests import ResourceSettings

        policy = SharedWorkerPolicy(mode=placement_mode, cell_ids=(selected_id,) if placement_mode == "selected" else (),
                                    profile="qualified-test", topology_key="topology.kubernetes.io/zone",
                                    topology_value="test-zone", occupancy=2,
                                    resources=ResourceSettings(cpu_request="1", memory_request="2Gi"), reserve_cpu="0", reserve_memory="0")
        _, placement = ClusterConfig(object_storage_bucket="test", shared_worker=policy).workload_for_cell(selected_id)
        settings = ("--set-json", "cellctl.sharedWorker=" + json.dumps(asdict(policy)))
        if placement_mode == "all-shared":
            settings += ("--set-json", f'cellctl.dedicatedCellIds=["{other_id}"]')
    documents = _render_platform("templates/cellctl.yaml", settings=(
        *settings, "--set-string", f"cellctl.cellImageRepository={STANDIN_REPOSITORY}",
    ))
    _apply_server_side(k3s.name, [doc for doc in documents if doc["kind"] != "Deployment"])
    selected = CellManifestSpec(cell_id=selected_id, image=STANDIN_REPOSITORY + "@sha256:" + "a" * 64,
                                replicas=0, read_only=False, dedicated_node=placement_mode == "dedicated", placement=placement,
                                hold_started_at="2026-01-01T00:00:00+00:00")
    other = CellManifestSpec(cell_id=other_id, image=selected.image, replicas=0, read_only=False,
                            dedicated_node=placement_mode == "dedicated", placement=placement)
    for spec in (selected, other):
        _apply_server_side(k3s.name, [render_namespace(spec), *render_network_policies(spec)])

    def dry_run(document: dict):
        return _kubectl(k3s.name, ["create", "--dry-run=server", "--filename=-",
                                  "--as=system:serviceaccount:exomem-cloud:cellctl"],
                        documents=[document], check=False)

    def policy_ready():
        policy = json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy",
                                              "exomem-cellctl-scope", "-o=json"]).stdout)
        status = policy.get("status", {})
        if status.get("observedGeneration") != policy["metadata"]["generation"]:
            return False
        assert not status.get("typeChecking", {}).get("expressionWarnings"), status
        # Type-check status can precede enforcement of an updated policy.
        # Require both admission and named-policy denial before the assertions.
        if dry_run(render_statefulset(selected)).returncode != 0:
            return False
        refused = dry_run(render_statefulset(other))
        return refused.returncode != 0 and "exomem-cellctl-scope" in refused.stderr

    _wait_for(policy_ready, timeout=30, description="dedicated placement policy admission")
    refused_other = dry_run(render_statefulset(other))
    assert refused_other.returncode != 0 and "exomem-cellctl-scope" in refused_other.stderr, refused_other.stderr
    for document in (
        render_statefulset(selected),
        render_backup_job(selected, bucket_name="bucket", endpoint="https://s3.example"),
        render_restore_job(selected, bucket_name="bucket", endpoint="https://s3.example", snapshot_id="a" * 64),
    ):
        admitted = dry_run(document)
        assert admitted.returncode == 0, admitted.stderr
        absent = copy.deepcopy(document)
        absent["spec"]["template"]["spec"].pop("nodeSelector")
        absent["spec"]["template"]["spec"].pop("tolerations")
        assert dry_run(absent).returncode == 0

    for mutate in (
        lambda pod: pod["nodeSelector"].update({"extra": "value"}),
        lambda pod: pod["nodeSelector"].update({"exomem.io/dedicated-cell" if placement_mode == "dedicated" else "exomem.io/shared-profile": other_id}),
        lambda pod: pod["tolerations"].append({"operator": "Exists"}),
        lambda pod: pod["tolerations"][0].update(value=other_id),
        lambda pod: pod["tolerations"][0].update(operator="Exists", value=""),
        lambda pod: pod.pop("tolerations"),
        lambda pod: pod.update(affinity={}),
        lambda pod: pod.update(nodeName=k3s.name),
    ):
        attack = render_statefulset(selected)
        mutate(attack["spec"]["template"]["spec"])
        denied = dry_run(attack)
        assert denied.returncode != 0 and "exomem-cellctl-scope" in denied.stderr, denied.stderr


# === Artifact transport: exact optional admission and a reviewer-only edge. ===


def test_artifact_broker_selected_cell_edge_and_admission(k3s: K3sCluster) -> None:
    from cellctl.manifests import CellManifestSpec, render_namespace, render_network_policies

    selected_id, other_id = _cell_id(), _cell_id()
    image = _import_image(k3s.name, _build_standin_image(IMAGE_DIR), repository=STANDIN_REPOSITORY)
    endpoint = "http://10.43.0.25:8767"
    settings = (
        "--set", "artifactBroker.enabled=true",
        "--set-string", f"artifactBroker.image={image}",
        "--set-string", f"artifactBroker.endpoint={endpoint}",
        "--set-json", f'artifactBroker.cellIds=["{selected_id}"]',
        "--set-string", f"cellctl.cellImageRepository={STANDIN_REPOSITORY}",
    )
    documents = _render_platform("templates/cellctl.yaml", "templates/artifact-broker.yaml", settings=settings)
    _apply_server_side(k3s.name, [doc for doc in documents if doc["kind"] != "Deployment"])
    broker_template = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "exomem-artifact-broker")["spec"]["template"]
    broker_pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "artifact-broker-security-check", "namespace": "exomem-cloud", **broker_template["metadata"]},
        "spec": broker_template["spec"],
    }
    admitted_broker = _kubectl(k3s.name, ["create", "--dry-run=server", "--filename=-"], documents=[broker_pod], check=False)
    assert admitted_broker.returncode == 0, admitted_broker.stderr
    username = "system:serviceaccount:exomem-cloud:cellctl"

    def _type_checked() -> bool:
        policy = json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy", "exomem-cellctl-scope", "-o=json"]).stdout)
        status = policy.get("status", {})
        if status.get("observedGeneration") != policy["metadata"]["generation"]:
            return False
        assert not status.get("typeChecking", {}).get("expressionWarnings"), status
        return True

    _wait_for(_type_checked, timeout=30, description="artifact broker CEL to type-check")
    specs = [CellManifestSpec(cell_id=cell_id, image=image, replicas=1, read_only=False,
                             artifact_broker_url=endpoint if cell_id == selected_id else "")
             for cell_id in (selected_id, other_id)]
    for spec in specs:
        _apply_server_side(k3s.name, [render_namespace(spec)])
    policies = [policy for spec in specs for policy in render_network_policies(spec)]
    selected = next(p for p in policies if p["metadata"]["namespace"] == specs[0].namespace and p["metadata"]["name"] == "runtime-ingress")

    def _dry_run(policy: dict) -> subprocess.CompletedProcess[str]:
        return _kubectl(k3s.name, ["apply", "--dry-run=server", "--filename=-", f"--as={username}"],
                        documents=[policy], check=False)

    forged = copy.deepcopy(selected)
    forged["spec"]["egress"][0]["ports"][0]["port"] = 8768
    # An older policy also denies the forged request. Wait for the new allowlist.
    _wait_for(lambda: _dry_run(selected).returncode == 0, timeout=30,
              description="selected-cell artifact broker policy admission")
    allowed = _dry_run(selected)
    assert allowed.returncode == 0, allowed.stderr
    mutations = [forged]
    for selector, key, value in (
        ("namespaceSelector", "kubernetes.io/metadata.name", "kube-system"),
        ("podSelector", "app.kubernetes.io/name", "exomem-cloud-gateway"),
    ):
        changed = copy.deepcopy(selected)
        changed["spec"]["egress"][0]["to"][0][selector]["matchLabels"][key] = value
        mutations.append(changed)
    unselected = copy.deepcopy(selected)
    unselected["metadata"]["namespace"] = specs[1].namespace
    unselected["spec"]["podSelector"]["matchLabels"]["exomem.io/cell"] = other_id
    mutations.append(unselected)
    dns = copy.deepcopy(selected)
    dns["spec"]["egress"].append({"ports": [{"protocol": "UDP", "port": 53}]})
    mutations.append(dns)
    for policy in mutations:
        refused = _dry_run(policy)
        assert refused.returncode != 0 and "exomem-cellctl-scope" in refused.stderr, refused.stderr
    _apply_server_side(k3s.name, policies)

    server = (
        "import http.server, threading, time; "
        "[threading.Thread(target=http.server.ThreadingHTTPServer(('0.0.0.0', p), "
        "http.server.SimpleHTTPRequestHandler).serve_forever, daemon=True).start() "
        "for p in (8767, 8768)]; time.sleep(3600)"
    )
    broker = _probe_pod("artifact-broker-probe", "exomem-cloud", image,
                        labels={"app.kubernetes.io/name": "exomem-artifact-broker"},
                        command=["python3", "-c", server], port=8767)
    decoy = _probe_pod("artifact-decoy", specs[1].namespace, image,
                       labels={"app.kubernetes.io/name": "exomem-artifact-broker"},
                       command=["python3", "-c", server])
    decoy_allow = {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "artifact-decoy-ingress", "namespace": specs[1].namespace},
        "spec": {"podSelector": {"matchLabels": decoy["metadata"]["labels"]}, "policyTypes": ["Ingress"], "ingress": [{}]},
    }
    clients = [_probe_pod("artifact-client", spec.namespace, image,
                          labels={"app.kubernetes.io/name": "exomem-cell", "exomem.io/cell": spec.cell_id}) for spec in specs]
    job = _probe_pod("artifact-job-client", specs[0].namespace, image,
                     labels={**clients[0]["metadata"]["labels"], "exomem.io/cell-job": "backup"})
    _apply_server_side(k3s.name, [broker, decoy, decoy_allow, *clients, job])
    _wait_pods_ready(k3s.name, "exomem-cloud", "artifact-broker-probe")
    for spec in specs:
        _wait_pods_ready(k3s.name, spec.namespace, "artifact-client")
    _wait_pods_ready(k3s.name, specs[0].namespace, "artifact-job-client")
    _wait_pods_ready(k3s.name, specs[1].namespace, "artifact-decoy")
    decoy_ip = _kubectl(k3s.name, ["get", "pod", "artifact-decoy", "-n", specs[1].namespace, "-o=jsonpath={.status.podIP}"]).stdout
    assert _tcp_probe(k3s.name, "exomem-cloud", "artifact-broker-probe", "127.0.0.1", 8768)
    _wait_for(lambda: _tcp_probe(k3s.name, specs[0].namespace, "artifact-client", "10.43.0.25", 8767),
              timeout=30, description="selected runtime to broker")
    for namespace, name, address, port in (
        (specs[1].namespace, "artifact-client", "10.43.0.25", 8767),
        (specs[0].namespace, "artifact-job-client", "10.43.0.25", 8767),
        (specs[0].namespace, "artifact-client", "10.43.0.25", 8768),
        (specs[0].namespace, "artifact-client", decoy_ip, 8767),
        (specs[0].namespace, "artifact-client", "1.1.1.1", 443),
        (specs[0].namespace, "artifact-client", "169.254.169.254", 80),
    ):
        assert not _tcp_probe(k3s.name, namespace, name, address, port), (namespace, name, address, port)
    dns_result = _exec_py(k3s.name, specs[0].namespace, "artifact-client",
                          "import socket; socket.setdefaulttimeout(3); socket.gethostbyname('kubernetes.default.svc.cluster.local')", check=False)
    assert dns_result.returncode != 0
    # The fleet switch gives the unlisted cell the same edge: its runtime reaches
    # the broker, and a Job pod in its namespace still does not.
    fleet = _render_platform("templates/cellctl.yaml", "templates/artifact-broker.yaml",
                             settings=(*settings, "--set", "artifactBroker.allCells=true"))
    _apply_server_side(k3s.name, [doc for doc in fleet if doc["kind"] != "Deployment"])
    unlisted = CellManifestSpec(cell_id=specs[1].cell_id, image=image, replicas=1, read_only=False,
                                artifact_broker_url=endpoint)
    _apply_server_side(k3s.name, list(render_network_policies(unlisted)))
    unlisted_job = _probe_pod("artifact-job-client", unlisted.namespace, image,
                              labels={**clients[1]["metadata"]["labels"], "exomem.io/cell-job": "backup"})
    _apply_server_side(k3s.name, [unlisted_job])
    _wait_pods_ready(k3s.name, unlisted.namespace, "artifact-job-client")
    _wait_for(lambda: _tcp_probe(k3s.name, unlisted.namespace, "artifact-client", "10.43.0.25", 8767),
              timeout=30, description="unlisted runtime to broker under the fleet switch")
    assert not _tcp_probe(k3s.name, unlisted.namespace, "artifact-job-client", "10.43.0.25", 8767)
    disabled = _render_platform("templates/cellctl.yaml")
    _apply_server_side(k3s.name, [doc for doc in disabled if doc["kind"] == "ValidatingAdmissionPolicy"])
    _wait_for(lambda: "exomem-cellctl-scope" in _dry_run(selected).stderr, timeout=30,
              description="disabled admission to refuse even the exact broker edge")


@pytest.mark.parametrize("all_cells", [True, False])
def test_artifact_broker_admission_for_an_unlisted_cell_follows_the_fleet_switch(
    k3s: K3sCluster, all_cells: bool,
) -> None:
    from cellctl.manifests import CellManifestSpec, render_namespace, render_network_policies

    listed_id, unlisted_id = _cell_id(), _cell_id()
    endpoint = "http://10.43.0.25:8767"
    image = STANDIN_REPOSITORY + "@sha256:" + "a" * 64
    documents = _render_platform("templates/cellctl.yaml", settings=(
        "--set", "artifactBroker.enabled=true",
        "--set-string", f"artifactBroker.image={image}",
        "--set-string", f"artifactBroker.endpoint={endpoint}",
        "--set-json", f'artifactBroker.cellIds=["{listed_id}"]',
        "--set", f"artifactBroker.allCells={str(all_cells).lower()}",
        "--set-string", f"cellctl.cellImageRepository={STANDIN_REPOSITORY}",
    ))
    _apply_server_side(k3s.name, [doc for doc in documents if doc["kind"] != "Deployment"])
    # cellctl renders the broker edge for a cell the switch selects, listed or not.
    specs = [CellManifestSpec(cell_id=cell_id, image=image, replicas=0, read_only=False,
                             artifact_broker_url=endpoint) for cell_id in (listed_id, unlisted_id)]
    exact = {}
    for spec in specs:
        _apply_server_side(k3s.name, [render_namespace(spec)])
        exact[spec.cell_id] = next(p for p in render_network_policies(spec)
                                   if p["metadata"]["name"] == "runtime-ingress")

    def dry_run(policy: dict) -> subprocess.CompletedProcess[str]:
        return _kubectl(k3s.name, ["apply", "--dry-run=server", "--filename=-",
                                  "--as=system:serviceaccount:exomem-cloud:cellctl"],
                        documents=[policy], check=False)

    def policy_settled() -> bool:
        policy = json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy",
                                              "exomem-cellctl-scope", "-o=json"]).stdout)
        status = policy.get("status", {})
        if status.get("observedGeneration") != policy["metadata"]["generation"]:
            return False
        assert not status.get("typeChecking", {}).get("expressionWarnings"), status
        # Type-check status can precede enforcement of an updated policy, so
        # wait until the unlisted cell's exact edge gets the answer under test.
        return (dry_run(exact[unlisted_id]).returncode == 0) == all_cells

    _wait_for(policy_settled, timeout=60, description="admission to follow the fleet switch")
    assert dry_run(exact[listed_id]).returncode == 0
    if not all_cells:
        refused = dry_run(exact[unlisted_id])
        assert "exomem-cellctl-scope" in refused.stderr, refused.stderr

    def wrong_port(policy: dict) -> None:
        policy["spec"]["egress"][0]["ports"][0]["port"] = 8768

    def wrong_peer(policy: dict) -> None:
        policy["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"] = {
            "app.kubernetes.io/name": "exomem-cloud-gateway"}

    def extra_dns_rule(policy: dict) -> None:
        policy["spec"]["egress"].append({"ports": [{"protocol": "UDP", "port": 53}]})

    def public_destination(policy: dict) -> None:
        policy["spec"]["egress"][0]["to"] = [{"ipBlock": {"cidr": "0.0.0.0/0"}}]

    # The switch widens who gets the exact broker edge, never what the edge is.
    for cell_id in (listed_id, unlisted_id):
        for widen in (wrong_port, wrong_peer, extra_dns_rule, public_destination):
            attack = copy.deepcopy(exact[cell_id])
            widen(attack)
            denied = dry_run(attack)
            assert denied.returncode != 0 and "exomem-cellctl-scope" in denied.stderr, (
                all_cells, cell_id, widen.__name__, denied.stderr)


# === add-cloud-media-store D12: the media edge peer follows the namespace label. ===


def test_media_edge_peer_is_admitted_only_in_a_namespace_cellctl_labelled(k3s: K3sCluster) -> None:
    from cellctl.manifests import render_namespace, render_network_policies

    from .test_media_edge import _spec

    labelled_id, unlabelled_id = _cell_id(), _cell_id()
    image = STANDIN_REPOSITORY + "@sha256:" + "a" * 64
    endpoint = "http://10.43.0.30:8769"
    documents = _render_platform("templates/cellctl.yaml", settings=(
        "--set", "artifactBroker.enabled=true",
        "--set-string", f"artifactBroker.image={image}",
        "--set-string", "artifactBroker.endpoint=http://10.43.0.25:8767",
        "--set-json", f'artifactBroker.cellIds=["{labelled_id}"]',
        "--set", "mediaEdge.enabled=true",
        "--set-string", f"mediaEdge.endpoint={endpoint}",
        "--set-string", "mediaEdge.bucket=media-bucket",
        "--set-json", f'mediaEdge.cellIds=["{labelled_id}"]',
        "--set-string", f"cellctl.cellImageRepository={STANDIN_REPOSITORY}",
    ))
    _apply_server_side(k3s.name, [doc for doc in documents if doc["kind"] != "Deployment"])
    served = {cell_id: _spec(cell_id=cell_id, image=image, media_edge_url=endpoint)
              for cell_id in (labelled_id, unlabelled_id)}
    # The labelled namespace is rendered as a served cell's; the other as one the edge never served.
    _apply_server_side(k3s.name, [render_namespace(served[labelled_id]),
                                  render_namespace(_spec(cell_id=unlabelled_id, image=image, media_edge_url=""))])
    edge_policy = {cell_id: next(p for p in render_network_policies(spec) if p["metadata"]["name"] == "runtime-ingress")
                   for cell_id, spec in served.items()}
    assert any(port["port"] == 8769 for rule in edge_policy[labelled_id]["spec"]["egress"]
               for port in rule.get("ports", []))
    cellctl = "--as=system:serviceaccount:exomem-cloud:cellctl"

    def dry_run(policy: dict) -> subprocess.CompletedProcess[str]:
        return _kubectl(k3s.name, ["apply", "--dry-run=server", "--filename=-", cellctl],
                        documents=[policy], check=False)

    def policy_settled() -> bool:
        policy = json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy",
                                              "exomem-cellctl-scope", "-o=json"]).stdout)
        status = policy.get("status", {})
        if status.get("observedGeneration") != policy["metadata"]["generation"]:
            return False
        assert not status.get("typeChecking", {}).get("expressionWarnings"), status
        # An earlier test's policy refuses the edge peer everywhere; wait for this one to enforce.
        return dry_run(edge_policy[labelled_id]).returncode == 0

    _wait_for(policy_settled, timeout=60, description="admission to give the edge peer to a labelled namespace")
    refused = dry_run(edge_policy[unlabelled_id])
    assert refused.returncode != 0 and "exomem-cellctl-scope" in refused.stderr, refused.stderr

    # cellctl's own order within one render: the namespace with the label, then the policy, back to back
    # in one client. Admission reads the namespace as it now stands, so the policy is admitted.
    paired = _kubectl(k3s.name, ["apply", "--server-side", "--field-manager=cellctl", "--filename=-", cellctl],
                      documents=[render_namespace(served[unlabelled_id]), edge_policy[unlabelled_id]], check=False)
    assert paired.returncode == 0, paired.stderr


# === harden-exomem-cloud-operator-access: the edge, the Cloud issuer and cell
# connect admission, on the same disposable K3s cluster. ===

EDGE_NAMESPACE = "exomem-edge"
EDGE_RELEASE = "cellctl-k3s-edge"
VALIDATION_VALUES = yaml.safe_load((PLATFORM_CHART / "values.validation.yaml").read_text(encoding="utf-8"))
MCP_HOSTNAME = VALIDATION_VALUES["cloudGateway"]["hostname"]
INGRESS_SOURCE_VALUE = VALIDATION_VALUES["cloudGateway"]["trustedIngressSourceValue"]
TRAEFIK_CHART = next((PLATFORM_CHART / "charts").glob("traefik-*.tgz"))

# The real Substrate gateway is not part of this suite. This stand-in serves
# the chart's gateway port and echoes what reached it, so the test can see
# the route, the path and the trusted-ingress header Traefik set.
GATEWAY_STANDIN = """
import http.server, json
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"stand_in": "exomem-cloud-gateway", "path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""

# A client outside the cluster, on the node's own port 443, with the MCP
# hostname as SNI and Host. It also sends a forged trusted-ingress header,
# which Traefik must overwrite.
PUBLIC_CLIENT = """
import http.client, json, socket, ssl, sys
host, address, path, header = sys.argv[1:5]
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
class Connection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(socket.create_connection((address, 443), timeout=10), server_hostname=host)
connection = Connection(host, 443, context=context, timeout=10)
connection.request("GET", path, headers={header: "forged"})
response = connection.getresponse()
print(json.dumps({"status": response.status, "body": response.read().decode(errors="replace")}))
"""

RESTRICTED_POD_SECURITY = {
    "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
    "container": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
}


def _render_platform(*templates: str, settings: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The chart's documents from these template paths, in render order. A
    path the chart does not have contributes nothing, so the same fixture
    runs against an older chart and fails on behaviour, not on rendering."""

    rendered = _run(
        [
            HELM, "template", EDGE_RELEASE, str(PLATFORM_CHART),
            "--namespace", "exomem-platform",
            "--values", str(PLATFORM_CHART / "values.validation.yaml"),
            *settings,
        ]
    ).stdout
    documents = []
    for chunk in re.split(r"^---$", rendered, flags=re.MULTILINE):
        source = re.search(r"^# Source: [^/]+/(.+)$", chunk, flags=re.MULTILINE)
        document = yaml.safe_load(chunk)
        if source and source.group(1) in templates and isinstance(document, dict):
            documents.append(document)
    return documents


def _apply_server_side(k3s_name: str, documents: list[dict[str, Any]]) -> None:
    _kubectl(k3s_name, ["apply", "--server-side", "--force-conflicts", "--field-manager=edge-test", "--filename=-"], documents=documents)


def _probe_pod(name: str, namespace: str, image: str, *, labels: dict[str, str] | None = None,
               command: list[str] | None = None, port: int | None = None) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "probe",
        "image": image,
        "command": command or ["python3", "-c", "import time; time.sleep(3600)"],
        "securityContext": RESTRICTED_POD_SECURITY["container"],
    }
    if port is not None:
        container["ports"] = [{"name": "http", "containerPort": port}]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels or {}},
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": RESTRICTED_POD_SECURITY["securityContext"],
            "containers": [container],
        },
    }


def _tcp_probe(k3s_name: str, namespace: str, pod: str, address: str, port: int) -> bool:
    source = (
        "import socket, sys; s = socket.socket(); s.settimeout(3); "
        f"sys.exit(0 if s.connect_ex(('{address}', {port})) == 0 else 1)"
    )
    return _exec_py(k3s_name, namespace, pod, source, check=False).returncode == 0


def _wait_pods_ready(k3s_name: str, namespace: str, *pods: str) -> None:
    for pod in pods:
        _kubectl(k3s_name, ["wait", "--namespace", namespace, "--for=condition=Ready", f"pod/{pod}", "--timeout=180s"])


def _container_ip(name: str) -> str:
    address = _run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name]).stdout.strip()
    assert address, f"container {name} has no IP"
    return address


@dataclass
class EdgePlatform:
    image: str
    cell_namespace: str
    gateway_service_ip: str
    # Where the render put Traefik and the Cloud Certificate, so every probe
    # below targets the identity the chart actually deploys.
    traefik_namespace: str
    traefik_account: str
    certificate_namespace: str
    # The API server's address after kube-proxy's DNAT: the node, on 6443.
    api_server_ip: str


@pytest.fixture(scope="module")
def edge_platform(k3s: K3sCluster, tmp_path_factory: pytest.TempPathFactory) -> EdgePlatform:
    """The chart's edge objects as rendered, over real Traefik on hostPort 443.

    Stand-ins, named here: the gateway is a stand-in pod behind the chart's
    own gateway Service and NetworkPolicy; the TLS Secret is a self-signed
    certificate in place of the one cert-manager would issue (cert-manager's
    CRDs are installed, its controller is not: ACME cannot run here).
    """

    work = tmp_path_factory.mktemp("edge")
    image = _import_image(k3s.name, _build_standin_image(IMAGE_DIR), repository=STANDIN_REPOSITORY)

    print("[edge] installing the Traefik and cert-manager CRDs the chart's objects need")
    traefik_crds = [doc for doc in yaml.safe_load_all(_run([HELM, "show", "crds", str(TRAEFIK_CHART)]).stdout) if isinstance(doc, dict)]
    cert_manager_crds = _render_platform(
        *(f"charts/cert-manager/templates/crd-cert-manager.io_{kind}.yaml"
          for kind in ("certificates", "certificaterequests", "clusterissuers", "issuers")),
    )
    _apply_server_side(k3s.name, traefik_crds + cert_manager_crds)
    for crd in traefik_crds + cert_manager_crds:
        _kubectl(k3s.name, ["wait", "--for=condition=Established", f"crd/{crd['metadata']['name']}", "--timeout=60s"])

    print("[edge] applying the chart's namespaces, Traefik, Cloud route and gateway policy")
    api_server_ip = _kubectl(
        k3s.name, ["get", "endpoints", "kubernetes", "--output=jsonpath={.subsets[0].addresses[0].ip}"]
    ).stdout.strip()
    assert api_server_ip, "the kubernetes Service has no endpoint"
    platform = _render_platform(
        "templates/namespaces.yaml",
        "templates/cloud-ingress.yaml",
        "templates/cloud-gateway.yaml",
        *(f"charts/traefik/templates/{template}" for template in (
            "rbac/serviceaccount.yaml", "rbac/role.yaml", "rbac/rolebinding.yaml",
            "rbac/clusterrole.yaml", "rbac/clusterrolebinding.yaml",
            "deployment.yaml", "service.yaml", "ingressclass.yaml",
        )),
        settings=("--set-json", f'edge.apiServerCidrs=["{api_server_ip}/32"]'),
    )
    # The gateway Deployment runs Substrate's image, replaced by a stand-in
    # below; cellctl.yaml contributes only exomem-cloud and its default-deny.
    platform = [doc for doc in platform if not (doc["kind"] == "Deployment" and doc["metadata"]["name"] == "exomem-cloud-gateway")]
    platform += [
        doc for doc in _render_platform("templates/cellctl.yaml")
        if doc["kind"] == "Namespace" or (doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "default-deny")
    ]
    order = {"Namespace": 0, "ServiceAccount": 1, "ClusterRole": 2, "Role": 2}
    platform.sort(key=lambda doc: order.get(doc["kind"], 10))
    _apply_server_side(k3s.name, platform)
    traefik = next(
        doc for doc in platform
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == f"{EDGE_RELEASE}-traefik"
    )
    traefik_namespace = traefik["metadata"]["namespace"]
    traefik_account = f"system:serviceaccount:{traefik_namespace}:{traefik['spec']['template']['spec']['serviceAccountName']}"
    certificate_namespace = next(
        doc for doc in platform if doc["kind"] == "Certificate" and doc["metadata"]["name"] == "exomem-cloud-gateway"
    )["metadata"]["namespace"]

    _run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", f"/CN={MCP_HOSTNAME}", "-addext", f"subjectAltName=DNS:{MCP_HOSTNAME}",
         "-keyout", str(work / "tls.key"), "-out", str(work / "tls.crt")]
    )
    tls_secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls",
        "metadata": {"name": "exomem-cloud-gateway-tls", "namespace": certificate_namespace},
        "data": {
            "tls.crt": base64.b64encode((work / "tls.crt").read_bytes()).decode(),
            "tls.key": base64.b64encode((work / "tls.key").read_bytes()).decode(),
        },
    }
    _apply_server_side(k3s.name, [tls_secret])

    gateway = _probe_pod(
        "gateway-standin", "exomem-cloud", image,
        labels={"app.kubernetes.io/name": "exomem-cloud-gateway"},
        command=["python3", "-c", GATEWAY_STANDIN], port=8080,
    )
    cell_namespace = namespace_name(_cell_id())
    _apply_server_side(k3s.name, [gateway, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": cell_namespace}}])
    _wait_pods_ready(k3s.name, "exomem-cloud", "gateway-standin")
    _kubectl(k3s.name, ["rollout", "status", "--namespace", traefik_namespace, f"deployment/{EDGE_RELEASE}-traefik", "--timeout=300s"])
    service_ip = json.loads(
        _kubectl(k3s.name, ["get", "service", "exomem-cloud-gateway", "--namespace", "exomem-cloud", "--output=json"]).stdout
    )["spec"]["clusterIP"]
    return EdgePlatform(
        image=image, cell_namespace=cell_namespace, gateway_service_ip=service_ip,
        traefik_namespace=traefik_namespace, traefik_account=traefik_account, certificate_namespace=certificate_namespace,
        api_server_ip=api_server_ip,
    )


def _public_request(k3s: K3sCluster, path: str) -> tuple[int, str]:
    response = _run(
        ["docker", "run", "--rm", "--network", k3s.network, "python:3.12-alpine", "python3", "-c", PUBLIC_CLIENT,
         MCP_HOSTNAME, _container_ip(k3s.name), path, "x-exomem-ingress-source"]
    ).stdout
    parsed = json.loads(response)
    return parsed["status"], parsed["body"]


def test_the_edge_reads_no_key_and_still_reaches_the_gateway(k3s: K3sCluster, edge_platform: EdgePlatform) -> None:
    print(f"[edge] scenario: Traefik's ServiceAccount ({edge_platform.traefik_account}) is refused Secrets outside its namespace")
    account = edge_platform.traefik_account
    for namespace in ("exomem-cloud", "exomem-platform", edge_platform.cell_namespace):
        for verb in ("get", "list", "watch"):
            answer = _kubectl(
                k3s.name, ["auth", "can-i", verb, "secrets", "--namespace", namespace, f"--as={account}"], check=False,
            )
            print(f"[edge] can-i {verb} secrets -n {namespace} as Traefik: {answer.stdout.strip()}")
            assert answer.stdout.strip() == "no", (namespace, verb, answer.stdout, answer.stderr)
    cluster_wide = _kubectl(
        k3s.name, ["auth", "can-i", "list", "secrets", "--all-namespaces", f"--as={account}"], check=False,
    )
    assert cluster_wide.stdout.strip() == "no", cluster_wide.stdout
    own = _kubectl(
        k3s.name, ["auth", "can-i", "get", "secrets", "--namespace", edge_platform.traefik_namespace, f"--as={account}"], check=False,
    )
    assert own.stdout.strip() == "yes", "the identity probed above is not Traefik's"

    print("[edge] scenario: Traefik's namespace holds only the certificate Secret")
    assert edge_platform.certificate_namespace == edge_platform.traefik_namespace
    secrets = json.loads(
        _kubectl(k3s.name, ["get", "secrets", "--namespace", edge_platform.traefik_namespace, "--output=json"]).stdout
    )["items"]
    assert [secret["metadata"]["name"] for secret in secrets] == ["exomem-cloud-gateway-tls"]

    print("[edge] scenario: a request through the public route reaches the gateway")

    def _routed() -> bool:
        status, body = _public_request(k3s, "/mcp")
        assert status == 200, f"public /mcp returned HTTP {status}: {body[:1024]!r}"
        return True

    try:
        _wait_for(_routed, timeout=120, interval=3, description="Traefik to route the Cloud MCP path to the gateway")
    except AssertionError:
        try:
            for args in (
                ["logs", "--namespace", edge_platform.traefik_namespace,
                 f"deployment/{EDGE_RELEASE}-traefik", "--all-containers", "--tail=100"],
                ["get", "ingressroute", "exomem-cloud-gateway", "--namespace",
                 edge_platform.traefik_namespace, "--output=yaml"],
                ["get", "middlewares.traefik.io", "exomem-cloud-trusted-ingress", "--namespace",
                 edge_platform.traefik_namespace, "--output=yaml"],
                ["get", "middlewares.traefik.io", "--namespace", edge_platform.traefik_namespace,
                 f"--as={account}", "--output=yaml"],
                ["auth", "can-i", "watch", "middlewares.traefik.io", "--namespace",
                 edge_platform.traefik_namespace, f"--as={account}"],
                ["get", "service", "exomem-cloud-gateway", "--namespace",
                 edge_platform.traefik_namespace, "--output=yaml"],
                ["get", "service", "exomem-cloud-gateway", "--namespace", "exomem-cloud", "--output=wide"],
                ["get", "endpointslices", "--namespace", "exomem-cloud",
                 "--selector=kubernetes.io/service-name=exomem-cloud-gateway", "--output=wide"],
                ["get", "pod", "gateway-standin", "--namespace", "exomem-cloud", "--output=wide"],
            ):
                code, output = _kubectl_bounded(k3s.name, args, seconds=5)
                print(f"[edge] route diagnostics {args!r}: exit={code}\n{output}")
        except Exception as error:  # noqa: BLE001 - diagnostics must not replace the route failure
            print(f"[edge] route diagnostics failed: {error}")
        raise
    status, body = _public_request(k3s, "/mcp")
    assert status == 200, (status, body)
    echoed = json.loads(body)
    print(f"[edge] public /mcp -> {status}: path={echoed['path']} ingress-source-set={echoed['headers'].get('x-exomem-ingress-source') == INGRESS_SOURCE_VALUE}")
    assert echoed["stand_in"] == "exomem-cloud-gateway" and echoed["path"] == "/mcp"
    assert echoed["headers"]["x-exomem-ingress-source"] == INGRESS_SOURCE_VALUE
    assert echoed["headers"]["host"] == MCP_HOSTNAME
    unrouted, _ = _public_request(k3s, "/hosted/mcp")
    assert unrouted == 404, unrouted

    print("[edge] scenario: the edge is default-deny; Traefik-labelled egress reaches only the gateway, DNS and the API server")
    edge = edge_platform.traefik_namespace
    target = _probe_pod(
        "edge-egress-target", "exomem-platform", edge_platform.image,
        labels={"app.kubernetes.io/name": "edge-egress-target"},
        command=["python3", "-m", "http.server", "8080"], port=8080,
    )
    target_service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "edge-egress-target", "namespace": "exomem-platform"},
        "spec": {"selector": {"app.kubernetes.io/name": "edge-egress-target"}, "ports": [{"port": 8080, "targetPort": 8080}]},
    }
    control = _probe_pod("edge-egress-control", "exomem-platform", edge_platform.image)
    edge_probe = _probe_pod("edge-egress-probe", edge, edge_platform.image, labels={"exomem.io/ingress": "traefik"})
    _apply_server_side(k3s.name, [target, target_service, control, edge_probe])
    _wait_pods_ready(k3s.name, "exomem-platform", "edge-egress-target", "edge-egress-control")
    _wait_pods_ready(k3s.name, edge, "edge-egress-probe")
    target_ip = json.loads(
        _kubectl(k3s.name, ["get", "service", "edge-egress-target", "--namespace", "exomem-platform", "--output=json"]).stdout
    )["spec"]["clusterIP"]
    api_service_ip = json.loads(_kubectl(k3s.name, ["get", "service", "kubernetes", "--output=json"]).stdout)["spec"]["clusterIP"]
    try:
        _wait_for(
            lambda: _tcp_probe(k3s.name, "exomem-platform", "edge-egress-control", target_ip, 8080),
            timeout=60, interval=2, description="a pod outside the edge to reach the target (else the refusal is vacuous)",
        )
    except AssertionError:
        print(_kubectl(k3s.name, ["logs", "--namespace", "exomem-platform", "edge-egress-target"], check=False).stdout)
        print(_kubectl(k3s.name, ["get", "endpoints", "edge-egress-target", "--namespace", "exomem-platform", "-o", "yaml"], check=False).stdout)
        raise
    # kube-router admits a new pod's traffic until it syncs the pod into its
    # sets, so wait for the refusal before asserting what stays open.
    _wait_for(
        lambda: not _tcp_probe(k3s.name, edge, "edge-egress-probe", target_ip, 8080),
        timeout=90, interval=3, description="the edge's default-deny to refuse a Service in exomem-platform",
    )
    reached = {
        "exomem-platform Service": _tcp_probe(k3s.name, edge, "edge-egress-probe", target_ip, 8080),
        "node kubelet 10250": _tcp_probe(k3s.name, edge, "edge-egress-probe", edge_platform.api_server_ip, 10250),
        "gateway 8080": _tcp_probe(k3s.name, edge, "edge-egress-probe", edge_platform.gateway_service_ip, 8080),
        "API server via its Service": _tcp_probe(k3s.name, edge, "edge-egress-probe", api_service_ip, 443),
    }
    dns = _exec_py(
        k3s.name, edge, "edge-egress-probe",
        "import socket; socket.setdefaulttimeout(3); print(socket.gethostbyname('kubernetes.default.svc.cluster.local'))",
        check=False,
    )
    print(f"[edge] from a Traefik-labelled edge pod: {reached}, DNS exit={dns.returncode}")
    assert reached == {
        "exomem-platform Service": False,
        "node kubelet 10250": False,
        "gateway 8080": True,
        "API server via its Service": True,
    }
    assert dns.returncode == 0, dns.stderr
    print("[edge] scenario: with the policy enforced, Traefik stays Ready (kubelet probes pass) and still serves /mcp")
    time.sleep(15)  # beyond one readiness period (10 s, failureThreshold 1)
    ready = _kubectl(
        k3s.name,
        ["get", "pods", "--namespace", edge, "--selector=app.kubernetes.io/name=traefik",
         "--output=jsonpath={.items[*].status.conditions[?(@.type=='Ready')].status}"],
    ).stdout.split()
    assert ready == ["True"], ready
    assert _public_request(k3s, "/mcp")[0] == 200
    for namespace, name in (("exomem-platform", "edge-egress-target"), ("exomem-platform", "edge-egress-control"), (edge, "edge-egress-probe")):
        _kubectl(k3s.name, ["delete", "pod", name, "--namespace", namespace, "--wait=false"])

    print("[edge] scenario: the gateway admits no pod but the edge's Traefik, not even a Traefik-labelled one elsewhere")
    impostors = [
        _probe_pod("old-edge-peer", "exomem-platform", edge_platform.image, labels={"exomem.io/ingress": "traefik"}),
        _probe_pod("unlabelled-edge-pod", edge_platform.traefik_namespace, edge_platform.image),
    ]
    _apply_server_side(k3s.name, impostors)
    for impostor in impostors:
        namespace, name = impostor["metadata"]["namespace"], impostor["metadata"]["name"]
        _wait_pods_ready(k3s.name, namespace, name)
        assert not _tcp_probe(k3s.name, namespace, name, edge_platform.gateway_service_ip, 8080), (namespace, name)
        _kubectl(k3s.name, ["delete", "pod", name, "--namespace", namespace, "--wait=false"])


def _certificate(kind: str, namespace: str, issuer_kind: str | None = "ClusterIssuer",
                 issuer: str = "exomem-cloud-dns01") -> dict[str, Any]:
    issuer_ref: dict[str, str] = {"name": issuer}
    if issuer_kind is not None:
        issuer_ref["kind"] = issuer_kind
    spec: dict[str, Any] = {"issuerRef": issuer_ref}
    if kind == "Certificate":
        spec |= {"secretName": "probe-tls", "dnsNames": [MCP_HOSTNAME]}
    else:
        spec |= {"request": base64.b64encode(b"not a real CSR; admission runs before any signer").decode()}
    return {"apiVersion": "cert-manager.io/v1", "kind": kind, "metadata": {"name": "issuer-scope-probe", "namespace": namespace}, "spec": spec}


def test_only_the_edge_namespace_may_name_the_cloud_cluster_issuer(k3s: K3sCluster, edge_platform: EdgePlatform) -> None:
    def _dry_run(document: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        return _kubectl(k3s.name, ["create", "--dry-run=server", "--filename=-"], documents=[document], check=False)

    print("[edge] scenario: a Certificate naming the Cloud ClusterIssuer outside the edge namespace is refused")
    refused = _certificate("Certificate", "exomem-cloud")

    def _refused() -> bool:
        result = _dry_run(refused)
        return result.returncode != 0 and "exomem-cloud-issuer-scope" in result.stderr

    _wait_for(_refused, timeout=60, interval=2, description="exomem-cloud-issuer-scope to refuse a Certificate in exomem-cloud")
    print(f"[edge] refused: {_dry_run(refused).stderr.strip()}")
    explicit_group = _certificate("Certificate", "exomem-platform")
    explicit_group["spec"]["issuerRef"]["group"] = "cert-manager.io"
    for document in (_certificate("CertificateRequest", edge_platform.cell_namespace), explicit_group):
        result = _dry_run(document)
        assert result.returncode != 0 and "exomem-cloud-issuer-scope" in result.stderr, (document, result.stderr)

    print("[edge] scenario: the edge's own Certificate, and other issuers elsewhere, are admitted")
    for document in (
        _certificate("Certificate", EDGE_NAMESPACE),
        _certificate("CertificateRequest", EDGE_NAMESPACE),
        _certificate("Certificate", "exomem-cloud", issuer_kind="Issuer"),
        _certificate("Certificate", "exomem-cloud", issuer_kind=None),
        _certificate("Certificate", "exomem-cloud", issuer="some-other-issuer"),
    ):
        result = _dry_run(document)
        assert result.returncode == 0, (document, result.stderr)


# harden-exomem-cloud-operator-access tasks 2.2 and 2.3: the chart's D5
# ValidatingAdmissionPolicy (operator-access.yaml, applied by k3s)
# on the CONNECT operations it matches. It shipped because this probe showed
# the API server enforcing it; a failure here means D4 must stand alone.
BREAK_GLASS = ["--as=break-glass-probe", "--as-group=exomem:break-glass"]


def _kubectl_bounded(k3s_name: str, args: list[str], *, seconds: int = 20) -> tuple[int | None, str]:
    """Runs kubectl inside the node; None means it was still running (admitted
    and streaming) when the bound expired."""

    try:
        result = subprocess.run(
            ["docker", "exec", k3s_name, "timeout", "--signal=KILL", str(seconds), "kubectl", *args],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=seconds + 30,
        )
    except subprocess.TimeoutExpired:
        return None, "still running"
    if result.returncode in (124, 137):
        return None, (result.stdout + result.stderr).strip()
    return result.returncode, (result.stdout + result.stderr).strip()


def test_cell_connect_admission_is_enforced_for_every_identity_but_break_glass(
    k3s: K3sCluster, edge_platform: EdgePlatform,
) -> None:
    cell = edge_platform.cell_namespace
    other = "exomem-connect-probe"
    # The export runbook's scratch shape: exo-scratch-<cell id>-<8 hex>.
    scratch = f"exo-scratch-{cell.removeprefix('exo-cell-')}-{uuid.uuid4().hex[:8]}"
    for namespace in (other, scratch):
        _apply_server_side(k3s.name, [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}])
    _apply_server_side(k3s.name, [_probe_pod("probe", namespace, edge_platform.image) for namespace in (cell, other, scratch)])
    for namespace in (cell, other, scratch):
        _wait_pods_ready(k3s.name, namespace, "probe")

    policy = json.loads(
        _kubectl(k3s.name, ["get", "validatingadmissionpolicy", "exomem-cell-connect-guard", "--output=json"]).stdout
    )
    print(f"[connect] the chart's D5 policy matches: {policy['spec']['matchConstraints']['resourceRules']}")

    def _denied_by_policy(outcome: tuple[int | None, str]) -> bool:
        code, output = outcome
        return code not in (None, 0) and "exomem-cell-connect-guard" in output

    exec_args = ["exec", "--namespace", cell, "probe", "--", "true"]
    _wait_for(lambda: _denied_by_policy(_kubectl_bounded(k3s.name, exec_args)), timeout=60, interval=2,
              description="the D5 policy to take effect on exec")

    probes = {
        "exec": exec_args,
        "attach": ["attach", "--namespace", cell, "probe", "--container=probe"],
        "port-forward": ["port-forward", "--namespace", cell, "pod/probe", "18765:8765"],
        "ephemeral container": ["debug", "--namespace", cell, "probe", f"--image={edge_platform.image}",
                                "--container=debugger", "--", "true"],
        "raw CONNECT exec": ["get", "--raw", f"/api/v1/namespaces/{cell}/pods/probe/exec?command=true&stdout=true"],
        "raw CONNECT attach": ["get", "--raw", f"/api/v1/namespaces/{cell}/pods/probe/attach?container=probe&stdout=true"],
        "raw CONNECT portforward": ["get", "--raw", f"/api/v1/namespaces/{cell}/pods/probe/portforward?ports=8765"],
    }
    print("[connect] observed as the K3s admin (system:masters), not break-glass:")
    for described, args in probes.items():
        outcome = _kubectl_bounded(k3s.name, args)
        print(f"[connect]   {described}: exit={outcome[0]} output={outcome[1]!r}")
        assert _denied_by_policy(outcome), (described, outcome)

    print("[connect] observed in an export scratch namespace, as the same admin:")
    for described, args in (
        ("exec", ["exec", "--namespace", scratch, "probe", "--", "true"]),
        ("raw CONNECT portforward", ["get", "--raw", f"/api/v1/namespaces/{scratch}/pods/probe/portforward?ports=8765"]),
    ):
        outcome = _kubectl_bounded(k3s.name, args)
        print(f"[connect]   {described} in {scratch}: exit={outcome[0]} output={outcome[1]!r}")
        assert _denied_by_policy(outcome), (described, outcome)

    print("[connect] observed outside a cell namespace, as the same admin:")
    outcome = _kubectl_bounded(k3s.name, ["exec", "--namespace", other, "probe", "--", "true"])
    print(f"[connect]   exec in {other}: exit={outcome[0]} output={outcome[1]!r}")
    assert outcome[0] == 0, outcome

    print("[connect] observed as the break-glass group:")
    for described, args in (
        ("exec", [*BREAK_GLASS, *exec_args]),
        ("exec in scratch", [*BREAK_GLASS, "exec", "--namespace", scratch, "probe", "--", "true"]),
        ("ephemeral container", [*BREAK_GLASS, *probes["ephemeral container"]]),
        ("raw CONNECT portforward", [*BREAK_GLASS, *probes["raw CONNECT portforward"]]),
    ):
        outcome = _kubectl_bounded(k3s.name, args)
        print(f"[connect]   {described}: exit={outcome[0]} output={outcome[1]!r}")
        assert "exomem-cell-connect-guard" not in outcome[1], (described, outcome)
        if described != "raw CONNECT portforward":
            assert outcome[0] == 0, (described, outcome)
        else:
            # Admitted, then refused by the kubelet proxy for want of an upgrade.
            assert "Upgrade request required" in outcome[1] or "upgrade" in outcome[1].lower(), outcome


# harden-exomem-cloud-operator-access task 2.4: the operator-access runbook's
# marked blocks, run as written against this cluster. `k3s kubectl` is shimmed
# to the host's kubectl, and the admin kubeconfig is this cluster's.
OPERATOR_RUNBOOK = REPO_ROOT / "docs/runbooks/cloud-operator-access.md"


def _runbook_block(marker: str) -> str:
    text = OPERATOR_RUNBOOK.read_text(encoding="utf-8")
    match = re.search(rf"<!-- rehearsed: {re.escape(marker)} -->\n```bash\n(.*?)\n```", text, flags=re.DOTALL)
    assert match, f"no rehearsed block {marker!r} in {OPERATOR_RUNBOOK}"
    return match.group(1)


def test_the_operator_access_runbook_on_real_k3s(
    k3s: K3sCluster, edge_platform: EdgePlatform, tmp_path: Path,
) -> None:
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "k3s").write_text('#!/bin/sh\n[ "$1" = kubectl ] && shift\nexec kubectl "$@"\n', encoding="utf-8")
    (shim / "k3s").chmod(0o755)
    shared_memory = tmp_path / "shm"
    shared_memory.mkdir(mode=0o700)
    operator_kubeconfig = tmp_path / "root-kube" / "exomem-operator.kubeconfig"
    audit_copy = tmp_path / "audit.log"
    environment = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "ADMIN_KUBECONFIG": str(k3s.kubeconfig),
        "OPERATOR_KUBECONFIG": str(operator_kubeconfig),
        "BREAK_GLASS_TMP": str(shared_memory),
        "AUDIT_LOG": str(audit_copy),
    }
    environment.pop("KUBECONFIG", None)

    def _bash(script: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(["bash", "-c", script], env=environment, cwd=tmp_path, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"{script}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        return result

    def _as(kubeconfig: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig), *args], env=environment, capture_output=True, text=True, check=False,
        )

    cell = edge_platform.cell_namespace
    _apply_server_side(k3s.name, [_probe_pod("runbook-probe", cell, edge_platform.image)])
    _wait_pods_ready(k3s.name, cell, "runbook-probe")

    print("[runbook] issue the 30-day everyday operator certificate")
    issued = _bash(_runbook_block("issue-operator"))
    print(f"[runbook] {issued.stdout.strip()}")
    assert "O = exomem:operators" in issued.stdout and "CN = exomem-operator" in issued.stdout
    assert (operator_kubeconfig.stat().st_mode & 0o777) == 0o600
    print("[runbook] the operator identity reads status and logs, and no Secret or connect subresource")
    checked = _bash(_runbook_block("check-operator"))
    assert "exomem:operators" in checked.stdout, checked.stdout
    # The block's checks are not vacuous: the admin identity passes the same one.
    admin_can = _as(k3s.kubeconfig, "auth", "can-i", "create", "pods", "--subresource=exec", "--all-namespaces")
    assert admin_can.stdout.strip() == "yes", admin_can
    assert _as(operator_kubeconfig, "get", "pods", "--namespace", cell).returncode == 0
    assert _as(operator_kubeconfig, "logs", "--namespace", cell, "runbook-probe").returncode == 0
    for denied in (
        ("get", "secrets", "--namespace", "exomem-cloud"),
        ("exec", "--namespace", cell, "runbook-probe", "--", "true"),
    ):
        result = _as(operator_kubeconfig, *denied)
        print(f"[runbook] operator {denied[0]}: exit={result.returncode} {result.stderr.strip()!r}")
        assert result.returncode != 0 and "forbidden" in result.stderr.lower(), (denied, result.stderr)

    print("[runbook] mint a one-hour break-glass identity, use it, end it, then find it in the audit log")
    session = "\n".join((
        _runbook_block("mint-break-glass"),
        'kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" auth whoami',
        f'kubectl --kubeconfig "$BREAK_GLASS_KUBECONFIG" exec --namespace {cell} runbook-probe -- true',
        # An admin impersonating the group is break-glass use too.
        f'kubectl --kubeconfig "$ADMIN_KUBECONFIG" --as=impersonation-probe --as-group=exomem:break-glass '
        f'get pods --namespace {cell} >/dev/null',
        _runbook_block("end-break-glass"),
        # No file for the identity outlives the task.
        'test -z "$(ls -A "$BREAK_GLASS_TMP")"',
        "sleep 2",
        f"docker cp {k3s.name}:{K3S_AUDIT_LOG} \"$AUDIT_LOG\"",
        _runbook_block("audit-break-glass"),
    ))
    result = _bash(session)
    lines = result.stdout.splitlines()
    # The mint's own progress, notAfter included, goes to stderr: only the
    # directory comes back on stdout.
    progress = result.stderr.splitlines()
    print("[runbook] " + "\n[runbook] ".join([*progress, *(line for line in lines if not line.startswith("{"))]))
    enddate = next(line for line in progress if line.startswith("notAfter="))
    expires = time.mktime(time.strptime(enddate.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z"))
    assert expires - time.time() <= 3600 + 120, enddate
    audit = [json.loads(line) for line in lines if line.startswith("{")]
    csr_events = [event for event in audit if event["objectRef"].get("resource") == "certificatesigningrequests"]
    assert {event["objectRef"].get("subresource") for event in csr_events} >= {None, "approval"}, csr_events
    exec_events = [
        event for event in audit
        if event["user"]["username"] == "exomem-break-glass" and event["objectRef"].get("subresource") == "exec"
    ]
    assert exec_events and exec_events[0]["objectRef"]["namespace"] == cell
    assert exec_events[0]["objectRef"]["name"] == "runbook-probe"
    assert "exomem:break-glass" in exec_events[0]["user"]["groups"]
    impersonated = [event for event in audit if "exomem:break-glass" in event.get("impersonatedUser", {}).get("groups", [])]
    assert impersonated and impersonated[0]["user"]["username"] != "exomem-break-glass", audit
    for event in audit:
        assert event["level"] == "Metadata", event
        assert "requestObject" not in event and "responseObject" not in event, event


# === move-cloud-cells-to-local-storage 3.2: local storage admission ===


def test_local_storage_admission_confines_backup_clones_and_retains_before_a_claim_delete(k3s: K3sCluster) -> None:
    from cellctl.manifests import (
        CORDON_TOLERATION,
        CellManifestSpec,
        render_backup_job,
        render_clone_claim,
        render_namespace,
        render_network_policies,
        render_pvc,
        render_resource_quota,
        render_statefulset,
        render_volume_snapshot,
    )
    from cellctl.reconcile import RETAINED_DELETE_POLICY_NAME
    from cellctl.storage_config import LocalStorage

    local = LocalStorage()
    cellctl = "--as=system:serviceaccount:exomem-cloud:cellctl"
    documents = _render_platform("templates/cellctl.yaml", "templates/cell-local-storage.yaml", settings=(
        "--set-string", f"cellctl.cellImageRepository={STANDIN_REPOSITORY}",
    ))
    # Policies, RBAC and the snapshot CRDs; nothing here needs a workload.
    _apply_server_side(k3s.name, [doc for doc in documents if doc["kind"] != "Deployment"
                                  and doc["metadata"].get("namespace") != "exomem-platform"])
    spec = CellManifestSpec(cell_id=_cell_id(), image=STANDIN_REPOSITORY + "@sha256:" + "a" * 64, replicas=0,
                            read_only=False, storage_class=local.class_name, local_volume=True,
                            hold_started_at="2026-01-01T00:00:00+00:00")
    _apply_server_side(k3s.name, [render_namespace(spec), *render_network_policies(spec)])

    def as_cellctl(*args: str, document: dict | None = None) -> subprocess.CompletedProcess[str]:
        return _kubectl(k3s.name, [*args, "--dry-run=server", cellctl],
                        documents=None if document is None else [document], check=False)

    def create(document: dict) -> subprocess.CompletedProcess[str]:
        return as_cellctl("create", "--filename=-", document=document)

    def policies_ready() -> bool:
        for name in ("exomem-cellctl-scope", RETAINED_DELETE_POLICY_NAME):
            policy = json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy", name, "-o=json"]).stdout)
            status = policy.get("status", {})
            if status.get("observedGeneration") != policy["metadata"]["generation"]:
                return False
            assert not status.get("typeChecking", {}).get("expressionWarnings"), status
        return create(render_clone_claim(spec, clone_class=local.clone_class)).returncode == 0

    _wait_for(policies_ready, timeout=60, description="local storage admission")

    for admitted in (render_pvc(spec), render_resource_quota(spec),
                     render_volume_snapshot(spec, snapshot_class=local.snapshot_class)):
        result = create(admitted)
        assert result.returncode == 0, result.stderr
    # Tasks 6.4 and 7.3: a local backup Job tolerates its node's cordon, so a
    # move's stopped backup runs on the cordoned agent the move leaves. No
    # wider toleration, and no cell pod, may.
    backup = render_backup_job(spec, bucket_name="b", endpoint="https://s3.example", retention=None)
    assert backup["spec"]["template"]["spec"]["tolerations"] == [CORDON_TOLERATION]
    result = create(backup)
    assert result.returncode == 0, result.stderr
    every_taint = copy.deepcopy(backup)
    every_taint["spec"]["template"]["spec"]["tolerations"][0].pop("key")
    serving = render_statefulset(spec)
    serving["spec"]["template"]["spec"]["tolerations"] = [dict(CORDON_TOLERATION)]
    foreign = render_clone_claim(spec, clone_class=local.clone_class)
    foreign["spec"]["dataSourceRef"]["name"] = "cell-snapshot-" + "0" * 10
    unconfigured = render_pvc(spec)
    unconfigured["spec"]["storageClassName"] = "local-path"
    for refused in (foreign, unconfigured, every_taint, serving):
        result = create(refused)
        assert result.returncode != 0 and "exomem-cellctl-scope" in result.stderr, result.stderr

    def volume(name: str, claim_namespace: str) -> dict:
        return {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": name},
                "spec": {"capacity": {"storage": "10Gi"}, "accessModes": ["ReadWriteOnce"],
                         "persistentVolumeReclaimPolicy": "Delete", "storageClassName": local.class_name,
                         "claimRef": {"namespace": claim_namespace, "name": "cell-data"},
                         "csi": {"driver": local.driver, "volumeHandle": name}}}

    cell_volume, other_volume = "local-cell-volume", "local-other-volume"
    _apply_server_side(k3s.name, [volume(cell_volume, spec.namespace), volume(other_volume, "default"),
                                  render_pvc(spec)])

    def patch(name: str, body: dict) -> subprocess.CompletedProcess[str]:
        return as_cellctl("patch", "persistentvolume", name, "--type=merge", f"--patch={json.dumps(body)}")

    retain = {"spec": {"persistentVolumeReclaimPolicy": "Retain"}}
    assert patch(cell_volume, retain).returncode == 0
    for refused in (
        patch(other_volume, retain),
        patch(cell_volume, {"spec": {**retain["spec"], "capacity": {"storage": "20Gi"}}}),
        patch(cell_volume, {**retain, "metadata": {"labels": {"exomem.io/cell": "other"}}}),
    ):
        assert refused.returncode != 0 and "exomem-cellctl-scope" in refused.stderr, refused.stderr

    def delete_claim() -> subprocess.CompletedProcess[str]:
        return as_cellctl("delete", "persistentvolumeclaim", "cell-data", f"--namespace={spec.namespace}")

    under_delete = delete_claim()
    assert under_delete.returncode != 0 and RETAINED_DELETE_POLICY_NAME in under_delete.stderr, under_delete.stderr
    _kubectl(k3s.name, ["patch", "persistentvolume", cell_volume, "--type=merge", f"--patch={json.dumps(retain)}"])
    _wait_for(lambda: delete_claim().returncode == 0, timeout=30, description="the claim delete once its volume is retained")


def test_topolvm_node_agent_writes_only_its_own_node_and_volumes(k3s: K3sCluster) -> None:
    # TopoLVM's chart lets its node agent write every Node and LogicalVolume;
    # the platform's admission confines each agent's token to its own node.
    documents = _render_platform(
        "charts/topolvm/templates/crds/topolvm.io_logicalvolumes.yaml",
        "charts/topolvm/templates/node/clusterrole.yaml",
        "charts/topolvm/templates/node/clusterrolebinding.yaml",
        "templates/cell-local-storage.yaml",
    )
    _apply_server_side(k3s.name, [doc for doc in documents if doc["kind"] in {
        "CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding",
        "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}])
    (agent,) = [subject for doc in documents if doc["kind"] == "ClusterRoleBinding"
                for subject in doc["subjects"] if subject["name"].endswith("-topolvm-node")]
    node = _kubectl(k3s.name, ["get", "nodes", "-o=jsonpath={.items[0].metadata.name}"]).stdout.strip()

    def as_agent_on(token_node: str, *args: str, document: dict | None = None) -> subprocess.CompletedProcess[str]:
        return _kubectl(k3s.name, [
            *args, "--dry-run=server", f"--as=system:serviceaccount:{agent['namespace']}:{agent['name']}",
            f"--as-user-extra=authentication.kubernetes.io/node-name={token_node}",
        ], documents=None if document is None else [document], check=False)

    def volume(node_name: str) -> dict:
        return {"apiVersion": "topolvm.io/v1", "kind": "LogicalVolume", "metadata": {"name": "cell-volume"},
                "spec": {"name": "cell-volume", "nodeName": node_name, "size": "1Gi", "deviceClass": "thin"}}

    def annotate_capacity(token_node: str) -> subprocess.CompletedProcess[str]:
        return as_agent_on(token_node, "patch", "node", node, "--type=merge",
                           '--patch={"metadata":{"annotations":{"capacity.topolvm.io/thin":"1073741824"}}}')

    def policies_ready() -> bool:
        for name in ("exomem-topolvm-node-volumes", "exomem-topolvm-node-self"):
            policy = json.loads(_kubectl(k3s.name, ["get", "validatingadmissionpolicy", name, "-o=json"]).stdout)
            status = policy.get("status", {})
            if status.get("observedGeneration") != policy["metadata"]["generation"]:
                return False
            assert not status.get("typeChecking", {}).get("expressionWarnings"), status
        refused = as_agent_on("another-node", "create", "--filename=-", document=volume(node))
        return refused.returncode != 0 and "exomem-topolvm-node-volumes" in refused.stderr

    _wait_for(policies_ready, timeout=60, description="the TopoLVM node agent policies")

    own_volume = as_agent_on(node, "create", "--filename=-", document=volume(node))
    assert own_volume.returncode == 0, own_volume.stderr
    other_volume = as_agent_on(node, "create", "--filename=-", document=volume("another-node"))
    assert other_volume.returncode != 0 and "exomem-topolvm-node-volumes" in other_volume.stderr, other_volume.stderr

    own_node = annotate_capacity(node)
    assert own_node.returncode == 0, own_node.stderr
    other_node = annotate_capacity("another-node")
    assert other_node.returncode != 0 and "exomem-topolvm-node-self" in other_node.stderr, other_node.stderr
    relabel = as_agent_on(node, "label", "node", node, "node-restriction.kubernetes.io/exomem-local-storage=true")
    assert relabel.returncode != 0 and "exomem-topolvm-node-self" in relabel.stderr, relabel.stderr
