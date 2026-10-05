"""Node-local cell storage on the runner: K3s, a loop-device volume group, TopoLVM.

Used by the storage spike (`storage_spike.py`) and meant to be reused by the
node-loss drill: `install_topolvm` and `Kube` take no spike state, only a
running cluster.

Unlike the full rehearsal, this K3s runs directly on the runner. TopoLVM's
lvmd drives the host's LVM through its own PID namespace, which a K3s inside a
Docker container cannot give it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .images import REPO_ROOT
from .shell import run, wait_for

KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"
NODE_NAME = "spike-1"
VG_NAME = "cells"
POOL_NAME = "pool0"
DEVICE_CLASS = "thin"
TOPOLVM_DRIVER = "topolvm.io"
TOPOLVM_CHART_VERSION = "17.2.0"
TOPOLVM_NAMESPACE = "exomem-platform"
# The release name and namespace the platform chart will give its TopoLVM subchart, so
# object names in the rehearsal and in the policy test match production's.
TOPOLVM_RELEASE = "exomem-platform"
SNAPSHOTTER_VERSION = "v8.6.0"
VALUES_FILE = REPO_ROOT / "infra/cloud-rehearsal/storage/topolvm-values.yaml"
# sha256 of the `k3s` amd64 binary of each release, from the release's sha256sum-amd64.txt.
K3S_SHA256 = {"v1.35.6+k3s1": "2b52a2c1ca6eb502e2a0ffa1a4cf79eef94875926577c1e43347ed292cc92432"}

GIB = 1024**3


def tool_version(name: str) -> str:
    text = (REPO_ROOT / "infra/tool-versions.env").read_text(encoding="utf-8")
    match = re.search(rf"^{name}=(\S+)$", text, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"{name} is not pinned in infra/tool-versions.env")
    return match.group(1)


def sudo(*command: str, check: bool = True, timeout: float | None = 900) -> subprocess.CompletedProcess[str]:
    return run(["sudo", *command], check=check, timeout=timeout)


class Kube:
    """kubectl and helm against the runner's K3s."""

    def __init__(self, kubeconfig: str = KUBECONFIG) -> None:
        self.env = {**os.environ, "KUBECONFIG": kubeconfig}

    def kubectl(
        self, *args: str, input_text: str | None = None, check: bool = True, timeout: float | None = 900
    ) -> subprocess.CompletedProcess[str]:
        return run(["k3s", "kubectl", *args], input_text=input_text, check=check, env=self.env, timeout=timeout)

    def json(self, *args: str) -> Any:
        return json.loads(self.kubectl(*args, "--output=json").stdout)

    def apply(self, *documents: dict[str, Any]) -> None:
        payload = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(documents)})
        self.kubectl("apply", "--filename=-", input_text=payload)

    def helm(self, *args: str, timeout: float | None = 1200) -> subprocess.CompletedProcess[str]:
        return run(["helm", *args], env=self.env, timeout=timeout)

    def exec(
        self, namespace: str, pod: str, *command: str, container: str | None = None,
        check: bool = True, timeout: float | None = 900,
    ) -> subprocess.CompletedProcess[str]:
        target = ["--container", container] if container else []
        return self.kubectl("exec", "--namespace", namespace, pod, *target, "--", *command, check=check, timeout=timeout)

    def delete(self, *args: str, wait: bool = True, timeout: float = 300) -> None:
        self.kubectl(
            "delete", *args, "--ignore-not-found", f"--wait={'true' if wait else 'false'}",
            f"--timeout={int(timeout)}s", timeout=timeout + 60,
        )


# --- host: volume group ----------------------------------------------------------------


def create_volume_group(image_dir: Path, *, image_gib: int, pool_gib: int, metadata_mib: int) -> dict[str, Any]:
    """A sparse file on a loop device, one PV, one VG, one thin pool (TopoLVM creates none)."""

    run(
        ["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "--yes", "--no-install-recommends",
         "lvm2", "thin-provisioning-tools"],
        timeout=600,
    )
    sudo("modprobe", "dm_thin_pool")
    sudo("mkdir", "-p", str(image_dir))
    image = image_dir / "cells.img"
    sudo("truncate", "-s", f"{image_gib}G", str(image))
    loop = sudo("losetup", "--find", "--show", str(image)).stdout.strip()
    sudo("pvcreate", "--yes", loop)
    sudo("vgcreate", VG_NAME, loop)
    sudo(
        "lvcreate", "--yes", "--type", "thin-pool", "--name", POOL_NAME, "--size", f"{pool_gib}G",
        "--poolmetadatasize", f"{metadata_mib}M", "--chunksize", "64k", VG_NAME,
    )
    return {"loop_device": loop, "image_gib": image_gib, "pool_gib": pool_gib, "metadata_mib": metadata_mib, "chunk": "64k"}


@dataclass(frozen=True)
class PoolUsage:
    """What `lvs` reports for the thin pool and the thin volumes in it."""

    pool_bytes: int
    data_percent: float
    metadata_percent: float
    metadata_bytes: int
    thin_volumes: int
    virtual_bytes: int


def parse_lvs(raw: str, pool: str = POOL_NAME) -> PoolUsage:
    """Reads `lvs -a --reportformat json --units b --nosuffix` output."""

    rows = json.loads(raw)["report"][0]["lv"]

    def number(value: str) -> float:
        return float(value) if value else 0.0

    pool_row = next((row for row in rows if row["lv_name"] == pool), None)
    if pool_row is None:
        raise ValueError(f"no thin pool named {pool} in the lvs report")
    # A thin volume or thin snapshot has attr 'V' and names the pool it lives in.
    thin = [row for row in rows if row.get("pool_lv") == pool and row["lv_attr"].startswith("V")]
    return PoolUsage(
        pool_bytes=int(number(pool_row["lv_size"])),
        data_percent=number(pool_row["data_percent"]),
        metadata_percent=number(pool_row["metadata_percent"]),
        metadata_bytes=int(number(pool_row["lv_metadata_size"])),
        thin_volumes=len(thin),
        virtual_bytes=sum(int(number(row["lv_size"])) for row in thin),
    )


def read_pool_usage() -> PoolUsage:
    out = sudo(
        "lvs", "-a", "--reportformat", "json", "--units", "b", "--nosuffix",
        "-o", "lv_name,lv_size,pool_lv,origin,data_percent,metadata_percent,lv_attr,lv_metadata_size",
    ).stdout
    return parse_lvs(out)


_QUANTITY = re.compile(r"^(?P<number>[0-9]+(?:\.[0-9]+)?)(?P<suffix>Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?$")
_SUFFIX = {
    None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
}


def parse_quantity(text: str) -> int:
    """A Kubernetes byte quantity as published in CSIStorageCapacity (`15Gi`, `20000000000`)."""

    match = _QUANTITY.match(text.strip())
    if match is None:
        raise ValueError(f"not a byte quantity: {text!r}")
    return int(float(match["number"]) * _SUFFIX[match["suffix"]])


def parse_thinpool_metrics(text: str) -> dict[str, dict[str, float]]:
    """The `topolvm_thinpool_*` and `topolvm_volumegroup_*` gauges of the node plugin, by device class."""

    wanted = re.compile(r'^(topolvm_(?:thinpool|volumegroup)_\w+)\{(?P<labels>[^}]*)\}\s+(?P<value>\S+)$')
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        match = wanted.match(line)
        if match is None:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', match["labels"]))
        out.setdefault(labels.get("device_class", ""), {})[match.group(1)] = float(match["value"])
    return out


# --- K3s ------------------------------------------------------------------------------


def install_k3s(workdir: Path) -> None:
    version = tool_version("K3S_VERSION")
    expected = K3S_SHA256.get(version)
    if expected is None:
        raise RuntimeError(f"no sha256 pinned for K3s {version} in storage.K3S_SHA256")
    binary = workdir / "k3s"
    run(
        ["curl", "--fail", "--silent", "--show-error", "--location", "--output", str(binary),
         f"https://github.com/k3s-io/k3s/releases/download/{version.replace('+', '%2B')}/k3s"],
        timeout=600,
    )
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError(f"k3s {version} sha256 is {digest}, expected {expected}")
    sudo("install", "-m", "0755", str(binary), "/usr/local/bin/k3s")
    log = (workdir / "k3s.log").open("ab")
    # The runner is disposable, so no unit: the process dies with the job.
    subprocess.Popen(
        [
            "sudo", "/usr/local/bin/k3s", "server",
            "--disable=traefik", "--disable=servicelb", "--disable=metrics-server",
            "--write-kubeconfig-mode=644", f"--node-name={NODE_NAME}",
            # Images are pulled once and must stay; the runner's root disk is shared with the job.
            "--kubelet-arg=image-gc-high-threshold=100", "--kubelet-arg=image-gc-low-threshold=99",
            "--kubelet-arg=eviction-hard=imagefs.available<1%,nodefs.available<1%",
        ],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    kube = Kube()
    wait_for(lambda: kube.kubectl("get", "--raw=/readyz", check=False).stdout.strip() == "ok",
             timeout=240, interval=3, description="the K3s API server")
    wait_for(
        lambda: kube.kubectl(
            "get", "node", NODE_NAME, "--output=jsonpath={.status.conditions[?(@.type=='Ready')].status}", check=False
        ).stdout == "True",
        timeout=240, interval=3, description="the K3s node to be Ready",
    )
    wait_for(
        lambda: "1/1" in kube.kubectl("get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns", "--no-headers",
                                      check=False).stdout,
        timeout=300, interval=3, description="CoreDNS",
    )


def bundled_snapshot_support(kube: Kube) -> dict[str, Any]:
    """What K3s ships for volume snapshots, read before anything is installed."""

    crds = kube.kubectl("get", "crd", "--output=name", check=False).stdout.split()
    deployments = kube.kubectl("get", "deployments", "--all-namespaces", "--output=name", check=False).stdout.split()
    manifests = sudo("ls", "/var/lib/rancher/k3s/server/manifests", check=False).stdout.split()
    return {
        "snapshot_crds": [name for name in crds if "snapshot.storage.k8s.io" in name],
        "snapshot_deployments": [name for name in deployments if "snapshot" in name],
        "k3s_manifests": manifests,
    }


# --- cluster add-ons ------------------------------------------------------------------


def install_snapshot_controller(kube: Kube, workdir: Path) -> None:
    source = workdir / "external-snapshotter"
    run(["git", "clone", "--quiet", "--depth", "1", "--branch", SNAPSHOTTER_VERSION,
         "https://github.com/kubernetes-csi/external-snapshotter", str(source)], timeout=300)
    kube.kubectl("apply", "--server-side", "-k", str(source / "client/config/crd"))
    kube.kubectl("apply", "--server-side", "-k", str(source / "deploy/kubernetes/snapshot-controller"))
    kube.kubectl("scale", "--namespace", "kube-system", "deployment/snapshot-controller", "--replicas=1")
    kube.kubectl("rollout", "status", "--namespace", "kube-system", "deployment/snapshot-controller", "--timeout=300s")


def install_topolvm(kube: Kube) -> None:
    """cert-manager, then the pinned TopoLVM chart with the thin device class."""

    cert_manager = tool_version("CERT_MANAGER_CHART_VERSION")
    kube.helm("repo", "add", "jetstack", "https://charts.jetstack.io", "--force-update")
    kube.helm("repo", "add", "topolvm", "https://topolvm.github.io/topolvm", "--force-update")
    kube.helm(
        "upgrade", "--install", "cert-manager", "jetstack/cert-manager", "--version", cert_manager,
        "--namespace", "cert-manager", "--create-namespace", "--set", "crds.enabled=true", "--wait", "--timeout", "10m",
    )
    kube.kubectl("create", "namespace", TOPOLVM_NAMESPACE, check=False)
    kube.helm(
        "upgrade", "--install", TOPOLVM_RELEASE, "topolvm/topolvm", "--version", TOPOLVM_CHART_VERSION,
        "--namespace", TOPOLVM_NAMESPACE, "--values", str(VALUES_FILE), "--wait", "--timeout", "10m",
    )
    wait_for(
        lambda: f"capacity.topolvm.io/{DEVICE_CLASS}" in kube.json("get", "node", NODE_NAME)["metadata"].get("annotations", {}),
        timeout=180, interval=3, description="TopoLVM to publish the node's capacity",
    )
