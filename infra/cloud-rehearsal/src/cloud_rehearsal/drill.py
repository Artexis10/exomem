"""`exomem-cloud-rehearsal local-storage-drill`: the node-loss drill of
move-cloud-cells-to-local-storage (tasks 4.1-4.3), and the rehearsal
evidence for tasks 2.3, 2.4 and 2.9.

What runs:
- drill_cluster's K3s server with embedded etcd and two agents, each with
  its own loop-device `cells` volume group;
- PostgreSQL holding cellctl's fixture schema of the control database's Cloud
  tables, the schema cellctl's own tests and live suite run against;
- the S3 double as the restic target;
- the platform chart rendered with `cellStorage.local.enabled`: TopoLVM, the
  snapshot controller, the cell classes, cellctl and its admission all come
  from that one render, applied as rendered apart from P3's cellctl overlay.

The checks run in order, each on the state the one before leaves:
1. 2.3: a serving cell on agent A is backed up three times, hourly holds
   made due by moving its row's `last_backup_at` back.
2. 4.1 and 2.4: one more write, then agent A and its disk are destroyed and
   the cell relocated onto agent B from its last backup.
3. 4.2: the backed-up cell started on an empty claim refuses to initialise.
4. 4.3: a relocation's restore is interrupted; the cell stays down, the
   retained volumes stay as they were, and the retried restore serves.
5. 2.9: the server is restored from an etcd snapshot older than a cell, and
   that cell is re-adopted by the runbook's steps.

Each check names the failure it exists to catch where it raises. Every value
in the report says where it was read.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import datetime as dt
import json
import os
import secrets
import shutil
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import boto3
from botocore.config import Config
from cellctl.k8s_client import OUT_OF_SERVICE_TAINT, VOLUME_LOST_ANNOTATION
from cellctl.manifests import (
    BACKUP_JOB_NAME,
    CLONE_CLAIM_NAME,
    HOLD_ANNOTATION,
    HOLD_STARTED_ANNOTATION,
    JOB_KIND_LABEL,
    JOB_KIND_RESTORE,
    SNAPSHOT_NAME,
    hold_job_name,
    namespace_name,
)
from cellctl.state import EMPTY_VOLUME_REFUSED, RESTORE_FAILED, SNAPSHOT_BACKUP
from cellctl.storage_config import LocalStorage

from . import build, drill_cluster, images, infra, platform, substrate
from .report import (
    BLOCKED,
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

SCHEMA = "exomem-local-storage-drill-report-v1"
LOCAL = LocalStorage()
FIXTURES = images.REPO_ROOT / "infra/cellctl/tests/fixtures"
# Migration 0056 references exomem_tenants(id), which an earlier Substrate
# migration owns; cellctl's conftest creates the same stand-in.
TENANTS_STANDIN_SQL = "CREATE TABLE exomem_tenants (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), status text NOT NULL DEFAULT 'active')"
SNAPSHOT_CLASS_API = "snapshot.storage.k8s.io/v1/VolumeSnapshotClass"
CELL_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
# docs/runbooks/cloud-node-loss.md, "After restoring etcd" steps 2 and 4.4.
LOCAL_ROWS_SQL = (
    "SELECT cell_id, volume_id, node FROM exomem_cloud_cells "
    "WHERE desired_state <> 'deleted' AND volume_id ~ '^[0-9a-f-]{36}$'"
)
RELEASE_IDENTITY_SQL = "UPDATE exomem_cloud_cells SET volume_id = NULL WHERE cell_id = $1 AND volume_id = $2"
# D3: an hourly backup is due 55 minutes after the last one.
MAKE_BACKUP_DUE_SQL = (
    "UPDATE exomem_cloud_cells SET last_backup_at = now() - interval '61 minutes' "
    "WHERE cell_id = $1 AND hold_kind IS NULL"
)

# Runs inside a cell on its own bearer: MCP over the pod's loopback.
CELL_PROBE = r'''
import asyncio, json, os, sys, time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

def structured(result):
    return getattr(result, "structuredContent", None) or getattr(result, "structured_content", None) or {}

async def main(mode, marker):
    headers = {"Authorization": "Bearer " + os.environ["EXOMEM_CLOUD_CELL_TOKEN"]}
    async with httpx.AsyncClient(headers=headers, timeout=180) as http, streamable_http_client(
        "http://127.0.0.1:8765/mcp", http_client=http
    ) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            if mode == "recall":
                result = structured(await session.call_tool("ask_memory", {"query": marker}))
                print(json.dumps({"recall": json.dumps(result)}))
                return
            body = marker + "\n\n## Observations\n\n- [drill] " + marker + " #drill\n"
            deadline = time.monotonic() + 120
            while True:
                result = structured(await session.call_tool(
                    "remember", {"title": "Drill " + marker, "content": body, "status": "draft"}))
                error = result.get("error")
                if not (isinstance(error, dict) and error.get("code") == "MUTATION_WARMING") or time.monotonic() > deadline:
                    print(json.dumps({"error": error}))
                    return
                await asyncio.sleep(max(0.1, error.get("retry_after_ms", 500) / 1000))

asyncio.run(main(sys.argv[1], sys.argv[2]))
'''
# Which drill markers the cell's vault holds, read from its notes on disk.
FIND_MARKERS = (
    "import json,os,re,sys;found=set()\n"
    "for root,_,names in os.walk('/data/vault'):\n"
    "    for name in names:\n"
    "        if name.endswith('.md'):\n"
    "            found.update(re.findall(sys.argv[1], open(os.path.join(root,name),errors='replace').read()))\n"
    "print(json.dumps(sorted(found)))"
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, dt.datetime) else value


@dataclass
class DrillReport:
    run_id: str
    started_at: str = field(default_factory=lambda: _now().isoformat())
    finished_at: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    adaptations: list[str] = field(default_factory=list)
    overlays: list[str] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    checks: list[StepRecord] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def passed(self) -> bool:
        return (
            bool(self.checks)
            and all(check.status == PASSED for check in self.checks)
            and all(stage.get("status") == "passed" for stage in self.stages.values())
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": {
                "all_checks_passed": self.passed(),
                "checks": {status: sum(1 for c in self.checks if c.status == status) for status in (PASSED, FAILED, BLOCKED)},
            },
            "inputs": self.inputs,
            "adaptations": self.adaptations,
            "overlays": self.overlays,
            "stages": self.stages,
            "checks": [asdict(check) for check in self.checks],
            "measurements": self.measurements,
            "notes": self.notes,
        }

    def write(self, path: Path) -> None:
        self.finished_at = _now().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2, default=_iso) + "\n", encoding="utf-8")


@dataclass
class Cell:
    label: str
    cell_id: str
    # label -> marker written into the vault
    markers: dict[str, str] = field(default_factory=dict)

    @property
    def namespace(self) -> str:
        return namespace_name(self.cell_id)


@dataclass
class Drill:
    cluster: drill_cluster.Cluster
    report: DrillReport
    cell_image: str
    storage_label: str
    cells: dict[str, Cell] = field(default_factory=dict)
    # PVs a relocation or an operator step set to Retain on a live agent.
    retained_pvs: list[str] = field(default_factory=list)

    @property
    def stack(self) -> infra.Stack:
        return self.cluster.stack

    # --- the cluster, as the operator ---------------------------------------------------

    def kubectl(self, *args: str, check: bool = True, input_text: str | None = None):
        return self.cluster.server.kubectl(*args, check=check, input_text=input_text)

    def kube_json(self, *args: str) -> dict[str, Any]:
        return json.loads(self.kubectl(*args, "--output=json").stdout)

    def maybe_json(self, *args: str) -> dict[str, Any] | None:
        result = self.kubectl(*args, "--output=json", check=False)
        return json.loads(result.stdout) if result.returncode == 0 else None

    def apply(self, *documents: dict[str, Any]) -> None:
        payload = "\n---\n".join(json.dumps(document) for document in documents)
        self.kubectl("apply", "--server-side", "--field-manager=drill-operator", "--filename=-", input_text=payload)

    def cell_pod(self, cell: Cell) -> dict[str, Any] | None:
        pods = self.kube_json("get", "pods", "--namespace", cell.namespace, f"--selector=!{JOB_KIND_LABEL}")["items"]
        live = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
        return live[0] if live else None

    def cell_exec(self, cell: Cell, *command: str, check: bool = True):
        pod = self.cell_pod(cell)
        if pod is None:
            raise StepFailure(f"cell {cell.label} has no pod to run {command[0]} in")
        return self.kubectl(
            "exec", "--namespace", cell.namespace, pod["metadata"]["name"], "--container", "exomem", "--", *command,
            check=check,
        )

    def write(self, cell: Cell, label: str) -> str:
        marker = f"drillmark-{self.report.run_id}-{cell.label}-{label}"
        result = json.loads(self.cell_exec(cell, "python3", "-c", CELL_PROBE, "write", marker).stdout.strip().splitlines()[-1])
        if result.get("error"):
            raise StepFailure(f"cell {cell.label} refused the governed write {label}: {result['error']}")
        cell.markers[label] = marker
        return marker

    def recalls(self, cell: Cell, label: str) -> bool:
        marker = cell.markers[label]
        result = json.loads(self.cell_exec(cell, "python3", "-c", CELL_PROBE, "recall", marker).stdout.strip().splitlines()[-1])
        return marker in result["recall"]

    def markers(self, cell: Cell) -> set[str]:
        """The labels of this cell's markers its vault holds on disk."""

        pattern = f"drillmark-{self.report.run_id}-{cell.label}-[a-z0-9-]+"
        found = set(json.loads(self.cell_exec(cell, "python3", "-c", FIND_MARKERS, pattern).stdout.strip().splitlines()[-1]))
        return {label for label, marker in cell.markers.items() if marker in found}

    def governance(self, cell: Cell) -> list[Any]:
        result = self.cell_exec(cell, "exomem", "governance-schema", "status", "--vault", "/data/vault", "--json", check=False)
        try:
            body: Any = json.loads(result.stdout)
        except ValueError:
            body = result.stdout.strip()[-300:]
        return [result.returncode, body]

    def accept(self, cell: Cell, *, recall: str, governance: list[Any] | None, write: str) -> dict[str, Any]:
        """Design D9 acceptance: recall of an earlier write, the same
        governance-schema status as before, and a governed write."""

        status = self.governance(cell)
        evidence = {"recall_found": self.recalls(cell, recall), "governance_schema_status": status}
        self.write(cell, write)
        evidence["governed_write"] = "committed"
        if not evidence["recall_found"]:
            raise StepFailure(f"cell {cell.label} does not recall its write {recall}")
        if governance is not None and status != governance:
            raise StepFailure(f"cell {cell.label}'s governance-schema status changed: {governance} -> {status}")
        return evidence

    def pause_cellctl(self) -> None:
        """Runbook "Restore etcd" step 6."""

        self.kubectl("--namespace", platform.CLOUD_NAMESPACE, "scale", "deployment", "cellctl", "--replicas=0")
        wait_for(
            lambda: not self.kube_json("get", "pods", "--namespace", platform.CLOUD_NAMESPACE, "--selector=app.kubernetes.io/name=cellctl")["items"],
            timeout=180, interval=2, description="cellctl to stop",
        )

    def resume_cellctl(self) -> None:
        self.kubectl("--namespace", platform.CLOUD_NAMESPACE, "scale", "deployment", "cellctl", "--replicas=1")

    def stop_cell(self, cell: Cell) -> None:
        self.kubectl("--namespace", cell.namespace, "scale", "statefulset", "--all", "--replicas=0")
        wait_for(lambda: self.cell_pod(cell) is None, timeout=180, interval=2, description=f"cell {cell.label}'s pod to stop")

    def retire_claim(self, cell: Cell) -> str | None:
        """Runbook "After restoring etcd" step 4.3: the claim goes, its volume
        stays. Returns the retained PV."""

        claim = self.maybe_json("get", "persistentvolumeclaim", "cell-data", "--namespace", cell.namespace)
        if claim is None:
            return None
        volume = claim["spec"].get("volumeName")
        if volume:
            self.kubectl("patch", "persistentvolume", volume, "--type=merge",
                         "--patch", '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}')
            self.retained_pvs.append(volume)
        self.kubectl("--namespace", cell.namespace, "delete", "persistentvolumeclaim", "cell-data", "--timeout=180s")
        if volume:
            wait_for(lambda: (self.kube_json("get", "persistentvolume", volume).get("status") or {}).get("phase") == "Released",
                     timeout=120, interval=2, description=f"PV {volume} to be released")
        return volume

    def claim_volume(self, cell: Cell) -> dict[str, Any]:
        claim = self.kube_json("get", "persistentvolumeclaim", "cell-data", "--namespace", cell.namespace)
        pv = self.kube_json("get", "persistentvolume", claim["spec"]["volumeName"])
        return {"pv": pv["metadata"]["name"], "volume_id": pv["spec"]["csi"]["volumeHandle"], "node": pinned_node(pv),
                "storage_class": pv["spec"].get("storageClassName")}

    def label_storage(self, agent: drill_cluster.Agent) -> None:
        """What the k3s role does from the server when an agent with a cell
        volume group joins; a kubelet cannot set this label on itself."""

        self.kubectl("label", "node", agent.name, self.storage_label, "--overwrite")
        wait_for(
            lambda: LOCAL.capacity_annotation in (self.kube_json("get", "node", agent.name)["metadata"].get("annotations") or {}),
            timeout=300, interval=3, description=f"TopoLVM to publish {agent.name}'s capacity",
        )

    def bucket(self, cell: Cell) -> dict[str, tuple[int, dt.datetime]]:
        store = self.stack.object_store
        client = boto3.client(
            "s3", endpoint_url=store.endpoint(from_host=True), aws_access_key_id=store.access_key,
            aws_secret_access_key=store.secret_key, region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}, proxies={}),
        )
        objects: dict[str, tuple[int, dt.datetime]] = {}
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=infra.BACKUP_BUCKET, Prefix=f"cells/{cell.cell_id}/"):
            for item in page.get("Contents", []):
                objects[item["Key"]] = (item["Size"], item["LastModified"])
        return objects

    def retained_state(self, agent: drill_cluster.Agent) -> dict[str, Any]:
        """Each retained PV on `agent`: its object, its LogicalVolume and its
        volume on disk with how much of it is written."""

        usage = volume_usage(agent)
        logical = {item["status"].get("volumeID"): item["metadata"]["name"]
                   for item in self.kube_json("get", "logicalvolumes.topolvm.io")["items"] if item.get("status")}
        state = {}
        for name in self.retained_pvs:
            pv = self.maybe_json("get", "persistentvolume", name)
            if pv is None or pinned_node(pv) != agent.name:
                continue
            handle = pv["spec"]["csi"]["volumeHandle"]
            state[name] = {
                "reclaim_policy": pv["spec"].get("persistentVolumeReclaimPolicy"),
                "phase": (pv.get("status") or {}).get("phase"),
                "logical_volume_object": logical.get(handle),
                "on_disk": usage.get(handle),
            }
        return state

    # --- the control database, as the operator ----------------------------------------------

    async def sql(self, query: str, *args: Any) -> str:
        connection = await asyncpg.connect(self.stack.postgres.dsn("substrate_owner", from_host=True))
        try:
            return await connection.execute(query, *args)
        finally:
            await connection.close()

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        connection = await asyncpg.connect(self.stack.postgres.dsn("substrate_owner", from_host=True))
        try:
            return [dict(row) for row in await connection.fetch(query, *args)]
        finally:
            await connection.close()

    async def row(self, cell: Cell) -> dict[str, Any]:
        rows = await self.fetch("SELECT * FROM exomem_cloud_cells WHERE cell_id = $1", cell.cell_id)
        return rows[0]

    async def wait_row(
        self, cell: Cell, predicate: Callable[[dict[str, Any]], bool], *, timeout: float, description: str,
        interval: float = 2.0, during: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            if during is not None:
                during()
            row = await self.row(cell)
            if predicate(row):
                return row
            if time.monotonic() > deadline:
                raise StepFailure(f"timed out after {timeout:.0f}s waiting for {description}; last row: {row_summary(row)}")
            await asyncio.sleep(interval)

    async def add_cell(self, label: str, *, owner: bool) -> Cell:
        cell = Cell(label=label, cell_id="".join(secrets.choice(CELL_ALPHABET) for _ in range(16)))
        tenant = uuid.uuid4()
        await self.sql("INSERT INTO exomem_tenants (id) VALUES ($1)", tenant)
        # D6: a local cell's default size; Substrate's default moves to it at the cutover (task 7.2).
        await self.sql(
            "INSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state, storage_gib, rollout_priority) "
            "VALUES ($1, $2, 'running', $3, $4)",
            cell.cell_id, tenant, LOCAL.default_cell_gib, 0 if owner else 1,
        )
        self.cells[label] = cell
        return cell


def serving(row: dict[str, Any]) -> bool:
    return row.get("observed_state") == "running" and bool(row.get("ready")) and row.get("hold_kind") is None


def row_summary(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("observed_state", "ready", "hold_kind", "last_error_code", "node", "volume_id", "last_backup_at",
            "last_backup_snapshot")
    return {key: _iso(row.get(key)) for key in keys}


def pinned_node(pv: dict[str, Any]) -> str | None:
    """The one node a local PV's node affinity names on TopoLVM's topology key."""

    terms = ((((pv.get("spec") or {}).get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms")) or []
    nodes = {
        value
        for term in terms
        for expression in term.get("matchExpressions") or []
        if expression.get("key") == LOCAL.topology_key and expression.get("operator") == "In"
        for value in expression.get("values") or []
    }
    return nodes.pop() if len(nodes) == 1 else None


def pod_ready(pod: dict[str, Any] | None) -> bool:
    return bool(pod) and any(
        c["type"] == "Ready" and c["status"] == "True" for c in (pod.get("status") or {}).get("conditions", [])  # type: ignore[union-attr]
    )


def container_times(pod: dict[str, Any]) -> dict[str, Any]:
    """The first container's terminated state, as Kubernetes recorded it."""

    for status in (pod.get("status") or {}).get("containerStatuses") or []:
        terminated = (status.get("state") or {}).get("terminated")
        if terminated:
            started = dt.datetime.fromisoformat(terminated["startedAt"].replace("Z", "+00:00"))
            finished = dt.datetime.fromisoformat(terminated["finishedAt"].replace("Z", "+00:00"))
            return {"exit_code": terminated.get("exitCode"), "started_at": terminated["startedAt"],
                    "finished_at": terminated["finishedAt"], "seconds": (finished - started).total_seconds()}
    return {}


def volume_usage(agent: drill_cluster.Agent) -> dict[str, dict[str, Any]]:
    """{volume: size and written share} from `lvs` on the agent."""

    out = drill_cluster.agent_shell(
        agent, f"lvs --reportformat json --units b --nosuffix -o lv_name,lv_size,data_percent {drill_cluster.VOLUME_GROUP}"
    ).stdout
    return {row["lv_name"]: {"bytes": int(row["lv_size"]), "data_percent": row["data_percent"]}
            for row in json.loads(out)["report"][0]["lv"]}


# --- checks ------------------------------------------------------------------------------


async def observe_backup(drill: Drill, cell: Cell, previous_snapshot: str | None) -> dict[str, Any]:
    """Follows one hourly hold of `cell` to its end: its snapshot, its clone's
    PV, its Job's pod, and whether the cell kept serving."""

    seen: dict[str, Any] = {"hold_started_at": None, "snapshot": None, "clone": None, "job": None,
                            "polls": 0, "polls_not_ready": 0}

    def look() -> None:
        statefulset = drill.maybe_json("get", "statefulset", "cell", "--namespace", cell.namespace) or {}
        annotations = statefulset.get("metadata", {}).get("annotations") or {}
        if annotations.get(HOLD_ANNOTATION) == SNAPSHOT_BACKUP:
            seen["hold_started_at"] = annotations.get(HOLD_STARTED_ANNOTATION)
        started = seen["hold_started_at"]
        if started:
            snapshot = drill.maybe_json("get", "volumesnapshot", hold_job_name(SNAPSHOT_NAME, started), "--namespace", cell.namespace)
            if snapshot and (snapshot.get("status") or {}).get("readyToUse"):
                seen["snapshot"] = {"name": snapshot["metadata"]["name"],
                                    "created_at": snapshot["status"].get("creationTime"),
                                    "content": snapshot["status"].get("boundVolumeSnapshotContentName")}
            clone = drill.maybe_json("get", "persistentvolumeclaim", hold_job_name(CLONE_CLAIM_NAME, started), "--namespace", cell.namespace)
            if clone and clone["spec"].get("volumeName") and seen["clone"] is None:
                pv = drill.maybe_json("get", "persistentvolume", clone["spec"]["volumeName"])
                if pv:
                    seen["clone"] = {"claim": clone["metadata"]["name"], "class": clone["spec"].get("storageClassName"),
                                     "pv": pv["metadata"]["name"], "pv_node": pinned_node(pv),
                                     "volume_id": pv["spec"]["csi"]["volumeHandle"]}
            job_name = hold_job_name(BACKUP_JOB_NAME, started)
            job = drill.maybe_json("get", "job", job_name, "--namespace", cell.namespace)
            pods = drill.kube_json("get", "pods", "--namespace", cell.namespace, f"--selector=batch.kubernetes.io/job-name={job_name}")["items"]
            if job and pods:
                template = job["spec"]["template"]["spec"]
                seen["job"] = {
                    "name": job_name, "pod": pods[0]["metadata"]["name"], "node": pods[0]["spec"].get("nodeName"),
                    "template_node_selector": template.get("nodeSelector"), "template_affinity": template.get("affinity"),
                    "template_node_name": template.get("nodeName"), "container": container_times(pods[0]),
                }
        seen["polls"] += 1
        seen["polls_not_ready"] += 0 if pod_ready(drill.cell_pod(cell)) else 1

    row = await drill.wait_row(
        cell, lambda r: r.get("last_backup_snapshot") not in (None, previous_snapshot) and r.get("hold_kind") is None,
        timeout=900, interval=1, during=look, description=f"an hourly backup of cell {cell.label} to finish",
    )
    look()
    seen["row"] = row_summary(row)
    return seen


async def check_hourly_backups(drill: Drill, record: StepRecord) -> None:
    """2.3: the hourly hold backs a serving cell up from a snapshot clone on
    its own node, and leaves no clone or snapshot behind."""

    x = await drill.add_cell("x", owner=True)
    agent_a = drill.cluster.agents["drill-agent-a"]
    row = await drill.wait_row(x, serving, timeout=900, description="cell X to serve")
    pod = drill.cell_pod(x)
    first_pod = {"uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
                 "restarts": sum(s.get("restartCount", 0) for s in pod["status"].get("containerStatuses", []))}
    record.evidence["cell"] = {"id": x.cell_id, "first_pod": first_pod, "volume": drill.claim_volume(x), "row": row_summary(row)}
    drill.write(x, "seed")

    backups = []
    previous = None  # the first backup is any backup at all
    for number in (1, 2, 3):
        if number > 1:
            # A write the next backup must hold, then the backup made due.
            drill.write(x, f"w{number - 1}")
            status = await drill.sql(MAKE_BACKUP_DUE_SQL, x.cell_id)
            if status != "UPDATE 1":
                raise StepFailure(f"moving cell X's last_backup_at back reported {status}")
        before = drill.bucket(x)
        observed = await observe_backup(drill, x, previous)
        after = drill.bucket(x)
        added = {key: value for key, value in after.items() if key not in before}
        times = sorted(value[1] for value in added.values())
        observed["bucket"] = {
            "objects_added": len(added), "bytes_added": sum(value[0] for value in added.values()),
            "first_to_last_object_seconds": (times[-1] - times[0]).total_seconds() if times else None,
        }
        observed["leftovers_gone_after_seconds"] = leftovers_gone(drill, x, agent_a, observed)
        backups.append(observed)
        previous = observed["row"]["last_backup_snapshot"]
    record.evidence["backups"] = backups

    pod = drill.cell_pod(x)
    last_pod = {"uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
                "restarts": sum(s.get("restartCount", 0) for s in pod["status"].get("containerStatuses", []))}
    record.evidence["last_pod"] = last_pod
    drill.report.measurements["backup_upload"] = [
        {
            "backup": number,
            "restic_container_seconds": (backup.get("job") or {}).get("container", {}).get("seconds"),
            "objects_added": backup["bucket"]["objects_added"],
            "bytes_added": backup["bucket"]["bytes_added"],
            "first_to_last_object_seconds": backup["bucket"]["first_to_last_object_seconds"],
            "source": "restic_container_seconds: the backup Job container's started and finished times (Kubernetes); "
            "objects and bytes: the S3 double's listing of the cell's prefix before and after; the S3 double "
            "runs on the same runner, so no WAN upload to B2 is in these numbers",
        }
        for number, backup in enumerate(backups, start=1)
    ]

    problems = []
    if last_pod["uid"] != first_pod["uid"] or last_pod["restarts"] != first_pod["restarts"]:
        problems.append(f"the cell's pod restarted during its backups: {first_pod} -> {last_pod}")
    # The first backup starts as soon as the cell serves and may finish
    # before it is watched; the two the drill made due must be seen whole.
    for number, backup in enumerate(backups, start=1):
        job, clone = backup.get("job"), backup.get("clone")
        if number == 1 and not (job and clone):
            continue
        if not job or not clone or not backup.get("snapshot"):
            problems.append(f"backup {number}: its snapshot, clone or Job was never seen")
            continue
        if job["template_node_selector"] or job["template_affinity"] or job["template_node_name"]:
            problems.append(f"backup {number}: the Job carries its own placement")
        if not (job["node"] == clone["pv_node"] == first_pod["node"]):
            problems.append(f"backup {number}: Job on {job['node']}, clone on {clone['pv_node']}, cell on {first_pod['node']}")
        if clone["class"] != LOCAL.clone_class:
            problems.append(f"backup {number}: the clone is on class {clone['class']}")
        if job["container"].get("exit_code") != 0:
            problems.append(f"backup {number}: the Job exited {job['container'].get('exit_code')}")
        if backup["polls_not_ready"]:
            problems.append(f"backup {number}: the cell read not ready on {backup['polls_not_ready']} of {backup['polls']} polls")
        if backup["leftovers_gone_after_seconds"] is None:
            problems.append(f"backup {number}: its clone or snapshot outlived the hold")
    if problems:
        raise StepFailure("; ".join(problems))


def leftovers_gone(drill: Drill, cell: Cell, agent: drill_cluster.Agent, backup: dict[str, Any]) -> float | None:
    """Seconds after the hold ended until its clone claim, clone PV, snapshot
    and their logical volumes were gone from the cluster and the disk; None
    if they outlived three minutes."""

    started = backup.get("hold_started_at")
    if not started:
        return None
    clone_pv = (backup.get("clone") or {}).get("pv")
    began = time.monotonic()

    def gone() -> bool:
        if drill.maybe_json("get", "persistentvolumeclaim", hold_job_name(CLONE_CLAIM_NAME, started), "--namespace", cell.namespace):
            return False
        if drill.maybe_json("get", "volumesnapshot", hold_job_name(SNAPSHOT_NAME, started), "--namespace", cell.namespace):
            return False
        if clone_pv and drill.maybe_json("get", "persistentvolume", clone_pv):
            return False
        cell_volume = drill.claim_volume(cell)["volume_id"]
        objects = {item["spec"]["nodeName"] + "/" + ((item.get("status") or {}).get("volumeID") or "")
                   for item in drill.kube_json("get", "logicalvolumes.topolvm.io")["items"] if item["spec"]["nodeName"] == agent.name}
        return objects == {f"{agent.name}/{cell_volume}"} and set(drill_cluster.thin_volumes(agent)) == {cell_volume}

    try:
        wait_for(gone, timeout=180, interval=2, description="the hold's clone and snapshot to go")
    except TimeoutError:
        return None
    return round(time.monotonic() - began, 1)


async def check_node_loss(drill: Drill, record: StepRecord) -> None:
    """4.1 and 2.4: a lost agent's cell is relocated from its last hourly
    backup onto another agent, only after the operator confirms the stop,
    and serves again with what that backup held."""

    x = drill.cells["x"]
    agent_a, agent_b = drill.cluster.agents["drill-agent-a"], drill.cluster.agents["drill-agent-b"]
    row = await drill.row(x)
    old = drill.claim_volume(x)
    if old["node"] != agent_a.name:
        raise StepFailure(f"cell X's volume is on {old['node']}, not {agent_a.name}")
    governance = drill.governance(x)
    drill.write(x, "last")  # after the last backup: expected lost
    before = drill.markers(x)
    restore_point = row["last_backup_at"]

    marks: dict[str, float] = {}
    began = time.monotonic()
    destroyed_at = _now()
    drill_cluster.destroy_agent(drill.cluster, agent_a)
    marks["agent_and_disk_destroyed"] = time.monotonic() - began
    # Runbook step 1: replacement capacity, here agent B's own cells disk.
    record.evidence["replacement_volume_group"] = drill_cluster.create_volume_group(agent_b)
    drill.label_storage(agent_b)
    marks["replacement_capacity_published"] = time.monotonic() - began
    # Step 2: the stop is confirmed: the node's container no longer exists.
    container_gone = run(["docker", "inspect", agent_a.container], check=False).returncode != 0
    wait_for(lambda: drill_cluster.node_ready(drill.cluster.server, agent_a.name) is False,
             timeout=300, interval=2, description=f"{agent_a.name} to read NotReady")
    marks["node_not_ready"] = time.monotonic() - began
    # Step 3: the runbook's taint.
    drill.kubectl("taint", "node", agent_a.name, f"{OUT_OF_SERVICE_TAINT}=nodeshutdown:NoExecute", "--overwrite")
    marks["out_of_service_taint"] = time.monotonic() - began

    restore: dict[str, Any] = {}

    def look() -> None:
        pods = drill.kube_json("get", "pods", "--namespace", x.namespace, f"--selector={JOB_KIND_LABEL}={JOB_KIND_RESTORE}")["items"]
        for pod in pods:
            restore.update({"pod": pod["metadata"]["name"], "node": pod["spec"].get("nodeName"), "container": container_times(pod)})

    await drill.wait_row(x, lambda r: r.get("hold_kind") == "restore", timeout=600, during=look,
                         description="cellctl to start cell X's relocation")
    marks["relocation_started"] = time.monotonic() - began
    row = await drill.wait_row(x, lambda r: serving(r) and r.get("node") == agent_b.name, timeout=1200, during=look,
                               description="cell X to serve on agent B")
    recovered = time.monotonic() - began
    marks["serving_on_replacement"] = recovered
    look()

    pod = drill.cell_pod(x)
    new = drill.claim_volume(x)
    old_pv = drill.maybe_json("get", "persistentvolume", old["pv"])
    record.evidence.update({
        "stop_confirmation": {"container_gone": container_gone, "taint": f"{OUT_OF_SERVICE_TAINT}=nodeshutdown:NoExecute"},
        "restore_job": restore,
        "new_pod": {"node": pod["spec"]["nodeName"], "created_at": pod["metadata"]["creationTimestamp"]},
        "old_volume": {**old, "reclaim_policy": (old_pv or {}).get("spec", {}).get("persistentVolumeReclaimPolicy"),
                       "phase": ((old_pv or {}).get("status") or {}).get("phase")},
        "new_volume": new,
        "row": row_summary(row),
        "marks_seconds": {name: round(value, 1) for name, value in marks.items()},
    })
    after = drill.markers(x)
    kept, lost = sorted(after & before), sorted(before - after)
    record.evidence["writes"] = {"before_loss": sorted(before), "after_relocation": sorted(after), "lost": lost}
    record.evidence["acceptance"] = drill.accept(x, recall="seed", governance=governance, write="after-relocation")

    drill.report.measurements["recovery_point"] = {
        "restored_backup_taken_at": _iso(restore_point),
        "agent_destroyed_at": destroyed_at.isoformat(),
        "seconds_of_writes_at_risk": round((destroyed_at - restore_point).total_seconds(), 1),
        "writes_kept": kept, "writes_lost": lost,
        "source": "restored_backup_taken_at: the row's last_backup_at, which cellctl records from the backup's "
        "VolumeSnapshot creationTime; agent_destroyed_at: the drill's clock at `docker rm`; writes: drill "
        "markers found in the vault's notes on disk before the loss and after the relocation",
    }
    drill.report.measurements["recovery_time"] = {
        "seconds": round(recovered, 1),
        "from": "agent A's container and disk destroyed (the drill's clock)",
        "to": "cellctl's row reads running and ready on agent B (control database)",
        "marks_seconds": {name: round(value, 1) for name, value in marks.items()},
        "note": "the drill confirms the stop and taints the node as soon as it reads NotReady, so no human "
        "latency is in this number; agent B's TopoLVM image was pulled before the loss",
    }

    problems = []
    if restore.get("node") != agent_b.name or new["node"] != agent_b.name or pod["spec"]["nodeName"] != agent_b.name:
        problems.append(f"restore on {restore.get('node')}, volume on {new['node']}, pod on {pod['spec']['nodeName']}")
    if (old_pv or {}).get("spec", {}).get("persistentVolumeReclaimPolicy") != "Retain":
        problems.append("the lost volume's PV is not retained")
    if new["volume_id"] == old["volume_id"] or row.get("volume_id") != new["volume_id"]:
        problems.append(f"the row records {row.get('volume_id')}, the new volume is {new['volume_id']}")
    finished = (restore.get("container") or {}).get("finished_at")
    if not finished or pod["metadata"]["creationTimestamp"] < finished:
        problems.append(f"the cell's pod ({pod['metadata']['creationTimestamp']}) did not start after the restore finished ({finished})")
    if lost != ["last"]:
        problems.append(f"expected only the write after the last backup lost, lost {lost}")
    if problems:
        raise StepFailure("; ".join(problems))
    drill.retained_pvs.append(old["pv"])


async def check_empty_vault_guard(drill: Drill, record: StepRecord) -> None:
    """4.2: a backed-up cell started on an empty claim refuses to create a
    vault, stays not ready, and reports why on its row. The empty claim comes
    from a re-adoption gone wrong: the claim retired and the identity
    released, but no PV made for the real volume."""

    x = drill.cells["x"]
    row = await drill.row(x)
    drill.pause_cellctl()
    drill.stop_cell(x)
    retained = drill.retire_claim(x)
    status = await drill.sql(RELEASE_IDENTITY_SQL, x.cell_id, row["volume_id"])
    if status != "UPDATE 1":
        raise StepFailure(f"releasing cell X's identity reported {status}")
    drill.resume_cellctl()
    refused = await drill.wait_row(x, lambda r: r.get("last_error_code") == EMPTY_VOLUME_REFUSED, timeout=600,
                                   description="cell X's row to report the empty-volume refusal")
    # It stays down: watched for a minute after the refusal.
    ready_seen = []
    for _ in range(12):
        current = await drill.row(x)
        pod = drill.cell_pod(x)
        ready_seen.append(bool(current.get("ready")) or pod_ready(pod))
        await asyncio.sleep(5)
    pod = drill.cell_pod(x) or {}
    init = next((s for s in (pod.get("status") or {}).get("initContainerStatuses", []) if s["name"] == "cell-init"), {})
    terminated = (init.get("lastState") or {}).get("terminated") or (init.get("state") or {}).get("terminated") or {}
    record.evidence.update({
        "row": row_summary(refused),
        "cell_init": {"message": terminated.get("message"), "exit_code": terminated.get("exitCode"),
                      "restarts": init.get("restartCount"), "waiting": (init.get("state") or {}).get("waiting", {}).get("reason")},
        "empty_volume": drill.claim_volume(x),
        "retained_volume": retained,
        "ready_in_the_minute_after": any(ready_seen),
    })
    if any(ready_seen):
        raise StepFailure("the cell read ready on an empty volume after its refusal")
    if terminated.get("message") != EMPTY_VOLUME_REFUSED:
        raise StepFailure(f"cell-init's termination message is {terminated.get('message')!r}")


async def check_interrupted_restore(drill: Drill, record: StepRecord) -> None:
    """4.3: a relocation whose restore is interrupted never starts the cell
    and leaves every retained volume as it was; its retried restore serves.
    The relocation is the runbook's for CELL_INIT_EMPTY_VOLUME_REFUSED: the
    empty claim retired and its volume marked lost."""

    x = drill.cells["x"]
    agent_b = drill.cluster.agents["drill-agent-b"]
    row = await drill.row(x)
    drill.pause_cellctl()
    drill.stop_cell(x)
    drill.retire_claim(x)
    drill.kubectl("annotate", "namespace", x.namespace, f"{VOLUME_LOST_ANNOTATION}={row['volume_id']}", "--overwrite")
    before = drill.retained_state(agent_b)
    object_store = drill.stack.object_store.container
    run(["docker", "pause", object_store])
    try:
        drill.resume_cellctl()
        selector = f"--selector={JOB_KIND_LABEL}={JOB_KIND_RESTORE}"

        def restore_running() -> dict[str, Any] | None:
            pods = drill.kube_json("get", "pods", "--namespace", x.namespace, selector)["items"]
            return next((p for p in pods if (p.get("status") or {}).get("phase") == "Running"), None)

        pod = wait_for(restore_running, timeout=600, interval=2, description="cell X's restore to start")
        await asyncio.sleep(5)
        drill.kubectl("delete", "pod", pod["metadata"]["name"], "--namespace", x.namespace, "--grace-period=5", "--wait=false")
        failed = await drill.wait_row(x, lambda r: r.get("last_error_code") == RESTORE_FAILED, timeout=600,
                                      description="cell X's row to report the failed restore")
        statefulset = drill.kube_json("get", "statefulset", "cell", "--namespace", x.namespace)
        after = drill.retained_state(agent_b)
        record.evidence.update({
            "interrupted_restore_pod": {"name": pod["metadata"]["name"], "node": pod["spec"].get("nodeName")},
            "row": row_summary(failed),
            "statefulset_replicas": statefulset["spec"].get("replicas"),
            "cell_pod": (drill.cell_pod(x) or {}).get("metadata", {}).get("name"),
            "retained_before": before, "retained_after": after,
        })
    finally:
        run(["docker", "unpause", object_store])
    problems = []
    if statefulset["spec"].get("replicas") != 0 or record.evidence["cell_pod"] or failed.get("ready"):
        problems.append("the cell started after an interrupted restore")
    if not before or after != before:
        problems.append("a retained volume changed, or none was found to compare")
    if any(state["reclaim_policy"] != "Retain" or not state["logical_volume_object"] or not state["on_disk"]
           for state in after.values()):
        problems.append("a retained volume lost its Retain policy, its LogicalVolume or its disk volume")
    if problems:
        raise StepFailure("; ".join(problems))

    # The failed Job expires after its TTL and cellctl runs the restore again.
    began = time.monotonic()
    await drill.wait_row(x, serving, timeout=1200, description="cell X to serve after its retried restore")
    record.evidence["retried_restore_serving_after_seconds"] = round(time.monotonic() - began, 1)
    record.evidence["writes"] = sorted(drill.markers(x))
    record.evidence["acceptance"] = drill.accept(x, recall="w2", governance=None, write="after-retry")
    drill.kubectl("annotate", "namespace", x.namespace, f"{VOLUME_LOST_ANNOTATION}-")
    if not {"seed", "w1", "w2"} <= set(record.evidence["writes"]):
        raise StepFailure(f"the retried restore holds {record.evidence['writes']}")


async def check_etcd_restore(drill: Drill, record: StepRecord) -> None:
    """2.9: after an etcd restore from a snapshot older than a cell, the
    runbook's listing and readopt report find that cell's volume, its
    re-adoption serves the cell's own data, and the identity check holds."""

    x = drill.cells["x"]
    agent_b = drill.cluster.agents["drill-agent-b"]
    await drill.wait_row(x, serving, timeout=900, description="cell X to serve before the etcd snapshot")
    snapshot = drill_cluster.etcd_snapshot(drill.cluster, "drill-before-cell-y")
    record.evidence["etcd_snapshot"] = snapshot

    y = await drill.add_cell("y", owner=False)
    await drill.wait_row(y, lambda r: serving(r) and r.get("last_backup_snapshot") is not None, timeout=1200,
                         description="cell Y to serve with its first backup")
    drill.write(y, "seed")
    drill.write(x, "after-snapshot")
    for cell in (x, y):
        await drill.wait_row(cell, lambda r: r.get("hold_kind") is None, timeout=900, description=f"cell {cell.label} to hold nothing")
    y_row = await drill.row(y)
    old_id = y_row["volume_id"]
    y_volume = drill.claim_volume(y)
    governance_y = drill.governance(y)

    record.evidence["reset"] = drill_cluster.restore_etcd(drill.cluster, snapshot)
    drill.pause_cellctl()
    record.evidence["rows_when_paused"] = {cell.label: row_summary(await drill.row(cell)) for cell in (x, y)}
    replicas = drill.kubectl("--namespace", platform.CLOUD_NAMESPACE, "get", "deployment", "cellctl",
                             "--output=jsonpath={.spec.replicas}").stdout.strip()
    if replicas != "0":
        raise StepFailure(f"cellctl's replicas read {replicas!r} after pausing")

    # Step 2: the three inputs. Agent A is destroyed, so it writes no listing.
    inputs = drill.stack.workdir / "readopt"
    volumes = inputs / "volumes"
    volumes.mkdir(parents=True, exist_ok=True)
    (volumes / f"{agent_b.name}.json").write_text(json.dumps(drill_cluster.list_volumes(agent_b)), encoding="utf-8")
    (inputs / "logicalvolumes.json").write_text(drill.kubectl("get", "logicalvolumes.topolvm.io", "-o", "json").stdout, encoding="utf-8")
    with (inputs / "rows.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([row["cell_id"], row["volume_id"], row["node"]] for row in await drill.fetch(LOCAL_ROWS_SQL))
    # Step 3: match them.
    report = json.loads(run(
        [sys.executable, "-m", "cellctl.readopt", "--lvs-dir", str(volumes), "--rows", str(inputs / "rows.csv"),
         "--logical-volumes", str(inputs / "logicalvolumes.json")],
    ).stdout)
    record.evidence["readopt_report"] = report
    if [y.cell_id, agent_b.name, old_id] not in report["readopt"] or x.cell_id not in report["in_place"]:
        raise StepFailure(f"readopt did not report cell Y to re-adopt and cell X in place: {report}")

    # Step 4.1: a LogicalVolume like a surviving one, for the PV to come.
    x_volume = drill.claim_volume(x)
    template = next(item for item in drill.kube_json("get", "logicalvolumes.topolvm.io")["items"]
                    if (item.get("status") or {}).get("volumeID") == x_volume["volume_id"])
    pv_name = f"pvc-{uuid.uuid4()}"
    size = drill_cluster.thin_volumes(agent_b)[old_id]
    logical = {"apiVersion": template["apiVersion"], "kind": "LogicalVolume", "metadata": {"name": pv_name},
               "spec": {**template["spec"], "name": pv_name, "nodeName": agent_b.name, "size": str(size)}}
    drill.apply(logical)
    new_id = wait_for(lambda: (drill.kube_json("get", "logicalvolumes.topolvm.io", pv_name).get("status") or {}).get("volumeID"),
                      timeout=180, interval=2, description="the new LogicalVolume's volume ID")
    # Step 4.2: on the node, the old volume takes the new one's name.
    drill_cluster.agent_shell(agent_b, f'lvremove --yes "cells/{new_id}" && lvrename cells "{old_id}" "{new_id}"')
    # Step 4.3: no claim survived in the namespace (it is newer than the
    # snapshot), so only the PV, like a surviving cell's, bound to Y's claim.
    surviving = drill.kube_json("get", "persistentvolume", x_volume["pv"])
    pv_spec = copy.deepcopy(surviving["spec"])
    pv_spec["csi"]["volumeHandle"] = new_id
    pv_spec["capacity"] = {"storage": str(size)}
    pv_spec["claimRef"] = {"namespace": y.namespace, "name": "cell-data"}
    drill.apply({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": pv_name}, "spec": pv_spec})
    # Step 4.4: release the old identity by compare-and-set.
    status = await drill.sql(RELEASE_IDENTITY_SQL, y.cell_id, old_id)
    if status != "UPDATE 1":
        raise StepFailure(f"releasing cell Y's identity reported {status}")
    # Step 6: resume.
    drill.resume_cellctl()
    row = await drill.wait_row(y, serving, timeout=1200, description="cell Y to serve on its re-adopted volume")
    adopted = drill.claim_volume(y)
    record.evidence.update({
        "cell_y": {"old_volume": y_volume, "new_volume_id": new_id, "adopted": adopted, "row": row_summary(row)},
        "writes_y": sorted(drill.markers(y)),
        "writes_x": sorted(drill.markers(x)),
    })
    record.evidence["acceptance_y"] = drill.accept(y, recall="seed", governance=governance_y, write="after-readopt")
    await drill.wait_row(x, serving, timeout=900, description="cell X to serve after the etcd restore")
    problems = []
    if adopted["pv"] != pv_name or row.get("volume_id") != new_id:
        problems.append(f"cell Y is bound to {adopted}, its row records {row.get('volume_id')}")
    if "seed" not in record.evidence["writes_y"]:
        problems.append("cell Y's re-adopted volume does not hold its write")
    if "after-snapshot" not in record.evidence["writes_x"]:
        problems.append("cell X lost a write made after the etcd snapshot")
    if problems:
        raise StepFailure("; ".join(problems))


# --- setup --------------------------------------------------------------------------------


async def control_database(stack: infra.Stack) -> None:
    await substrate.bootstrap_database(stack)
    owner = await asyncpg.connect(stack.postgres.dsn("substrate_owner", from_host=True))
    try:
        await owner.execute(TENANTS_STANDIN_SQL)
        await owner.execute((FIXTURES / "exomem_cloud_schema.sql").read_text(encoding="utf-8"))
        await owner.execute((FIXTURES / "exomem_cloud_grants.sql").read_text(encoding="utf-8"))
    finally:
        await owner.close()


def install_platform(stack: infra.Stack, cellctl_image: str, report: DrillReport) -> str:
    """Renders the platform chart with local storage and applies what the
    drill runs: TopoLVM, the snapshot CRDs and controller, the classes,
    cellctl with its RBAC and admission. Returns the node label TopoLVM's
    node plugin selects."""

    values = {
        "cellctl": platform.cellctl_values(stack, image=cellctl_image, cell_repository=build.CELL_REPOSITORY),
        "cells": {"jobEgressExcept": [infra.K3S_POD_CIDR, infra.K3S_SERVICE_CIDR]},
        "cellStorage": {"domain": "local", "local": {"enabled": True}},
    }
    rendered = platform.render_chart(stack, values, dependencies=True, api_versions=(SNAPSHOT_CLASS_API,))
    documents = [
        doc for source, doc in rendered
        if source in ("templates/cellctl.yaml", "templates/cell-local-storage.yaml")
        or source.startswith("charts/topolvm/")
        or (source == "templates/namespaces.yaml" and doc["kind"] == "Namespace"
            and doc["metadata"]["name"] == platform.PLATFORM_NAMESPACE)
    ]
    report.overlays.append(platform.overlay_cellctl(documents))
    report.adaptations.append(
        f"the chart is rendered with --api-versions {SNAPSHOT_CLASS_API}, standing in for the second upgrade "
        "after which its VolumeSnapshotClass renders"
    )
    lvmd = next(doc for doc in documents if doc["kind"] == "DaemonSet" and doc["metadata"]["name"].endswith("-lvmd-0"))
    [(key, value)] = lvmd["spec"]["template"]["spec"]["nodeSelector"].items()
    report.inputs["topolvm_image"] = lvmd["spec"]["template"]["spec"]["containers"][0]["image"]

    first = [doc for doc in documents if doc["kind"] in ("Namespace", "CustomResourceDefinition")]
    workloads = [doc for doc in documents if doc["kind"] in ("Deployment", "DaemonSet")]
    rest = [doc for doc in documents if doc not in first and doc not in workloads]
    platform._kubectl_apply(stack, first)
    stack.k3s.kubectl("wait", "--for=condition=Established", "crd", "--all", "--timeout=120s")
    platform._kubectl_apply(stack, rest)
    secrets_values = platform.cellctl_secrets(
        stack, cell_token_key=secrets.token_bytes(32), control_plane_key=substrate.b64url(secrets.token_bytes(32))
    )
    platform._kubectl_apply(stack, platform.cellctl_secret_documents(stack, secrets_values))
    platform._kubectl_apply(stack, workloads)
    report.stages["platform"]["applied"] = sorted({f"{doc['kind']}/{doc['metadata']['name']}" for doc in documents})
    for namespace, deployment in (
        (platform.CLOUD_NAMESPACE, "cellctl"),
        (platform.PLATFORM_NAMESPACE, "exomem-platform-topolvm-controller"),
        (platform.PLATFORM_NAMESPACE, "snapshot-controller"),
    ):
        platform.wait_rollout(stack, namespace, deployment, timeout=420)
    return f"{key}={value}"


ADAPTATIONS = (
    "the agents share the runner's kernel, where device-mapper names are global, so two `cells/pool0` pools "
    "cannot be active at once: agent B runs without cell storage until agent A's disk is destroyed, then gets "
    "its volume group and the storage label as the replacement capacity",
    "each agent's LVM reads only its own loop device (an lvm.conf device filter), and creates its device "
    "nodes itself, as no udev runs in the agent containers",
    "the agents run `mount --make-rshared /` before K3s, for TopoLVM's Bidirectional mount propagation",
    "the control database holds cellctl's fixture schema (infra/cellctl/tests/fixtures), not Substrate's "
    "migrations; cells are seeded as rows, as cellctl's live suite does",
    "the etcd snapshot is taken on the server's disk with `k3s etcd-snapshot save`, so the runbook's B2 "
    "listing and its --etcd-s3 restore transport are not exercised",
    "TopoLVM and the snapshot controller are pulled by the nodes from ghcr.io and registry.k8s.io by digest; "
    "agent B pulls TopoLVM's image before the loss",
)


async def run_drill(args: argparse.Namespace) -> int:
    run_id = secrets.token_hex(4)
    workdir = args.workdir or Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"exomem-drill-{run_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    report = DrillReport(run_id=run_id)
    report.adaptations.extend(ADAPTATIONS)
    report.inputs = {
        "exomem_commit": run(["git", "-C", str(images.REPO_ROOT), "rev-parse", "HEAD"]).stdout.strip(),
        "exomem_worktree_clean": run(["git", "-C", str(images.REPO_ROOT), "status", "--porcelain"]).stdout.strip() == "",
        "k3s_image": images.K3S, "postgres_image": images.POSTGRES, "s3_double_image": images.S3_DOUBLE,
        "environment": infra.environment_facts(),
    }
    stack: infra.Stack | None = None
    cluster: drill_cluster.Cluster | None = None
    drill: Drill | None = None
    code = 2
    try:
        with stage(report, "images"):
            cell_tag = f"{build.CELL_REPOSITORY}:{run_id}"
            build.build_cell_image(cell_tag)
            cellctl_tag = build.build_cellctl_image(run_id, workdir, mode="dockerfile")
            agent_image = drill_cluster.build_agent_image(run_id, workdir)
        with stage(report, "infrastructure"):
            run(["sudo", "modprobe", "dm_thin_pool"])
            network = drill_cluster.create_network(run_id)
            stack = infra.Stack(run_id=run_id, workdir=workdir, network=network, subnet=drill_cluster.SUBNET,
                                k3s=None, postgres=None, object_store=None)  # type: ignore[arg-type]
            stack.postgres = infra._start_postgres(stack)
            stack.object_store = infra._start_object_store(stack)
            cluster = drill_cluster.start_server(stack)
            cluster.agent_image = agent_image
            cellctl_image = build.load_into_k3s(stack.k3s.container, cellctl_tag)
        with stage(report, "control_database"):
            await control_database(stack)
        with stage(report, "platform"):
            storage_label = install_platform(stack, cellctl_image, report)
        with stage(report, "agents"):
            agent_a, agent_b = (drill_cluster.start_agent(cluster, name, ip) for name, ip in drill_cluster.AGENTS.items())
            cell_images = {build.load_into_k3s(agent.container, cell_tag) for agent in (agent_a, agent_b)}
            if len(cell_images) != 1:
                raise RuntimeError(f"the agents hold different cell image digests: {cell_images}")
            [cell_image] = cell_images
            run(["docker", "exec", agent_b.container, "crictl", "pull", report.inputs["topolvm_image"]], timeout=600)
            drill = Drill(cluster=cluster, report=report, cell_image=cell_image, storage_label=storage_label)
            report.stages["agents"]["agent_a_volume_group"] = drill_cluster.create_volume_group(agent_a)
            drill.label_storage(agent_a)
            await drill.sql("INSERT INTO exomem_cloud_settings (key, value) VALUES ('cell_image', $1)", json.dumps(cell_image))
            report.inputs.update({"cell_image": cell_image, "cellctl_image": cellctl_image, "agent_image": agent_image,
                                  "storage_label": storage_label})
        report.adaptations.extend(item for item in stack.adaptations if item not in report.adaptations)

        checks: list[tuple[str, Callable[[Drill, StepRecord], Awaitable[None]]]] = [
            ("2.3 hourly snapshot backups of a serving cell", check_hourly_backups),
            ("4.1 and 2.4 node loss, relocated from the last backup", check_node_loss),
            ("4.2 the empty-vault guard", check_empty_vault_guard),
            ("4.3 an interrupted relocation restore", check_interrupted_restore),
            ("2.9 re-adoption after an older etcd snapshot", check_etcd_restore),
        ]
        for number, (name, body) in enumerate(checks, start=1):
            await run_check(drill, number, name, body)
        harness_errors = [c.number for c in report.checks if c.failure and "traceback" in c.failure]
        code = 2 if harness_errors else (0 if report.passed() else 1)
    except StageFailed:
        code = 2
    except Exception as error:  # noqa: BLE001 - recorded in the report
        report.stages.setdefault("harness", {})["failure"] = failure_record(error)
        code = 2
    finally:
        if stack is not None and stack.k3s is not None and not args.keep:
            guarded(report, "diagnostics", lambda: collect_diagnostics(stack, cluster, workdir))
        guarded(report, "report", lambda: report.write(args.report))
        if not args.keep:
            guarded(report, "teardown", lambda: infra.teardown(stack, keep=False))
            guarded(report, "disks", lambda: drill_cluster.release_disks(cluster))
            tags = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], check=False).stdout.split()
            mine = [tag for tag in tags if tag.endswith(f":{run_id}") or f":{run_id}-" in tag]
            if mine:
                run(["docker", "rmi", "--force", *mine], check=False)
            if args.workdir is None:
                shutil.rmtree(workdir, ignore_errors=True)
    print(f"[drill] report: {args.report}")
    for check in report.checks:
        print(f"[drill]   {check.number} {check.name:<55} {check.status}")
    print(f"[drill] measurements: {json.dumps(report.measurements, default=_iso)}")
    return code


async def run_check(drill: Drill, number: int, name: str, body: Callable[[Drill, StepRecord], Awaitable[None]]) -> None:
    record = StepRecord(number=number, name=name)
    drill.report.checks.append(record)
    if any(check.status != PASSED for check in drill.report.checks[:-1]):
        record.failure = {"message": "blocked: an earlier check did not pass, and this one runs on the state it leaves"}
        print(f"[drill] check {number} {name}: blocked", flush=True)
        return
    record.started_at = _now().isoformat()
    began = time.monotonic()
    print(f"[drill] check {number} {name}: start", flush=True)
    try:
        await body(drill, record)
        record.status = PASSED
    except Exception as error:  # noqa: BLE001 - recorded as the check's failure
        record.status = FAILED
        record.failure = failure_record(error)
    record.seconds = round(time.monotonic() - began, 1)
    print(f"[drill] check {number} {name}: {record.status} in {record.seconds}s "
          f"{(record.failure or {}).get('message', '')[:1500]}", flush=True)


def collect_diagnostics(stack: infra.Stack, cluster: drill_cluster.Cluster | None, workdir: Path) -> None:
    out = workdir / "diagnostics"
    out.mkdir(exist_ok=True)
    for name, command in {
        "nodes": ["get", "nodes", "-o", "wide", "--show-labels"],
        "pods": ["get", "pods", "--all-namespaces", "-o", "wide"],
        "events": ["get", "events", "--all-namespaces", "--sort-by=.lastTimestamp"],
        "storage": ["get", "pv,pvc,volumesnapshots,volumesnapshotcontents,logicalvolumes.topolvm.io,csistoragecapacities", "--all-namespaces", "-o", "wide"],
        "cellctl": ["logs", "--namespace", platform.CLOUD_NAMESPACE, "deployment/cellctl", "--tail=600"],
        "topolvm-controller": ["logs", "--namespace", platform.PLATFORM_NAMESPACE, "deployment/exomem-platform-topolvm-controller", "--all-containers", "--tail=300"],
        "topolvm-node": ["logs", "--namespace", platform.PLATFORM_NAMESPACE, "--selector=app.kubernetes.io/component=node", "--all-containers", "--tail=300", "--prefix"],
        "lvmd": ["logs", "--namespace", platform.PLATFORM_NAMESPACE, "--selector=app.kubernetes.io/component=lvmd", "--tail=300", "--prefix"],
    }.items():
        result = stack.k3s.kubectl(*command, check=False)
        (out / f"{name}.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    for container in stack.containers:
        logs = run(["docker", "logs", "--tail", "300", container], check=False)
        (out / f"{container}.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
    if cluster is not None:
        for agent in cluster.agents.values():
            if not agent.destroyed:
                lvs = run(["docker", "exec", agent.container, "lvs", "-a"], check=False)
                (out / f"{agent.name}-lvs.txt").write_text(lvs.stdout + lvs.stderr, encoding="utf-8")
    artifacts = os.environ.get("REHEARSAL_DIAGNOSTICS_DIR")
    if artifacts:
        shutil.copytree(out, Path(artifacts), dirs_exist_ok=True)
