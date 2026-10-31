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
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path

from . import images, tls
from .shell import run, wait_for

K3S_POD_CIDR = "10.42.0.0/16"
K3S_SERVICE_CIDR = "10.43.0.0/16"
S3_PORT = 443  # The double serves HTTP or HTTPS on this port, selected by serve_tls.
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
    host_port: int | None
    access_key: str
    secret_key: str
    ca_path: Path | None = None

    def endpoint(self, *, from_host: bool) -> str:
        if self.ca_path is not None:
            # The internal bridge has no published port; host and pods use its private IP.
            return f"https://{self.ip}"
        return f"http://127.0.0.1:{self.host_port}" if from_host else f"http://{self.ip}:{S3_PORT}"


@dataclass
class K3s:
    container: str
    ip: str
    # Published only by P3, which reaches the API and ingress from the host.
    api_host_port: int | None = None
    ingress_host_port: int | None = None
    kubeconfig: Path | None = None

    def kubectl(self, *args: str, input_text: str | None = None, check: bool = True):
        return run(
            ["docker", "exec", "--interactive", self.container, "kubectl", *args],
            input_text=input_text,
            check=check,
        )


@dataclass(frozen=True)
class NodeStorage:
    """A K3s node that serves cell volumes from TopoLVM: the image that adds LVM to the
    pinned K3s, and the lvm.conf that limits LVM to the node's loop device
    (drill_cluster.prepare_node_storage builds both)."""

    image: str
    lvm_conf: Path


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
    volumes: list[str] = field(default_factory=list)
    pki: tls.RehearsalPki | None = None
    sealed_network: str | None = None


def docker_labels(run_id: str, *, image: bool = False) -> list[str]:
    """Keep every disposable resource bound to its run and active agent."""
    labels = ["--label", f"io.exomem.rehearsal.run={run_id}"]
    if shutil.which("agent-docker-clean"):
        label = run(["agent-docker-clean", "--print-image-label" if image else "--print-label"]).stdout.strip()
        labels += ["--label", label]
    return labels


def data_volume(stack: Stack, suffix: str) -> str:
    """Track an owned volume before any dependent container starts."""
    name = f"exo-rehearsal-{stack.run_id}-{suffix}"
    stack.volumes.append(name)
    run(["docker", "volume", "create", *docker_labels(stack.run_id), name])
    return name


def _container_ip(name: str, network: str) -> str:
    raw = run(["docker", "inspect", name]).stdout
    return json.loads(raw)[0]["NetworkSettings"]["Networks"][network]["IPAddress"]


def _host_port(name: str, port: str) -> int:
    published = run(["docker", "port", name, port]).stdout.strip().splitlines()[0]
    return int(published.rsplit(":", 1)[1])


def create_stack(run_id: str, workdir: Path, *, tls_storage: bool = False,
                 node_storage: NodeStorage | None = None) -> Stack:
    network = f"exo-rehearsal-{run_id}"
    stack = Stack(
        run_id=run_id,
        workdir=workdir,
        network=network,
        subnet="",
        k3s=None,  # type: ignore[arg-type]
        postgres=None,  # type: ignore[arg-type]
        object_store=None,  # type: ignore[arg-type]
    )
    try:
        run(["docker", "network", "create", *docker_labels(run_id), network])
        stack.subnet = json.loads(run(["docker", "network", "inspect", network]).stdout)[0]["IPAM"]["Config"][0]["Subnet"]
        stack.postgres = _start_postgres(stack)
        if tls_storage:
            stack.sealed_network = f"{network}-sealed"
            run(["docker", "network", "create", *docker_labels(run_id), "--internal", stack.sealed_network])
            stack.networks.append(stack.sealed_network)
        stack.object_store = _start_object_store(stack)
        stack.k3s = _start_k3s(stack, node_storage)
        if stack.sealed_network:
            run(["docker", "network", "connect", stack.sealed_network, stack.k3s.container])
    except BaseException:
        # The caller cannot retain a Stack that setup did not return.
        teardown(stack, keep=False)
        raise
    return stack


def _start_postgres(stack: Stack) -> Postgres:
    name = f"exo-rehearsal-pg-{stack.run_id}"
    superuser_password = secrets.token_urlsafe(24)
    volume = data_volume(stack, "postgres")
    stack.containers.append(name)
    run(
        [
            "docker", "run", "--detach", *docker_labels(stack.run_id), "--name", name, "--network", stack.network,
            "--publish", "127.0.0.1::5432",
            "--env", "POSTGRES_PASSWORD_FILE=/run/pg-password",
            "--env", f"POSTGRES_DB={images.PG_DATABASE_BOOT}",
            "--mount", f"type=volume,source={volume},target=/var/lib/postgresql/data",
            "--mount", f"type=bind,source={_write_secret(stack, 'pg-password', superuser_password)},target=/run/pg-password,readonly",
            images.POSTGRES, "postgres",
            "-c", "max_connections=300",
        ]
    )
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


def _write_storage_certificate(certificates: Path, leaf: tls.LeafCertificate) -> None:
    (certificates / "cert.pem").write_text(leaf.cert_pem, encoding="utf-8")
    key = certificates / "key.pem"
    key.write_text(leaf.key_pem, encoding="utf-8")
    key.chmod(0o600)


def _wait_storage_tls(address: str, ca_path: Path, *, server_hostname: str) -> None:
    context = ssl.create_default_context(cafile=str(ca_path))
    context.verify_flags |= ssl.VERIFY_X509_STRICT

    def probe() -> bool:
        with socket.create_connection((address, S3_PORT), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname=server_hostname):
                return True

    wait_for(probe, timeout=30, description=f"the S3 double to serve TLS for {server_hostname}")


def _start_object_store(stack: Stack, *, serve_tls: bool | None = None) -> ObjectStore:
    # A sealed network implies TLS; the drill asks for TLS on its own routed network.
    serve_tls = bool(stack.sealed_network) if serve_tls is None else serve_tls
    name = f"exo-rehearsal-s3-{stack.run_id}"
    access_key = f"rehearsal{secrets.token_hex(4)}"
    secret_key = secrets.token_urlsafe(24)
    volume = data_volume(stack, "storage")
    data_mount = f"type=volume,source={volume},target=/data"
    network = stack.sealed_network or stack.network
    options: list[str] = ["--publish", f"127.0.0.1::{S3_PORT}"]
    arguments = ["--port", f":{S3_PORT}", "posix", "/data"]
    bootstrap = None
    if serve_tls:
        certificates = stack.workdir / "storage-tls"
        certificates.mkdir(mode=0o700)
        bootstrap = tls.make_pki(certificates / "bootstrap", ("127.0.0.1",))
        _write_storage_certificate(certificates, bootstrap.leaves["127.0.0.1"])
        options = ["--user", f"{os.getuid()}:{os.getgid()}",
                   "--mount", f"type=bind,source={certificates},target=/certificates,readonly"]
        arguments = ["--port", f":{S3_PORT}", "--cert", "/certificates/cert.pem", "--key", "/certificates/key.pem",
                     "posix", "--versioning-dir", "/data/versions", "/data/buckets"]
    # The image runs as this user for TLS; initialize writable, owned volume paths.
    bucket = f"/data/buckets/{BACKUP_BUCKET}" if serve_tls else f"/data/{BACKUP_BUCKET}"
    run(["docker", "run", "--rm", *docker_labels(stack.run_id), "--mount", data_mount,
         images.BUSYBOX, "sh", "-ec", 'mkdir -p /data/versions "$1"; chmod -R 777 /data', "_", bucket])
    stack.containers.append(name)
    run(
        [
            "docker", "run", "--detach", *docker_labels(stack.run_id), "--name", name, "--network", network, *options,
            "--env", f"ROOT_ACCESS_KEY={access_key}",
            "--env", f"ROOT_SECRET_KEY={secret_key}",
            "--mount", data_mount,
            images.S3_DOUBLE, *arguments,
        ]
    )
    address = _container_ip(name, network)
    if bootstrap is not None:
        # A completed handshake means Versity's listener and HUP handler are ready.
        _wait_storage_tls(address, bootstrap.ca_path, server_hostname="127.0.0.1")
        pki = tls.make_pki(stack.workdir, (tls.SUBSTRATE_HOST, tls.MCP_HOST, address, "127.0.0.1"))
        _write_storage_certificate(certificates, pki.leaves[address])
        # Pinned Versity reloads the complete keypair on HUP, without a restart.
        run(["docker", "kill", "--signal", "HUP", name])
        _wait_storage_tls(address, pki.ca_path, server_hostname=address)
        stack.pki = pki
    else:
        wait_for(
            lambda: run(["docker", "exec", name, "true"], check=False).returncode == 0,
            timeout=30, description="the S3 double to start",
        )
    return ObjectStore(
        container=name,
        ip=address,
        host_port=None if serve_tls else _host_port(name, f"{S3_PORT}/tcp"),
        access_key=access_key,
        secret_key=secret_key,
        ca_path=stack.pki.ca_path if stack.pki else None,
    )


SERVER_ARGS = (
    "--disable=traefik",
    "--disable=servicelb",
    "--disable=metrics-server",
    "--write-kubeconfig-mode=600",
    f"--cluster-cidr={K3S_POD_CIDR}",
    f"--service-cidr={K3S_SERVICE_CIDR}",
    # Secrets are encrypted at rest on the real node (design context).
    "--secrets-encryption",
)


def node_args(stack: Stack) -> list[str]:
    """The kubelet and kube-proxy flags every K3s node here runs with, server
    or agent. Each host adaptation is recorded once per stack."""

    args = [
        # K3s's kube-proxy sets nf_conntrack_max, which an unprivileged
        # network namespace may not write; 0 leaves the host's value.
        "--kube-proxy-arg=conntrack-max-per-core=0",
        "--kubelet-arg=oom-score-adj=0",
        "--kube-proxy-arg=oom-score-adj=0",
        # Every image here is imported, none can be re-pulled: image GC on a
        # busy shared disk would delete them, and disk-pressure eviction
        # would evict cells for the host's usage rather than the node's.
        "--kubelet-arg=image-gc-high-threshold=100",
        "--kubelet-arg=image-gc-low-threshold=99",
        "--kubelet-arg=eviction-hard=imagefs.available<1%,nodefs.available<1%",
    ]
    adaptations = [
        "kubelet image GC off and disk eviction at 1%: imported images cannot be re-pulled, and the "
        "node shares the host's disk",
    ]
    if cgroup_v1_host():
        args.append("--kubelet-arg=fail-cgroupv1=false")
        adaptations.append("kubelet fail-cgroupv1=false: this host runs cgroup v1")
    stack.adaptations.extend(item for item in adaptations if item not in stack.adaptations)
    return args


def containerd_mount(stack: Stack) -> str:
    """A `docker run --mount` value adding the rehearsal's containerd drop-in."""

    containerd_dir = stack.workdir / "containerd.d"
    containerd_dir.mkdir(parents=True, exist_ok=True)
    (containerd_dir / "rehearsal.toml").write_text(
        "[plugins.'io.containerd.cri.v1.runtime']\n  restrict_oom_score_adj = true\n", encoding="utf-8"
    )
    adaptation = (
        "containerd restrict_oom_score_adj=true: pods never get a lower OOM score than the node "
        "(needed where the host withholds CAP_SYS_RESOURCE; affects OOM-kill order only)"
    )
    if adaptation not in stack.adaptations:
        stack.adaptations.append(adaptation)
    return (
        f"type=bind,source={containerd_dir / 'rehearsal.toml'},"
        "target=/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/rehearsal.toml,readonly"
    )


def image_data_mounts(stack: Stack, image: str, prefix: str, *, temporary: bool = False) -> list[str]:
    """Keep image-declared data on owned volumes or disposable helper tmpfs."""
    ensure_image(image)
    declared = json.loads(run(["docker", "image", "inspect", "--format", "{{json .Config.Volumes}}", image]).stdout)
    mounts = []
    for index, path in enumerate(sorted(declared or {})):
        if temporary:
            mounts += ["--tmpfs", path]
        else:
            volume = data_volume(stack, f"{prefix}-{index}")
            mounts += ["--mount", f"type=volume,source={volume},target={path}"]
    return mounts


def _start_k3s(stack: Stack, node_storage: NodeStorage | None = None) -> K3s:
    name = f"exo-rehearsal-k3s-{stack.run_id}"
    mount = containerd_mount(stack)
    args = ["server", *SERVER_ARGS, *node_args(stack)]
    image = node_storage.image if node_storage else images.K3S
    mounts = image_data_mounts(stack, image, "node")
    stack.containers.append(name)
    if node_storage:
        # As the drill's agents run: LVM sees only the node's disk, and TopoLVM's node
        # plugin mounts volumes with Bidirectional propagation, which needs / shared.
        mounts += ["--mount", f"type=bind,source={node_storage.lvm_conf},target=/etc/lvm/lvm.conf,readonly"]
        entry = ["--entrypoint", "/bin/sh", image, "-c", 'mount --make-rshared / && exec /bin/k3s "$@"', "k3s-server"]
        stack.adaptations.append(
            "the K3s node runs the pinned image plus LVM with `mount --make-rshared /`, and serves cell volumes "
            "from a loop-device thin pool through TopoLVM (the drill's agent adaptation, on the server)"
        )
    else:
        entry = [image]
    run(
        [
            "docker", "run", "--privileged", "--detach", *docker_labels(stack.run_id), "--name", name,
            "--network", stack.network,
            "--publish", "127.0.0.1::6443",
            "--publish", "127.0.0.1::30443",
            *mounts, "--mount", mount,
            *entry, *args,
        ]
    )
    wait_api_server(name)
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


def wait_api_server(container: str, *, timeout: float = 180) -> None:
    wait_for(
        lambda: run(["docker", "exec", container, "kubectl", "get", "--raw=/readyz"], check=False).stdout.strip() == "ok",
        timeout=timeout, interval=2, description="the K3s API server",
    )


def ensure_image(reference: str) -> None:
    """Retain docker run's missing-image pull before reading image metadata."""
    if run(["docker", "image", "inspect", reference], check=False).returncode != 0:
        run(["docker", "pull", "--quiet", reference], timeout=900)


def import_images(k3s_container: str, references: list[str]) -> None:
    """Loads host Docker images into K3s's containerd (namespace k8s.io)."""

    for reference in references:
        ensure_image(reference)
    save = run(["bash", "-c", "set -o pipefail; docker save \"$@\" | docker exec --interactive " + k3s_container + " ctr --namespace k8s.io images import -", "_", *references], timeout=1800)
    del save


def teardown(stack: Stack | None, *, keep: bool) -> None:
    if stack is None or keep:
        return
    for name in reversed(stack.containers):
        try:
            logs = run(["docker", "logs", "--tail", "400", name], check=False, timeout=15)
            (stack.workdir / f"{name}.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        except Exception:  # noqa: BLE001 - diagnostics cannot prevent cleanup
            pass
    selector = f"label=io.exomem.rehearsal.run={stack.run_id}"

    def owned(kind: str) -> list[str]:
        # Docker's container listing excludes stopped containers unless --all is set.
        return run(["docker", kind, "ls", *(["--all"] if kind == "container" else []),
                    "--quiet", "--filter", selector]).stdout.split()

    for kind in ("container", "network", "volume"):
        # Include a transient helper that exited before its caller could retain its name.
        listed = owned(kind)
        if listed:
            command = ["docker", kind, "rm", *(["--force", "--volumes"] if kind == "container" else []), *listed]
            try:
                run(command, check=False, timeout=60)
            except Exception:  # noqa: BLE001 - inspect actual absence after all removals
                pass
    for kind in ("container", "network", "volume"):
        remaining = owned(kind)
        if remaining:
            raise RuntimeError(f"owned {kind} resources remain after rehearsal cleanup")


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
