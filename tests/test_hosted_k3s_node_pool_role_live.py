"""add-cloud-node-provisioning N8: run the REAL site.yml and remove-agent.yml
against disposable systemd Ubuntu 24.04 containers standing in for the K3s
server and two agents, over Ansible's docker connection.

What it proves:
  - site.yml converges the server and an agent, and a second run reports
    changed=0 on every host;
  - the agent joins with the CA-pinned agent token (byte-equal to the token
    the server itself writes to /var/lib/rancher/k3s/server/agent-token),
    reports Ready and carries the node-pool label;
  - a host outside the inventory cannot reach the K3s API through the host
    firewall, and an admitted peer presenting a wrong agent password is
    refused (401) where the right one is accepted;
  - a second agent added in a later run joins (the first agent's firewall
    admits it, so both peer-admission checks pass), and the run after that
    is changed=0 again;
  - the removal preflight refuses when cell volumes exceed the remaining
    slots, before cordoning anything;
  - removal stops the agent, deletes its node, revokes its inter-node rules,
    and a rerun reports changed=0;
  - site.yml refuses to rejoin the removed agent.

Deviations from a real host, all confined to this rig:
  - the pinned K3s binary is served from a file:// copy of the
    rancher/k3s-upgrade image's /opt/k3s; the test asserts its SHA-256 equals
    the role's pin, so the role's checksum verification runs unchanged;
  - a config.yaml.d drop-in appends kubelet `fail-cgroupv1=false`, because
    sandbox kernels may run cgroup v1, which kubelet 1.35 refuses by default;
  - /var/lib/rancher and /var/lib/kubelet sit on Docker volumes, because
    containerd cannot use overlayfs on top of the container's overlayfs;
  - systemd-timesyncd's container condition is cleared, as a real host
    starts it (otherwise base's time-sync task reports a change every run);
  - /run is a tmpfs, so the rig creates sshd's /run/sshd as a real host has it;
  - the etcd-s3 endpoint is unreachable (snapshots are scheduled, not taken);
  - no Hetzner CSI driver runs, so the join takes the "driver not installed"
    path and every node has an unknown attachment limit (zero slots).

The containers are privileged and apply the role's kernel settings, which are
host-global: run this only on a disposable host. Gated behind
RUN_K3S_NODE_POOL_ROLE_TEST=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "infra/ansible"
RUN_LIVE = os.environ.get("RUN_K3S_NODE_POOL_ROLE_TEST") == "1"
DOCKER = shutil.which("docker")
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK_BIN") or shutil.which("ansible-playbook")

pytestmark = [
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="set RUN_K3S_NODE_POOL_ROLE_TEST=1 to run the real k3s role in systemd containers",
    ),
    pytest.mark.timeout(3600),
]

K3S_UPGRADE_IMAGE = "rancher/k3s-upgrade:v1.35.6-k3s1"
TAG = uuid.uuid4().hex[:8]
IMAGE = f"exomem-node-pool-test:{TAG}"
NETWORK = f"exomem-node-pool-{TAG}"
SUBNET_OCTET = 200 + int(TAG[:2], 16) % 50
SUBNET = f"172.30.{SUBNET_OCTET}.0/24"
SERVER = "exomem-alpha"
AGENT_1 = "exomem-agent-01"
AGENT_2 = "exomem-agent-02"
IPS = {
    SERVER: f"172.30.{SUBNET_OCTET}.10",
    AGENT_1: f"172.30.{SUBNET_OCTET}.31",
    AGENT_2: f"172.30.{SUBNET_OCTET}.32",
}
SERVER_TOKEN = "server-" + uuid.uuid4().hex + uuid.uuid4().hex
AGENT_TOKEN = "agent-" + uuid.uuid4().hex + uuid.uuid4().hex

DOCKERFILE = textwrap.dedent(
    f"""
    FROM {K3S_UPGRADE_IMAGE} AS k3s
    FROM ubuntu:24.04
    RUN apt-get update \\
     && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        systemd systemd-sysv systemd-timesyncd dbus python3 openssh-server sudo \\
        iproute2 iptables kmod ca-certificates apparmor cryptsetup curl fail2ban jq \\
        logrotate unattended-upgrades ufw procps util-linux \\
     && rm -rf /var/lib/apt/lists/*
    COPY --from=k3s /opt/k3s /opt/k3s-release
    # timesyncd refuses to start in a container (ConditionVirtualization);
    # on a real host it runs, so base's `state: started` converges.
    RUN mkdir -p /etc/systemd/system/systemd-timesyncd.service.d \\
     && printf '[Unit]\\nConditionVirtualization=\\n' \\
        > /etc/systemd/system/systemd-timesyncd.service.d/99-test-container.conf
    CMD ["/sbin/init"]
    """
)


def _docker(*args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    assert DOCKER is not None
    return subprocess.run(
        [DOCKER, *args], check=check, capture_output=True, text=True, timeout=timeout
    )


def _exec(container: str, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return _docker("exec", container, *argv, check=check)


def _kubectl(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return _exec(SERVER, "/usr/local/bin/k3s", "kubectl", *argv, check=check)


def _start(name: str) -> None:
    _docker(
        "run", "-d", "--name", name, "--hostname", name,
        "--network", NETWORK, "--ip", IPS[name],
        # A private cgroup namespace gives systemd and kubelet the same root
        # paths a real host has; a shared one breaks kubelet's QoS cgroups.
        "--privileged", "--cgroupns=private",
        "--tmpfs", "/run", "--tmpfs", "/run/lock",
        "-v", "/var/lib/rancher", "-v", "/var/lib/kubelet",
        IMAGE,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = _exec(name, "systemctl", "is-system-running", check=False).stdout.strip()
        if state in {"running", "degraded"}:
            break
        time.sleep(1)
    else:
        raise AssertionError(f"systemd never came up in {name}")
    # /run is a fresh tmpfs here; a real host has sshd's privilege-separation
    # directory, which the base role's `sshd -t` handler needs.
    _exec(name, "install", "-d", "-m", "0755", "/run/sshd")
    # Sandbox kernels may run cgroup v1; kubelet 1.35 refuses it unless told.
    _exec(name, "mkdir", "-p", "/etc/rancher/k3s/config.yaml.d")
    _exec(
        name, "sh", "-c",
        "printf 'kubelet-arg+:\\n  - fail-cgroupv1=false\\n' "
        "> /etc/rancher/k3s/config.yaml.d/99-test-cgroupv1.yaml",
    )


def _inventory(path: Path, agents: list[str]) -> None:
    def host(name: str) -> dict:
        return {
            "ansible_connection": "community.docker.docker",
            "ansible_host": name,
            "ansible_python_interpreter": "/usr/bin/python3",
            "private_node_ip": IPS[name],
        }

    hosted: dict = {"hosts": {SERVER: host(SERVER)}}
    if agents:
        hosted["children"] = {"k3s_agents": {"hosts": {name: host(name) for name in agents}}}
    document = {
        "all": {
            "vars": {
                "base_admin_ssh_cidrs": ["192.0.2.1/32"],
                "base_administrator_authorized_keys": [
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyReplaceMe test"
                ],
                "k3s_server_token": SERVER_TOKEN,
                "k3s_agent_token": AGENT_TOKEN,
                "k3s_etcd_s3_endpoint": "s3.invalid.example",
                "k3s_etcd_s3_region": "invalid",
                "k3s_etcd_s3_bucket": "invalid",
                "k3s_etcd_s3_access_key": "invalid",
                "k3s_etcd_s3_secret_key": "invalid",
                "k3s_release_url_amd64": "file:///opt/k3s-release",
                "k3s_join_retries": 60,
                "k3s_join_delay": 5,
            },
            "children": {"hosted_nodes": hosted},
        }
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    path.chmod(0o600)


def _playbook(inventory: Path, playbook: str, *extra: str) -> tuple[int, dict, str]:
    env = dict(os.environ)
    env["ANSIBLE_STDOUT_CALLBACK"] = "content_free_json"
    env["ANSIBLE_CALLBACK_PLUGINS"] = str(ANSIBLE / "callback_plugins")
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    result = subprocess.run(
        [str(ANSIBLE_PLAYBOOK), "-i", str(inventory), str(ANSIBLE / playbook), *extra],
        cwd=ANSIBLE, env=env, capture_output=True, text=True, timeout=2400,
    )
    stats: dict = {}
    for line in result.stdout.splitlines():
        if line.startswith('{"stats"'):
            stats = json.loads(line)["stats"]
    return result.returncode, stats, result.stderr[-4000:]


def _changed(stats: dict) -> dict[str, int]:
    return {host: counters["changed"] for host, counters in stats.items()}


def _node_ready(name: str) -> str:
    return _kubectl(
        "get", "node", name, "--ignore-not-found",
        "-o", 'jsonpath={.status.conditions[?(@.type=="Ready")].status}',
    ).stdout.strip()


def _ufw_added(container: str) -> str:
    return _exec(container, "/usr/sbin/ufw", "show", "added").stdout


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory):
    if DOCKER is None or ANSIBLE_PLAYBOOK is None:
        pytest.skip("needs docker and ansible-playbook with community.docker")
    build = tmp_path_factory.mktemp("image")
    (build / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    _docker("build", "-q", "-t", IMAGE, str(build), timeout=1800)
    _docker("network", "create", "--subnet", SUBNET, NETWORK)
    try:
        for name in (SERVER, AGENT_1, AGENT_2):
            _start(name)
        yield tmp_path_factory.mktemp("inventory")
    finally:
        for name in (SERVER, AGENT_1, AGENT_2):
            _docker("rm", "-f", "-v", name, check=False)
        _docker("network", "rm", NETWORK, check=False)
        _docker("rmi", "-f", IMAGE, check=False)


def test_node_pool_join_rerun_second_agent_preflight_removal_and_rejoin_refusal(rig: Path) -> None:
    # The file:// binary is the exact release the role pins.
    pinned = yaml.safe_load((ANSIBLE / "roles/k3s/defaults/main.yml").read_text())[
        "k3s_sha256_amd64"
    ]
    binary = _exec(SERVER, "sha256sum", "/opt/k3s-release").stdout.split()[0]
    assert binary == pinned

    one_agent = rig / "one-agent.yml"
    two_agents = rig / "two-agents.yml"
    _inventory(one_agent, [AGENT_1])
    _inventory(two_agents, [AGENT_1, AGENT_2])

    # 1. First converge: server + one agent.
    code, stats, stderr = _playbook(one_agent, "site.yml")
    assert code == 0, stderr
    assert _node_ready(AGENT_1) == "True"
    labels = json.loads(_kubectl("get", "node", AGENT_1, "-o", "json").stdout)["metadata"]["labels"]
    assert labels["exomem.io/node-pool"] == "agent"
    assert "node-role.kubernetes.io/control-plane" not in labels

    # The agent presents exactly the secure token the server itself derives,
    # and its host never received the server token or etcd credentials.
    server_side = _exec(SERVER, "cat", "/var/lib/rancher/k3s/server/agent-token").stdout.strip()
    agent_config = yaml.safe_load(_exec(AGENT_1, "cat", "/etc/rancher/k3s/config.yaml").stdout)
    assert agent_config["token"] == server_side
    assert agent_config["token"].startswith("K10") and agent_config["token"].endswith(
        "::node:" + AGENT_TOKEN
    )
    agent_files = _exec(AGENT_1, "sh", "-c", "cat /etc/rancher/k3s/config.yaml").stdout
    assert SERVER_TOKEN not in agent_files
    assert "etcd" not in agent_files
    ca = _exec(SERVER, "cat", "/var/lib/rancher/k3s/server/tls/server-ca.crt").stdout.encode()
    assert agent_config["token"][3:67] == hashlib.sha256(ca).hexdigest()

    # 2. Rerun: nothing changes, K3s is not restarted.
    code, stats, stderr = _playbook(one_agent, "site.yml")
    assert code == 0, stderr
    assert _changed(stats) == {SERVER: 0, AGENT_1: 0}, stats

    # 3a. A host that is not an inventoried K3s node cannot reach the API:
    # the server's host firewall admits 6443 only from peer addresses.
    blocked = _exec(
        AGENT_2, "curl", "-sk", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}",
        f"https://{IPS[SERVER]}:6443/cacerts", check=False,
    )
    assert blocked.stdout.strip() == "000", blocked.stdout

    # 3b. From an admitted peer, the node-authenticated bootstrap endpoint the
    # agent uses refuses a wrong agent password and accepts the right one.
    def node_auth(password: str) -> str:
        return _exec(
            AGENT_1, "curl", "-sk", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
            "-u", f"node:{password}", f"https://{IPS[SERVER]}:6443/v1-k3s/config",
            check=False,
        ).stdout.strip()

    assert node_auth("wrong" + AGENT_TOKEN) == "401"
    assert node_auth(SERVER_TOKEN + "x") == "401"
    assert node_auth(AGENT_TOKEN) not in {"401", "000"}

    # 4. A second agent added in a later run joins; both peer checks pass.
    code, stats, stderr = _playbook(two_agents, "site.yml")
    assert code == 0, stderr
    assert _node_ready(AGENT_2) == "True"
    assert f"from {IPS[AGENT_2]} to any port 8472 proto udp" in _ufw_added(AGENT_1)
    assert f"from {IPS[AGENT_2]} to any port 6443 proto tcp" in _ufw_added(SERVER)
    assert f"from {IPS[AGENT_2]} to any port 6443 proto tcp" not in _ufw_added(AGENT_1)
    code, stats, stderr = _playbook(two_agents, "site.yml")
    assert code == 0, stderr
    assert _changed(stats) == {SERVER: 0, AGENT_1: 0, AGENT_2: 0}, stats

    # 5. Preflight refusal: a cell volume that no remaining slot can hold.
    _kubectl("create", "namespace", "exo-cell-aaaaaaaaaaaaaaaa")
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data", "namespace": "exo-cell-aaaaaaaaaaaaaaaa"},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
    }
    subprocess.run(
        [DOCKER, "exec", "-i", SERVER, "/usr/local/bin/k3s", "kubectl", "apply", "-f", "-"],
        input=json.dumps(pvc), text=True, check=True, capture_output=True,
    )
    code, stats, stderr = _playbook(two_agents, "remove-agent.yml", "-e", f"k3s_remove_node={AGENT_2}")
    assert code != 0
    node = json.loads(_kubectl("get", "node", AGENT_2, "-o", "json").stdout)
    assert not node["spec"].get("unschedulable"), "the preflight must refuse before cordoning"
    assert _exec(AGENT_2, "systemctl", "is-active", "k3s-agent", check=False).stdout.strip() == "active"
    # Only the PVC matters to the preflight; namespace finalization can stall
    # on aggregated APIs that this rig does not serve.
    _kubectl(
        "delete", "persistentvolumeclaim", "data",
        "--namespace", "exo-cell-aaaaaaaaaaaaaaaa", "--wait=true", "--timeout=120s",
    )

    # 6. Removal: stop, delete, revoke.
    code, stats, stderr = _playbook(two_agents, "remove-agent.yml", "-e", f"k3s_remove_node={AGENT_2}")
    assert code == 0, stderr
    assert _kubectl("get", "node", AGENT_2, "--ignore-not-found", "-o", "name").stdout == ""
    assert _exec(AGENT_2, "systemctl", "is-active", "k3s-agent", check=False).stdout.strip() != "active"
    assert _exec(AGENT_2, "pgrep", "-f", "containerd-shim", check=False).returncode == 1
    assert _exec(AGENT_2, "test", "-f", "/etc/rancher/k3s/removed", check=False).returncode == 0
    assert IPS[AGENT_2] not in _ufw_added(SERVER)
    assert IPS[AGENT_2] not in _ufw_added(AGENT_1)
    assert _node_ready(AGENT_1) == "True"

    # 7. Rerun removal: completes and changes nothing.
    code, stats, stderr = _playbook(two_agents, "remove-agent.yml", "-e", f"k3s_remove_node={AGENT_2}")
    assert code == 0, stderr
    assert sum(_changed(stats).values()) == 0, stats

    # 8. site.yml refuses to rejoin the removed agent.
    code, stats, stderr = _playbook(two_agents, "site.yml")
    assert code != 0
    assert _kubectl("get", "node", AGENT_2, "--ignore-not-found", "-o", "name").stdout == ""
