"""`exomem-cloud-rehearsal storage-spike`: task 1.1 of move-cloud-cells-to-local-storage.

Stands up K3s on the runner with a loop-device volume group and TopoLVM (thin
device class, overprovision ratio 1.0, ext4), runs a real cell on it, and
measures what the later phases of that change depend on. Every finding is
printed as a `[spike] FINDING` line with the command that produced it and is
written to the report; a measurement that cannot be taken is recorded as such
with its reason, never as a guess. The cell image is the published one, so
nothing here builds Substrate or an image.
"""

from __future__ import annotations

import calendar
import json
import math
import secrets
import subprocess
import time
import traceback
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import storage
from .shell import run, wait_for
from .storage import DEVICE_CLASS, GIB, NODE_NAME, TOPOLVM_DRIVER, TOPOLVM_NAMESPACE, Kube

CELL_IMAGE = "ghcr.io/artexis10/exomem@sha256:018302998f0cf55b789e048291d9030be5e52217320f5bdca134f2a63e284ea5"
CELL_CLASS = "exomem-local"
IMMEDIATE_CLASS = "exomem-local-immediate"
SNAPSHOT_CLASS = "exomem-local-snapshots"
NAMESPACE = "spike-cell"
CELL_GIB = 4
BULK_FILES = 1024  # 1 MiB each: a vault-sized body of data for the restic timing
CHURN_CYCLES = 24
CHURN_MIB = 128
RESTIC_REPO = Path("/mnt/spike/restic-repo")

# Runs inside a cell on its own bearer: MCP over the pod's loopback, as the
# rehearsal's scratch-restore probe does.
CELL_PROBE = r'''
import asyncio, json, os, sys, time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

def structured(result):
    return getattr(result, "structuredContent", None) or getattr(result, "structured_content", None) or {}

def body(marker):
    return marker + "\n\n## Observations\n\n- [spike] " + marker + " #spike\n"

async def write(session, title, marker):
    deadline = time.monotonic() + 90
    while True:
        result = structured(await session.call_tool("remember", {"title": title, "content": body(marker), "status": "draft"}))
        error = result.get("error")
        if not (isinstance(error, dict) and error.get("code") == "MUTATION_WARMING") or time.monotonic() > deadline:
            return error
        await asyncio.sleep(max(0.1, error.get("retry_after_ms", 500) / 1000))

async def main(mode, args):
    headers = {"Authorization": "Bearer " + os.environ["EXOMEM_CLOUD_CELL_TOKEN"]}
    async with httpx.AsyncClient(headers=headers, timeout=120) as http, streamable_http_client(
        "http://127.0.0.1:8765/mcp", http_client=http
    ) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            if mode == "recall":
                print(json.dumps({"recall": json.dumps(structured(await session.call_tool("ask_memory", {"query": args[0]})))}))
            elif mode == "write":
                print(json.dumps({"error": await write(session, args[0], args[1])}))
            elif mode == "writer":
                end = time.time() + float(args[0])
                i = 0
                while time.time() < end:
                    started = time.time()
                    error = await write(session, "Spike writer %05d" % i, "spike-writer-%05d" % i)
                    print(json.dumps({"i": i, "t_start": started, "t_ack": time.time(), "error": error}), flush=True)
                    i += 1
                    await asyncio.sleep(0.2)

asyncio.run(main(sys.argv[1], sys.argv[2:]))
'''

LIST_WRITER_NOTES = (
    "import json,os,re;found=set()\n"
    "for root,_,names in os.walk('/data/vault'):\n"
    "    for n in names:\n"
    "        if n.endswith('.md'):\n"
    "            found.update(int(m) for m in re.findall(r'spike-writer-([0-9]{5})', open(os.path.join(root,n),errors='replace').read()))\n"
    "print(json.dumps(sorted(found)))"
)
BULK = (
    "import os,sys;d='/data/spike-bulk';os.makedirs(d,exist_ok=True)\n"
    "for i in range(int(sys.argv[1])):\n"
    "    open('%s/f%04d.bin'%(d,i),'wb').write(os.urandom(1<<20))\n"
    "os.sync()"
)
REWRITE_FILES = (
    "import os\n"
    "for i in range(5):\n"
    "    open('/data/spike-bulk/f%04d.bin'%i,'wb').write(os.urandom(1<<20))\n"
    "os.sync()"
)
CHURN = (
    "import os,sys;p='/data/spike-bulk/churn.bin'\n"
    "f=open(p,'r+b' if os.path.exists(p) else 'wb')\n"
    "for _ in range(int(sys.argv[1])):\n"
    "    f.write(os.urandom(1<<20))\n"
    "f.flush();os.fsync(f.fileno())"
)


def _security() -> dict[str, Any]:
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001}


def _pod_security() -> dict[str, Any]:
    return {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
            "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}}


def pvc_doc(name: str, gib: int, storage_class: str, *, snapshot: str | None = None, namespace: str = NAMESPACE) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"], "storageClassName": storage_class,
        "resources": {"requests": {"storage": f"{gib}Gi"}},
    }
    if snapshot:
        spec["dataSourceRef"] = {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": snapshot}
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def snapshot_doc(name: str, pvc: str, namespace: str = NAMESPACE) -> dict[str, Any]:
    return {
        "apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"volumeSnapshotClassName": SNAPSHOT_CLASS, "source": {"persistentVolumeClaimName": pvc}},
    }


def cell_pod_doc(name: str, pvc: str, token: str) -> dict[str, Any]:
    """The cell's pod as cellctl renders it (init container, serving container, /tmp), without its policies."""

    env = [
        {"name": "EXOMEM_CLOUD_CELL", "value": "1"}, {"name": "EXOMEM_CLOUD_CELL_ID", "value": "spike"},
        {"name": "EXOMEM_CLOUD_CELL_TOKEN", "value": token},
        {"name": "EXOMEM_VAULT_PATH", "value": "/data/vault"}, {"name": "TMPDIR", "value": "/tmp"},
        {"name": "EXOMEM_LOG_DIR", "value": "/tmp/exomem-logs"},
    ]
    mounts = [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"}]
    return {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "spike-cell"}},
        "spec": {
            "automountServiceAccountToken": False, "restartPolicy": "Always", "securityContext": _pod_security(),
            "initContainers": [{
                "name": "cell-init", "image": CELL_IMAGE, "command": ["exomem", "cell-init"],
                "args": ["--vault", "/data/vault", "--json"], "env": env[:2] + [env[4]],
                "securityContext": _security(), "volumeMounts": mounts,
            }],
            "containers": [{
                "name": "exomem", "image": CELL_IMAGE,
                "command": ["exomem", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"],
                "ports": [{"name": "http", "containerPort": 8765}], "env": env,
                "resources": {"requests": {"cpu": "250m", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "3Gi"}},
                "securityContext": _security(), "volumeMounts": mounts,
                "readinessProbe": {"httpGet": {"path": "/health/ready", "port": "http"}, "periodSeconds": 5},
                "startupProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 10, "failureThreshold": 60},
            }],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc}}, {"name": "tmp", "emptyDir": {}}],
        },
    }


def shell_pod_doc(name: str, pvc: str, script: str, *, namespace: str = NAMESPACE, read_only: bool = False) -> dict[str, Any]:
    """A pod from the cell image running a shell script over one claim."""

    return {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "automountServiceAccountToken": False, "restartPolicy": "Never", "securityContext": _pod_security(),
            "containers": [{
                "name": "main", "image": CELL_IMAGE, "command": ["sh", "-c", script], "securityContext": _security(),
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
                "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": read_only}, {"name": "tmp", "mountPath": "/tmp"}],
            }],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc, "readOnly": read_only}}, {"name": "tmp", "emptyDir": {}}],
        },
    }


def backup_job_doc(name: str, pvc: str) -> dict[str, Any]:
    """cellctl's backup Job (same image, same restic call), against a local repository."""

    script = (
        "restic snapshots >/dev/null 2>&1 || restic init >/dev/null; "
        "restic backup --json /data/vault /data/host /data/spike-bulk > /tmp/backup.json && tail -n 1 /tmp/backup.json"
    )
    pod = shell_pod_doc(name, pvc, script, read_only=True)["spec"]
    pod["containers"][0]["env"] = [
        {"name": "RESTIC_REPOSITORY", "value": "/repo"}, {"name": "RESTIC_PASSWORD", "value": "spike"},
        {"name": "RESTIC_CACHE_DIR", "value": "/cache"},
    ]
    pod["containers"][0]["volumeMounts"] += [{"name": "repo", "mountPath": "/repo"}, {"name": "cache", "mountPath": "/cache"}]
    pod["volumes"] += [{"name": "repo", "hostPath": {"path": str(RESTIC_REPO), "type": "Directory"}}, {"name": "cache", "emptyDir": {}}]
    pod["restartPolicy"] = "Never"
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 900, "template": {"spec": pod}},
    }


class Spike:
    def __init__(self, kube: Kube, workdir: Path, report_path: Path) -> None:
        self.kube = kube
        self.workdir = workdir
        self.report_path = report_path
        self.findings: dict[str, dict[str, Any]] = {}
        self.pools: list[dict[str, Any]] = []
        self.setup: dict[str, Any] = {}
        self.token = secrets.token_urlsafe(32)
        self.started = time.time()
        self.seed_phrase = ""

    # --- recording ---------------------------------------------------------------------

    def record(self, key: str, item: str, observed: Any, command: str) -> None:
        self.findings[key] = {"item": item, "observed": observed, "command": command}
        print(f"[spike] FINDING {key}: {json.dumps(observed, sort_keys=True, default=str)} | command: {command}", flush=True)

    def unmeasured(self, key: str, item: str, reason: str) -> None:
        self.findings[key] = {"item": item, "observed": None, "could_not_measure": reason}
        print(f"[spike] FINDING {key}: COULD NOT MEASURE: {reason}", flush=True)

    def step(self, key: str, item: str, action: Callable[[], None]) -> None:
        started = time.monotonic()
        print(f"[spike] step {key}: start", flush=True)
        try:
            action()
        except Exception as error:  # noqa: BLE001 - recorded as the finding's reason
            traceback.print_exc()
            self.unmeasured(key, item, f"{type(error).__name__}: {str(error)[:600]}")
        print(f"[spike] step {key}: done in {time.monotonic() - started:.0f}s", flush=True)

    # --- cluster helpers ---------------------------------------------------------------

    def wait_snapshot(self, name: str, timeout: float = 300) -> dict[str, Any]:
        """Waits for readyToUse; returns when the snapshot was taken (CSI creationTime) and how long it took."""

        asked = time.time()
        self.kube.kubectl("wait", "--namespace", NAMESPACE, f"volumesnapshot/{name}",
                          "--for=jsonpath={.status.readyToUse}=true", f"--timeout={int(timeout)}s")
        ready = time.time()
        content = self.kube.json("get", "volumesnapshot", name, "--namespace", NAMESPACE)["status"]["boundVolumeSnapshotContentName"]
        created_ns = self.kube.json("get", "volumesnapshotcontent", content)["status"]["creationTime"]
        return {"created_at": created_ns / 1e9, "ready_seconds": round(ready - asked, 2), "content": content}

    def wait_bound(self, pvc: str, timeout: float = 300) -> str:
        self.kube.kubectl("wait", "--namespace", NAMESPACE, f"pvc/{pvc}", "--for=jsonpath={.status.phase}=Bound",
                          f"--timeout={int(timeout)}s")
        return self.kube.json("get", "pvc", pvc, "--namespace", NAMESPACE)["spec"]["volumeName"]

    def wait_ready(self, pod: str, timeout: float = 900) -> None:
        try:
            self.kube.kubectl("wait", "--namespace", NAMESPACE, f"pod/{pod}", "--for=condition=Ready", f"--timeout={int(timeout)}s")
        except Exception:
            print(self.kube.kubectl("describe", "pod", pod, "--namespace", NAMESPACE, check=False).stdout[-3000:], flush=True)
            raise

    def cell_exec(self, pod: str, *command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.kube.exec(NAMESPACE, pod, *command, container="exomem", check=check)

    def probe(self, pod: str, mode: str, *args: str) -> dict[str, Any]:
        out = self.cell_exec(pod, "python3", "-c", CELL_PROBE, mode, *args).stdout.strip().splitlines()
        return json.loads(out[-1])

    def delete_claim(self, *claims: str) -> None:
        for claim in claims:
            self.kube.delete("pvc", claim, "--namespace", NAMESPACE)

    def release_clone(self, claim: str) -> None:
        """Deletes a claim and waits for its PV, which is when the logical volume is released."""

        volume = self.kube.json("get", "pvc", claim, "--namespace", NAMESPACE)["spec"].get("volumeName")
        self.delete_claim(claim)
        if volume:
            self.kube.kubectl("wait", "--for=delete", f"pv/{volume}", "--timeout=180s", check=False)

    def wait_volumes(self, expected: int) -> None:
        """Waits until the pool holds `expected` thin volumes: snapshots and clones are removed asynchronously."""

        wait_for(lambda: storage.read_pool_usage().thin_volumes == expected, timeout=180, interval=2,
                 description=f"the pool to hold {expected} thin volumes")

    def delete_snapshot(self, name: str) -> None:
        self.kube.delete("volumesnapshot", name, "--namespace", NAMESPACE)

    # --- pool observation ----------------------------------------------------------------

    def node_free_annotation(self) -> int:
        annotations = self.kube.json("get", "node", NODE_NAME)["metadata"].get("annotations", {})
        return int(annotations[f"capacity.topolvm.io/{DEVICE_CLASS}"])

    def csi_capacities(self) -> list[dict[str, Any]]:
        items = self.kube.json("get", "csistoragecapacities", "--all-namespaces")["items"]
        return [
            {"storage_class": item["storageClassName"], "capacity_bytes": storage.parse_quantity(item["capacity"]),
             "maximum_volume_bytes": storage.parse_quantity(item["maximumVolumeSize"]) if "maximumVolumeSize" in item else None,
             "node_topology": item.get("nodeTopology", {}).get("matchLabels")}
            for item in items if item["storageClassName"] in (CELL_CLASS, IMMEDIATE_CLASS)
        ]

    def plugin_metrics(self) -> dict[str, dict[str, float]]:
        pods = self.kube.json("get", "pods", "--namespace", TOPOLVM_NAMESPACE, "--selector=app.kubernetes.io/component=node")["items"]
        ip = pods[0]["status"]["podIP"]
        with urllib.request.urlopen(f"http://{ip}:8080/metrics", timeout=15) as response:
            return storage.parse_thinpool_metrics(response.read().decode())

    def observe_pool(self, label: str, *, settle: bool = True) -> dict[str, Any]:
        """LVM's own numbers, and what TopoLVM publishes, side by side."""

        free = self.node_free_annotation()
        if settle:
            # The plugin republishes on its own cycle: wait for two equal reads.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                time.sleep(5)
                again = self.node_free_annotation()
                if again == free:
                    break
                free = again
        usage = storage.read_pool_usage()
        observation = {
            "label": label, "at": round(time.time() - self.started, 1),
            "lvm": {"pool_bytes": usage.pool_bytes, "data_percent": usage.data_percent,
                    "metadata_percent": usage.metadata_percent, "metadata_bytes": usage.metadata_bytes,
                    "thin_volumes": usage.thin_volumes, "virtual_bytes": usage.virtual_bytes},
            "published_free_bytes": free,
        }
        self.pools.append(observation)
        print(f"[spike] pool {label}: {json.dumps(observation, sort_keys=True)}", flush=True)
        return observation

    # --- the measurements ------------------------------------------------------------------

    def capacity_publication(self) -> None:
        wait_for(lambda: self.csi_capacities(), timeout=180, interval=5, description="TopoLVM's CSIStorageCapacity objects")
        usage = storage.read_pool_usage()
        annotations = {
            key: value for key, value in self.kube.json("get", "node", NODE_NAME)["metadata"].get("annotations", {}).items()
            if "topolvm" in key
        }
        csinode = next(d for d in self.kube.json("get", "csinode", NODE_NAME)["spec"]["drivers"] if d["name"] == TOPOLVM_DRIVER)
        self.record(
            "csinode_allocatable", "does CSINode carry an allocatable count",
            {"driver_entry": csinode, "has_allocatable_count": "count" in (csinode.get("allocatable") or {})},
            f"kubectl get csinode {NODE_NAME} -o json | jq '.spec.drivers[] | select(.name==\"{TOPOLVM_DRIVER}\")'",
        )
        metrics = self.plugin_metrics().get(DEVICE_CLASS, {})
        self.record(
            "published_capacity", "pool size and free capacity TopoLVM publishes per node",
            {
                "lvm_pool_bytes": usage.pool_bytes, "lvm_metadata_bytes": usage.metadata_bytes,
                "node_annotations": annotations,
                "csi_storage_capacity": self.csi_capacities(),
                "node_plugin_metrics": metrics,
                "pool_size_published_as": [name for name in metrics if name.endswith("_size_bytes")],
            },
            f"kubectl get node {NODE_NAME} -o json (annotations); kubectl get csistoragecapacities -A -o json; "
            "GET http://<topolvm-node pod>:8080/metrics; sudo lvs -a --units b",
        )

    def start_cell(self) -> None:
        self.kube.apply(
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
            {"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshotClass", "metadata": {"name": SNAPSHOT_CLASS},
             "driver": TOPOLVM_DRIVER, "deletionPolicy": "Delete"},
        )
        self.kube.apply(pvc_doc("cell-data", CELL_GIB, CELL_CLASS), cell_pod_doc("cell-0", "cell-data", self.token))
        started = time.monotonic()
        self.wait_ready("cell-0")
        self.setup["cell_ready_seconds"] = round(time.monotonic() - started, 1)
        self.cell_exec("cell-0", "python3", "-c", BULK, str(BULK_FILES))
        self.cell_exec("cell-0", "python3", "-c", CHURN, "16")
        self.seed_phrase = f"ultramarine-{secrets.token_hex(4)}-heron"
        error = self.probe("cell-0", "write", f"Seed {self.seed_phrase}", self.seed_phrase)["error"]
        if error:
            raise RuntimeError(f"the seed write failed: {error}")
        sizes = self.cell_exec("cell-0", "du", "-sb", "/data/vault", "/data/host", "/data/spike-bulk").stdout
        self.setup["vault_bytes"] = sizes.split()

    def round_trip(self) -> None:
        self.observe_pool("cell running")
        log = self.workdir / "writer.log"
        with log.open("wb") as out:
            writer = subprocess.Popen(
                ["k3s", "kubectl", "exec", "--namespace", NAMESPACE, "cell-0", "--container", "exomem", "--",
                 "python3", "-c", CELL_PROBE, "writer", "60"],
                stdout=out, stderr=subprocess.STDOUT, env=self.kube.env,
            )
            wait_for(lambda: log.read_text().count('"i"') >= 8, timeout=180, interval=1, description="the writer's first acks")
            asked = time.time()
            self.kube.apply(snapshot_doc("rt-snap", "cell-data"))
            snapshot = self.wait_snapshot("rt-snap")
            snapshot["asked_at"] = asked
            time.sleep(6)  # writes continue after the snapshot
            writer.wait(timeout=240)
        acks = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
        taken = snapshot["created_at"]
        self.observe_pool("snapshot ready")

        self.kube.apply(pvc_doc("cell-restored", CELL_GIB, CELL_CLASS, snapshot="rt-snap"), cell_pod_doc("cell-restored-0", "cell-restored", self.token))
        started = time.monotonic()
        self.wait_ready("cell-restored-0")
        restore_ready = round(time.monotonic() - started, 1)
        self.observe_pool("snapshot and restored clone")

        present = set(json.loads(self.cell_exec("cell-restored-0", "python3", "-c", LIST_WRITER_NOTES).stdout.strip().splitlines()[-1]))
        before = [w for w in acks if w["t_ack"] < taken and not w["error"]]
        after = [w for w in acks if w["t_start"] > taken and not w["error"]]
        missing = [w for w in before if w["i"] not in present]
        newer = [w for w in acks if w["t_start"] > taken and w["i"] in present]
        highest = max(present) if present else None
        recall_seed = self.seed_phrase in self.probe("cell-restored-0", "recall", self.seed_phrase)["recall"]
        recall_latest = (
            f"spike-writer-{highest:05d}" in self.probe("cell-restored-0", "recall", f"spike-writer-{highest:05d}")["recall"]
            if highest is not None else None
        )
        status = ("exomem", "governance-schema", "status", "--vault", "/data/vault", "--json")
        source = self.cell_exec("cell-0", *status, check=False)
        restored = self.cell_exec("cell-restored-0", *status, check=False)
        write = self.probe("cell-restored-0", "write", "Written after restore", "spike-after-restore")
        self.record(
            "round_trip_midwrite", "snapshot while the cell writes, restored, then recall / governance status / governed write",
            {
                "writer_acks_before_snapshot": len(before), "writer_acks_after_snapshot": len(after),
                "snapshot_ready_seconds": snapshot["ready_seconds"],
                "snapshot_taken_seconds_after_request": round(taken - snapshot["asked_at"], 2),
                "restored_cell_ready_seconds": restore_ready,
                "writes_present_in_restored": len(present),
                "acked_before_snapshot_but_missing": [
                    {"i": w["i"], "acked_seconds_before_snapshot": round(taken - w["t_ack"], 2)} for w in missing
                ],
                "started_after_snapshot_but_present": [w["i"] for w in newer],
                "recall_of_seed_note": recall_seed, "recall_of_latest_present_write": recall_latest,
                "governance_status_matches_source": (source.returncode, source.stdout) == (restored.returncode, restored.stdout),
                "governance_status": {"source_rc": source.returncode, "restored_rc": restored.returncode},
                "governed_write_on_restored_error": write["error"],
            },
            "kubectl exec cell-0 -- python3 <writer, MCP remember in a loop>; VolumeSnapshot rt-snap; PVC cell-restored dataSourceRef rt-snap; "
            "kubectl exec cell-restored-0 -- python3 <ask_memory / remember>; exomem governance-schema status --vault /data/vault --json",
        )

    def pool_accounting_and_gate(self) -> None:
        by_label = {p["label"]: p for p in self.pools}
        cell, snap, clone = (by_label[k] for k in ("cell running", "snapshot ready", "snapshot and restored clone"))
        self.record(
            "pool_accounting", "how a snapshot and its clone count against the pool at ratio 1.0",
            {
                "pool_bytes": cell["lvm"]["pool_bytes"], "volume_gib": CELL_GIB,
                "cell_only": {"thin_volumes": cell["lvm"]["thin_volumes"], "virtual_bytes": cell["lvm"]["virtual_bytes"],
                              "published_free_bytes": cell["published_free_bytes"]},
                "plus_snapshot": {"thin_volumes": snap["lvm"]["thin_volumes"], "virtual_bytes": snap["lvm"]["virtual_bytes"],
                                  "published_free_bytes": snap["published_free_bytes"]},
                "plus_clone": {"thin_volumes": clone["lvm"]["thin_volumes"], "virtual_bytes": clone["lvm"]["virtual_bytes"],
                               "published_free_bytes": clone["published_free_bytes"]},
                "free_drop_for_snapshot_bytes": cell["published_free_bytes"] - snap["published_free_bytes"],
                "free_drop_for_clone_bytes": snap["published_free_bytes"] - clone["published_free_bytes"],
            },
            f"sudo lvs -a --units b (thin volumes, virtual size sum) and node annotation capacity.topolvm.io/{DEVICE_CLASS}, "
            "read after the cell, after the snapshot, after the restored clone",
        )
        free = clone["published_free_bytes"]
        lag_started = time.monotonic()
        converged = None
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            published = [c["capacity_bytes"] for c in self.csi_capacities() if c["storage_class"] == CELL_CLASS]
            if published and published[0] == free:
                converged = round(time.monotonic() - lag_started, 1)
                break
            time.sleep(5)
        over = math.floor(free / GIB) + 1
        self.kube.apply(pvc_doc("over", over, CELL_CLASS), shell_pod_doc("over", "over", "sleep 600"))
        time.sleep(25)
        condition = next(
            (c for c in self.kube.json("get", "pod", "over", "--namespace", NAMESPACE)["status"].get("conditions", [])
             if c["type"] == "PodScheduled"), {},
        )
        self.kube.delete("pod", "over", "--namespace", NAMESPACE)
        self.delete_claim("over")
        fit = math.floor(free / GIB)
        self.kube.apply(pvc_doc("fit", fit, CELL_CLASS), shell_pod_doc("fit", "fit", "sleep 600"))
        fit_started = time.monotonic()
        try:
            self.kube.kubectl("wait", "--namespace", NAMESPACE, "pod/fit", "--for=condition=Ready", "--timeout=180s")
            fits = True
        except Exception:  # noqa: BLE001 - the answer is the finding
            fits = False
        self.kube.delete("pod", "fit", "--namespace", NAMESPACE)
        self.delete_claim("fit")
        self.record(
            "capacity_gate", "a claim larger than the published free capacity is refused, one that fits is served",
            {
                "published_free_bytes": free, "csi_storage_capacity_matched_annotation_after_seconds": converged,
                "oversize_claim_gib": over, "oversize_pod_scheduled": condition.get("status"),
                "oversize_message": condition.get("message"), "fitting_claim_gib": fit, "fitting_pod_ready": fits,
                "fitting_pod_seconds": round(time.monotonic() - fit_started, 1),
            },
            f"PVC of {over}Gi and of {fit}Gi on {CELL_CLASS} with a sleeping pod; kubectl get pod -o json .status.conditions",
        )
        self.kube.delete("pod", "cell-restored-0", "--namespace", NAMESPACE)
        self.delete_claim("cell-restored")
        self.delete_snapshot("rt-snap")

    def clone_pin(self) -> None:
        self.kube.apply(snapshot_doc("pin-snap", "cell-data"))
        self.wait_snapshot("pin-snap")
        self.kube.apply(pvc_doc("pin-wait", CELL_GIB, CELL_CLASS, snapshot="pin-snap"),
                        pvc_doc("pin-now", CELL_GIB, IMMEDIATE_CLASS, snapshot="pin-snap"))
        volume = self.wait_bound("pin-now", 180)
        time.sleep(10)
        deferred = self.kube.json("get", "pvc", "pin-wait", "--namespace", NAMESPACE)
        pv = self.kube.json("get", "pv", volume)
        affinity = pv["spec"].get("nodeAffinity")
        self.record(
            "clone_pv_pinned", "whether the clone's PV is pinned to the source's node",
            {
                "immediate_clone_pv": volume, "node_affinity": affinity, "volume_attributes": pv["spec"]["csi"].get("volumeAttributes"),
                "wait_for_first_consumer_clone_phase": deferred["status"]["phase"],
                "wait_for_first_consumer_clone_has_pv": bool(deferred["spec"].get("volumeName")),
                "source_node": NODE_NAME,
                "nodes_in_cluster": 1,
                "note": "single-node cluster: the PV's nodeAffinity is observed, scheduling across nodes is not exercised",
            },
            "kubectl get pv <immediate clone PV> -o json (.spec.nodeAffinity); kubectl get pvc pin-wait -o json (a clone on the "
            "WaitForFirstConsumer class stays Pending with no PV until a pod is scheduled)",
        )
        self.delete_claim("pin-wait", "pin-now")
        self.delete_snapshot("pin-snap")

    def backup_cycle(self, label: str) -> dict[str, Any]:
        began = time.monotonic()
        volumes_before = storage.read_pool_usage().thin_volumes
        snap, clone, job = f"{label}-snap", f"{label}-clone", f"{label}-job"
        self.kube.apply(snapshot_doc(snap, "cell-data"))
        snapshot = self.wait_snapshot(snap)
        snapshot_done = time.monotonic()
        self.kube.apply(pvc_doc(clone, CELL_GIB, CELL_CLASS, snapshot=snap), backup_job_doc(job, clone))
        try:
            self.kube.kubectl("wait", "--namespace", NAMESPACE, f"job/{job}", "--for=condition=complete", "--timeout=900s")
        except Exception:
            print(self.kube.kubectl("logs", f"job/{job}", "--namespace", NAMESPACE, check=False).stdout[-2000:], flush=True)
            raise
        job_done = time.monotonic()
        pod = self.kube.json("get", "pods", "--namespace", NAMESPACE, f"--selector=job-name={job}")["items"][0]
        state = pod["status"]["containerStatuses"][0]["state"]["terminated"]
        summary = json.loads(self.kube.kubectl("logs", f"job/{job}", "--namespace", NAMESPACE).stdout.strip().splitlines()[-1])
        self.kube.delete("job", job, "--namespace", NAMESPACE)
        self.release_clone(clone)
        self.delete_snapshot(snap)
        self.wait_volumes(volumes_before)
        cleaned = time.monotonic()
        started = calendar.timegm(time.strptime(state["startedAt"], "%Y-%m-%dT%H:%M:%SZ"))
        finished = calendar.timegm(time.strptime(state["finishedAt"], "%Y-%m-%dT%H:%M:%SZ"))
        return {
            "cycle": label, "snapshot_ready_seconds": round(snapshot_done - began, 1),
            "clone_bind_mount_and_job_seconds": round(job_done - snapshot_done, 1),
            "restic_container_seconds": round(finished - started, 1),
            "restic_total_duration_seconds": round(summary.get("total_duration", 0.0), 2),
            "restic_files_new": summary.get("files_new"), "restic_files_changed": summary.get("files_changed"),
            "restic_files_unmodified": summary.get("files_unmodified"), "restic_data_added_bytes": summary.get("data_added"),
            "cleanup_seconds": round(cleaned - job_done, 1), "whole_cycle_seconds": round(cleaned - began, 1),
            "snapshot_taken_at": snapshot["created_at"],
        }

    def incremental_backup(self) -> None:
        sudo = storage.sudo
        sudo("mkdir", "-p", str(RESTIC_REPO))
        sudo("chmod", "0777", str(RESTIC_REPO))
        cycles = [self.backup_cycle("full")]
        self.cell_exec("cell-0", "python3", "-c", REWRITE_FILES)
        self.probe("cell-0", "write", "Backup cycle two", f"spike-writer-{90001:05d}")
        cycles.append(self.backup_cycle("incremental-small"))
        self.cell_exec("cell-0", "python3", "-c", CHURN, str(CHURN_MIB))
        cycles.append(self.backup_cycle("incremental-churn"))
        self.record(
            "incremental_backup", "incremental restic backup time on a clone, which sets per-node backup concurrency",
            {"vault_bytes_du": self.setup.get("vault_bytes"), "bulk_files": BULK_FILES, "changes": {
                "incremental-small": "5 x 1 MiB rewritten, 1 note", "incremental-churn": f"{CHURN_MIB} MiB rewritten"},
             "repository": "local restic repository on the runner's disk", "cycles": cycles},
            "VolumeSnapshot, PVC dataSourceRef clone, Job running `restic backup --json /data/vault /data/host /data/spike-bulk` "
            "against a hostPath repository; times from the objects' timestamps and restic's summary line",
        )

    def metadata_churn(self) -> None:
        series = [{"cycle": 0, **self.observe_pool("churn start", settle=False)["lvm"]}]
        cycles = []
        for index in range(1, CHURN_CYCLES + 1):
            began = time.monotonic()
            self.cell_exec("cell-0", "python3", "-c", CHURN, str(CHURN_MIB))
            snap, clone = f"c{index}-snap", f"c{index}-clone"
            self.kube.apply(snapshot_doc(snap, "cell-data"))
            self.wait_snapshot(snap)
            self.kube.apply(pvc_doc(clone, CELL_GIB, IMMEDIATE_CLASS, snapshot=snap))
            self.wait_bound(clone)
            held = storage.read_pool_usage()
            self.release_clone(clone)
            self.delete_snapshot(snap)
            self.wait_volumes(series[0]["thin_volumes"])
            after = storage.read_pool_usage()
            cycles.append({
                "cycle": index, "seconds": round(time.monotonic() - began, 1),
                "metadata_percent_with_snapshot_and_clone": held.metadata_percent, "data_percent_with_snapshot_and_clone": held.data_percent,
                "thin_volumes_with": held.thin_volumes, "metadata_percent_after_delete": after.metadata_percent,
                "data_percent_after_delete": after.data_percent, "thin_volumes_after": after.thin_volumes,
            })
            print(f"[spike] churn cycle {json.dumps(cycles[-1])}", flush=True)
        end = self.observe_pool("churn end", settle=False)
        peak = max(c["metadata_percent_with_snapshot_and_clone"] for c in cycles)
        self.record(
            "metadata_churn", "thin-pool metadata use across 24 simulated hourly snapshot cycles",
            {
                "cycles": CHURN_CYCLES, "rewritten_per_cycle_mib": CHURN_MIB, "metadata_bytes": end["lvm"]["metadata_bytes"],
                "chunk_size": "64k", "metadata_percent_start": series[0]["metadata_percent"],
                "metadata_percent_peak": peak, "metadata_percent_end": end["lvm"]["metadata_percent"],
                "thin_volumes_start": series[0]["thin_volumes"], "thin_volumes_end": end["lvm"]["thin_volumes"],
                "data_percent_start": series[0]["data_percent"], "data_percent_end": end["lvm"]["data_percent"],
                "per_cycle": cycles,
            },
            "per cycle: rewrite 128 MiB in place in the cell, VolumeSnapshot, clone PVC (Immediate class), sudo lvs -a "
            "(metadata_percent), delete clone and snapshot; the cycles run back to back, not an hour apart",
        )

    def readopt(self) -> None:
        kube, ns = self.kube, "spike-readopt"
        marker = f"readopt-{secrets.token_hex(6)}"
        kube.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}})
        kube.apply(pvc_doc("adopt", 1, CELL_CLASS, namespace=ns),
                   shell_pod_doc("writer", "adopt", f"echo {marker} > /data/marker && sync && echo written", namespace=ns))
        kube.kubectl("wait", "--namespace", ns, "pod/writer", "--for=jsonpath={.status.phase}=Succeeded", "--timeout=300s")
        volume = kube.json("get", "pvc", "adopt", "--namespace", ns)["spec"]["volumeName"]
        pv = kube.json("get", "pv", volume)
        lv = kube.json("get", "logicalvolume", volume)
        old_uid, old_handle = lv["metadata"]["uid"], pv["spec"]["csi"]["volumeHandle"]
        old_name = lv["status"]["volumeID"]  # the host logical volume's name

        def host_lvs() -> list[str]:
            return storage.sudo("lvs", "--noheadings", "-o", "lv_name", storage.VG_NAME).stdout.split()

        before = host_lvs()
        kube.delete("pod", "writer", "--namespace", ns)
        kube.kubectl("patch", "pv", volume, "--type=merge", "-p", '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}')
        kube.delete("pvc", "adopt", "--namespace", ns)
        # What an etcd restore leaves: the volume on the host, the objects gone. The finalizer is removed
        # first, or deleting the LogicalVolume would remove the volume with it.
        kube.kubectl("patch", "logicalvolume", volume, "--type=merge", "-p", '{"metadata":{"finalizers":null}}')
        kube.delete("logicalvolume", volume)
        kube.delete("pv", volume)
        orphaned = host_lvs()

        fresh = {"apiVersion": lv["apiVersion"], "kind": "LogicalVolume", "metadata": {"name": volume, "labels": lv["metadata"].get("labels", {})},
                 "spec": lv["spec"]}
        kube.apply(fresh)
        new_uid = wait_for(lambda: kube.json("get", "logicalvolume", volume)["metadata"]["uid"], timeout=60, description="the new LogicalVolume")
        new_volume_id = wait_for(lambda: kube.json("get", "logicalvolume", volume).get("status", {}).get("volumeID"), timeout=120,
                                 interval=2, description="the new LogicalVolume to be created")
        naive = host_lvs()
        print("[spike] diag readopt " + json.dumps({"old_uid": old_uid, "old_volume_id": old_name, "new_uid": new_uid, "new_volume_id": new_volume_id,
                                                  "lv_status": lv.get("status", {}), "before": before, "orphaned": orphaned, "naive": naive}), flush=True)

        # The operator step: the old volume takes the name the recreated object was given.
        storage.sudo("lvremove", "--yes", f"{storage.VG_NAME}/{new_volume_id}")
        storage.sudo("lvrename", storage.VG_NAME, old_name, new_volume_id)
        claim = pvc_doc("adopt", 1, CELL_CLASS, namespace=ns)
        claim["spec"]["volumeName"] = volume
        new_pv = {
            "apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": volume},
            "spec": {**{k: v for k, v in pv["spec"].items() if k != "claimRef"},
                     "csi": {**pv["spec"]["csi"], "volumeHandle": new_volume_id},
                     "claimRef": {"namespace": ns, "name": "adopt"}},
        }
        kube.apply(new_pv, claim, shell_pod_doc("reader", "adopt", "cat /data/marker", namespace=ns))
        kube.kubectl("wait", "--namespace", ns, "pod/reader", "--for=jsonpath={.status.phase}=Succeeded", "--timeout=300s")
        read = kube.kubectl("logs", "pod/reader", "--namespace", ns).stdout.strip()
        self.record(
            "readopt_lv", "an existing logical volume re-adopted by recreating its LogicalVolume object and PV",
            {
                "recreating_object_and_pv_alone": {
                    "new_volume_id_equals_old": new_volume_id == old_name,
                    "old_volume_still_on_host": old_name in naive, "new_empty_volume_created": new_volume_id in naive,
                    "lv_name_is_object_uid": old_name == old_uid,
                },
                "old_uid": old_uid, "new_uid": new_uid, "old_volume_id": old_name, "new_volume_id": new_volume_id,
                "host_volumes": {"before": before, "after_losing_objects": orphaned, "after_recreating_object": naive},
                "with_operator_lvrename": {"marker_read_back": read == marker, "marker": marker},
                "csi_volume_handle": {"before_loss": old_handle, "after_readoption": new_volume_id, "unchanged": old_handle == new_volume_id},
            },
            "kubectl patch logicalvolume (finalizers null) + delete logicalvolume,pv; kubectl apply LogicalVolume (same spec); "
            "sudo lvremove <new volumeID>; sudo lvrename cells <old volumeID> <new volumeID>; kubectl apply PV (same spec, new volumeHandle) + PVC + reader pod",
        )

    # --- report --------------------------------------------------------------------------------

    def diagnostics(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        commands = {
            "pods.txt": ["get", "pods", "--all-namespaces", "-o", "wide"],
            "events.txt": ["get", "events", "--all-namespaces", "--sort-by=.lastTimestamp"],
            "logicalvolumes.txt": ["get", "logicalvolumes", "-o", "yaml"],
            "csistoragecapacities.txt": ["get", "csistoragecapacities", "-A", "-o", "yaml"],
        }
        for name, args in commands.items():
            (directory / name).write_text(self.kube.kubectl(*args, check=False).stdout, encoding="utf-8")
        for component in ("controller", "node", "lvmd"):
            logs = self.kube.kubectl(
                "logs", "--namespace", TOPOLVM_NAMESPACE, f"--selector=app.kubernetes.io/component={component}",
                "--all-containers", "--tail=300", "--prefix", check=False,
            )
            (directory / f"topolvm-{component}.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        (directory / "lvs.txt").write_text(storage.sudo("lvs", "-a", "-o", "+chunk_size,metadata_percent", check=False).stdout, encoding="utf-8")
        (directory / "dmesg.txt").write_text(run(["sudo", "dmesg", "--ctime"], check=False).stdout[-60000:], encoding="utf-8")
        for log in self.workdir.glob("*.log"):
            (directory / log.name).write_text(log.read_text(errors="replace")[-200000:], encoding="utf-8")

    def write_report(self) -> None:
        report = {"setup": self.setup, "findings": self.findings, "pool_observations": self.pools,
                  "seconds": round(time.time() - self.started, 1)}
        self.report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
        lines = ["| item | observed |", "|---|---|"]
        for key, finding in self.findings.items():
            value = finding.get("could_not_measure") and f"COULD NOT MEASURE: {finding['could_not_measure']}" or json.dumps(finding["observed"], sort_keys=True, default=str)[:600]
            lines.append(f"| {finding['item']} (`{key}`) | `{value}` |")
        self.report_path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


ITEMS = (
    "snapshot_controller_bundled", "round_trip_midwrite", "pool_accounting", "published_capacity", "csinode_allocatable",
    "clone_pv_pinned", "metadata_churn", "incremental_backup", "readopt_lv",
)


def run_spike(report_path: Path, workdir: Path, diagnostics: Path | None) -> int:
    workdir.mkdir(parents=True, exist_ok=True)
    kube = Kube()
    spike = Spike(kube, workdir, report_path)
    code = 0
    try:
        spike.setup["volume_group"] = storage.create_volume_group(Path("/mnt/spike"), image_gib=32, pool_gib=24, metadata_mib=128)
        storage.install_k3s(workdir)
        bundled = storage.bundled_snapshot_support(kube)
        spike.record(
            "snapshot_controller_bundled", "whether K3s bundles a CSI snapshot controller",
            {**bundled, "bundled": bool(bundled["snapshot_crds"] or bundled["snapshot_deployments"])},
            "k3s kubectl get crd | grep snapshot.storage.k8s.io; kubectl get deployments -A | grep snapshot; ls /var/lib/rancher/k3s/server/manifests "
            "(before installing anything)",
        )
        storage.install_snapshot_controller(kube, workdir)
        storage.install_topolvm(kube)
        spike.setup["versions"] = {
            "k3s": storage.tool_version("K3S_VERSION"), "topolvm_chart": storage.TOPOLVM_CHART_VERSION,
            "external_snapshotter": storage.SNAPSHOTTER_VERSION, "cell_image": CELL_IMAGE,
        }
    except Exception:  # noqa: BLE001 - setup failure is the run's failure; the report still carries what exists
        traceback.print_exc()
        code = 2
    else:
        spike.step("published_capacity", "capacity publication", spike.capacity_publication)
        spike.step("cell", "cell running on the local volume", spike.start_cell)
        if "cell" not in spike.findings:
            spike.step("round_trip_midwrite", "snapshot round trip while writing", spike.round_trip)
            spike.step("pool_accounting", "pool accounting and the capacity gate", spike.pool_accounting_and_gate)
            spike.step("clone_pv_pinned", "clone PV node pin", spike.clone_pin)
            spike.step("incremental_backup", "incremental backup on a clone", spike.incremental_backup)
            spike.step("metadata_churn", "thin-pool metadata over 24 cycles", spike.metadata_churn)
        spike.step("readopt_lv", "re-adopting a logical volume", spike.readopt)
    finally:
        if code == 0 or spike.setup:
            try:
                spike.diagnostics(diagnostics or workdir / "diagnostics")
            except Exception:  # noqa: BLE001 - diagnostics never mask the result
                traceback.print_exc()
        spike.write_report()
    missing = [key for key in ITEMS if key not in spike.findings]
    print("[spike] ---- findings ----", flush=True)
    for key in ITEMS:
        finding = spike.findings.get(key)
        state = "not reached" if finding is None else ("COULD NOT MEASURE" if finding.get("could_not_measure") else "measured")
        print(f"[spike] {key}: {state}", flush=True)
    if missing and code == 0:
        code = 2
    return code
