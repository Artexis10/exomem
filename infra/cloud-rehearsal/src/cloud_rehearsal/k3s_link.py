"""`exomem-cloud-rehearsal k3s-link`: the private-link rehearsal of
move-cloud-cells-to-local-storage tasks 6.5, 5.7 and 5.8, on a K3s server and
one agent with no cell images. It takes about ten minutes and needs no sudo.

What runs:
- the pinned K3s image as a server with embedded etcd and as one agent. Each
  is a privileged container on a Docker network at the Hetzner Cloud network's
  1450-byte MTU, and the agent lowers its own interface to a vSwitch's 1400;
- the k3s role's flannel configuration on both nodes, rendered by the pinned
  ansible-core from the role's own template and defaults;
- the k3s role's bootstrap re-save helper on the server;
- the k3s role's client-CA rotation helper, against the server.

The checks are listed in infra/cloud-rehearsal/README.md. Each names the
failure it exists to catch, and every value in the report says where it was
read. A failed 6.5 check does not block the 5.7 checks, which use the cluster
but not its MTU. A failed 5.7 check blocks the 5.8 checks, which rotate the
client CA on the agent token 5.7 leaves, as the runbook requires.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import shutil
import ssl
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import images, infra
from .drill import DrillReport
from .drill_cluster import node_ready
from .report import (
    FAILED,
    PASSED,
    StageFailed,
    StepFailure,
    StepRecord,
    failure_record,
    guarded,
    stage,
)
from .shell import run, wait_for

SCHEMA = "exomem-k3s-link-rehearsal-report-v1"
ROLE = images.REPO_ROOT / "infra/ansible/roles/k3s"
TOOL_VERSIONS = images.REPO_ROOT / "infra/tool-versions.env"
SUBNET = "172.29.84.0/24"
# Docker assigns the other containers' addresses from here, never a node's.
DYNAMIC_RANGE = "172.29.84.128/25"
SERVER_IP, AGENT_IP = "172.29.84.10", "172.29.84.11"
SERVER_NAME, AGENT_NAME = "link-server", "link-agent"
# Design D8: the Hetzner Cloud network carries 1450-byte packets and a vSwitch
# 1400. The Docker network models the first; the agent's interface the second.
CLOUD_MTU, VSWITCH_MTU = 1450, 1400
# ping -s sets the ICMP payload; the packet adds 20 bytes of IPv4 and 8 of ICMP.
IP_ICMP_HEADERS = 28
K3S_DATA = "/var/lib/rancher/k3s"
RESAVE_SOURCE = ROLE / "files/exomem-k3s-resave-bootstrap.sh"
RESAVE = "/usr/local/sbin/exomem-k3s-resave-bootstrap"
ROTATE_CLIENT_CA_SOURCE = ROLE / "files/exomem-k3s-rotate-client-ca.sh"
ROTATE_CLIENT_CA = "/usr/local/sbin/exomem-k3s-rotate-client-ca"
# A check of one task also stops when a check of the task it builds on failed.
BUILDS_ON = {"5.8": "5.7"}
# K3s's fatal message when a file on disk is newer than its bootstrap copy in
# etcd (ReconcileBootstrapData in pkg/cluster/bootstrap.go, pinned version).
NEWER_THAN_DATASTORE = "newer than datastore"
# Busybox ping cannot forbid fragmentation; iputils can (-M do). openssl runs
# the k3s role's client-CA helper, which the K3s image cannot run.
TOOLS_DOCKERFILE = "FROM {alpine}\nRUN apk add --no-cache iputils-ping openssl\n"
ADAPTATIONS = [
    "both nodes run in privileged containers on one runner; the agent's eth0 is lowered to 1400 bytes to "
    "model a vSwitch, and the Docker network's 1450 models the Hetzner Cloud network",
    "a K3s restart is a container restart, which also recreates the node's network namespace and pods",
    "the server takes its token and agent token from a bind-mounted config.yaml, which the checks rewrite",
    "the K3s image has no openssl, so the client-CA helper runs in the tools image, in the server's network "
    "namespace, on the server's data volume and with the server's own k3s binary",
    "the lost disk is a copy of the agent's kubelet client certificate, key and node password, taken while it runs",
]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Node:
    name: str
    ip: str
    container: str
    config: Path
    # Docker volume name -> the path it is mounted on.
    volumes: dict[str, str]

    @property
    def k3s(self) -> infra.K3s:
        return infra.K3s(container=self.container, ip=self.ip)

    def volume_at(self, path: str) -> str:
        return next(name for name, mounted in self.volumes.items() if mounted == path)


@dataclass
class Link:
    report: DrillReport
    stack: infra.Stack
    labels: list[str]
    # The role's values as ansible-core evaluated them, and its rendered flannel configuration.
    role: dict[str, Any] = field(default_factory=dict)
    conf: Path | None = None
    tools_image: str = ""
    server: Node | None = None
    agent: Node | None = None
    server_token: str = ""
    agent_tokens: dict[str, str] = field(default_factory=dict)
    ca_hash: str = ""
    volumes: list[str] = field(default_factory=list)
    # The copy of the agent's kubelet credentials that stands for a lost disk.
    lost_disk: Path | None = None


def tool_version(name: str) -> str:
    for line in TOOL_VERSIONS.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key == name:
            return value.strip()
    raise RuntimeError(f"{name} is not pinned in {TOOL_VERSIONS}")


def render_role(workdir: Path) -> tuple[dict[str, Any], Path]:
    """The k3s role's flannel configuration and the values the checks expect,
    rendered by the pinned ansible-core from the role's template and defaults,
    so that no MTU or CIDR in this module is a copy of the role's.

    The rehearsal uses the role's k3s_flannel_mtu once it is set. While it is
    unset, before the rollout commit, it uses the smallest dedicated link the
    role declares, which is the value that commit sets."""

    conf, values, play = workdir / "flannel-net-conf.json", workdir / "role-values.json", workdir / "render-role.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "vars": {"ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "tasks": [
            {"ansible.builtin.include_vars": {"file": str(ROLE / "defaults/main.yml")}},
            {"ansible.builtin.set_fact": {"k3s_role_default_flannel_mtu": "{{ k3s_flannel_mtu }}"}},
            {"ansible.builtin.set_fact": {"k3s_flannel_mtu": (
                "{{ k3s_role_default_flannel_mtu if k3s_role_default_flannel_mtu is not none"
                " else [k3s_vswitch_mtu, k3s_wireguard_mtu] | min }}")}},
            {"ansible.builtin.template": {"src": str(ROLE / "templates/flannel-net-conf.json.j2"),
                                          "dest": str(conf), "mode": "0644"}},
            {"ansible.builtin.copy": {"dest": str(values), "mode": "0644", "content": (
                "{{ {'k3s_version': k3s_version, 'cluster_cidr': k3s_cluster_cidr,"
                " 'role_default_flannel_mtu': k3s_role_default_flannel_mtu,"
                " 'flannel_mtu': k3s_flannel_mtu | int, 'pod_mtu': k3s_flannel_pod_mtu | int,"
                " 'conf_path': k3s_flannel_conf_path} | to_json }}")}},
        ],
    }]), encoding="utf-8")
    run(["uvx", "--from", f"ansible-core=={tool_version('ANSIBLE_CORE_VERSION')}", "ansible-playbook",
         "-i", "localhost,", "-c", "local", str(play)], cwd=workdir, timeout=600)
    return json.loads(values.read_text(encoding="utf-8")), conf


def image_k3s_version(labels: list[str]) -> str:
    """The version the pinned K3s image's binary reports, e.g. v1.35.6+k3s1."""

    printed = run(["docker", "run", "--rm", *labels, "--entrypoint", "/bin/k3s", images.K3S, "--version"]).stdout
    # "k3s version v1.35.6+k3s1 (87243446)"
    return printed.split()[2]


def require_role_k3s_version(image_version: str, role_version: str) -> None:
    """The image is pinned by digest in the K3s gate contract and the role pins
    its binary by version: a rehearsal on another K3s proves nothing about the
    reconcile and token behaviour of the one the nodes run."""

    if image_version != role_version:
        raise RuntimeError(f"the rehearsal's K3s image reports {image_version}, but the k3s role pins {role_version}")


def build_tools_image(run_id: str, workdir: Path, image_labels: list[str]) -> str:
    context = workdir / "tools-image"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(TOOLS_DOCKERFILE.format(alpine=images.ALPINE), encoding="utf-8")
    tag = f"rehearsal.local/k3s-link-tools:{run_id}"
    run(["docker", "build", *image_labels, "--tag", tag, str(context)], timeout=900)
    return tag


def create_network(link: Link) -> None:
    network = f"exo-link-{link.stack.run_id}"
    run(["docker", "network", "create", *link.labels, "--subnet", SUBNET, "--ip-range", DYNAMIC_RANGE,
         "--opt", f"com.docker.network.driver.mtu={CLOUD_MTU}", network])
    link.stack.network = network


def node_volumes(link: Link, node: str) -> dict[str, str]:
    """A labelled volume on each path the K3s image declares a VOLUME, so Docker
    makes no anonymous volume, and a stopped node's data stays reachable."""

    declared = json.loads(run(["docker", "image", "inspect", "--format", "{{json .Config.Volumes}}", images.K3S]).stdout)
    volumes: dict[str, str] = {}
    for path in sorted(declared or {}):
        name = f"exo-link-{link.stack.run_id}-{node}-{path.strip('/').replace('/', '-')}"
        run(["docker", "volume", "create", *link.labels, name])
        link.volumes.append(name)
        volumes[name] = path
    return volumes


def node_mounts(link: Link, node: Node) -> list[str]:
    assert link.conf is not None
    mounts = ["--mount", infra.containerd_mount(link.stack)]
    for name, path in node.volumes.items():
        mounts += ["--mount", f"type=volume,src={name},dst={path}"]
    return mounts + [
        "--mount", f"type=bind,source={node.config},target=/etc/rancher/k3s/config.yaml,readonly",
        "--mount", f"type=bind,source={link.conf},target={link.role['conf_path']},readonly",
    ]


def flannel_args(link: Link) -> list[str]:
    return ["--flannel-iface=eth0", f"--flannel-conf={link.role['conf_path']}"]


def write_config(path: Path, settings: dict[str, str]) -> None:
    # In place: a running container holds this inode through its bind mount.
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    path.chmod(0o600)


def write_server_config(link: Link, agent_token: str) -> None:
    assert link.server is not None
    write_config(link.server.config, {"token": link.server_token, "agent-token": link.agent_tokens[agent_token]})


def write_agent_config(link: Link, agent_token: str) -> None:
    assert link.agent is not None
    # The CA-pinned secure format the k3s role composes (tasks/agent.yml).
    token = f"K10{link.ca_hash}::node:{link.agent_tokens[agent_token]}"
    write_config(link.agent.config, {"server": f"https://{SERVER_IP}:6443", "token": token})


def start_server(link: Link) -> None:
    stack = link.stack
    container = f"exo-link-server-{stack.run_id}"
    link.server = Node(SERVER_NAME, SERVER_IP, container, stack.workdir / "server-config.yaml", node_volumes(link, "server"))
    link.server_token = secrets.token_hex(24)
    link.agent_tokens = {"A": secrets.token_hex(24), "B": secrets.token_hex(24)}
    write_server_config(link, "A")
    run([
        "docker", "run", "--privileged", "--detach", "--name", container, "--hostname", SERVER_NAME, *link.labels,
        "--network", stack.network, "--ip", SERVER_IP, *node_mounts(link, link.server),
        "--mount", f"type=bind,source={RESAVE_SOURCE},target={RESAVE},readonly",
        images.K3S, "server", "--cluster-init", f"--node-name={SERVER_NAME}", f"--tls-san={SERVER_IP}",
        "--disable=traefik", "--disable=servicelb", "--disable=metrics-server", "--disable=local-storage",
        "--write-kubeconfig-mode=600", f"--cluster-cidr={link.role['cluster_cidr']}",
        *flannel_args(link), *infra.node_args(stack),
    ])
    stack.containers.append(container)
    stack.k3s = link.server.k3s
    infra.wait_api_server(container, timeout=300)
    infra.import_images(container, [*images.K3S_SYSTEM_IMAGES, link.tools_image])
    wait_ready(link, link.server)
    ca = run(["docker", "exec", container, "sha256sum", f"{K3S_DATA}/server/tls/server-ca.crt"]).stdout
    link.ca_hash = ca.split()[0]


def start_agent(link: Link) -> None:
    stack = link.stack
    container = f"exo-link-agent-{stack.run_id}"
    link.agent = Node(AGENT_NAME, AGENT_IP, container, stack.workdir / "agent-config.yaml", node_volumes(link, "agent"))
    write_agent_config(link, "A")
    run([
        "docker", "run", "--privileged", "--detach", "--name", container, "--hostname", AGENT_NAME, *link.labels,
        "--network", stack.network, "--ip", AGENT_IP, *node_mounts(link, link.agent),
        # Every start, before K3s: the container's interface returns at 1450.
        "--entrypoint", "/bin/sh", images.K3S,
        "-c", f'ip link set dev eth0 mtu {VSWITCH_MTU} && exec /bin/k3s agent "$@"', "k3s-agent",
        f"--node-name={AGENT_NAME}", *flannel_args(link), *infra.node_args(stack),
    ])
    stack.containers.append(container)
    wait_ready(link, link.agent)
    infra.import_images(container, [*images.K3S_SYSTEM_IMAGES, link.tools_image])


def wait_ready(link: Link, node: Node, timeout: float = 300) -> None:
    assert link.server is not None
    server = link.server.k3s
    wait_for(lambda: node_ready(server, node.name), timeout=timeout, interval=2, description=f"node {node.name} to be Ready")


def running(node: Node) -> bool:
    state = run(["docker", "inspect", "--format", "{{.State.Running}}", node.container], check=False).stdout.strip()
    return state == "true"


def server_outcome(link: Link, timeout: float = 300) -> str:
    """'ready' once the restarted server's API answers ready and its node is
    Ready, 'exited' if K3s stopped first. The reconcile runs before the API
    server starts, so a server that stops on it is never ready first."""

    assert link.server is not None
    server = link.server

    def probe() -> str | None:
        if not running(server):
            return "exited"
        ready = run(["docker", "exec", server.container, "kubectl", "get", "--raw=/readyz"], check=False)
        if ready.stdout.strip() == "ok" and node_ready(server.k3s, server.name):
            return "ready"
        return None

    return wait_for(probe, timeout=timeout, interval=2, description="the restarted K3s server")


def fatal_line(node: Node, since: str) -> str | None:
    logs = run(["docker", "logs", "--since", since, node.container], check=False)
    lines = [line for line in (logs.stdout + logs.stderr).splitlines() if NEWER_THAN_DATASTORE in line]
    # The line names files, never their content.
    return lines[-1].split("msg=", 1)[-1][:400] if lines else None


def remove_from_volume(link: Link, node: Node, relative: str) -> None:
    """Delete a file under the node's K3s data while the node is stopped."""

    volume = node.volume_at(K3S_DATA)
    run(["docker", "run", "--rm", *link.labels, "--mount", f"type=volume,src={volume},dst=/data",
         images.BUSYBOX, "rm", "-f", f"/data/{relative}"])


def restart(node: Node) -> str:
    """Restart the node's container; returns the time to read its logs from."""

    since = f"{time.time():.3f}"
    run(["docker", "restart", "--time", "60", node.container], timeout=180)
    return since


def ping_from_node(link: Link, node: Node, address: str, packet_bytes: int) -> bool:
    """A ping of exactly `packet_bytes` with fragmentation forbidden, sent
    from the node's own network namespace."""

    result = run(["docker", "run", "--rm", *link.labels, "--network", f"container:{node.container}", link.tools_image,
                  "ping", "-M", "do", "-c", "2", "-W", "2", "-s", str(packet_bytes - IP_ICMP_HEADERS), address],
                 check=False, timeout=60)
    return result.returncode == 0


def flannel_state(node: Node) -> dict[str, int] | None:
    """flannel.1's MTU and the FLANNEL_MTU K3s's CNI gives new pods, read where
    the k3s role's handler reads them."""

    result = run(["docker", "exec", node.container, "cat", "/sys/class/net/flannel.1/mtu", "/run/flannel/subnet.env"],
                 check=False)
    lines = result.stdout.splitlines()
    if result.returncode != 0 or not lines:
        return None
    subnet_env = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
    if "FLANNEL_MTU" not in subnet_env:
        return None
    return {"flannel.1": int(lines[0]), "FLANNEL_MTU": int(subnet_env["FLANNEL_MTU"])}


def ping_pod(name: str, node: str, image: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "default"},
        "spec": {"nodeName": node, "terminationGracePeriodSeconds": 0, "containers": [{
            "name": "ping", "image": image, "imagePullPolicy": "Never", "command": ["sleep", "86400"],
        }]},
    }


def pod_status(link: Link, name: str) -> dict[str, Any]:
    assert link.server is not None
    return json.loads(link.server.k3s.kubectl("get", "pod", name, "--namespace", "default", "-o", "json").stdout)["status"]


def server_tls(link: Link, credentials: Path | None = None) -> ssl.SSLContext:
    """A client TLS context that trusts only the server's CA. With
    `credentials`, it presents the kubelet client certificate kept there."""

    assert link.server is not None
    ca = link.stack.workdir / "server-ca.crt"
    ca.write_text(run(["docker", "exec", link.server.container, "cat", f"{K3S_DATA}/server/tls/server-ca.crt"]).stdout,
                  encoding="utf-8")
    context = ssl.create_default_context(cafile=str(ca))
    if credentials is not None:
        context.load_cert_chain(credentials / "client-kubelet.crt", credentials / "client-kubelet.key")
    return context


def pod_running_since(status: dict[str, Any], since: dt.datetime) -> bool:
    containers = status.get("containerStatuses") or []
    started = [(c.get("state") or {}).get("running", {}).get("startedAt") for c in containers]
    return bool(started) and all(
        value is not None and dt.datetime.fromisoformat(value.replace("Z", "+00:00")) > since for value in started
    ) and all(c.get("ready") for c in containers)


# --- 6.5 ------------------------------------------------------------------


async def check_link(link: Link, record: StepRecord) -> None:
    """The agent's link carries 1400-byte packets and drops 1450-byte ones.
    Catches a rehearsal link that does not model the vSwitch, on which the pod
    check would pass and prove nothing."""

    assert link.server is not None and link.agent is not None
    evidence: dict[str, dict[str, bool]] = {}
    for source, target in ((link.server, link.agent), (link.agent, link.server)):
        evidence[f"{source.name} -> {target.name}"] = {
            str(size): ping_from_node(link, source, target.ip, size) for size in (CLOUD_MTU, VSWITCH_MTU)
        }
    record.evidence["node_df_ping_passed_by_packet_bytes"] = evidence
    for direction, passed in evidence.items():
        if passed[str(CLOUD_MTU)] or not passed[str(VSWITCH_MTU)]:
            raise StepFailure(f"{direction}: the link does not carry exactly {VSWITCH_MTU}-byte packets: {passed}")


async def check_flannel_devices(link: Link, record: StepRecord) -> None:
    """Each node's flannel.1 and FLANNEL_MTU equal the role's pod MTU, the
    values the role's handler asserts. Catches a node that ignores the role's
    configuration and runs flannel at its interface's MTU."""

    assert link.server is not None and link.agent is not None
    expected = {"flannel.1": link.role["pod_mtu"], "FLANNEL_MTU": link.role["pod_mtu"]}
    observed = {
        node.name: wait_for(lambda node=node: flannel_state(node), timeout=180, interval=3,
                            description=f"flannel on {node.name}")
        for node in (link.server, link.agent)
    }
    record.evidence["flannel_mtu_by_node"] = observed
    record.evidence["expected_from_role"] = expected
    wrong = {name: state for name, state in observed.items() if state != expected}
    if wrong:
        raise StepFailure(f"flannel is not at the role's pod MTU {link.role['pod_mtu']}: {wrong}")


async def check_pods(link: Link, record: StepRecord) -> None:
    """A pod on each node has eth0 at the role's pod MTU, and a ping of that
    full size with fragmentation forbidden crosses the 1400-byte link both
    ways. Catches pod traffic whose VXLAN packets overflow the private link."""

    assert link.server is not None and link.agent is not None
    pods = {node.name: f"link-ping-{node.name}" for node in (link.server, link.agent)}
    manifest = {"apiVersion": "v1", "kind": "List", "items": [
        ping_pod(pod, node, link.tools_image) for node, pod in pods.items()]}
    link.server.k3s.kubectl("apply", "-f", "-", input_text=json.dumps(manifest))
    for pod in pods.values():
        link.server.k3s.kubectl("wait", "--namespace", "default", "--for=condition=Ready", f"pod/{pod}", "--timeout=180s")
    addresses = {pod: pod_status(link, pod)["podIP"] for pod in pods.values()}
    mtus = {
        pod: int(link.server.k3s.kubectl("exec", "--namespace", "default", pod, "--",
                                         "cat", "/sys/class/net/eth0/mtu").stdout.strip())
        for pod in pods.values()
    }
    payload = link.role["pod_mtu"] - IP_ICMP_HEADERS
    first, second = pods.values()
    pings = {}
    for source, target in ((first, second), (second, first)):
        result = link.server.k3s.kubectl("exec", "--namespace", "default", source, "--", "ping", "-M", "do", "-c", "3",
                                         "-W", "2", "-s", str(payload), addresses[target], check=False)
        pings[f"{source} -> {target}"] = result.returncode == 0
    record.evidence.update({"pod_eth0_mtu": mtus, "df_ping_packet_bytes": link.role["pod_mtu"], "df_ping_passed": pings})
    if set(mtus.values()) != {link.role["pod_mtu"]} or not all(pings.values()):
        raise StepFailure(f"pods are not at the role's pod MTU {link.role['pod_mtu']}, or full-size "
                          f"traffic does not cross the link: mtu {mtus}, ping {pings}")


# --- 5.7 ------------------------------------------------------------------


async def check_restart_hazard(link: Link, record: StepRecord) -> None:
    """The server's agent token changes from A to B; the server restarts, then
    restarts again without the re-save. Records whether the second restart
    stops on K3s's reconcile fatal, which decides whether the runbook needs the
    re-save, and recovers as K3s's message directs."""

    assert link.server is not None
    write_server_config(link, "B")
    restart(link.server)
    first = server_outcome(link)
    if first != "ready":
        raise StepFailure("the first restart after the agent-token change did not come up")
    since = restart(link.server)
    second = server_outcome(link)
    record.evidence["first_restart"], record.evidence["second_restart_without_resave"] = first, second
    if second == "exited":
        message = fatal_line(link.server, since)
        if message is None:
            raise StepFailure("the second restart stopped without the reconcile fatal")
        record.evidence["fatal"] = message
        # K3s's message: remove the file from disk and restart.
        remove_from_volume(link, link.server, "server/cred/passwd")
        run(["docker", "start", link.server.container])
        if server_outcome(link) != "ready":
            raise StepFailure("the server did not start after cred/passwd was removed")
        record.evidence["recovered_by_removing"] = "server/cred/passwd"
    link.report.measurements["second_restart_after_an_agent_token_change"] = {
        "observed": "stopped on the reconcile fatal" if second == "exited" else "ready",
        "source": f"check {record.number}, the server container's state and log",
    }


async def check_resave(link: Link, record: StepRecord) -> None:
    """The role's re-save helper leaves the server token file unchanged, and
    the server then restarts ready. Catches a re-save that rotates the real
    server token, and one that does not clear the reconcile fatal."""

    assert link.server is not None
    token_file = f"{K3S_DATA}/server/token"

    def digest() -> str:
        return run(["docker", "exec", link.server.container, "sha256sum", token_file]).stdout.split()[0]

    before = digest()
    resave = run(["docker", "exec", link.server.container, "sh", RESAVE], check=False, timeout=120)
    after = digest()
    record.evidence["resave"] = {"exit": resave.returncode, "stdout": resave.stdout.strip()[-300:],
                                 "token_file_sha256_unchanged": before == after}
    if resave.returncode != 0 or before != after:
        raise StepFailure(f"the re-save failed or changed the server token: {record.evidence['resave']}")
    since = restart(link.server)
    outcome = server_outcome(link)
    record.evidence["restart_after_resave"] = outcome
    if outcome != "ready":
        raise StepFailure(f"the server did not restart ready after the re-save: {fatal_line(link.server, since)}")


async def check_token_probe(link: Link, record: StepRecord) -> None:
    """The supervisor refuses the old agent token and admits the new one, each
    presented alone. Catches a rotation that leaves the old token valid, which
    no Ready node shows: joined agents authenticate with their kubelet client
    certificate."""

    context = server_tls(link)
    attempts: list[dict[str, int]] = []

    def probe() -> dict[str, int] | None:
        # The runbook's probe, from the runner instead of the server's loopback.
        with httpx.Client(verify=context, trust_env=False, timeout=15) as client:
            codes = {label: client.get(f"https://{SERVER_IP}:6443/v1-k3s/readyz",
                                       auth=("node", link.agent_tokens[label])).status_code for label in ("A", "B")}
        attempts.append(codes)
        # 503 comes from the readyz handler, past authentication: the server
        # is still starting its core controllers.
        return None if 503 in codes.values() else codes

    codes = wait_for(probe, timeout=180, interval=3, description="the supervisor to answer past 503")
    record.evidence["readyz_status"] = {"old agent token (A)": codes["A"], "new agent token (B)": codes["B"]}
    record.evidence["probes_until_past_503"] = len(attempts)
    if codes != {"A": 401, "B": 200}:
        raise StepFailure(f"expected 401 for the old agent token and 200 for the new one: {codes}")


async def check_agent_rejoin(link: Link, record: StepRecord) -> None:
    """The agent, moved to the new token and stripped of its kubelet client
    certificate, rejoins and runs its pod again. Catches a new token that a
    joining agent cannot use: with its certificate the agent would rejoin on
    that alone."""

    assert link.agent is not None
    write_agent_config(link, "B")
    run(["docker", "stop", "--time", "60", link.agent.container], timeout=120)
    for name in ("client-kubelet.crt", "client-kubelet.key"):
        remove_from_volume(link, link.agent, f"agent/{name}")
    since = _now()
    run(["docker", "start", link.agent.container])
    pod = f"link-ping-{AGENT_NAME}"
    wait_for(lambda: pod_running_since(pod_status(link, pod), since), timeout=300, interval=3,
             description=f"{pod} to run again on {AGENT_NAME}")
    issued = run(["docker", "exec", link.agent.container, "stat", "-c", "%Y", f"{K3S_DATA}/agent/client-kubelet.crt"])
    record.evidence.update({
        "node_ready": node_ready(link.server.k3s, AGENT_NAME) if link.server else None,
        "kubelet_client_certificate_reissued": int(issued.stdout.strip()) >= int(since.timestamp()),
        "pod_running_since_restart": True,
    })
    if not record.evidence["node_ready"] or not record.evidence["kubelet_client_certificate_reissued"]:
        raise StepFailure(f"the agent did not rejoin on the new token: {record.evidence}")


# --- 5.8 ------------------------------------------------------------------


def copy_kubelet_credentials(link: Link, directory: Path) -> Path:
    """The agent's kubelet client certificate and key and its node password,
    the credentials an agent's disk holds, copied to the runner. Nothing
    prints their contents."""

    assert link.agent is not None
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    sources = {"client-kubelet.crt": f"{K3S_DATA}/agent/client-kubelet.crt",
               "client-kubelet.key": f"{K3S_DATA}/agent/client-kubelet.key",
               "node-password": "/etc/rancher/node/password"}
    for name, source in sources.items():
        target = directory / name
        target.touch(mode=0o600)
        target.write_text(run(["docker", "exec", link.agent.container, "cat", source]).stdout, encoding="utf-8")
    return directory


def throwaway_csr() -> bytes:
    """A certificate request for a key made here and then dropped. The
    supervisor signs it as it signs an agent's own request, and returns no key."""

    key = ec.generate_private_key(ec.SECP256R1())
    request = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, hashes.SHA256())
    return request.public_bytes(serialization.Encoding.DER)


def kubelet_credential_status(link: Link, credentials: Path) -> dict[str, int]:
    """The status of three requests made from the runner with the kubelet
    credentials in `credentials`, as a lost disk's holder would make them:
    the agent's own Node from the API server, which the node authorizer admits;
    a kube-proxy certificate from the supervisor, which needs the certificate
    alone; and a kubelet certificate, which also needs the node password."""

    password = (credentials / "node-password").read_text(encoding="utf-8").strip()
    base = f"https://{SERVER_IP}:6443"
    with httpx.Client(verify=server_tls(link, credentials), trust_env=False, timeout=15) as client:
        return {
            "api_server_own_node": client.get(f"{base}/api/v1/nodes/{AGENT_NAME}").status_code,
            "supervisor_kube_proxy_certificate": client.post(
                f"{base}/v1-k3s/client-kube-proxy.crt", content=throwaway_csr()).status_code,
            "supervisor_kubelet_certificate_with_node_password": client.post(
                f"{base}/v1-k3s/client-kubelet.crt", content=throwaway_csr(),
                headers={"K3s-Node-Name": AGENT_NAME, "K3s-Node-Password": password}).status_code,
        }


def settled_status(link: Link, credentials: dict[str, Path]) -> dict[str, dict[str, int]]:
    """kubelet_credential_status for each set of credentials, once no request
    gets 503, which means the server is still starting."""

    def probe() -> dict[str, dict[str, int]] | None:
        codes = {label: kubelet_credential_status(link, path) for label, path in credentials.items()}
        return None if any(503 in by_request.values() for by_request in codes.values()) else codes

    return wait_for(probe, timeout=120, interval=3, description="the server to answer past 503")


def public_key(certificate_pem: str) -> bytes:
    certificate = x509.load_pem_x509_certificates(certificate_pem.encode())[0]
    return certificate.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)


def renewed_lease_since(link: Link, node: Node, since: dt.datetime) -> bool:
    """Whether the node's kubelet renewed its lease after `since`. A Ready
    condition alone can be one the kubelet posted before a restart."""

    assert link.server is not None
    renewed = link.server.k3s.kubectl("get", "lease", node.name, "--namespace", "kube-node-lease",
                                      "--output=jsonpath={.spec.renewTime}", check=False).stdout.strip()
    return bool(renewed) and dt.datetime.fromisoformat(renewed.replace("Z", "+00:00")) > since


def node_file(node: Node, path: str) -> str:
    return run(["docker", "exec", node.container, "cat", path]).stdout


def fingerprint(pem: str) -> str:
    """The SHA-256 fingerprint of a bundle's first certificate, as openssl prints it."""

    return x509.load_pem_x509_certificates(pem.encode())[0].fingerprint(hashes.SHA256()).hex(":").upper()


def run_client_ca_helper(link: Link) -> subprocess.CompletedProcess[str]:
    """The role's client-CA helper, run as the runbook runs it on the server:
    as root, on the server's data, with its k3s binary and its API on the
    loopback address."""

    assert link.server is not None
    k3s = link.stack.workdir / "k3s"
    run(["docker", "cp", "--follow-link", f"{link.server.container}:/bin/k3s", str(k3s)])
    return run([
        "docker", "run", "--rm", *link.labels, "--network", f"container:{link.server.container}",
        "--mount", f"type=volume,src={link.server.volume_at(K3S_DATA)},dst={K3S_DATA}",
        "--mount", f"type=bind,source={k3s},target=/usr/local/bin/k3s,readonly",
        "--mount", f"type=bind,source={ROTATE_CLIENT_CA_SOURCE},target={ROTATE_CLIENT_CA},readonly",
        link.tools_image, "sh", ROTATE_CLIENT_CA,
    ], check=False, timeout=300)


async def check_lost_disk_before(link: Link, record: StepRecord) -> None:
    """Copies the agent's kubelet credentials, as a lost disk holds them, and
    presents them before the rotation: the API server and the supervisor admit
    them. Catches probes that would read as refused whatever the rotation did."""

    link.lost_disk = copy_kubelet_credentials(link, link.stack.workdir / "lost-disk")
    codes = settled_status(link, {"lost disk": link.lost_disk})["lost disk"]
    record.evidence["status_before_rotation"] = codes
    if set(codes.values()) != {200}:
        raise StepFailure(f"the agent's own credentials were not admitted before the rotation: {codes}")


async def check_client_ca_rotation(link: Link, record: StepRecord) -> None:
    """Runs the role's client-CA helper against the server, then restarts the
    server, and the agent as soon as the server is ready, as the runbook does.
    Both nodes are Ready again, the server serves the CA the helper made, and
    the agent holds it. Catches a CA that K3s does not load, and a node that
    does not come back on it."""

    assert link.server is not None and link.agent is not None
    server_ca_path, agent_ca_path = f"{K3S_DATA}/server/tls/client-ca.crt", f"{K3S_DATA}/agent/client-ca.crt"
    before = node_file(link.server, server_ca_path)
    helper = run_client_ca_helper(link)
    made = next((line.removeprefix("new client CA: ") for line in helper.stdout.splitlines()
                 if line.startswith("new client CA: ")), None)
    record.evidence["helper"] = {"exit": helper.returncode, "stdout": helper.stdout.strip()[-600:],
                                 "stderr": helper.stderr.strip()[-600:]}
    if helper.returncode != 0 or made is None:
        raise StepFailure(f"the client-CA helper failed: {record.evidence['helper']}")
    restart(link.server)
    restarted = {link.server.name: _now()}
    if server_outcome(link) != "ready":
        raise StepFailure("the server did not restart ready on the new client CA; its log is in the diagnostics")
    # The agent stands for the host whose disk left custody: as the runbook
    # directs for that host, its new certificates take new keys.
    run(["docker", "exec", link.agent.container, "sh", "-c", f"rm -f {K3S_DATA}/agent/client-*.key"])
    restart(link.agent)
    restarted[link.agent.name] = _now()
    seconds_to_ready = {}
    for node in (link.server, link.agent):
        since = restarted[node.name]
        try:
            wait_for(lambda node=node, since=since: node_ready(link.server.k3s, node.name)
                     and renewed_lease_since(link, node, since),
                     timeout=300, interval=2, description=f"{node.name} to be Ready on a lease renewed since its restart")
        except TimeoutError as error:
            raise StepFailure(f"{node.name} is not Ready on the new client CA: {error}") from error
        seconds_to_ready[node.name] = round((_now() - since).total_seconds(), 1)
    after = node_file(link.server, server_ca_path)
    record.evidence.update({
        "client_ca_sha256_before": fingerprint(before),
        "client_ca_sha256_after": fingerprint(after),
        "client_ca_sha256_the_helper_made": made,
        "agent_holds_the_server_client_ca": node_file(link.agent, agent_ca_path) == after,
        "seconds_from_restart_to_ready_by_node": seconds_to_ready,
    })
    if fingerprint(after) != made or after == before or not record.evidence["agent_holds_the_server_client_ca"]:
        raise StepFailure(f"the cluster does not run on the client CA the helper made: {record.evidence}")


async def check_lost_disk_refused(link: Link, record: StepRecord) -> None:
    """Presents the lost disk's credentials again: the API server and the
    supervisor refuse its certificate, with the node password too. The agent's
    certificate issued after the rotation, on a new key, gets 200 for the same
    requests. Catches a rotation that leaves the old certificate valid, as
    K3s's cross-signed rotation does."""

    assert link.lost_disk is not None
    current = copy_kubelet_credentials(link, link.stack.workdir / "issued-after-rotation")
    codes = settled_status(link, {"lost disk": link.lost_disk, "issued after the rotation": current})
    new_key = public_key((current / "client-kubelet.crt").read_text(encoding="utf-8")) != public_key(
        (link.lost_disk / "client-kubelet.crt").read_text(encoding="utf-8"))
    record.evidence.update({"status_after_rotation": codes, "kubelet_certificate_on_a_new_key": new_key})
    if set(codes["lost disk"].values()) != {401} or set(codes["issued after the rotation"].values()) != {200}:
        raise StepFailure(f"expected 401 for every request with the lost disk's certificate and 200 with "
                          f"the certificate issued after the rotation: {codes}")
    if not new_key:
        raise StepFailure("the agent's certificate issued after the rotation is on the lost disk's key")


async def check_workload_after_rotation(link: Link, record: StepRecord) -> None:
    """A new pod that the scheduler places on the agent runs, and the API
    server execs into it through the agent's kubelet. Catches a scheduler left
    on a certificate the new CA refuses, and a kubelet that still trusts only
    the old client CA."""

    assert link.server is not None
    name = "link-after-client-ca"
    pod = {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "default"},
        "spec": {"nodeSelector": {"kubernetes.io/hostname": AGENT_NAME}, "terminationGracePeriodSeconds": 0,
                 "containers": [{"name": "tools", "image": link.tools_image, "imagePullPolicy": "Never",
                                 "command": ["sleep", "86400"]}]},
    }
    kubectl = link.server.k3s.kubectl
    kubectl("apply", "-f", "-", input_text=json.dumps(pod))
    ready = kubectl("wait", "--namespace", "default", "--for=condition=Ready", f"pod/{name}", "--timeout=180s",
                    check=False)
    placed = kubectl("get", "pod", name, "--namespace", "default", "--output=jsonpath={.spec.nodeName}",
                     check=False).stdout.strip()
    execed = kubectl("exec", "--namespace", "default", name, "--", "true", check=False)
    record.evidence.update({"pod_ready": ready.returncode == 0, "scheduled_on": placed,
                            "exec_exit": execed.returncode, "exec_stderr": execed.stderr.strip()[-300:]})
    if not record.evidence["pod_ready"] or placed != AGENT_NAME or execed.returncode != 0:
        raise StepFailure(f"a new pod on the agent did not run or could not be reached: {record.evidence}")


CHECKS: list[tuple[str, str, Callable[[Link, StepRecord], Awaitable[None]]]] = [
    ("6.5", "the agent's link carries 1400 bytes and drops 1450", check_link),
    ("6.5", "flannel.1 and FLANNEL_MTU at the role's pod MTU on both nodes", check_flannel_devices),
    ("6.5", "full-MTU pod traffic crosses the link both ways", check_pods),
    ("5.7", "a second server restart after an agent-token change", check_restart_hazard),
    ("5.7", "the re-save keeps the server token and clears the fatal", check_resave),
    ("5.7", "the old agent token is refused, the new one admitted", check_token_probe),
    ("5.7", "the agent rejoins on the new token alone", check_agent_rejoin),
    ("5.8", "a lost disk's kubelet certificate and node password work before the rotation",
     check_lost_disk_before),
    ("5.8", "the client-CA rotation loads a new CA, and both nodes are Ready on it", check_client_ca_rotation),
    ("5.8", "the lost disk's certificate is refused, one issued after the rotation admitted",
     check_lost_disk_refused),
    ("5.8", "a new pod schedules on the agent, and the API server execs into it",
     check_workload_after_rotation),
]


async def run_check(link: Link, number: int, task: str, name: str,
                    body: Callable[[Link, StepRecord], Awaitable[None]]) -> None:
    record = StepRecord(number=number, name=f"{task} {name}")
    tasks = sorted({task, BUILDS_ON.get(task, task)})
    earlier = [check for check in link.report.checks if check.name.split(" ", 1)[0] in tasks]
    link.report.checks.append(record)
    if any(check.status != PASSED for check in earlier):
        record.failure = {"message": f"blocked: an earlier {' or '.join(tasks)} check did not pass, and this one runs "
                                     "on the state it leaves"}
        print(f"[k3s-link] check {number} {record.name}: blocked", flush=True)
        return
    record.started_at = _now().isoformat()
    began = time.monotonic()
    print(f"[k3s-link] check {number} {record.name}: start", flush=True)
    try:
        await body(link, record)
        record.status = PASSED
    except Exception as error:  # noqa: BLE001 - recorded as the check's failure
        record.status = FAILED
        record.failure = failure_record(error)
    record.seconds = round(time.monotonic() - began, 1)
    print(f"[k3s-link] check {number} {record.name}: {record.status} in {record.seconds}s "
          f"{(record.failure or {}).get('message', '')[:1500]}", flush=True)


async def run_link(args: argparse.Namespace) -> int:
    run_id = secrets.token_hex(4)
    workdir = args.workdir or Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"exomem-k3s-link-{run_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    labels = ["--label", args.label] if args.label else []
    image_labels = ["--label", args.image_label] if args.image_label else []
    report = DrillReport(run_id=run_id, schema=SCHEMA)
    report.adaptations.extend(ADAPTATIONS)
    report.inputs = {
        "exomem_commit": run(["git", "-C", str(images.REPO_ROOT), "rev-parse", "HEAD"]).stdout.strip(),
        "exomem_worktree_clean": run(["git", "-C", str(images.REPO_ROOT), "status", "--porcelain"]).stdout.strip() == "",
        "k3s_image": images.K3S, "environment": infra.environment_facts(),
    }
    stack = infra.Stack(run_id=run_id, workdir=workdir, network="", subnet=SUBNET,
                        k3s=None, postgres=None, object_store=None)  # type: ignore[arg-type]
    link = Link(report=report, stack=stack, labels=labels)
    code = 2
    try:
        with stage(report, "inputs"):
            link.role, link.conf = render_role(workdir)
            version = image_k3s_version(labels)
            report.inputs.update({
                "k3s_image_version": version, "role_values": link.role,
                "role_values_source": "infra/ansible/roles/k3s/defaults/main.yml, evaluated by ansible-core "
                                      + tool_version("ANSIBLE_CORE_VERSION"),
                "flannel_conf": json.loads(link.conf.read_text(encoding="utf-8")),
            })
            require_role_k3s_version(version, link.role["k3s_version"])
        with stage(report, "images"):
            link.tools_image = build_tools_image(run_id, workdir, image_labels)
            report.inputs["tools_image"] = link.tools_image
        with stage(report, "cluster"):
            create_network(link)
            start_server(link)
            start_agent(link)
        report.adaptations.extend(item for item in stack.adaptations if item not in report.adaptations)
        for number, (task, name, body) in enumerate(CHECKS, start=1):
            await run_check(link, number, task, name, body)
        harness_errors = [c.number for c in report.checks if c.failure and "traceback" in c.failure]
        code = 2 if harness_errors else (0 if report.passed() else 1)
    except StageFailed:
        code = 2
    except Exception as error:  # noqa: BLE001 - recorded in the report
        report.stages.setdefault("harness", {})["failure"] = failure_record(error)
        code = 2
    finally:
        if stack.containers and not args.keep:
            guarded(report, "diagnostics", lambda: collect_diagnostics(link))
        if not guarded(report, "report", lambda: report.write(args.report)):
            code = 2
        if not args.keep:
            guarded(report, "teardown", lambda: teardown(link))
            if args.workdir is None:
                shutil.rmtree(workdir, ignore_errors=True)
    print(f"[k3s-link] report: {args.report}")
    for check in report.checks:
        print(f"[k3s-link]   {check.number} {check.name:<70} {check.status}")
    return code


def teardown(link: Link) -> None:
    infra.teardown(link.stack if link.stack.network else None, keep=False)
    for volume in link.volumes:
        run(["docker", "volume", "rm", "--force", volume], check=False)
    if link.tools_image:
        run(["docker", "rmi", "--force", link.tools_image], check=False)


def collect_diagnostics(link: Link) -> None:
    out = link.stack.workdir / "diagnostics"
    out.mkdir(exist_ok=True)
    if link.server is not None and running(link.server):
        for name, command in {
            "nodes": ["get", "nodes", "-o", "wide"],
            "pods": ["get", "pods", "--all-namespaces", "-o", "wide"],
            "events": ["get", "events", "--all-namespaces", "--sort-by=.lastTimestamp"],
        }.items():
            result = link.server.k3s.kubectl(*command, check=False)
            (out / f"{name}.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    for container in link.stack.containers:
        logs = run(["docker", "logs", "--tail", "400", container], check=False)
        (out / f"{container}.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
    artifacts = os.environ.get("REHEARSAL_DIAGNOSTICS_DIR")
    if artifacts:
        shutil.copytree(out, Path(artifacts), dirs_exist_ok=True)
