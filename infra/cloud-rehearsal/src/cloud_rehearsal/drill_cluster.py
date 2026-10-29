"""The node-loss drill's cluster: a K3s server with embedded etcd and two
agents, each with its own loop-device `cells` volume group
(move-cloud-cells-to-local-storage, task 4).

Every node is a privileged container on one Docker network with fixed
addresses, so the server can be restored from an etcd snapshot in place and
the agents rejoin it.

The agents run the pinned K3s image plus LVM. TopoLVM's lvmd runs every LVM
command through `nsenter` into the namespaces of PID 1 on its node, which in
an agent container is K3s itself, so the container must hold `/sbin/lvm` and
the thin-pool tools. Each agent's LVM reads only its own loop device.

The agents share the runner's kernel, and device-mapper names are
kernel-global: two volume groups named `cells`, each with a pool `pool0`,
cannot be active at once. Agent B is therefore the replacement capacity: its
volume group is created once agent A's disk has been destroyed, as an
operator brings up a replacement agent (design D4, recovery order step 3).
A third agent is the planned move's destination: its volume group is created
once agent B's is deactivated, between the move's backup and its restore.
"""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import images, infra
from .shell import run, wait_for

SUBNET = "172.29.83.0/24"
# Docker assigns the other containers' addresses from here, never a node's.
DYNAMIC_RANGE = "172.29.83.128/25"
SERVER_IP = "172.29.83.10"
SERVER_NAME = "drill-server"
AGENTS = {"drill-agent-a": "172.29.83.11", "drill-agent-b": "172.29.83.12"}
# The planned move's destination, started only for that check.
MOVE_TARGET = ("drill-agent-c", "172.29.83.13")
VOLUME_GROUP = "cells"
POOL = "pool0"
GIB = 1024**3
# Room for the cells, retained volumes, snapshots and clones the drill leaves
# on one agent, and for D10's growth check: the step and the 8 GiB cell's
# reserve while that backup's own snapshot and clone still take their space.
# Sparse: only written blocks take runner disk.
POOL_GIB = 64
# Design D1: 64 KiB chunks and 64 bytes of pool metadata per chunk.
CHUNK_BYTES = 64 * 1024


def metadata_mib(pool_gib: int) -> int:
    return math.ceil(pool_gib * GIB / CHUNK_BYTES * 64 / 1024**2)


METADATA_MIB = metadata_mib(POOL_GIB)
SNAPSHOT_DIR = "/var/lib/rancher/k3s/server/db/snapshots"
LIST_PLAY = images.REPO_ROOT / "infra/ansible/list-cell-volumes.yml"

AGENT_DOCKERFILE = """\
FROM {alpine} AS lvm
RUN apk add --no-cache lvm2-static thin-provisioning-tools
FROM {k3s}
COPY --from=lvm /usr/sbin/lvm.static /sbin/lvm
COPY --from=lvm /lib/ld-musl-x86_64.so.1 /lib/
COPY --from=lvm /usr/lib/libudev.so.1 /usr/lib/libgcc_s.so.1 /usr/lib/
COPY --from=lvm /usr/sbin/pdata_tools /usr/sbin/
RUN for tool in thin_check thin_dump thin_repair thin_restore; do ln -s pdata_tools /usr/sbin/$tool; done \\
 && for command in lvs vgs pvs pvcreate vgcreate vgchange lvcreate lvremove lvrename; do ln -s lvm /sbin/$command; done
"""

# Each agent's LVM sees only its own disk; no udev runs in a container, so
# LVM creates its device nodes itself.
LVM_CONF = """\
devices {{
    global_filter = [ "a|^{device}$|", "r|.*|" ]
    use_devicesfile = 0
}}
activation {{
    udev_sync = 0
    udev_rules = 0
}}
"""


@dataclass
class Agent:
    name: str
    container: str
    ip: str
    disk: Path
    loop_device: str
    lvm_conf: Path
    destroyed: bool = False
    volume_group: bool = False


@dataclass
class Cluster:
    stack: infra.Stack
    token: str
    agent_image: str
    agents: dict[str, Agent] = field(default_factory=dict)

    @property
    def server(self) -> infra.K3s:
        return self.stack.k3s


def create_network(run_id: str) -> str:
    network = f"exo-drill-{run_id}"
    run(["docker", "network", "create", *infra.docker_labels(run_id), "--subnet", SUBNET,
         "--ip-range", DYNAMIC_RANGE, network])
    return network


def build_agent_image(run_id: str, workdir: Path) -> str:
    context = workdir / "agent-image"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(AGENT_DOCKERFILE.format(alpine=images.ALPINE, k3s=images.K3S), encoding="utf-8")
    tag = f"rehearsal.local/k3s-lvm-agent:{run_id}"
    run(["docker", "build", *infra.docker_labels(run_id, image=True), "--tag", tag, str(context)], timeout=900)
    return tag


def _server_command(stack: infra.Stack, token: str) -> list[str]:
    return [
        "server", "--cluster-init", f"--token={token}", f"--node-name={SERVER_NAME}", f"--tls-san={SERVER_IP}",
        # Cells live only on TopoLVM volumes here.
        "--disable=local-storage",
        *infra.SERVER_ARGS, *infra.node_args(stack),
    ]


def start_server(stack: infra.Stack) -> Cluster:
    token = secrets.token_hex(24)
    name = f"exo-drill-server-{stack.run_id}"
    mounts = infra.image_data_mounts(stack, images.K3S, SERVER_NAME)
    run(
        [
            "docker", "run", "--privileged", "--detach", *infra.docker_labels(stack.run_id),
            "--name", name, "--hostname", SERVER_NAME,
            "--network", stack.network, "--ip", SERVER_IP, *mounts, "--mount", infra.containerd_mount(stack),
            images.K3S, *_server_command(stack, token),
        ]
    )
    stack.containers.append(name)
    stack.k3s = infra.K3s(container=name, ip=SERVER_IP)
    infra.wait_api_server(name)
    infra.import_images(name, list(images.K3S_SYSTEM_IMAGES))
    wait_node_ready(stack.k3s, SERVER_NAME)
    wait_for(
        lambda: "1/1" in stack.k3s.kubectl("get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns", "--no-headers", check=False).stdout,
        timeout=240, interval=3, description="CoreDNS",
    )
    return Cluster(stack=stack, token=token, agent_image="")


def node_ready(server: infra.K3s, node: str) -> bool | None:
    """True or False from the node's Ready condition; None if it cannot be read."""

    result = server.kubectl(
        "get", "node", node, "--output=jsonpath={.status.conditions[?(@.type=='Ready')].status}", check=False
    )
    return None if result.returncode != 0 else result.stdout.strip() == "True"


def wait_node_ready(server: infra.K3s, node: str, timeout: float = 240) -> None:
    wait_for(lambda: node_ready(server, node), timeout=timeout, interval=2, description=f"node {node} to be Ready")


def attach_cells_disk(workdir: Path, name: str, pool_gib: int) -> tuple[Path, str, Path]:
    """A sparse disk attached as a loop device, and the lvm.conf that reads
    only it. The device must exist before its container starts: a container's
    /dev holds only the host devices present when it starts."""

    disk = workdir / f"{name}-cells.img"
    with disk.open("wb") as handle:
        handle.truncate((pool_gib + 1) * GIB)
    loop = run(["sudo", "losetup", "--find", "--show", str(disk)]).stdout.strip()
    conf = workdir / f"lvm-{name}.conf"
    conf.write_text(LVM_CONF.format(device=loop), encoding="utf-8")
    return disk, loop, conf


def start_agent(cluster: Cluster, name: str, ip: str) -> Agent:
    """Attaches a sparse disk as a loop device, then starts the agent."""

    stack = cluster.stack
    disk, loop, conf = attach_cells_disk(stack.workdir, name, POOL_GIB)
    container = f"exo-drill-{name}-{stack.run_id}"
    mounts = infra.image_data_mounts(stack, cluster.agent_image, name)
    run(
        [
            "docker", "run", "--privileged", "--detach", *infra.docker_labels(stack.run_id),
            "--name", container, "--hostname", name,
            "--network", stack.network, "--ip", ip, *mounts, "--mount", infra.containerd_mount(stack),
            "--mount", f"type=bind,source={conf},target=/etc/lvm/lvm.conf,readonly",
            # TopoLVM's node plugin mounts volumes with Bidirectional
            # propagation, which needs the kubelet's directories shared.
            "--entrypoint", "/bin/sh", cluster.agent_image,
            "-c", 'mount --make-rshared / && exec /bin/k3s agent "$@"', "k3s-agent",
            f"--server=https://{SERVER_IP}:6443", f"--token={cluster.token}", f"--node-name={name}",
            *infra.node_args(stack),
        ]
    )
    stack.containers.append(container)
    agent = Agent(name=name, container=container, ip=ip, disk=disk, loop_device=loop, lvm_conf=conf)
    cluster.agents[name] = agent
    wait_node_ready(cluster.server, name)
    infra.import_images(container, list(images.K3S_SYSTEM_IMAGES))
    return agent


def agent_shell(agent: Agent, script: str, *, check: bool = True):
    return run(["docker", "exec", agent.container, "sh", "-c", script], check=check, timeout=300)


def create_volume_group(agent: Agent, pool_gib: int = POOL_GIB) -> dict[str, Any]:
    """The `cells` volume group and its thin pool, as spike 1.1 made them."""

    metadata = metadata_mib(pool_gib)
    agent_shell(
        agent,
        f"pvcreate --yes {agent.loop_device} && vgcreate {VOLUME_GROUP} {agent.loop_device} && "
        f"lvcreate --yes --type thin-pool --name {POOL} --size {pool_gib}G "
        f"--poolmetadatasize {metadata}M --chunksize 64k {VOLUME_GROUP}",
    )
    agent.volume_group = True
    return {"loop_device": agent.loop_device, "pool_gib": pool_gib, "metadata_mib": metadata, "chunk": "64k"}


def pool_gib_for(volume_gib: int) -> int:
    """Pool size for one cell of `volume_gib` and two overlapping backups. TopoLVM counts
    every thin volume at its virtual size (overprovisioning 1.0), and a backup holds a
    snapshot and a clone of the cell's size; the margin covers the pool's own overhead."""

    return 5 * volume_gib + 8


def prepare_node_storage(run_id: str, workdir: Path, pool_gib: int) -> tuple[infra.NodeStorage, Agent]:
    """What a single K3s node needs to serve cells from TopoLVM: the image with LVM,
    a loop-device disk and its lvm.conf. The kernel modules load on the runner because
    the node's LVM cannot load them from inside its container."""

    run(["sudo", "modprobe", "--all", "dm_thin_pool", "dm_snapshot"])
    image = build_agent_image(run_id, workdir)
    disk, loop, conf = attach_cells_disk(workdir, "node", pool_gib)
    node = Agent(name="node", container="", ip="", disk=disk, loop_device=loop, lvm_conf=conf)
    return infra.NodeStorage(image=image, lvm_conf=conf), node


def release_node_storage(node: Agent, stack: infra.Stack | None, agent_image: str) -> None:
    """Teardown of `prepare_node_storage`, after the node's container is gone. It also
    covers a stack that setup never returned, which has no volume group yet."""

    if node.volume_group and stack is not None:
        deactivate_volume_group(Cluster(stack=stack, token="", agent_image=agent_image), node, timeout=60)
    if node.disk.exists():
        run(["sudo", "losetup", "--detach", node.loop_device], check=False)
        node.disk.unlink(missing_ok=True)


def list_volumes_argv() -> list[str]:
    """The `lvs` call of infra/ansible/list-cell-volumes.yml, the runbook's
    listing step, with its volume group filled in."""

    play = yaml.safe_load(LIST_PLAY.read_text(encoding="utf-8"))[0]
    task = next(task for task in play["tasks"] if "ansible.builtin.command" in task)
    group = play["vars"]["cell_volume_group"]
    return [arg.replace("{{ cell_volume_group }}", group) for arg in task["ansible.builtin.command"]["argv"]]


def list_volumes(agent: Agent) -> dict[str, Any]:
    """Read the active volume group's `lvs` report through the listing play."""

    result = run(["docker", "exec", agent.container, *list_volumes_argv()], timeout=120)
    return json.loads(result.stdout)


def thin_volumes(agent: Agent) -> dict[str, int]:
    """{name: virtual bytes} of the thin volumes in the agent's pool."""

    rows = list_volumes(agent)["report"][0]["lv"]
    return {row["lv_name"]: int(row["lv_size"]) for row in rows if row.get("pool_lv") == POOL and row["lv_attr"].startswith("V")}


def recreate_empty(agent: Agent, name: str, size: int) -> None:
    """Replace a thin volume with an empty one of the same name and size, as a
    disk replaced under a surviving LogicalVolume would leave it. The node
    unmounts the volume shortly after its pod stops, so removal is retried."""

    wait_for(lambda: agent_shell(agent, f'lvremove --yes "{VOLUME_GROUP}/{name}"'), timeout=120, interval=3,
             description=f"the node to release volume {name} for removal")
    agent_shell(agent, f'lvcreate --yes --thin --virtualsize {size}b --name "{name}" "{VOLUME_GROUP}/{POOL}"')


def destroy_agent(cluster: Cluster, agent: Agent) -> None:
    """The node and its disk are gone: the container and its volumes are
    removed, then its volume group is deactivated (the kernel would
    otherwise keep its device-mapper devices), its loop device detached and
    the disk file deleted."""

    run(["docker", "rm", "--force", "--volumes", agent.container])
    cluster.stack.containers.remove(agent.container)
    agent.destroyed = True
    deactivate_volume_group(cluster, agent, timeout=120)
    run(["sudo", "losetup", "--detach", agent.loop_device])
    agent.disk.unlink(missing_ok=True)


def deactivate_volume_group(cluster: Cluster, agent: Agent, *, timeout: float) -> None:
    """Deactivates the agent's `cells` volume group from a separate
    container that reads only the agent's disk, retrying while a volume in
    it is still open. Its volumes and their metadata stay on the disk."""

    def deactivated() -> bool:
        mounts = infra.image_data_mounts(cluster.stack, cluster.agent_image, "deactivate", temporary=True)
        result = run(
            [
                "docker", "run", "--rm", "--privileged", *infra.docker_labels(cluster.stack.run_id),
                *mounts, "--mount", f"type=bind,source={agent.lvm_conf},target=/etc/lvm/lvm.conf,readonly",
                "--entrypoint", "/sbin/lvm", cluster.agent_image, "vgchange", "--activate", "n", VOLUME_GROUP,
            ],
            check=False,
        )
        return result.returncode == 0

    wait_for(deactivated, timeout=timeout, interval=5, description=f"{agent.name}'s volume group to deactivate")
    agent.volume_group = False


def release_disks(cluster: Cluster | None) -> None:
    """Teardown, after the containers are gone: no volume group stays active
    and no loop device stays attached on the runner."""

    if cluster is None:
        return
    for agent in cluster.agents.values():
        if agent.volume_group:
            deactivate_volume_group(cluster, agent, timeout=60)
        if agent.disk.exists():
            run(["sudo", "losetup", "--detach", agent.loop_device], check=False)
            agent.disk.unlink(missing_ok=True)


def etcd_snapshot(cluster: Cluster, name: str) -> str:
    """An on-demand snapshot on the server's disk; returns its path."""

    before = set(_snapshots(cluster))
    run(["docker", "exec", cluster.server.container, "k3s", "etcd-snapshot", "save", "--name", name], timeout=300)
    new = sorted(set(_snapshots(cluster)) - before)
    if len(new) != 1:
        raise RuntimeError(f"expected one new etcd snapshot, found {new}")
    return f"{SNAPSHOT_DIR}/{new[0]}"


def _snapshots(cluster: Cluster) -> list[str]:
    return run(["docker", "exec", cluster.server.container, "ls", SNAPSHOT_DIR], check=False).stdout.split()


def restore_etcd(cluster: Cluster, snapshot: str) -> str:
    """Runbook "Restore etcd from an older snapshot" steps 3-5 on the drill's
    server: stop K3s, reset the cluster from `snapshot` on the same address,
    data and arguments, then start K3s. Returns the reset's last log lines."""

    server = cluster.server.container
    run(["docker", "stop", "--time", "60", server], timeout=120)
    reset = run(
        [
            "docker", "run", "--rm", "--privileged", *infra.docker_labels(cluster.stack.run_id),
            "--name", f"{server}-reset", "--hostname", SERVER_NAME,
            "--network", cluster.stack.network, "--ip", SERVER_IP, "--volumes-from", server,
            images.K3S, *_server_command(cluster.stack, cluster.token),
            "--cluster-reset", f"--cluster-reset-restore-path={snapshot}",
        ],
        check=False, timeout=900,
    )
    output = reset.stdout + reset.stderr
    reported = [line for line in output.splitlines() if "has been reset" in line]
    if not reported:
        raise RuntimeError(f"k3s --cluster-reset did not report a reset (exit {reset.returncode}): {output[-1500:]}")
    run(["docker", "start", server])
    infra.wait_api_server(server, timeout=300)
    return reported[-1].strip()[-300:]
