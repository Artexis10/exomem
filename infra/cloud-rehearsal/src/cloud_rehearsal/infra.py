"""Disposable infrastructure: one Docker network holding K3s, PostgreSQL and
an S3-compatible object store.

Every container is named with the run id and removed on teardown. Every
image is pinned by digest (images.py). K3s runs privileged in Docker, as the
cellctl 3.10 suite does, with three host adaptations that are detected, not
assumed, and each recorded in the report:

- ``fail-cgroupv1=false`` only on a cgroup v1 host (Kubernetes 1.35's kubelet
  refuses cgroup v1 by default);
- ``restrict_oom_score_adj`` in containerd's CRI config, which clamps a pod's
  OOM score adjustment at the node's own instead of lowering it. It is needed
  where the host withholds CAP_SYS_RESOURCE from containers and changes only
  OOM-kill ordering between pods on this one disposable node;
- the K3s system images are imported from the host's Docker store rather
  than pulled by K3s's containerd, so the cluster never needs registry egress
  of its own.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import images
from .shell import run, wait_for

K3S_POD_CIDR = "10.42.0.0/16"
K3S_SERVICE_CIDR = "10.43.0.0/16"
S3_PORT = 443  # restic reaches the double over plain HTTP on 443, as the cellctl 3.10 suite does
BACKUP_BUCKET = "exomem-cloud-backups"
PG_ROLES = ("substrate_owner", "substrate_app", "exomem_gateway", "exomem_cellctl")
PG_DATABASE = "substrate"


def cgroup_v1_host() -> bool:
    return not Path("/sys/fs/cgroup/cgroup.controllers").exists()


@dataclass
class Postgres:
    container: str
    ip: str
    host_port: int
    passwords: dict[str, str]

    def dsn(self, role: str, *, from_host: bool) -> str:
        host, port = ("127.0.0.1", self.host_port) if from_host else (self.ip, 5432)
        return f"postgresql://{role}:{self.passwords[role]}@{host}:{port}/{PG_DATABASE}"


@dataclass
class ObjectStore:
    container: str
    ip: str
    host_port: int
    access_key: str
    secret_key: str

    def endpoint(self, *, from_host: bool) -> str:
        return f"http://127.0.0.1:{self.host_port}" if from_host else f"http://{self.ip}:{S3_PORT}"


@dataclass
class K3s:
    container: str
    ip: str
    api_host_port: int
    ingress_host_port: int
    kubeconfig: Path

    def kubectl(self, *args: str, input_text: str | None = None, check: bool = True):
        return run(
            ["docker", "exec", "--interactive", self.container, "kubectl", *args],
            input_text=input_text,
            check=check,
        )


@dataclass
class Stack:
    run_id: str
    workdir: Path
    network: str
    subnet: str
    k3s: K3s
    postgres: Postgres
    object_store: ObjectStore
    adaptations: list[str] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)


def _container_ip(name: str, network: str) -> str:
    raw = run(["docker", "inspect", name]).stdout
    return json.loads(raw)[0]["NetworkSettings"]["Networks"][network]["IPAddress"]


def _host_port(name: str, port: str) -> int:
    published = run(["docker", "port", name, port]).stdout.strip().splitlines()[0]
    return int(published.rsplit(":", 1)[1])


def create_stack(run_id: str, workdir: Path) -> Stack:
    network = f"exo-rehearsal-{run_id}"
    run(["docker", "network", "create", network])
    subnet = json.loads(run(["docker", "network", "inspect", network]).stdout)[0]["IPAM"]["Config"][0]["Subnet"]
    stack = Stack(
        run_id=run_id,
        workdir=workdir,
        network=network,
        subnet=subnet,
        k3s=None,  # type: ignore[arg-type]
        postgres=None,  # type: ignore[arg-type]
        object_store=None,  # type: ignore[arg-type]
    )
    stack.postgres = _start_postgres(stack)
    stack.object_store = _start_object_store(stack)
    stack.k3s = _start_k3s(stack)
    return stack


def _start_postgres(stack: Stack) -> Postgres:
    name = f"exo-rehearsal-pg-{stack.run_id}"
    superuser_password = secrets.token_urlsafe(24)
    run(
        [
            "docker", "run", "--detach", "--name", name, "--network", stack.network,
            "--publish", "127.0.0.1::5432",
            "--env", "POSTGRES_PASSWORD_FILE=/run/pg-password",
            "--env", f"POSTGRES_DB={images.PG_DATABASE_BOOT}",
            "--mount", f"type=bind,source={_write_secret(stack, 'pg-password', superuser_password)},target=/run/pg-password,readonly",
            images.POSTGRES, "postgres",
            "-c", "max_connections=300",
        ]
    )
    stack.containers.append(name)
    wait_for(
        lambda: run(["docker", "exec", name, "pg_isready", "-U", "postgres"], check=False).returncode == 0,
        timeout=90, description="PostgreSQL to accept connections",
    )
    passwords = {role: secrets.token_urlsafe(24) for role in PG_ROLES}
    passwords["postgres"] = superuser_password
    return Postgres(container=name, ip=_container_ip(name, stack.network), host_port=_host_port(name, "5432/tcp"), passwords=passwords)


def _write_secret(stack: Stack, filename: str, value: str) -> Path:
    path = stack.workdir / "secrets" / filename
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value, encoding="utf-8")
    # Read by the container's own non-root users (postgres, versitygw).
    path.chmod(0o644)
    return path


def _start_object_store(stack: Stack) -> ObjectStore:
    name = f"exo-rehearsal-s3-{stack.run_id}"
    access_key = f"rehearsal{secrets.token_hex(4)}"
    secret_key = secrets.token_urlsafe(24)
    data = stack.workdir / "s3-data"
    data.mkdir(parents=True, exist_ok=True)
    (data / BACKUP_BUCKET).mkdir(exist_ok=True)
    data.chmod(0o777)
    (data / BACKUP_BUCKET).chmod(0o777)
    run(
        [
            "docker", "run", "--detach", "--name", name, "--network", stack.network,
            "--publish", f"127.0.0.1::{S3_PORT}",
            "--env", f"ROOT_ACCESS_KEY={access_key}",
            "--env", f"ROOT_SECRET_KEY={secret_key}",
            "--mount", f"type=bind,source={data},target=/data",
            images.S3_DOUBLE, "--port", f":{S3_PORT}", "posix", "/data",
        ]
    )
    stack.containers.append(name)
    store = ObjectStore(
        container=name,
        ip=_container_ip(name, stack.network),
        host_port=_host_port(name, f"{S3_PORT}/tcp"),
        access_key=access_key,
        secret_key=secret_key,
    )
    wait_for(
        lambda: run(["docker", "exec", name, "true"], check=False).returncode == 0,
        timeout=30, description="the S3 double to start",
    )
    return store


def _start_k3s(stack: Stack) -> K3s:
    name = f"exo-rehearsal-k3s-{stack.run_id}"
    containerd_dir = stack.workdir / "containerd.d"
    containerd_dir.mkdir(parents=True, exist_ok=True)
    (containerd_dir / "rehearsal.toml").write_text(
        "[plugins.'io.containerd.cri.v1.runtime']\n  restrict_oom_score_adj = true\n", encoding="utf-8"
    )
    stack.adaptations.append(
        "containerd restrict_oom_score_adj=true: pods never get a lower OOM score than the node "
        "(needed where the host withholds CAP_SYS_RESOURCE; affects OOM-kill order only)"
    )
    args = [
        "server",
        "--disable=traefik",
        "--disable=servicelb",
        "--disable=metrics-server",
        "--write-kubeconfig-mode=600",
        f"--cluster-cidr={K3S_POD_CIDR}",
        f"--service-cidr={K3S_SERVICE_CIDR}",
        # K3s's kube-proxy sets nf_conntrack_max, which an unprivileged
        # network namespace may not write; 0 leaves the host's value.
        "--kube-proxy-arg=conntrack-max-per-core=0",
        "--kubelet-arg=oom-score-adj=0",
        "--kube-proxy-arg=oom-score-adj=0",
        # Secrets are encrypted at rest on the real node (design context).
        "--secrets-encryption",
        # Every image here is imported, none can be re-pulled: image GC on a
        # busy shared disk would delete them, and disk-pressure eviction
        # would evict cells for the host's usage rather than the node's.
        "--kubelet-arg=image-gc-high-threshold=100",
        "--kubelet-arg=image-gc-low-threshold=99",
        "--kubelet-arg=eviction-hard=imagefs.available<1%,nodefs.available<1%",
    ]
    stack.adaptations.append(
        "kubelet image GC off and disk eviction at 1%: imported images cannot be re-pulled, and the "
        "node shares the host's disk"
    )
    if cgroup_v1_host():
        args.append("--kubelet-arg=fail-cgroupv1=false")
        stack.adaptations.append("kubelet fail-cgroupv1=false: this host runs cgroup v1")
    run(
        [
            "docker", "run", "--privileged", "--detach", "--name", name,
            "--network", stack.network,
            "--publish", "127.0.0.1::6443",
            "--publish", "127.0.0.1::30443",
            "--mount", f"type=bind,source={containerd_dir / 'rehearsal.toml'},target=/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/rehearsal.toml,readonly",
            images.K3S, *args,
        ]
    )
    stack.containers.append(name)
    wait_for(
        lambda: run(["docker", "exec", name, "kubectl", "get", "--raw=/readyz"], check=False).stdout.strip() == "ok",
        timeout=180, interval=2, description="the K3s API server",
    )
    import_images(name, list(images.K3S_SYSTEM_IMAGES))
    stack.adaptations.append("K3s system images imported from the host Docker store (no registry egress from K3s)")
    wait_for(
        lambda: '"True"' in run(
            ["docker", "exec", name, "kubectl", "get", "nodes", "-o", "jsonpath={.items[0].status.conditions[?(@.type==\"Ready\")].status}"],
            check=False,
        ).stdout.replace("True", '"True"'),
        timeout=180, interval=2, description="the K3s node to be Ready",
    )
    wait_for(
        lambda: "1/1" in run(["docker", "exec", name, "kubectl", "get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns", "--no-headers"], check=False).stdout,
        timeout=240, interval=3, description="CoreDNS",
    )
    api_port = _host_port(name, "6443/tcp")
    raw = run(["docker", "exec", name, "cat", "/etc/rancher/k3s/k3s.yaml"]).stdout
    kubeconfig = stack.workdir / "kubeconfig.yaml"
    kubeconfig.write_text(raw.replace("https://127.0.0.1:6443", f"https://127.0.0.1:{api_port}"), encoding="utf-8")
    kubeconfig.chmod(0o600)
    return K3s(
        container=name,
        ip=_container_ip(name, stack.network),
        api_host_port=api_port,
        ingress_host_port=_host_port(name, "30443/tcp"),
        kubeconfig=kubeconfig,
    )


def import_images(k3s_container: str, references: list[str]) -> None:
    """Loads host Docker images into K3s's containerd (namespace k8s.io)."""

    for reference in references:
        if run(["docker", "image", "inspect", reference], check=False).returncode != 0:
            run(["docker", "pull", "--quiet", reference], timeout=900)
    save = run(["bash", "-c", "set -o pipefail; docker save \"$@\" | docker exec --interactive " + k3s_container + " ctr --namespace k8s.io images import -", "_", *references], timeout=1800)
    del save


def teardown(stack: Stack | None, *, keep: bool) -> None:
    if stack is None or keep:
        return
    for name in reversed(stack.containers):
        logs = run(["docker", "logs", "--tail", "400", name], check=False)
        (stack.workdir / f"{name}.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        run(["docker", "rm", "--force", "--volumes", name], check=False)
    for network in [*stack.networks, stack.network]:
        run(["docker", "network", "rm", network], check=False)
    s3_data = stack.workdir / "s3-data"
    if s3_data.exists():
        # versitygw writes as root inside its container.
        run(["docker", "run", "--rm", "--mount", f"type=bind,source={stack.workdir},target=/w", images.BUSYBOX, "rm", "-rf", "/w/s3-data"], check=False)
    if s3_data.exists():
        shutil.rmtree(s3_data, ignore_errors=True)


def environment_facts() -> dict[str, object]:
    docker = json.loads(run(["docker", "info", "--format", "{{json .}}"]).stdout)
    return {
        "docker_server_version": docker.get("ServerVersion"),
        "cgroup_version": docker.get("CgroupVersion"),
        "cpus": docker.get("NCPU"),
        "memory_bytes": docker.get("MemTotal"),
        "kernel": docker.get("KernelVersion"),
        "ci": bool(os.environ.get("CI")),
    }
