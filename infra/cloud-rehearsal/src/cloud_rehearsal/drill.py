"""`exomem-cloud-rehearsal local-storage-drill`: the node-loss drill of
move-cloud-cells-to-local-storage (tasks 4.1-4.3), and the rehearsal
evidence for tasks 2.3, 2.4, 2.9, 2.10, and the planned move of 6.4 and 7.3.

What runs:
- drill_cluster's K3s server with embedded etcd and two agents, each with
  its own loop-device `cells` volume group, and a third agent as the planned
  move's destination;
- PostgreSQL holding cellctl's fixture schema of the control database's Cloud
  tables, the schema cellctl's own tests and live suite run against;
- the S3 double as the restic target;
- the platform chart rendered with `cellStorage.local.enabled`: TopoLVM, the
  snapshot controller, the cell classes, cellctl and its admission all come
  from that one render, applied as rendered apart from P3's cellctl overlay.

The checks, in the order they run, are listed in infra/cloud-rehearsal/README.md.
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
import re
import secrets
import shutil
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import boto3
import yaml
from botocore.config import Config
from cellctl.alerts import GROWTH_ALERT
from cellctl.control_test_contract import control_test_sql, export_test_sql, import_test_sql
from cellctl.k8s_client import (
    DELETION_SECRET_NAME_ANNOTATION,
    DELETION_SECRET_NAMESPACE_ANNOTATION,
    MOVE_VOLUME_ANNOTATION,
    OUT_OF_SERVICE_TAINT,
    VOLUME_LOST_ANNOTATION,
)
from cellctl.manifests import (
    BACKUP_FAMILY_ANNOTATION,
    BACKUP_JOB_NAME,
    CLONE_CLAIM_NAME,
    HOLD_ANNOTATION,
    HOLD_STARTED_ANNOTATION,
    JOB_KIND_LABEL,
    JOB_KIND_RESTORE,
    SNAPSHOT_NAME,
    hold_job_name,
    namespace_name,
    render_backup_family_secret,
    snapshot_backup_name,
)
from cellctl.state import (
    BACKUP_FAILED,
    EMPTY_VOLUME_REFUSED,
    GROWTH_AT_CAP,
    GROWTH_PLANNED,
    MOVE_BACKUP_FAILED,
    RESTORE_FAILED,
    SNAPSHOT_BACKUP,
)
from cellctl.storage.topolvm import FamilyRequest
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
# A cell stopped by its row, as Substrate stops one.
STOP_CELL_SQL = "UPDATE exomem_cloud_cells SET desired_state = 'stopped' WHERE cell_id = $1 AND desired_state = 'running'"
# D10: the drill's cap, so one growth from the default size reaches it.
DRILL_MAX_CELL_GIB = 8
# Runs in a cell: the filesystem's used and total bytes, as the backup Job
# measures them. With arguments, it first allocates a filler file beside the
# vault, which no backup reads, up to that share of the filesystem.
FILESYSTEM_USE = r"""
import os, sys
if len(sys.argv) > 1:
    s = os.statvfs("/data")
    target = int(s.f_blocks * s.f_frsize * float(sys.argv[1])) - (s.f_blocks - s.f_bfree) * s.f_frsize
    fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.posix_fallocate(fd, 0, max(target, 1))
    os.close(fd)
s = os.statvfs("/data")
print((s.f_blocks - s.f_bfree) * s.f_frsize, s.f_blocks * s.f_frsize)
"""

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
    # The k3s-link rehearsal writes the same report under its own schema.
    schema: str = SCHEMA
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
            "schema": self.schema,
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
    # The host image the cell image was loaded from, for an agent started later.
    cell_tag: str
    provider_tag: str | None = None
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
        result = self.kubectl(*args, "--output=json", "--ignore-not-found")
        return json.loads(result.stdout) if result.stdout.strip() else None

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
        """Runbook "Restore etcd" step 6. cellctl's log so far is kept for
        the diagnostics, which run when its pod may be gone."""

        logs = self.kubectl("--namespace", platform.CLOUD_NAMESPACE, "logs", "deployment/cellctl", "--tail=400", check=False)
        kept = self.stack.workdir / "diagnostics" / f"cellctl-before-pause-{len(self.report.checks)}.txt"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text(logs.stdout + logs.stderr, encoding="utf-8")
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
        # A finished Job's pod that mounted the claim holds it until the Job
        # expires, five minutes after finishing (runbook step 4.3).
        self.kubectl("--namespace", cell.namespace, "delete", "persistentvolumeclaim", "cell-data", "--timeout=420s")
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

    def bucket(self, cell: Cell) -> dict[str, int]:
        store = self.stack.object_store
        client = boto3.client(
            "s3", endpoint_url=store.endpoint(from_host=True), aws_access_key_id=store.access_key,
            aws_secret_access_key=store.secret_key, region_name="us-east-1", verify=str(store.ca_path),
            config=Config(s3={"addressing_style": "path"}, proxies={}),
        )
        objects: dict[str, int] = {}
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=infra.BACKUP_BUCKET, Prefix=f"cells/{cell.cell_id}/"):
            for item in page.get("Contents", []):
                objects[item["Key"]] = item["Size"]
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

    async def slots(self, agent: drill_cluster.Agent) -> int | None:
        """The cell slots cellctl last published for `agent`; None if none."""

        rows = await self.fetch("SELECT cell_slots FROM exomem_cloud_capacity WHERE node = $1", agent.name)
        return rows[0]["cell_slots"] if rows else None

    def filesystem(self, cell: Cell, *fill: str) -> dict[str, int]:
        used, total = self.cell_exec(cell, "python3", "-c", FILESYSTEM_USE, *fill).stdout.split()
        return {"used_bytes": int(used), "total_bytes": int(total)}

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
            "last_backup_snapshot", "grown_storage_gib")
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


def pod_identity(pod: dict[str, Any]) -> dict[str, Any]:
    return {"uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
            "restarts": sum(s.get("restartCount", 0) for s in pod["status"].get("containerStatuses", []))}


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
            # cellctl clears the family annotation at closure; the last observed names still apply.
            raw = annotations.get(BACKUP_FAMILY_ANNOTATION)
            request = FamilyRequest.parse(raw) if raw else None
            if request is not None or "snapshot_name" not in seen:
                seen["snapshot_name"] = snapshot_backup_name(SNAPSHOT_NAME, cell.cell_id, started, request)
                seen["clone_name"] = snapshot_backup_name(CLONE_CLAIM_NAME, cell.cell_id, started, request)
            snapshot = drill.maybe_json("get", "volumesnapshot", seen["snapshot_name"], "--namespace", cell.namespace)
            error = ((snapshot or {}).get("status") or {}).get("error")
            if error:
                seen["snapshot_error"] = str(error.get("message"))[:600]
            if snapshot and (snapshot.get("status") or {}).get("readyToUse"):
                seen["snapshot"] = {"name": snapshot["metadata"]["name"],
                                    "created_at": snapshot["status"].get("creationTime"),
                                    "content": snapshot["status"].get("boundVolumeSnapshotContentName")}
                if request is not None and "secret_permissions" not in seen:
                    seen["secret_permissions"] = check_snapshot_secret_permissions(drill, cell, seen["snapshot_name"])
            clone = drill.maybe_json("get", "persistentvolumeclaim", seen["clone_name"], "--namespace", cell.namespace)
            if clone and clone["spec"].get("volumeName") and seen["clone"] is None:
                pv = drill.maybe_json("get", "persistentvolume", clone["spec"]["volumeName"])
                if pv:
                    seen["clone"] = {"claim": clone["metadata"]["name"], "class": clone["spec"].get("storageClassName"),
                                     "size": clone["spec"]["resources"]["requests"].get("storage"),
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
        cell,
        lambda r: r.get("last_error_code") == BACKUP_FAILED
        or (r.get("last_backup_snapshot") not in (None, previous_snapshot) and r.get("hold_kind") is None),
        timeout=1200, interval=1, during=look, description=f"an hourly backup of cell {cell.label} to finish",
    )
    look()
    seen["row"] = row_summary(row)
    if seen["hold_started_at"]:
        # The drill's clock when it read the hold's end from the row: an upper bound.
        seen["hold_seconds"] = round((_now() - dt.datetime.fromisoformat(seen["hold_started_at"])).total_seconds(), 1)
    if row.get("last_error_code") == BACKUP_FAILED:
        raise StepFailure(f"cellctl recorded BACKUP_FAILED for cell {cell.label}: {seen}")
    return seen


async def check_hourly_backups(drill: Drill, record: StepRecord) -> None:
    """2.3: the hourly hold backs a serving cell up from a snapshot clone on
    its own node, and leaves no clone or snapshot behind."""

    x = await drill.add_cell("x", owner=True)
    agent_a = drill.cluster.agents["drill-agent-a"]
    row = await drill.wait_row(x, serving, timeout=900, description="cell X to serve")
    first_pod = pod_identity(drill.cell_pod(x))
    record.evidence["cell"] = {"id": x.cell_id, "first_pod": first_pod, "volume": drill.claim_volume(x), "row": row_summary(row)}
    if drill.provider_tag:
        record.evidence["reservation_permissions"] = check_reservation_permissions(drill, x)
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
        observed["bucket"] = {"objects_added": len(added), "bytes_added": sum(added.values())}
        observed["leftovers_gone_after_seconds"] = leftovers_gone(drill, x, agent_a, observed)
        backups.append(observed)
        previous = observed["row"]["last_backup_snapshot"]
    record.evidence["backups"] = backups

    last_pod = pod_identity(drill.cell_pod(x))
    record.evidence["last_pod"] = last_pod
    drill.report.measurements["backup_upload"] = [
        {
            "backup": number,
            "restic_container_seconds": (backup.get("job") or {}).get("container", {}).get("seconds"),
            "objects_added": backup["bucket"]["objects_added"],
            "bytes_added": backup["bucket"]["bytes_added"],
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
        if backup["polls_not_ready"]:
            problems.append(f"backup {number}: the cell read not ready on {backup['polls_not_ready']} of {backup['polls']} polls")
        if backup["leftovers_gone_after_seconds"] is None:
            problems.append(f"backup {number}: its clone or snapshot outlived the hold")
        # A finished Job's pod pinning the clone kept a hold for the Job's
        # five-minute TTL, which caps a node at about 22 backups an hour.
        if backup.get("hold_seconds") is None or backup["hold_seconds"] > 180:
            problems.append(f"backup {number}: the hold lasted {backup.get('hold_seconds')}s, or was never seen to start")
    if problems:
        raise StepFailure("; ".join(problems))


def check_reservation_permissions(drill: Drill, cell: Cell) -> dict[str, Any]:
    """Exercise the request-only permission with server dry-runs on this fixture's LV."""
    handle = drill.claim_volume(cell)["volume_id"]
    volumes = drill.kube_json("get", "logicalvolumes.topolvm.io")["items"]
    volume = next(item for item in volumes if (item.get("status") or {}).get("volumeID") == handle)
    physical_uuid = volume["status"]["physicalUUID"]
    policy_name = "exomem-cellctl-reservations"
    policy = drill.kube_json("get", "validatingadmissionpolicy", policy_name)
    warnings = (policy.get("status") or {}).get("typeChecking", {}).get("expressionWarnings", [])
    if warnings:
        raise StepFailure(f"the request-only admission policy has type errors: {warnings}")
    identity = f"system:serviceaccount:{platform.CLOUD_NAMESPACE}:cellctl"

    def patch(changes: dict[str, Any]):
        return drill.kubectl("patch", "logicalvolumes.topolvm.io", volume["metadata"]["name"],
                             "--type=merge", "--dry-run=server", "--as", identity,
                             "--patch", json.dumps({"spec": changes}), check=False)

    # cellctl's own renderer builds the family capability it would write for a backup of this volume.
    family = FamilyRequest(volume["metadata"]["name"], physical_uuid, 0, uuid.uuid4().hex)
    probe_spec = SimpleNamespace(cell_id=cell.cell_id, hold_started_at=dt.datetime.now(dt.UTC).isoformat(),
                                 backup_family_request=family.annotation())
    family_secret = render_backup_family_secret(probe_spec, namespace=LOCAL.reservation_namespace,
                                                token="drill-probe-token", physical_uuid=physical_uuid)

    def ready() -> dict[str, Any] | None:
        allowed = patch({"reservationTargetBytes": LOCAL.default_cell_gib * drill_cluster.GIB,
                         "reservationTargetPhysicalUUID": physical_uuid})
        forbidden = patch({"nodeName": "invented-other-node"})
        secret = drill.kubectl("apply", "--server-side", "--field-manager=cellctl", "--force-conflicts",
                               "--dry-run=server", "--as", identity, "--filename=-",
                               input_text=json.dumps(family_secret), check=False)
        if (allowed.returncode == 0 and forbidden.returncode != 0 and policy_name in forbidden.stderr
                and secret.returncode == 0):
            return {"source_physical_uuid": physical_uuid, "target_request": "allowed",
                    "node_rewrite": "denied", "family_secret_write": "allowed",
                    "source": "Kubernetes server dry-runs as the cellctl service account"}
        # The timeout reports the last attempt, so a failure names which case the server decided wrongly.
        raise RuntimeError(f"target request exit {allowed.returncode}: {allowed.stderr.strip()[-400:]}; "
                           f"node rewrite exit {forbidden.returncode}: {forbidden.stderr.strip()[-400:]}; "
                           f"family Secret exit {secret.returncode}: {secret.stderr.strip()[-600:]}")

    return wait_for(ready, timeout=60, interval=2, description="the request-only admission policy to enforce both cases")


def check_snapshot_secret_permissions(drill: Drill, cell: Cell, secret_name: str) -> dict[str, Any]:
    """Read only resource names while checking the actual snapshotter account."""
    deployment = drill.kube_json("get", "deployment", "exomem-platform-topolvm-controller",
                                 "--namespace", platform.PLATFORM_NAMESPACE)
    account = deployment["spec"]["template"]["spec"]["serviceAccountName"]
    identity = f"system:serviceaccount:{platform.PLATFORM_NAMESPACE}:{account}"
    snapshot_class = drill.kube_json("get", "volumesnapshotclass", LOCAL.snapshot_class)
    namespace = snapshot_class["parameters"]["csi.storage.k8s.io/snapshotter-secret-namespace"]
    allowed = drill.kubectl("get", "secret", secret_name, "--namespace", namespace,
                            "--as", identity, "--output=name", check=False)
    cell_secrets = drill.kubectl("get", "secrets", "--namespace", cell.namespace, "--output=name").stdout.splitlines()
    if not cell_secrets:
        raise StepFailure("the cell has no credential Secret for the isolation check")
    forbidden = drill.kubectl("get", cell_secrets[0], "--namespace", cell.namespace,
                              "--as", identity, "--output=name", check=False)
    decisions = {verb: drill.kubectl("auth", "can-i", verb, "secrets", "--namespace", namespace,
                                     "--as", identity, "--quiet", check=False).returncode for verb in ("list", "watch")}
    # Forbidden is Kubernetes' fixed Status.reason; auth can-i uses exit 1 for a negative decision.
    if (allowed.returncode or forbidden.returncode == 0 or "Forbidden" not in forbidden.stderr
            or any(code != 1 for code in decisions.values())):
        raise StepFailure("the snapshotter account does not have exactly scoped Secret access")
    return {"service_account": identity, "namespace": namespace, "secret_name": secret_name,
            "family_get": "allowed", "cell_secret_get": "denied", "list": "denied", "watch": "denied",
            "source": "actual Kubernetes get requests and authorization reviews"}


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
        clone_name = backup.get("clone_name") or hold_job_name(CLONE_CLAIM_NAME, started)
        snapshot_name = backup.get("snapshot_name") or hold_job_name(SNAPSHOT_NAME, started)
        if drill.maybe_json("get", "persistentvolumeclaim", clone_name, "--namespace", cell.namespace):
            return False
        if drill.maybe_json("get", "volumesnapshot", snapshot_name, "--namespace", cell.namespace):
            return False
        permissions = backup.get("secret_permissions")
        if permissions:
            # The snapshotter still reads this capability before deleting its content.
            for content in drill.kube_json("get", "volumesnapshotcontents")["items"]:
                annotations = content["metadata"].get("annotations") or {}
                if (annotations.get(DELETION_SECRET_NAME_ANNOTATION) == permissions["secret_name"]
                        and annotations.get(DELETION_SECRET_NAMESPACE_ANNOTATION) == permissions["namespace"]):
                    return False
            secret = drill.kubectl("get", "secret", permissions["secret_name"], "--namespace", permissions["namespace"],
                                   "--ignore-not-found", "--output=name", check=False)
            if secret.returncode or secret.stdout.strip():
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
    vault, stays not ready, and reports why on its row. Its own volume comes
    back empty while it is stopped, as a disk replaced under a surviving
    LogicalVolume would: same claim, same volume ID, so the row still records
    it and the runbook can relocate the cell (4.3)."""

    x = drill.cells["x"]
    agent_b = drill.cluster.agents["drill-agent-b"]
    row = await drill.row(x)
    volume = drill.claim_volume(x)
    if row.get("volume_id") != volume["volume_id"] or volume["node"] != agent_b.name:
        raise StepFailure(f"cell X's row records volume {row.get('volume_id')}, its claim is on {volume}")
    drill.pause_cellctl()
    drill.stop_cell(x)
    drill_cluster.recreate_empty(agent_b, volume["volume_id"], drill_cluster.thin_volumes(agent_b)[volume["volume_id"]])
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
        "volume_before": volume,
        "volume_after": drill.claim_volume(x),
        "ready_in_the_minute_after": any(ready_seen),
    })
    if any(ready_seen):
        raise StepFailure("the cell read ready on an empty volume after its refusal")
    if record.evidence["volume_after"]["volume_id"] != volume["volume_id"]:
        raise StepFailure("the cell refused on a different volume from its own")
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
    if row.get("volume_id") is None:
        # cellctl relocates only a recorded volume; the mark would name none.
        raise StepFailure("cell X's row records no volume to mark lost")
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
        interrupted: list[str] = []

        def interrupt() -> None:
            # Every restore pod that runs is stopped, a replacement included,
            # so no restore can finish while object storage is paused.
            running = restore_running()
            if running and running["metadata"]["name"] not in interrupted:
                interrupted.append(running["metadata"]["name"])
                drill.kubectl("delete", "pod", running["metadata"]["name"], "--namespace", x.namespace,
                              "--grace-period=5", "--wait=false")

        failed = await drill.wait_row(x, lambda r: r.get("last_error_code") == RESTORE_FAILED, timeout=600,
                                      during=interrupt, interval=5,
                                      description="cell X's row to report the failed restore")
        statefulset = drill.kube_json("get", "statefulset", "cell", "--namespace", x.namespace)
        after = drill.retained_state(agent_b)
        record.evidence.update({
            "interrupted_restore_pods": interrupted, "restore_node": pod["spec"].get("nodeName"),
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


async def check_growth(drill: Drill, record: StepRecord) -> None:
    """2.10: a local cell that its hourly backup finds past 80% use grows
    online by one default size: the claim, the filesystem, the quota, the row
    and the published slots, with the pod kept. At the cap it stays, and
    cellctl decides to raise storage-growth-blocked. The filler is a file
    beside the vault, which no backup reads."""

    x = drill.cells["x"]
    agent_b = drill.cluster.agents["drill-agent-b"]
    size = (await drill.row(x))["storage_gib"]
    grown = min(size + LOCAL.default_cell_gib, DRILL_MAX_CELL_GIB)
    first_pod = pod_identity(drill.cell_pod(x))
    slots_before = await drill.slots(agent_b)
    if slots_before is None:
        raise StepFailure("cellctl published no slots for agent B")
    polls = {"polls": 0, "not_ready": 0}

    def claim_size() -> str | None:
        claim = drill.kube_json("get", "persistentvolumeclaim", "cell-data", "--namespace", x.namespace)
        polls["polls"] += 1
        polls["not_ready"] += 0 if pod_ready(drill.cell_pod(x)) else 1
        return ((claim.get("status") or {}).get("capacity") or {}).get("storage")

    def quota_storage() -> str | None:
        hard = drill.kube_json("get", "resourcequota", "cell-quota", "--namespace", x.namespace)["spec"]["hard"]
        return hard["requests.storage"] if hard.get("requests.storage") == f"{2 * grown}Gi" else None

    async def fill_and_back_up(label: str) -> dict[str, Any]:
        filled = drill.filesystem(x, "0.85", f"/data/drill-filler-{label}")
        previous = (await drill.row(x))["last_backup_snapshot"]
        status = await drill.sql(MAKE_BACKUP_DUE_SQL, x.cell_id)
        if status != "UPDATE 1":
            raise StepFailure(f"moving cell X's last_backup_at back reported {status}")
        return {"filled": filled, "backup": await observe_backup(drill, x, previous)}

    def cellctl_log() -> dict[str, list[str]]:
        lines = drill.kubectl("--namespace", platform.CLOUD_NAMESPACE, "logs", "deployment/cellctl", "--since=30m").stdout.splitlines()
        return {
            "measured": [line for line in lines if x.cell_id in line and "bytes used at" in line],
            "grows": [line for line in lines if x.cell_id in line and "grows to" in line],
            "refused": [line for line in lines if x.cell_id in line and "apply refused" in line],
            "growth_alert": [line for line in lines if f"{GROWTH_ALERT} alert" in line],
        }

    # Past 80% at the default size: the backup plans the growth, and the
    # hold's exit records it and applies the larger claim and quota.
    growth = await fill_and_back_up("1")
    hold_ended = time.monotonic()
    growth["row"] = row_summary(await drill.row(x))
    growth["claim_capacity"] = wait_for(lambda: claim_size() == f"{grown}Gi" and f"{grown}Gi", timeout=600, interval=3,
                                        description=f"cell X's claim to report {grown}Gi")
    growth["claim_expanded_seconds_after_hold"] = round(time.monotonic() - hold_ended, 1)
    growth["filesystem_after"] = drill.filesystem(x)
    growth["quota_storage"] = wait_for(quota_storage, timeout=300, interval=5, description="cell X's quota to cover the grown claim")
    growth["quota_seconds_after_hold"] = round(time.monotonic() - hold_ended, 1)
    # Guarded capacity also charges retained physical volumes. Legacy mode
    # keeps the earlier pool/reserve calculation and its cached observation.
    async def d6_slots(largest: int) -> int:
        reserve = 2 * largest * LOCAL.backup_concurrency_per_node
        extra = -(-largest // LOCAL.default_cell_gib) - 1
        if drill.provider_tag:
            return await guarded_slots_from_observation(drill, agent_b)
        return (drill_cluster.POOL_GIB - reserve) // LOCAL.default_cell_gib - extra

    deadline = time.monotonic() + 180
    while (slots_after := await drill.slots(agent_b)) != await d6_slots(grown) and time.monotonic() < deadline:
        await asyncio.sleep(5)
    growth["slots"] = {"before": slots_before, "after": slots_after,
                       "d6_before": None if drill.provider_tag else await d6_slots(size),
                       "d6_after": await d6_slots(grown),
                       "source": "exomem_cloud_capacity.cell_slots for agent B as cellctl published it; d6_ from "
                       f"design D6 on the drill's {drill_cluster.POOL_GIB} GiB pool"}

    # At the cap: the next backup measures the grown filesystem, and the
    # cell stays at its size.
    at_cap = await fill_and_back_up("2")
    at_cap["row"] = row_summary(await drill.row(x))
    at_cap["claim_capacity"] = claim_size()
    log = wait_for(lambda: (found := cellctl_log())["measured"] and found["measured"][-1].endswith(GROWTH_AT_CAP)
                   and found["growth_alert"] and found, timeout=240, interval=10,
                   description="cellctl's at-cap verdict and its growth alert")
    pattern = re.compile(r"is (?P<used>\d+) of (?P<total>\d+) bytes used at (?P<gib>\d+) GiB: (?P<verdict>\S+)")
    measured = [match.groupdict() for line in log["measured"] if (match := pattern.search(line))]
    record.evidence.update({
        "growth": growth, "at_cap": at_cap,
        "measured_by_backups": measured,
        "cellctl_log": {**log, "growth_alert": log["growth_alert"][:2]},
        "quota_apply": "refused, then applied after its park" if log["refused"] else "applied without a refusal",
        "pod": {"first": first_pod, "last": pod_identity(drill.cell_pod(x)), **polls},
    })

    problems = []
    if record.evidence["pod"]["last"] != first_pod:
        problems.append(f"the cell's pod restarted: {first_pod} -> {record.evidence['pod']['last']}")
    if polls["not_ready"] or growth["backup"]["polls_not_ready"] or at_cap["backup"]["polls_not_ready"]:
        problems.append("the cell read not ready while it grew")
    if growth["row"]["grown_storage_gib"] != grown or at_cap["row"]["grown_storage_gib"] != grown:
        problems.append(f"the row records {growth['row']['grown_storage_gib']}, then {at_cap['row']['grown_storage_gib']} GiB")
    if [m["verdict"] for m in measured[-2:]] != [GROWTH_PLANNED, GROWTH_AT_CAP] or int(measured[-1]["total"]) <= int(measured[-2]["total"]):
        problems.append(f"the backups measured {measured}, not a growth and then a larger filesystem at the cap")
    if growth["filesystem_after"]["total_bytes"] <= growth["filled"]["total_bytes"]:
        problems.append("the filesystem inside the cell did not grow")
    # A guarded clone requests its family's exact byte size, not a GiB quantity.
    if (at_cap["backup"].get("clone") or {}).get("size") != str(grown * drill_cluster.GIB):
        problems.append(f"the next backup's clone is {(at_cap['backup'].get('clone') or {}).get('size')}")
    if slots_after != await d6_slots(grown):
        problems.append(f"the published slots read {slots_after} after the growth, not D6's {await d6_slots(grown)}")
    if at_cap["claim_capacity"] != f"{grown}Gi":
        problems.append(f"the claim at the cap reads {at_cap['claim_capacity']}")
    if problems:
        raise StepFailure("; ".join(problems))


async def check_planned_move(drill: Drill, record: StepRecord) -> None:
    """6.4 and 7.3: a planned move takes a healthy cell to a new claim on
    another agent without losing a write. The operator cordons the source,
    then marks the cell. cellctl stops it, backs up its own volume, and only
    then retains that volume and restores the backup onto a new claim. A note
    written just before the mark must survive, which the last hourly backup
    would not hold.

    Only one `cells` pool can be active on the runner's kernel, so agent C
    gets its pool between the move's backup and its restore, once agent B's
    is deactivated. Until then the restore waits with the cordoned source as
    the only node with a pool, which shows that the cordon keeps the new
    claim off it."""

    x, y = drill.cells["x"], drill.cells["y"]
    agent_b = drill.cluster.agents["drill-agent-b"]
    for cell in (x, y):
        await drill.wait_row(cell, serving, timeout=900, description=f"cell {cell.label} to serve with no hold")
    agent_c = drill_cluster.start_agent(drill.cluster, *drill_cluster.MOVE_TARGET)
    loaded = build.load_into_k3s(agent_c.container, drill.cell_tag)
    if loaded != drill.cell_image:
        raise StepFailure(f"agent C holds cell image {loaded}, the cluster runs {drill.cell_image}")
    if drill.provider_tag:
        loaded_provider = build.load_into_k3s(agent_c.container, drill.provider_tag)
        if loaded_provider != drill.report.inputs["reservation_provider_image"]:
            raise StepFailure("the destination agent loaded a different reservation provider")
    else:
        run(["docker", "exec", agent_c.container, "crictl", "pull", drill.report.inputs["topolvm_image"]], timeout=600)

    # Cell Y's volume is in B's pool too, which must be inactive before C's
    # pool can be active.
    status = await drill.sql(STOP_CELL_SQL, y.cell_id)
    if status != "UPDATE 1":
        raise StepFailure(f"stopping cell Y reported {status}")
    await drill.wait_row(y, lambda r: r.get("observed_state") == "stopped" and r.get("hold_kind") is None,
                         timeout=600, description="cell Y to stop")
    wait_for(lambda: drill.cell_pod(y) is None, timeout=300, interval=2, description="cell Y's pod to go")

    row = await drill.row(x)
    old = drill.claim_volume(x)
    if old["node"] != agent_b.name or row.get("volume_id") != old["volume_id"]:
        raise StepFailure(f"cell X's row records volume {row.get('volume_id')}, its claim is on {old}")
    governance = drill.governance(x)
    drill.kubectl("cordon", agent_b.name)
    drill.write(x, "before-move")
    before = drill.markers(x)
    marked_at = _now()
    drill.kubectl("annotate", "namespace", x.namespace, f"{MOVE_VOLUME_ANNOTATION}={old['volume_id']}", "--overwrite")
    began = time.monotonic()
    marks: dict[str, float] = {}
    jobs: dict[str, dict[str, Any]] = {}
    since = marked_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    def job_pods(selector: str) -> list[dict[str, Any]]:
        """The move's Job pods: created at or after the mark."""

        pods = drill.kube_json("get", "pods", "--namespace", x.namespace, f"--selector={selector}")["items"]
        return [pod for pod in pods if pod["metadata"]["creationTimestamp"] >= since]

    def look() -> None:
        # The move's backup Job, then its restore Job, by their kind label.
        for pod in job_pods(JOB_KIND_LABEL):
            jobs[pod["metadata"]["labels"][JOB_KIND_LABEL]] = {
                "pod": pod["metadata"]["name"], "node": pod["spec"].get("nodeName"),
                "container": container_times(pod),
            }

    row = await drill.wait_row(
        x, lambda r: r.get("hold_kind") == "restore" or r.get("last_error_code") == MOVE_BACKUP_FAILED,
        timeout=1200, during=look, description="cell X's move to finish its stopped backup",
    )
    if row.get("last_error_code") == MOVE_BACKUP_FAILED:
        raise StepFailure(f"the move's stopped backup failed: {row_summary(row)}")
    marks["backup_done"] = time.monotonic() - began
    restore_point = row_summary(row)

    def restore_pod() -> dict[str, Any] | None:
        pods = job_pods(f"{JOB_KIND_LABEL}={JOB_KIND_RESTORE}")
        return pods[0] if pods else None

    pending = wait_for(restore_pod, timeout=600, interval=2, description="cell X's restore pod")
    unscheduled: list[str | None] = []
    for _ in range(12):
        pending = restore_pod() or pending
        unscheduled.append(pending["spec"].get("nodeName"))
        await asyncio.sleep(5)
    waiting = next((c for c in (pending.get("status") or {}).get("conditions", []) if c["type"] == "PodScheduled"), {})
    marks["restore_waited"] = time.monotonic() - began

    drill_cluster.deactivate_volume_group(drill.cluster, agent_b, timeout=300)
    record.evidence["destination_volume_group"] = drill_cluster.create_volume_group(agent_c)
    drill.label_storage(agent_c)
    marks["destination_capacity_published"] = time.monotonic() - began
    row = await drill.wait_row(x, lambda r: serving(r) and r.get("node") == agent_c.name, timeout=1500, during=look,
                               description="cell X to serve on agent C")
    marks["serving_on_destination"] = time.monotonic() - began
    look()

    pod = drill.cell_pod(x)
    new = drill.claim_volume(x)
    old_pv = drill.maybe_json("get", "persistentvolume", old["pv"]) or {}
    after = drill.markers(x)
    record.evidence.update({
        "source": agent_b.name, "destination": agent_c.name, "marked_at": marked_at.isoformat(),
        "row_after_backup": restore_point,
        "jobs": jobs,
        "restore_while_only_the_cordoned_source_had_a_pool": {
            "node_names_seen": unscheduled, "pod_scheduled": {k: waiting.get(k) for k in ("status", "reason", "message")},
        },
        "old_volume": {**old, "reclaim_policy": old_pv.get("spec", {}).get("persistentVolumeReclaimPolicy"),
                       "phase": (old_pv.get("status") or {}).get("phase"), "node_now": pinned_node(old_pv)},
        "new_volume": new,
        "new_pod": {"node": pod["spec"]["nodeName"], "created_at": pod["metadata"]["creationTimestamp"]},
        "row": row_summary(row),
        "writes": {"before_mark": sorted(before), "after_move": sorted(after)},
        "marks_seconds": {name: round(value, 1) for name, value in marks.items()},
        "marks_note": "from the mark; restore_waited and destination_capacity_published include the drill's pool "
        "swap, so they are not a downtime a production move would see",
    })
    record.evidence["acceptance"] = drill.accept(x, recall="before-move", governance=governance, write="after-move")
    drill.kubectl("annotate", "namespace", x.namespace, f"{MOVE_VOLUME_ANNOTATION}-")
    drill.retained_pvs.append(old["pv"])
    record.evidence["retained_on_source"] = drill.retained_state(agent_b)

    problems = []
    if any(unscheduled):
        problems.append(f"the restore pod was scheduled while only the cordoned source had a pool: {unscheduled}")
    if (jobs.get("backup") or {}).get("node") not in (None, agent_b.name):
        problems.append(f"the move's backup ran on {jobs['backup']['node']}, not on its volume's node {agent_b.name}")
    restore = jobs.get("restore") or {}
    if restore.get("node") != agent_c.name or new["node"] != agent_c.name or pod["spec"]["nodeName"] != agent_c.name:
        problems.append(f"restore on {restore.get('node')}, volume on {new['node']}, pod on {pod['spec']['nodeName']}")
    if new["volume_id"] == old["volume_id"] or row.get("volume_id") != new["volume_id"]:
        problems.append(f"the row records {row.get('volume_id')}, the new volume is {new['volume_id']}")
    if not row.get("last_backup_at") or row["last_backup_at"] <= marked_at:
        problems.append(f"the restore point {_iso(row.get('last_backup_at'))} is not a backup taken after the mark")
    if (old_pv.get("spec", {}).get("persistentVolumeReclaimPolicy"), (old_pv.get("status") or {}).get("phase"),
            pinned_node(old_pv)) != ("Retain", "Released", agent_b.name):
        problems.append(f"the old volume is not retained on the source: {record.evidence['old_volume']}")
    finished = (restore.get("container") or {}).get("finished_at")
    if not finished or pod["metadata"]["creationTimestamp"] < finished:
        problems.append(f"the cell's pod ({pod['metadata']['creationTimestamp']}) did not start after the restore finished ({finished})")
    if "before-move" not in after or not before <= after:
        problems.append(f"the move lost writes: before the mark {sorted(before)}, after it {sorted(after)}")
    if problems:
        raise StepFailure("; ".join(problems))


# --- setup --------------------------------------------------------------------------------


async def guarded_slots_from_observation(drill: Drill, agent: drill_cluster.Agent) -> int:
    """Compare published slots with the provider frame and this drill's actual claims."""
    node = drill.kube_json("get", "node", agent.name)
    frame = json.loads(node["metadata"]["annotations"][LOCAL.reservation_annotation])
    rows = await drill.fetch("SELECT cell_id, storage_gib, grown_storage_gib FROM exomem_cloud_cells "
                             "WHERE desired_state <> 'deleted'")
    volumes = drill.kube_json("get", "logicalvolumes.topolvm.io")["items"]
    allocations = {(item["name"], item["physical_uuid"]): item["reserved_bytes"] for item in frame["allocations"]}
    cells = {cell.cell_id: cell for cell in drill.cells.values()}
    credited = set()
    credit = 0
    extra = 0
    largest = LOCAL.default_cell_gib
    for row in rows:
        gib = max(row["storage_gib"], row["grown_storage_gib"] or 0)
        largest = max(largest, gib)
        extra += max(0, -(-gib // LOCAL.default_cell_gib) - 1)
        cell = cells[row["cell_id"]]
        claim = drill.maybe_json("get", "persistentvolumeclaim", "cell-data", "--namespace", cell.namespace)
        pv_name = (claim or {}).get("spec", {}).get("volumeName")
        if not pv_name:
            continue
        pv = drill.kube_json("get", "persistentvolume", pv_name)
        if pinned_node(pv) != agent.name:
            continue
        handle = pv["spec"]["csi"]["volumeHandle"]
        matching = [volume for volume in volumes if volume.get("status", {}).get("volumeID") == handle]
        if len(matching) == 1:
            volume = matching[0]
            identity = (volume["metadata"]["name"], volume["status"].get("physicalUUID"))
            if identity in allocations and identity not in credited:
                credit += min(allocations[identity], gib * drill_cluster.GIB)
                credited.add(identity)
    reserve = max(frame["effective_backup_reserve_bytes"],
                  2 * largest * drill_cluster.GIB * LOCAL.backup_concurrency_per_node)
    available = frame["pool_size_bytes"] - reserve - (frame["ordinary_bytes"] - credit)
    expected = max(0, available // (LOCAL.default_cell_gib * drill_cluster.GIB) - extra)
    drill.report.measurements["guarded_capacity_observation"] = {
        "node": agent.name, "provider_frame": frame, "counted_rows": rows,
        "credited_physical_identities": sorted(credited), "credited_bytes": credit,
        "prospective_reserve_bytes": reserve, "expected_slots": expected,
        "source": "one node provider frame, current PVC/PV/LogicalVolume bindings and non-deleted database rows",
    }
    return expected


def check_provider_process_chain(cluster: drill_cluster.Cluster, agent: drill_cluster.Agent,
                                 provider_root: Path, run_id: str) -> dict[str, Any]:
    """Repeat the fence proof with the provider pod's nsenter and the node's LVM."""
    pods = json.loads(cluster.server.kubectl("get", "pods", "--namespace", platform.PLATFORM_NAMESPACE,
                                            "--output=json").stdout)["items"]
    pod = next(item for item in pods if item["spec"].get("nodeName") == agent.name
               and any(container["name"] == "lvmd" for container in item["spec"]["containers"]))
    name = pod["metadata"]["name"]
    host_root = run(["docker", "exec", agent.container, "mktemp", "-d", "/tmp/reservation-chain.XXXXXXXX"]).stdout.strip()
    created = False
    try:
        for directory in ("lvm", "native-locks", "evidence"):
            run(["docker", "exec", agent.container, "mkdir", "-p", f"{host_root}/{directory}"])
        # The same node device filter limits this probe to the drill's own loop device.
        config = drill_cluster.LVM_CONF.format(device=agent.loop_device)
        config += f'\nglobal {{ wait_for_locks = 1 locking_dir = "{host_root}/native-locks" use_lvmlockd = 0 }}\n'
        run(["docker", "exec", "--interactive", agent.container, "sh", "-c", 'cat > "$1"',
             "_", f"{host_root}/lvm/lvm.conf"], input_text=config)
        run(["docker", "cp", str(provider_root / "build/reservation-host-proof"),
             f"{agent.container}:{host_root}/proof"])
        run(["docker", "exec", agent.container, "chmod", "0755", f"{host_root}/proof"])
        drill_cluster.agent_shell(agent, "lvcreate --yes --thin --virtualsize 16m --name fence cells/pool0")
        created = True
        prefix = ("exec", "--namespace", platform.PLATFORM_NAMESPACE, name, "--container", "lvmd", "--")
        identities = {
            "nsenter_version": cluster.server.kubectl(*prefix, "/usr/bin/nsenter", "--version").stdout.strip(),
            "binary_hashes": cluster.server.kubectl(*prefix, "sha256sum", "/usr/bin/nsenter",
                                                     "/proc/1/root/sbin/lvm").stdout.strip(),
            "lvm_version": drill_cluster.agent_shell(agent, "/sbin/lvm version").stdout.strip(),
        }
        result = cluster.server.kubectl(
            *prefix, "env", "EXOMEM_RESERVATION_RUNNER=disposable-github-runner",
            "PROOF_VG=cells", "PROOF_POOL=pool0", f"PROOF_FENCE_TAG=proof.{run_id}",
            "PROOF_PROVIDER_LOCKS=/run/topolvm", f"PROOF_NATIVE_LOCKS=/proc/1/root{host_root}/native-locks",
            f"PROOF_EVIDENCE=/proc/1/root{host_root}/evidence", f"LVM_SYSTEM_DIR={host_root}/lvm",
            "PROOF_LVM_BINARY=/proc/1/root/sbin/lvm", f"/proc/1/root{host_root}/proof",
            "-test.run=^TestReservationHostLVMParentDeath$", "-test.v", "-test.timeout=120s", check=False,
        )
        identities.update({"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise StepFailure(f"the actual provider process chain failed its fence proof: {identities}")
        return identities
    finally:
        diagnostics = cluster.stack.workdir / "diagnostics" / "provider-process-chain"
        diagnostics.mkdir(parents=True, exist_ok=True)
        copied = run(["docker", "cp", f"{agent.container}:{host_root}/evidence/.", str(diagnostics)], check=False)
        (diagnostics / "capture-status.txt").write_text(f"returncode={copied.returncode}\n{copied.stderr}")
        if created:
            drill_cluster.agent_shell(agent, "lvremove --yes cells/fence")
        run(["docker", "exec", agent.container, "rm", "-rf", "--", host_root])


async def control_database(stack: infra.Stack) -> None:
    await substrate.bootstrap_database(stack)
    owner = await asyncpg.connect(stack.postgres.dsn("substrate_owner", from_host=True))
    try:
        await owner.execute(TENANTS_STANDIN_SQL)
        schema_sql, grants_sql = control_test_sql(FIXTURES)
        await owner.execute(schema_sql)
        # cellctl reads a cell's open import and open exports on every pass.
        await owner.execute(import_test_sql(FIXTURES))
        await owner.execute(export_test_sql(FIXTURES))
        await owner.execute(grants_sql)
    finally:
        await owner.close()


def install_platform(stack: infra.Stack, cellctl_image: str, report: DrillReport,
                     *, provider_image: str | None = None) -> str:
    """Renders the platform chart with local storage and applies what the
    drill runs: TopoLVM, the snapshot CRDs and controller, the classes,
    cellctl with its RBAC and admission. Returns the node label TopoLVM's
    node plugin selects."""

    values = {
        "cellctl": platform.cellctl_values(stack, image=cellctl_image, cell_repository=build.CELL_REPOSITORY),
        "cells": {"jobEgressExcept": [infra.K3S_POD_CIDR, infra.K3S_SERVICE_CIDR]},
        "cellStorage": {"domain": "local", "local": {"enabled": True, "maxCellGib": DRILL_MAX_CELL_GIB}},
    }
    if provider_image:
        repository, digest = provider_image.split("@", 1)
        values["cellStorage"]["local"]["reservations"] = True
        values["topolvm"] = {"image": {"repository": repository, "reference": f"{stack.run_id}@{digest}"}}
    rendered = platform.render_chart(stack, values, dependencies=True, api_versions=(platform.SNAPSHOT_CLASS_API,))
    documents = [
        doc for source, doc in rendered
        if source in ("templates/cellctl.yaml", "templates/cell-local-storage.yaml")
        or source.startswith("charts/topolvm/")
        or (source == "templates/namespaces.yaml" and doc["kind"] == "Namespace"
            and doc["metadata"]["name"] == platform.PLATFORM_NAMESPACE)
    ]
    if provider_image:
        # Use the shipped Helm post-renderer; the drill owns no second schema transform.
        renderer = platform.PLATFORM_CHART / "reservation-post-renderer.py"
        rendered_schema = run([sys.executable, str(renderer)], input_text=yaml.safe_dump_all(documents))
        documents = [doc for doc in yaml.safe_load_all(rendered_schema.stdout) if doc]
    report.overlays.append(platform.overlay_cellctl(documents))
    report.overlays.append(platform.trust_storage_ca(stack, documents))
    report.adaptations.append(
        f"the chart is rendered with --api-versions {platform.SNAPSHOT_CLASS_API}, standing in for the second upgrade "
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
        *((platform.PLATFORM_NAMESPACE, name) for name in platform.LOCAL_STORAGE_CONTROLLERS),
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
    "the drill loads dm_thin_pool and dm_snapshot on the runner: the agents' LVM cannot load kernel modules "
    "from inside their containers, as a real agent's LVM does on demand",
    "TopoLVM and the snapshot controller are pulled by the nodes from ghcr.io and registry.k8s.io by digest; "
    "agent B pulls TopoLVM's image before the loss",
    f"the chart's cellStorage.local.maxCellGib is {DRILL_MAX_CELL_GIB}, so one growth from the 4 GiB default "
    "reaches the cap",
    "for the same device-mapper reason, the planned move's destination, agent C, gets its volume group only "
    "after the move's stopped backup succeeded and agent B's pool was deactivated, with cell Y, the other cell "
    "on B, stopped first; in production both pools stay active and the restore schedules at once",
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
    provider_root = Path(os.environ["RESERVATION_PROVIDER_ROOT"]) if os.environ.get("RESERVATION_PROVIDER_ROOT") else None
    provider_tag = None
    provider_image = None
    code = 2
    try:
        with stage(report, "images"):
            cell_tag = f"{build.CELL_REPOSITORY}:{run_id}"
            build.build_cell_image(cell_tag, labels=infra.docker_labels(run_id, image=True))
            cellctl_tag = build.build_cellctl_image(run_id, workdir, mode="dockerfile")
            agent_image = drill_cluster.build_agent_image(run_id, workdir)
            if provider_root:
                provider_tag = build.build_reservation_provider_image(run_id, workdir, provider_root)
                report.inputs["reservation_provider"] = json.loads((provider_root / "provenance.json").read_text())
        with stage(report, "infrastructure"):
            # The agents' LVM cannot load modules from their containers; a real
            # agent would load these on demand. LVM asks for the snapshot
            # target even for a thin snapshot.
            run(["sudo", "modprobe", "--all", "dm_thin_pool", "dm_snapshot"])
            network = drill_cluster.create_network(run_id)
            stack = infra.Stack(run_id=run_id, workdir=workdir, network=network, subnet=drill_cluster.SUBNET,
                                k3s=None, postgres=None, object_store=None)  # type: ignore[arg-type]
            stack.postgres = infra._start_postgres(stack)
            # Import Jobs accept only HTTPS transfer URLs, so storage serves TLS from the run CA, as in production.
            stack.object_store = infra._start_object_store(stack, serve_tls=True)
            cell_tag = build.trust_rehearsal_ca(cell_tag, stack.object_store.ca_path, workdir, run_id=run_id)
            cluster = drill_cluster.start_server(stack)
            cluster.agent_image = agent_image
            cellctl_image = build.load_into_k3s(stack.k3s.container, cellctl_tag)
            if provider_tag:
                provider_image = build.load_into_k3s(stack.k3s.container, provider_tag)
                report.inputs["reservation_provider_image"] = provider_image
        with stage(report, "control_database"):
            await control_database(stack)
        with stage(report, "platform"):
            storage_label = install_platform(stack, cellctl_image, report, provider_image=provider_image)
        with stage(report, "agents"):
            agent_a, agent_b = (drill_cluster.start_agent(cluster, name, ip) for name, ip in drill_cluster.AGENTS.items())
            cell_images = {build.load_into_k3s(agent.container, cell_tag) for agent in (agent_a, agent_b)}
            if len(cell_images) != 1:
                raise RuntimeError(f"the agents hold different cell image digests: {cell_images}")
            [cell_image] = cell_images
            if provider_tag:
                for agent in (agent_a, agent_b):
                    loaded_provider = build.load_into_k3s(agent.container, provider_tag)
                    if loaded_provider != provider_image:
                        raise RuntimeError("the agents loaded different reservation providers")
            else:
                run(["docker", "exec", agent_b.container, "crictl", "pull", report.inputs["topolvm_image"]], timeout=600)
            drill = Drill(cluster=cluster, report=report, cell_image=cell_image, storage_label=storage_label,
                          cell_tag=cell_tag, provider_tag=provider_tag)
            report.stages["agents"]["agent_a_volume_group"] = drill_cluster.create_volume_group(agent_a)
            drill.label_storage(agent_a)
            if provider_root:
                with stage(report, "provider_process_chain"):
                    report.stages["provider_process_chain"]["evidence"] = check_provider_process_chain(
                        cluster, agent_a, provider_root, run_id)
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
            ("2.10 online growth, then the cap", check_growth),
            ("6.4 and 7.3 a planned move to another agent", check_planned_move),
        ]
        if provider_root:
            from .local_import_growth import check_import_growth

            checks.append(("5.1 guarded local import growth and stored counts", check_import_growth))
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
        if not guarded(report, "report", lambda: report.write(args.report)):
            code = 2
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
