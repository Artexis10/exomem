"""The large agent-led import rehearsal (`scale-agent-led-imports` task 5.1).

A person's agent brings a large, realistic folder into a Cloud cell through the shipped CLI, files
it page by page through the MCP tools, and discards what stays staged. A second, media-heavy folder
follows into the same cell. The cell runs on a TopoLVM volume that cannot hold two copies of the
first folder, so the run only fits if filing an artifact removes its staged copy.

The pass criteria come from the generator's manifest (`large_import_fixture`), never from the
cell's own replies. Each measurement is stored with what it is, over which window and from which
source. A value that could not be read stays unknown; it is never recorded as zero.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import json
import shutil
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cellctl.manifests import namespace_name
from kubernetes.utils.quantity import parse_quantity

from . import drill_cluster, platform
from . import large_import_fixture as fixture
from .infra import docker_labels
from .mcp_client import TenantClient, ToolResult
from .native_import import retain_cli_diagnostics, run_cli_import
from .report import StepFailure, StepRecord, innermost, percentile
from .scenarios import Context, first_session
from .shell import CommandFailed, run

SEED = "exomem-large-import-v1"
# The loop-device disk is sparse and attached before K3s starts, so its size must not depend on the
# data. The volume group's pool is created later, at the size that the measured free disk allows;
# this is the largest pool that disk can carry (a pool of 95 GiB serves a volume of 17 GiB).
MAX_POOL_GIB = 95
SAMPLE_SECONDS = 10
DRAIN_POLLS, DRAIN_SECONDS, DRAIN_TIMEOUT = 3, 15, 900
INDEX_LAPS = {"notes": 1, "media": 3}
GATEWAY_REQUIRED = "CLOUD_GATEWAY_REQUIRED"  # the cell's documented refusal of a control-plane tool
APP_LOG_TAIL_BYTES = 20 << 20  # kept per file of the cell's app log; the pod's /tmp holds at most 64 MiB
TRANSIENT_ATTEMPTS = 8
CELL_COLLECTION = {"notes": "Imported notes", "media": "Imported media"}

# Everything below runs inside the cell's pod, against its volume, with the cell's own interpreter.
CEILING_SCRIPT = """import os
from exomem import client_artifacts, cloud_cell
os.environ[cloud_cell.CLOUD_MODE_ENV] = '1'
print(client_artifacts.adoption_ceiling())
"""
POD_CEILING_SCRIPT = "from exomem import client_artifacts; print(client_artifacts.adoption_ceiling())"
RECEIPT_SCRIPT = """import json, sys
from pathlib import Path
from exomem import import_stage
from exomem.state_paths import vault_state_dir
vault = Path('/data/vault')
receipt = json.loads((vault_state_dir(vault) / 'cloud-imports' / (sys.argv[1] + '.json')).read_text())
print(json.dumps({'proof': import_stage.CLOUD_PROOF, 'members': receipt['members'], 'result': receipt.get('result')}))
"""
# Streamed in 1 MiB blocks: the stage can hold a file of a few hundred MiB.
STAGE_SCRIPT = """import hashlib, json, os, sys
root = '/data/vault/_Imports/' + sys.argv[1]
found = {}
for directory, _names, files in os.walk(root):
    for name in files:
        full = os.path.join(directory, name)
        digest, size = hashlib.sha256(), 0
        with open(full, 'rb') as handle:
            for block in iter(lambda: handle.read(1 << 20), b''):
                digest.update(block)
                size += len(block)
        found[os.path.relpath(full, root)] = [size, digest.hexdigest()]
print(json.dumps(found))
"""
STAGE_GONE_SCRIPT = "import json, os, sys; print(json.dumps(not os.path.exists('/data/vault/_Imports/' + sys.argv[1])))"
INDEX_SCRIPT = """import json, sys, time
from pathlib import Path
from exomem import adoption_plan
root = Path('/data/vault')
laps, index = [], []
for _ in range(int(sys.argv[1])):
    started = time.perf_counter()
    index = adoption_plan.read_import_index(root)
    laps.append(round(time.perf_counter() - started, 3))
sources = {}
for entry in index:
    sources.setdefault(entry.imported_from, []).append(entry.original_sha256)
print(json.dumps({'laps': laps, 'entries': len(index), 'sources': sources}))
"""
# The queues that hold files still to be indexed. A reading that fails is None, never 0.
DRAIN_SCRIPT = """import json
from pathlib import Path
from exomem import deferred_index
vault = Path('/data/vault')
counts = {}
for name in ('status', 'full_status', 'graph_status'):
    try:
        counts[name] = int(getattr(deferred_index, name)(vault)['count'])
    except Exception:
        counts[name] = None
print(json.dumps(counts))
"""
SAMPLE_SCRIPT = """import json, os
def read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None
def number(text):
    return int(text) if text and text.isdigit() else None
cg = '/sys/fs/cgroup/'
current, peak, limit = (number(read(cg + name)) for name in ('memory.current', 'memory.peak', 'memory.max'))
anon = None
for line in (read(cg + 'memory.stat') or '').splitlines():
    key, _, value = line.partition(' ')
    if key == 'anon':
        anon = int(value)
if current is None:
    current, peak, limit = (number(read(cg + 'memory/' + name)) for name in
                            ('memory.usage_in_bytes', 'memory.max_usage_in_bytes', 'memory.limit_in_bytes'))
total = used = None
try:
    fs = os.statvfs('/data')
    total, used = fs.f_blocks * fs.f_frsize, (fs.f_blocks - fs.f_bfree) * fs.f_frsize
except OSError:
    pass
ready = None
try:
    import urllib.error, urllib.request
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/health/ready', timeout=3) as reply:
            body = json.loads(reply.read())
    except urllib.error.HTTPError as refused:
        body = json.loads(refused.read())
    # What readiness waits on, so a slow start shows its cause in the samples.
    ready = {key: body.get(key) for key in ('status', 'reasons')}
    ready['retrieval'] = {key: (body.get('retrieval') or {}).get(key) for key in ('state',)}
    ready['repair'] = {key: (body.get('repair') or (body.get('retrieval') or {}).get('repair') or {}).get(key)
                       for key in ('phase', 'age_seconds', 'last_result', 'last_duration_seconds')}
except Exception:  # a stopped or starting server leaves the sample without readiness, never without memory
    pass
print(json.dumps({'memory_current': current, 'memory_anon': anon, 'memory_peak': peak, 'memory_limit': limit,
                  'volume_total': total, 'volume_used': used, 'ready': ready}))
"""
# The last bytes of each file in the cell's log folder (EXOMEM_LOG_DIR, set by the cell's manifest), compressed
# and encoded so that any byte survives the text channel of `kubectl exec`.
APP_LOG_SCRIPT = """import base64, gzip, json, os, sys
root = os.environ.get('EXOMEM_LOG_DIR') or '/tmp/exomem-logs'
found = {}
for name in sorted(os.listdir(root)):
    full = os.path.join(root, name)
    if not os.path.isfile(full):
        continue
    with open(full, 'rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - int(sys.argv[1])))
        found[name] = {'size': size, 'tail': base64.b64encode(gzip.compress(handle.read())).decode()}
print(json.dumps(found))
"""


class OutOfTime(StepFailure):
    """The scenario's own deadline came before the work finished."""


@dataclass
class LargeImportPlan:
    """What `__main__` decides before the cluster has a cell: the data, the volume and the deadline."""

    sizing: fixture.Sizing
    ceiling: int
    cli_image: str
    deadline: float  # a time.monotonic() value
    free_bytes: int  # the runner's free disk when the sizing was made
    smoke: bool = False
    seed: str = SEED
    node: drill_cluster.Agent | None = None

    @property
    def volume_gib(self) -> int:
        return self.sizing.volume_gib

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def require_time(self, what: str, *, seconds: float = 0) -> None:
        if self.remaining() < seconds:
            raise OutOfTime(f"the scenario's deadline came before {what}")


def make_plan(*, run_id: str, workdir: Path, cli_image: str, files: int, target_bytes: int, smoke: bool,
              deadline: float) -> LargeImportPlan:
    """Size the folders and the volume from the runner's free disk, once the images are built and loaded.

    Without `smoke`, data that cannot show that the volume is below two copies is refused here,
    before the cluster has a cell: the run could not prove its claim.
    """
    free = shutil.disk_usage(workdir).free
    ceiling = cloud_ceiling(cli_image, run_id)
    sizing = fixture.plan_sizes(free_bytes=free, ceiling=ceiling, files=files, target_bytes=target_bytes)
    if not sizing.proves_one_copy_only and not smoke:
        raise fixture.SizingError(f"{sizing.note}; rerun on a runner with more free disk, or ask for a smoke run")
    if drill_cluster.pool_gib_for(sizing.volume_gib) > MAX_POOL_GIB:
        raise fixture.SizingError(f"a {sizing.volume_gib} GiB volume needs a pool of "
                                  f"{drill_cluster.pool_gib_for(sizing.volume_gib)} GiB, above the node disk's {MAX_POOL_GIB} GiB")
    return LargeImportPlan(sizing, ceiling, cli_image, deadline, free, smoke)


def cloud_ceiling(image: str, run_id: str) -> int:
    """The adoption ceiling a Cloud cell runs with, from the image itself, before any cell exists."""
    result = run(["docker", "run", "--rm", *docker_labels(run_id), "--network=none", "--tmpfs", "/data",
                  "--entrypoint=python3", image, "-c", CEILING_SCRIPT])
    return int(result.stdout.strip().splitlines()[-1])


# --- pure decisions ------------------------------------------------------------------------


def body_of(result: ToolResult) -> dict:
    """The reply object of a tool; the producer wraps a non-object output under `result`."""
    inner = result.structured.get("result")
    return inner if isinstance(inner, dict) and len(result.structured) == 1 else result.structured


def reported_states(value: Any) -> set[str]:
    """Every string that a reply gives under a `state` or `status` key, at any depth."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("state", "status", "observed_state") and isinstance(item, str):
                found.add(item)
            found |= reported_states(item)
    elif isinstance(value, list):
        for item in value:
            found |= reported_states(item)
    return found


def tolerated_refusals() -> frozenset[str]:
    """The refusals of a control-plane tool that the run reports without failing.

    A cell refuses `observe_transfer` and `transfer_memory` with `CLOUD_GATEWAY_REQUIRED` while the chart leaves
    the Cloud transfer tools off. The rehearsal sets that chart value (`platform.TRANSFER_TOOLS_ENABLED`); with the
    tools on, the same refusal means the tools are broken and fails the call.
    """
    return frozenset() if platform.TRANSFER_TOOLS_ENABLED else frozenset({GATEWAY_REQUIRED})


def apply_step(reply: Mapping[str, Any], seconds: float, *, before: int | None) -> dict[str, Any]:
    """One apply call as the page report keeps it: the reply's counts, the files this call filed, and whether it stopped.

    The reply's `applied` count is cumulative, so `before` is the count after the call before. A count that a
    reply does not carry leaves what depends on it unknown (None), never 0.
    """
    progress = reply.get("progress") or {}
    applied = progress.get("applied")
    return {"seconds": round(seconds, 2), "applied": applied, "remaining": progress.get("remaining"),
            "failed": progress.get("failed"), "stopped": bool(reply.get("apply_stop")),
            "files": applied - before if applied is not None and before is not None else None}


def files_per_second(steps: Sequence[Mapping[str, Any]]) -> float | None:
    """Files filed over the seconds of the apply calls that filed them; None when a call's files are unknown."""
    seconds = sum(step["seconds"] for step in steps)
    if not seconds or any(step["files"] is None for step in steps):
        return None
    return round(sum(step["files"] for step in steps) / seconds, 2)


def is_drained(polls: Sequence[Mapping[str, int | None]], needed: int = DRAIN_POLLS) -> bool:
    """The index queues were all empty on the last `needed` readings. A failed reading is not empty."""
    last = polls[-needed:]
    return len(last) == needed and all(count == 0 for poll in last for count in poll.values())


def peaks(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Highest reading per series over the ticks that could read it; None when no tick could."""

    def high(key: str, function=max) -> int | float | None:
        values = [row[key] for row in rows if row.get(key) is not None]
        return function(values) if values else None

    return {
        "ticks": len(rows),
        "ticks_with_cell_reading": sum(1 for row in rows if row.get("volume_used") is not None),
        "peak_volume_used_bytes": high("volume_used"),
        "volume_total_bytes": high("volume_total"),
        "peak_memory_current_bytes": high("memory_current"),
        "peak_memory_anon_bytes": high("memory_anon"),
        "cgroup_memory_peak_bytes": high("memory_peak"),
        "memory_limit_bytes": high("memory_limit"),
        "peak_thin_pool_allocated_bytes": high("thin_pool_allocated"),
        "peak_thin_volume_allocated_bytes": high("thin_volume_allocated"),
        "min_runner_free_bytes": high("runner_free", min),
    }


def decide_verdict(folders: Mapping[str, Mapping[str, Sequence[str]] | None], volume_problems: Sequence[str],
                   findings: Sequence[Any], *, smoke: bool, proves_one_copy_only: bool) -> str:
    """`pass` needs both folders judged with no problem, on a volume that cannot hold two copies.

    `smoke_only` is the same result on data that cannot prove the volume claim, or when asked for.
    """
    judged = [criteria for criteria in folders.values() if criteria is not None]
    if findings or len(judged) < len(folders) or volume_problems:
        return "fail"
    if any(problems for criteria in judged for problems in criteria.values()):
        return "fail"
    return "smoke_only" if smoke or not proves_one_copy_only else "pass"


# --- the person's agent --------------------------------------------------------------------


class Agent:
    """The MCP client of the person's agent. Each call opens its own session, so a restart of the
    cell between calls costs a retry and nothing more."""

    def __init__(self, client: TenantClient) -> None:
        self.client = client
        self.retries: list[str] = []
        self.seconds: dict[str, list[float]] = {}

    async def call(self, name: str, arguments: dict[str, Any], *, kind: str = "",
                   tolerated: frozenset[str] = frozenset()) -> tuple[dict, float]:
        """One tool call; an error code in `tolerated` comes back as `{"error_code": ...}` instead of failing."""
        label = f"{name}.{kind}" if kind else name
        last = "no attempt"
        for attempt in range(TRANSIENT_ATTEMPTS):
            try:
                async with self.client.mcp() as session:
                    result = await session.call(name, arguments)
            except Exception as error:  # noqa: BLE001 - a transport failure is retried, then reported
                last = f"{type(innermost(error)).__name__}: {str(innermost(error))[:200]}"
                self.retries.append(f"{label}: {last}")
                await asyncio.sleep(min(30, 2**attempt))
                continue
            if result.error_code == "MUTATION_WARMING":
                self.retries.append(f"{label}: MUTATION_WARMING")
                await asyncio.sleep(max(0.1, (result.structured.get("error") or {}).get("retry_after_ms", 500) / 1000))
                continue
            if result.error_code in tolerated:
                return {"error_code": result.error_code}, result.seconds
            if result.is_error or result.error_code:
                raise StepFailure(f"{label} failed: {result.error_code or result.text()[:500]}")
            self.seconds.setdefault(label, []).append(result.seconds)
            return body_of(result), result.seconds
        raise StepFailure(f"{label} kept failing after {TRANSIENT_ATTEMPTS} attempts; last: {last}")

    async def import_op(self, kind: str, **fields: Any) -> tuple[dict, float]:
        return await self.call("import_memory", {"operation": {"kind": kind, **fields}}, kind=kind)

    def stats(self) -> dict[str, Any]:
        return {
            "retries": len(self.retries), "retry_examples": self.retries[:10],
            "calls": {label: {"count": len(values), "total_seconds": round(sum(values), 1),
                              "p50_seconds": percentile(values, 0.5), "p95_seconds": percentile(values, 0.95),
                              "max_seconds": max(values)} for label, values in sorted(self.seconds.items())},
        }


# --- reading the cell ----------------------------------------------------------------------


def exec_json(ctx: Context, script: str, *args: str, attempts: int = 4) -> Any:
    """Run a script in the cell's pod and parse its one line of JSON. The pod may be replaced
    between attempts, for a backup or a discard, so each attempt looks it up again."""
    last: Exception | None = None
    for attempt in range(attempts):
        pod = ctx.runtime_pod(ctx.a.cell_id)
        try:
            if pod is None:
                raise RuntimeError("the cell has no runtime pod")
            result = ctx.exec_in(namespace_name(ctx.a.cell_id), pod["metadata"]["name"], "python3", "-c", script, *args)
            return json.loads(result.stdout)
        except (CommandFailed, RuntimeError, ValueError) as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(15)
    raise StepFailure(f"the cell's pod did not answer a probe after {attempts} attempts: {last}")


def thin_usage(node: drill_cluster.Agent) -> dict[str, int | None]:
    """What the thin pool and its largest volume have allocated, from LVM on the node."""
    output = run(["docker", "exec", node.container, "lvs", "--reportformat", "json", "--units", "b", "--nosuffix",
                  "-o", "lv_name,lv_attr,lv_size,data_percent,pool_lv", drill_cluster.VOLUME_GROUP], timeout=60).stdout
    rows = json.loads(output)["report"][0]["lv"]

    def allocated(row: Mapping[str, str]) -> int | None:
        try:
            return int(float(row["lv_size"]) * float(row["data_percent"]) / 100)
        except ValueError:
            return None

    # LVM fixes these attribute letters: `t` is a thin pool, `V` a thin volume.
    pools = [allocated(row) for row in rows if row["lv_attr"].startswith("t")]
    volumes = [allocated(row) for row in rows if row["lv_attr"].startswith("V")]
    return {"thin_pool_allocated": max((p for p in pools if p is not None), default=None),
            "thin_volume_allocated": max((v for v in volumes if v is not None), default=None)}


class Sampler:
    """Reads the volume, the cell's memory and the runner's disk every few seconds, labelled by phase.

    Each peak is a lower bound: the true peak can fall between two ticks, and no tick reads the cell
    while an import or discard Job holds its volume.
    """

    def __init__(self, ctx: Context, plan: LargeImportPlan) -> None:
        self.ctx, self.plan = ctx, plan
        self.label = "setup"
        self.rows: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self._started = time.monotonic()

    def read(self) -> dict[str, Any]:
        row: dict[str, Any] = {"at": round(time.monotonic() - self._started, 1), "label": self.label,
                               "runner_free": shutil.disk_usage(self.ctx.stack.workdir).free}
        try:
            pod = self.ctx.runtime_pod(self.ctx.a.cell_id)
            if pod is not None:
                result = self.ctx.exec_in(namespace_name(self.ctx.a.cell_id), pod["metadata"]["name"],
                                          "python3", "-c", SAMPLE_SCRIPT)
                row.update(json.loads(result.stdout))
        except Exception as error:  # noqa: BLE001 - an unread tick stays unknown
            row["cell_error"] = type(error).__name__
        if self.plan.node is not None:
            try:
                row.update(thin_usage(self.plan.node))
            except Exception as error:  # noqa: BLE001 - an unread tick stays unknown
                row["lvm_error"] = type(error).__name__
        return row

    async def run(self) -> None:
        while not self._stop.is_set():
            self.rows.append(await asyncio.to_thread(self.read))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), SAMPLE_SECONDS)

    async def stop(self, task: asyncio.Task) -> None:
        self._stop.set()
        await task

    def summary(self) -> dict[str, Any]:
        labels = sorted({row["label"] for row in self.rows})
        return {"interval_seconds": SAMPLE_SECONDS, "all": peaks(self.rows),
                "by_phase": {label: peaks([row for row in self.rows if row["label"] == label]) for label in labels}}


# --- the folders ---------------------------------------------------------------------------


@dataclass
class Folder:
    name: str
    source: Path
    specs: list[fixture.Spec]
    entries: list[fixture.Entry]
    manifest_sha256: str
    include_hidden: bool
    sentinel: str
    import_id: str = ""
    proof: str = ""  # the receipt's proof token, read from the cell
    residual: Mapping | None = None  # the last page's report, which covers the whole stage
    consumed: int = 0
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def marked_path(self) -> str:
        return next(spec.path for spec in self.specs if spec.marked)


def df_text(path: Path) -> str:
    return run(["df", "-h", str(path)]).stdout


async def generate(ctx: Context, plan: LargeImportPlan, name: str) -> Folder:
    first = name == "notes"
    files = plan.sizing.files_notes if first else plan.sizing.files_media
    total = plan.sizing.notes_bytes if first else plan.sizing.media_bytes
    workdir = ctx.stack.workdir
    free = shutil.disk_usage(workdir).free
    need = fixture.peak_disk_bytes(total) if first else fixture.generation_need_bytes(total)
    if free < need:
        raise fixture.SizingError(f"{free / fixture.GIB:.1f} GiB free cannot hold the {name} folder, which needs {need / fixture.GIB:.1f} GiB")
    # Hidden files go to the cell for the first folder only; the second uses the client's defaults.
    include_hidden = first
    before = df_text(workdir)
    print(f"[rehearsal] df -h before generating the {name} folder\n{before}", flush=True)
    specs = fixture.layout(name, seed=plan.seed, files=files, total_bytes=total, ceiling=plan.ceiling,
                           large_min=fixture.LARGE_MIN_BYTES, include_hidden=include_hidden)
    sentinel = fixture.sentinel_phrase(plan.seed, name)
    source = workdir / f"large-import-{name}"
    entries = await asyncio.to_thread(fixture.write_tree, source, specs, seed=plan.seed, sentinel=sentinel)
    manifest = workdir / f"manifest-{name}.jsonl"
    digest = await asyncio.to_thread(fixture.write_manifest, manifest, entries)
    diagnostics = workdir / "diagnostics"
    diagnostics.mkdir(exist_ok=True)
    with manifest.open("rb") as raw, gzip.open(diagnostics / f"manifest-{name}.jsonl.gz", "wb") as packed:
        shutil.copyfileobj(raw, packed)
    after = df_text(workdir)
    print(f"[rehearsal] df -h after generating the {name} folder\n{after}", flush=True)
    folder = Folder(name, source, specs, entries, digest, include_hidden, sentinel)
    by_disposition: dict[str, int] = {}
    for entry in entries:
        by_disposition[entry.disposition] = by_disposition.get(entry.disposition, 0) + 1
    folder.report.update({
        "files": len(entries), "bytes": sum(entry.size for entry in entries), "manifest_sha256": digest,
        "manifest_file": f"manifest-{name}.jsonl.gz in the diagnostics artifact",
        "files_by_expected_disposition": by_disposition, "client_sends_hidden_files": include_hidden,
        "sentinel_note": folder.marked_path, "df_before_generation": before, "df_after_generation": after,
    })
    return folder


async def upload(ctx: Context, plan: LargeImportPlan, folder: Folder) -> dict:
    """The CLI import as a person runs it; returns the import's row once the cell has resumed."""
    try:
        cli = await run_cli_import(ctx, plan.cli_image, folder.source, label=folder.name,
                                   include_hidden=folder.include_hidden, wait_seconds=int(max(300, min(2700, plan.remaining()))))
    finally:
        with contextlib.suppress(Exception):
            folder.report["cli_diagnostics"] = retain_cli_diagnostics(ctx, folder.name)
    folder.import_id = cli.import_id
    # The plain tree and the client's checkpoint (its ciphertext copy) are not needed again; the runner's disk is.
    for leftover in (folder.source, cli.checkpoint):
        await asyncio.to_thread(shutil.rmtree, leftover)
    run(["docker", "rm", "--force", cli.name], check=False, timeout=60)
    row = await ctx.fetchrow("SELECT * FROM exomem_cloud_imports WHERE cell_id = $1 AND import_id = $2::uuid",
                             ctx.a.cell_id, cli.import_id)
    await ctx.wait_cell(ctx.a.cell_id, lambda cell: cell.get("ready") and cell.get("observed_state") == "running"
                        and cell.get("hold_kind") is None, timeout=900, description="the imported cell to resume ready")
    return row or {}


def import_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("observed_state", "requested_state", "error_code", "declared_files", "declared_bytes", "ciphertext_bytes",
            "required_storage_gib", "staged_files", "staged_bytes")
    return {key: row.get(key) for key in keys if key in row}


async def prove_stage(ctx: Context, agent: Agent, folder: Folder, row: Mapping[str, Any]) -> list[str]:
    """The stage's manifest proof against the generator: the receipt, the import row and the tool's view."""
    problems = []
    sent = fixture.expected_stage(folder.entries)
    for key, expected in (("declared_files", len(sent)), ("declared_bytes", sum(size for size, _ in sent.values())),
                          ("staged_files", len(sent)), ("staged_bytes", sum(size for size, _ in sent.values()))):
        if row.get(key) is not None and row[key] != expected:
            problems.append(f"the import row says {key} {row[key]}, the generator sent {expected}")
    receipt = await asyncio.to_thread(exec_json, ctx, RECEIPT_SCRIPT, folder.import_id)
    problems += fixture.check_members(folder.entries, receipt["members"])
    folder.report["receipt"] = {"proof": receipt["proof"], "members": len(receipt["members"])}
    folder.proof = receipt["proof"]
    states: set[str] = set()
    for _ in range(4):
        # The chart serves observe_transfer from the gateway only when cloudGateway.transferTools is on; the CLI
        # already waited for `done`, so while the rehearsal leaves the tools off, the refusal is reported, not fatal.
        body, seconds = await agent.call("observe_transfer", {"action": "status", "import_id": folder.import_id},
                                         tolerated=tolerated_refusals())
        if body.get("error_code") == GATEWAY_REQUIRED:
            folder.report["observe_transfer"] = {"seconds": seconds, "served": False, "error_code": GATEWAY_REQUIRED}
            return problems
        states = reported_states(body)
        folder.report["observe_transfer"] = {"seconds": seconds, "served": True, "states_reported": sorted(states),
                                             "reply_head": json.dumps(body, sort_keys=True)[:1200]}
        if "done" in states:
            break
        await asyncio.sleep(10)
    # The tool's reply shape is not fixed by a schema; the import row above is the gate, this is reported.
    folder.report["observe_transfer"]["reported_done"] = "done" in states if states else None
    return problems


async def read_run(agent: Agent, run_id: str) -> dict[str, Any]:
    """The run as `read_import` status reports it, with what the compact `import_memory` replies leave out.

    The flow reads its control fields from the compact replies, as an agent does. The measurements also need
    the inventory, the recorded stage and the finish count. A read changes nothing and keeps those replies
    compact, where `response_detail: "full"` would wrap each reply in the terminal envelope and add a payload per call.
    """
    body, _ = await agent.call("read_import", {"operation": {"kind": "status", "run_id": run_id}}, kind="status")
    return body


async def apply_calls(agent: Agent, plan: LargeImportPlan, run_id: str, plan_id: str, *, applied_before: int | None = 0,
                      **first: Any) -> dict[str, Any]:
    """Call apply until a reply carries no continuation, or stops for lack of space.

    `applied_before` is the files already filed in the run, so the first call's own count is its difference.
    """
    fields: dict[str, Any] = {"run_id": run_id, "plan_id": plan_id, **first}
    seconds, steps = 0.0, []
    while True:
        plan.require_time("the page was filed")
        reply, took = await agent.import_op("apply", **fields)
        seconds += took
        steps.append(apply_step(reply, took, before=steps[-1]["applied"] if steps else applied_before))
        token = reply.get("continuation")
        if reply.get("apply_stop") or not token:
            return {"reply": reply, "seconds": round(seconds, 2), "calls": len(steps), "stop": reply.get("apply_stop"),
                    "steps": steps}
        fields = {"run_id": run_id, "plan_id": plan_id, "continuation": token}


async def file_pages(agent: Agent, plan: LargeImportPlan, folder: Folder) -> dict[str, list[str]]:
    """Page through the stage as the tool text says. Returns the problems found, by criterion."""
    stage_path = f"{fixture.STAGE_DIR}/{folder.import_id}"
    pages: list[dict[str, Any]] = folder.report.setdefault("pages", [])
    problems: dict[str, list[str]] = {"manifest_proof": [], "no_space_failure": [], "filing": [], "compact_reply": []}
    consumed = 0
    fields: dict[str, Any] = {"path": stage_path, "collection": CELL_COLLECTION[folder.name],
                              "initialize_kb": folder.name == "notes"}
    while True:
        plan.require_time(f"page {len(pages) + 1} of the {folder.name} folder was filed")
        reply, start_seconds = await agent.import_op("start", **fields)
        run_id = reply["run_id"]
        stored = await read_run(agent, run_id)
        rows = stored.get("inventory") or []
        page: dict[str, Any] = {"page": len(pages) + 1, "run_id": run_id, "rows": len(rows),
                                "row_bytes": sum(int(row.get("bytes") or 0) for row in rows),
                                "hidden_files_skipped": stored.get("hidden_files_skipped"),
                                "start_seconds": round(start_seconds, 2)}
        pages.append(page)
        if stored.get("stage") != {"stage": folder.import_id, "proof": folder.proof}:
            problems["manifest_proof"].append(f"page {page['page']} recorded the stage as {stored.get('stage')}, not its manifest proof")
        next_after = reply.get("next_after")
        if next_after != stored.get("next_after"):
            problems["compact_reply"].append(
                f"page {page['page']}: the compact start reply carried next_after {next_after!r}, the run holds {stored.get('next_after')!r}")
        await agent.import_op("select", run_id=run_id, include=[stage_path])
        planned, plan_seconds = await agent.import_op("plan", run_id=run_id)
        while planned.get("continuation"):
            more, took = await agent.import_op("plan", run_id=run_id, continuation=planned["continuation"])
            planned, plan_seconds = more, plan_seconds + took
        plan_id = (planned.get("plan") or {}).get("plan_id") or planned.get("plan_id")
        if not plan_id:
            raise StepFailure(f"page {page['page']}: plan returned no plan_id; keys {sorted(planned)}")
        page["plan_seconds"] = round(plan_seconds, 2)
        eligible = {row["path"] for row in rows if row.get("eligible")}
        large = [entry for entry in folder.entries if entry.disposition == fixture.FILED_ARTIFACT
                 and entry.size >= fixture.LARGE_MIN_BYTES and f"{stage_path}/{entry.path}" in eligible]
        solo: dict[str, Any] = {"seconds": 0.0, "calls": 0, "steps": []}
        if large and "large_binaries" not in folder.report:
            # Applied alone first, so that their copy and hash time is measured on its own.
            solo = await apply_calls(agent, plan, run_id, plan_id, only_paths=[f"{stage_path}/{e.path}" for e in large])
            size = sum(entry.size for entry in large)
            folder.report["large_binaries"] = {
                "files": len(large), "bytes": size, "seconds": solo["seconds"], "calls": solo["calls"],
                "mib_per_second": round(size / fixture.MIB / solo["seconds"], 2) if solo["seconds"] else None,
                "what": "bytes of the large files over the seconds of the apply calls that handled only them, "
                        "which hash, copy, write the Source and remove the staged copy",
            }
            if solo["stop"]:
                problems["no_space_failure"].append(f"page {page['page']}: apply stopped for lack of space: {json.dumps(solo['stop'])[:400]}")
                page["apply_stop"], page["apply_steps"] = solo["stop"], solo["steps"]
                break
        rest = await apply_calls(agent, plan, run_id, plan_id,
                                 applied_before=solo["steps"][-1]["applied"] if solo["steps"] else 0)
        steps = [*solo["steps"], *rest["steps"]]
        page["apply_seconds"] = round(solo["seconds"] + rest["seconds"], 2)
        page["apply_calls"] = solo["calls"] + rest["calls"]
        page["apply_steps"] = steps
        page["files_per_second"] = files_per_second(steps)
        page["progress"] = rest["reply"].get("progress")
        failed = (page["progress"] or {}).get("failed")
        if failed:
            problems["filing"].append(f"page {page['page']}: {failed} files failed to file")
        if rest["stop"]:
            problems["no_space_failure"].append(f"page {page['page']}: apply stopped for lack of space: {json.dumps(rest['stop'])[:400]}")
            page["apply_stop"] = rest["stop"]
            break
        finished, finish_seconds = await agent.import_op("finish", run_id=run_id)
        finish = finished.get("finish") or {}
        recorded = (await read_run(agent, run_id)).get("finish") or {}
        if finish.get("residual") != recorded.get("residual"):
            problems["compact_reply"].append(
                f"page {page['page']}: the compact finish reply carried residual {finish.get('residual')!r}, the run holds {recorded.get('residual')!r}")
        folder.residual = finish.get("residual")
        consumed += int(recorded.get("consumed") or 0)
        page.update({"finish_seconds": round(finish_seconds, 2), "consumed": recorded.get("consumed"),
                     "residual_files": (finish.get("residual") or {}).get("files")})
        print(f"[rehearsal] large-import {folder.name} page {page['page']}: {page['rows']} rows, "
              f"apply {page['apply_seconds']}s ({page['files_per_second']} files/s), consumed {page['consumed']}", flush=True)
        if not next_after:
            break
        fields = {"after": next_after}
    folder.consumed = consumed
    folder.report["residual_by_reason_files"] = fixture.residual_files_by_reason(folder.residual)
    folder.report["residual_files_left"] = int((folder.residual or {}).get("files", 0))
    folder.report["consumed"] = consumed
    return problems


def enospc_count(ctx: Context, seen: dict[str, int]) -> None:
    """Count the OS's out-of-space message and the product's own stop code in the cell's logs, per pod."""
    pod = ctx.runtime_pod(ctx.a.cell_id)
    if pod is None:
        return
    name = pod["metadata"]["name"]
    for flags in ((), ("--previous",)):
        logs = ctx.kubectl("logs", "--namespace", namespace_name(ctx.a.cell_id), name, "--container", "exomem",
                           *flags, check=False).stdout
        # ENOSPC's strerror and the product's apply_stop code are fixed strings, not meanings read from text.
        count = logs.count("No space left on device") + logs.count("INSUFFICIENT_SPACE")
        key = name + (" (previous container)" if flags else "")
        seen[key] = max(seen.get(key, 0), count)


def pod_health(ctx: Context) -> dict[str, Any]:
    """The cell pod's restarts and the reason of its last termination (an OOM kill shows here)."""
    pod = ctx.runtime_pod(ctx.a.cell_id)
    if pod is None:
        return {"pod": None}
    statuses = pod["status"].get("containerStatuses") or []
    return {"pod": pod["metadata"]["name"], "containers": [
        {"name": item["name"], "ready": item["ready"], "restarts": item["restartCount"],
         "last_termination_reason": ((item.get("lastState") or {}).get("terminated") or {}).get("reason")}
        for item in statuses]}


def save_cell_logs(ctx: Context) -> None:
    """Keep the end of the cell pod's log with the diagnostics; a failed run is too long to repeat for one."""
    pod = ctx.runtime_pod(ctx.a.cell_id)
    if pod is None:
        return
    logs = ctx.kubectl("logs", "--namespace", namespace_name(ctx.a.cell_id), pod["metadata"]["name"],
                       "--container", "exomem", "--tail=3000", check=False)
    (ctx.stack.workdir / "diagnostics" / "cell-pod.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")


def save_cell_app_logs(ctx: Context) -> dict[str, Any]:
    """Keep the tail of each file in the cell's app log folder with the diagnostics, next to the pod log.

    The folder sits on the pod's memory-backed /tmp, so a replaced pod takes its logs with it; this reads the
    live pod. Returns each file's size and the bytes kept, so a cut tail shows as one.
    """
    found = exec_json(ctx, APP_LOG_SCRIPT, str(APP_LOG_TAIL_BYTES), attempts=2)
    diagnostics = ctx.stack.workdir / "diagnostics"
    diagnostics.mkdir(exist_ok=True)
    kept = {}
    for name, item in found.items():
        tail = gzip.decompress(base64.b64decode(item["tail"]))
        (diagnostics / f"cell-app-{Path(name).name}").write_bytes(tail)
        kept[name] = {"file_bytes": item["size"], "kept_bytes": len(tail)}
    return kept


async def wait_import_state(ctx: Context, import_id: str, state: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        row = await ctx.fetchrow("SELECT * FROM exomem_cloud_imports WHERE cell_id = $1 AND import_id = $2::uuid",
                                 ctx.a.cell_id, import_id) or {}
        if row.get("observed_state") == state:
            return row
        if time.monotonic() > deadline or row.get("observed_state") in ("failed", "expired"):
            raise StepFailure(f"the import did not reach {state}: {import_summary(row)}")
        await asyncio.sleep(5)


async def discard(ctx: Context, agent: Agent, plan: LargeImportPlan, folder: Folder) -> tuple[list[str], list[str]]:
    """Check the leftover stage against the key, then discard it as the tool text says.

    Returns the problems with the leftover, then the problems with the discard.
    """
    listing = await asyncio.to_thread(exec_json, ctx, STAGE_SCRIPT, folder.import_id)
    leftover = fixture.check_stage(folder.entries, {path: (size, digest) for path, (size, digest) in listing.items()})
    folder.report["stage_left_files"] = len(listing)
    body, seconds = await agent.call("transfer_memory", {"action": "import_discard", "import_id": folder.import_id,
                                                         "confirm": True}, tolerated=tolerated_refusals())
    folder.report["discard_call"] = {"seconds": seconds, "route": "transfer_memory import_discard",
                                     "reply_head": json.dumps(body, sort_keys=True)[:600]}
    if body.get("error_code") == GATEWAY_REQUIRED:
        # Without the gateway's transfer tools, make the one write Substrate's discard route makes
        # (cloud-imports.ts: requested_state = 'discard_requested'); cellctl then runs the real cleanup Job.
        await ctx.execute("UPDATE exomem_cloud_imports SET requested_state = 'discard_requested' "
                          "WHERE cell_id = $1 AND import_id = $2::uuid", ctx.a.cell_id, folder.import_id)
        folder.report["discard_call"]["route"] = "substrate discard write; the gateway does not serve transfer_memory"
    row = await wait_import_state(ctx, folder.import_id, "discarded", min(900, max(60, plan.remaining())))
    await ctx.wait_cell(ctx.a.cell_id, lambda cell: cell.get("ready") and cell.get("observed_state") == "running"
                        and cell.get("hold_kind") is None, timeout=900, description="the cell to resume after the discard")
    gone = await asyncio.to_thread(exec_json, ctx, STAGE_GONE_SCRIPT, folder.import_id)
    folder.report["after_discard"] = {"import": import_summary(row), "stage_folder_gone": gone}
    return leftover, [] if gone is True else ["the stage folder is still in the vault after the discard"]


async def wait_index(ctx: Context, plan: LargeImportPlan) -> dict[str, Any]:
    """Wait until every index queue has been empty on three readings. Reported, never a pass criterion."""
    started = time.monotonic()
    deadline = min(started + DRAIN_TIMEOUT, plan.deadline)
    polls: list[dict[str, int | None]] = []
    while True:
        try:
            polls.append(await asyncio.to_thread(exec_json, ctx, DRAIN_SCRIPT, attempts=1))
        except StepFailure:
            polls.append({"unread": None})
        if is_drained(polls):
            state = "drained"
            break
        if time.monotonic() >= deadline:
            state = "timed_out"
            break
        await asyncio.sleep(DRAIN_SECONDS)
    return {"state": state, "seconds": round(time.monotonic() - started, 1), "polls": len(polls), "last_counts": polls[-1]}


async def check_sources(ctx: Context, agent: Agent, folder: Folder) -> list[str]:
    """Every filed file has a Source with its SHA-256, read back from the Source pages in the pod."""
    found = await asyncio.to_thread(exec_json, ctx, INDEX_SCRIPT, str(INDEX_LAPS[folder.name]))
    problems = fixture.check_sources(folder.entries, folder.import_id, found["sources"])
    folder.report["read_import_index"] = {
        "source_pages": found["entries"], "lap_seconds": found["laps"],
        "what": "adoption_plan.read_import_index over the vault, run in the cell's pod after the index queues "
                "drained; one lap per listed number, each a full walk of the Sources folder",
    }
    body, seconds = await agent.call("ask_memory", {"query": folder.sentinel})
    folder.report["recall"] = {"seconds": seconds, "answers_with_the_original_path": folder.marked_path in json.dumps(body)}
    return problems


async def run_folder(ctx: Context, agent: Agent, plan: LargeImportPlan, out: dict, sampler: Sampler, name: str,
                     space: dict[str, int]) -> dict[str, list[str]]:
    """One folder, from generation to the index check. Returns its criteria as lists of problems."""

    @contextlib.contextmanager
    def phase(step: str) -> Iterator[None]:
        label = f"{name}.{step}"
        sampler.label = label
        started = time.monotonic()
        print(f"[rehearsal] large-import {label}: start", flush=True)
        try:
            yield
        finally:
            out["phases"][label] = round(out["phases"].get(label, 0) + time.monotonic() - started, 1)
            print(f"[rehearsal] large-import {label}: {out['phases'][label]}s", flush=True)

    plan.require_time(f"the {name} folder was generated", seconds=600)
    with phase("generate"):
        folder = await generate(ctx, plan, name)
    out["folders"][name] = folder.report
    criteria: dict[str, list[str]] = {}
    with phase("upload"):
        row = await upload(ctx, plan, folder)
    folder.report["import_row"] = import_summary(row)
    with phase("stage_proof"):
        criteria["manifest_proof"] = await prove_stage(ctx, agent, folder, row)
    with phase("paging"):
        paging = await file_pages(agent, plan, folder)
    await asyncio.to_thread(enospc_count, ctx, space)
    folder.report["pod_after_paging"] = await asyncio.to_thread(pod_health, ctx)
    criteria["manifest_proof"] += paging["manifest_proof"]
    criteria["filing"], criteria["no_space_failure"] = paging["filing"], paging["no_space_failure"]
    criteria["compact_reply"] = paging["compact_reply"]
    if criteria["no_space_failure"]:
        # The stage is still on the volume; going on to the next folder would only repeat the failure.
        raise StepFailure(f"the {name} folder could not be filed: {criteria['no_space_failure'][0]}")
    criteria["consumed"] = fixture.check_consumed(folder.entries, folder.consumed)
    plan.require_time(f"the {name} folder's leftover was discarded", seconds=300)
    with phase("discard"):
        leftover, criteria["discarded"] = await discard(ctx, agent, plan, folder)
    criteria["residual"] = fixture.check_residual(folder.entries, folder.residual) + leftover
    with phase("index_drain"):
        folder.report["index_queues"] = await wait_index(ctx, plan)
    await asyncio.to_thread(enospc_count, ctx, space)
    folder.report["pod_after_index"] = await asyncio.to_thread(pod_health, ctx)
    with phase("source_check"):
        criteria["sources"] = await check_sources(ctx, agent, folder)
    folder.report["criteria"] = {key: {"passed": not problems, "problems": problems[:10], "problem_count": len(problems)}
                                 for key, problems in criteria.items()}
    return criteria


async def volume_state(ctx: Context) -> dict[str, Any]:
    pvc = await asyncio.to_thread(ctx.kube_json, "get", "pvc", "cell-data", "--namespace", namespace_name(ctx.a.cell_id))
    cell = await ctx.cell(ctx.a.cell_id)
    requested = pvc["spec"]["resources"]["requests"]["storage"]
    return {"pvc_request": requested, "pvc_request_bytes": int(parse_quantity(requested)),
            "pvc_storage_class": pvc["spec"].get("storageClassName"), "cell_row_storage_gib": cell.get("storage_gib")}


def measurements(out: dict, sampler: Sampler) -> dict[str, Any]:
    """The five figures the packet asks for, each with what it is and where it came from."""
    summary = sampler.summary()
    every = summary["all"]
    # The index builds while files are filed and again after the discard restarts the cell, so both windows are given.
    drain = {label: peak for label, peak in summary["by_phase"].items() if label.endswith((".paging", ".index_drain"))}
    large = [(name, folder["large_binaries"]) for name, folder in out["folders"].items() if "large_binaries" in folder]
    final = next((folder["read_import_index"] for folder in reversed(list(out["folders"].values()))
                  if "read_import_index" in folder), None)
    return {
        "peak_volume_used": {
            "bytes": every["peak_volume_used_bytes"], "of_total_bytes": every["volume_total_bytes"],
            "what": "highest used bytes of the cell's /data filesystem at any tick (a lower bound: one tick every "
                    f"{SAMPLE_SECONDS} s, and none while an import or discard Job holds the volume)",
            "source": "statvfs in the cell's pod",
            "thin_volume_allocated_peak_bytes": every["peak_thin_volume_allocated_bytes"],
            "thin_volume_allocated_note": "blocks allocated to the largest thin volume, read from lvs on the node; "
                                          "blocks freed by deleted files return only if the filesystem discards them, "
                                          "and it is read while the cell is stopped too",
        },
        "cell_memory_during_index_build": {
            "by_window": {label: {"peak_memory_current_bytes": peak["peak_memory_current_bytes"],
                                  "peak_memory_anon_bytes": peak["peak_memory_anon_bytes"],
                                  "cgroup_memory_peak_bytes": peak["cgroup_memory_peak_bytes"],
                                  "ticks_with_cell_reading": peak["ticks_with_cell_reading"]}
                          for label, peak in drain.items()},
            "memory_limit_bytes": every["memory_limit_bytes"],
            "what": "the cell's memory while files were filed and while the index queues drained (a lower "
                    "bound per tick); cgroup_memory_peak_bytes is the kernel's high-water mark for the pod since "
                    "it started, and a discard restarts the pod, so it covers the window since the last restart",
            "source": "memory.current, memory.stat anon and memory.peak of the pod's cgroup",
        },
        "read_import_index": final,
        "apply_throughput_large_binaries": dict(large) or {"measured": False, "why": "no page listed an eligible large file"},
        "phase_seconds": out["phases"],
    }


async def run_large_import(ctx: Context, plan: LargeImportPlan) -> None:
    """The stage body: write into `report.stages["large_import"]` as it goes, so partial results survive."""
    out = ctx.report.stages["large_import"]
    out.update({
        "mode": "smoke" if plan.smoke else "full", "seed": plan.seed, "ceiling_bytes": plan.ceiling,
        "sizing": asdict(plan.sizing), "runner_free_bytes_at_sizing": plan.free_bytes,
        "volume": {"gib": plan.volume_gib, "boundary": "TopoLVM thin volume on a loop-device pool; the chart caps "
                   "growth at this size, so the claim cannot be enlarged"},
        "transfer_tools_enabled": platform.TRANSFER_TOOLS_ENABLED,
        "phases": {}, "folders": {}, "findings": [], "measurements": {}, "verdict": None,
    })
    ctx.a.client = TenantClient(ctx.resolver, ctx.browser, ctx.a.session, ctx.substrate.redirect_uri)
    record = StepRecord(0, "large_import")
    await first_session(ctx.a.client, record)
    out["gateway_wait"] = record.evidence
    in_cell = await asyncio.to_thread(exec_json, ctx, POD_CEILING_SCRIPT)
    if in_cell != plan.ceiling:
        raise RuntimeError(f"the cell's adoption ceiling is {in_cell}, the fixture was laid out for {plan.ceiling}")
    agent, sampler = Agent(ctx.a.client), Sampler(ctx, plan)
    task = asyncio.create_task(sampler.run())
    space: dict[str, int] = {}
    folders: dict[str, dict[str, list[str]] | None] = {"notes": None, "media": None}
    volume = {"before": await volume_state(ctx)}
    try:
        for name in folders:
            folders[name] = await run_folder(ctx, agent, plan, out, sampler, name, space)
    except StepFailure as error:
        out["findings"].append({"phase": sampler.label, "message": str(error)[:1500], "deadline": isinstance(error, OutOfTime)})
        with contextlib.suppress(Exception):
            latest = await ctx.fetchrow("SELECT * FROM exomem_cloud_imports WHERE cell_id = $1 ORDER BY created_at DESC LIMIT 1",
                                        ctx.a.cell_id)
            out["findings"][-1]["latest_import"] = import_summary(latest or {})
    finally:
        await sampler.stop(task)
        with contextlib.suppress(Exception):
            volume["after"] = await volume_state(ctx)
        out["volume"].update(volume)
        out["enospc_log_counts"] = space
        out["agent_calls"] = agent.stats()
        out["measurements"] = measurements(out, sampler)
        out["sampler"] = sampler.summary()
        (ctx.stack.workdir / "diagnostics").mkdir(exist_ok=True)
        with gzip.open(ctx.stack.workdir / "diagnostics" / "large-import-samples.jsonl.gz", "wt") as handle:
            handle.writelines(json.dumps(row) + "\n" for row in sampler.rows)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(save_cell_logs, ctx)
        try:
            out["cell_app_logs"] = await asyncio.to_thread(save_cell_app_logs, ctx)
        except Exception as error:  # noqa: BLE001 - a log that could not be read is reported, and never fails the run
            out["cell_app_logs"] = {"error": f"{type(error).__name__}: {str(error)[:300]}"}
    expected = plan.volume_gib
    # Design D7: a local-storage cell publishes no import limits and legacy admission never grows its volume.
    problems = [f"the volume claim was {state['pvc_request']} at the {when}, not {expected}Gi"
                for when, state in volume.items() if state.get("pvc_request_bytes") != expected * fixture.GIB]
    problems += [f"the cell row recorded {state['cell_row_storage_gib']} GiB at the {when}, not {expected}"
                 for when, state in volume.items() if state.get("cell_row_storage_gib") != expected]
    if "after" not in volume:
        problems.append("the volume claim could not be read after the run")
    problems += [f"enospc in the cell's logs: {key} {count}" for key, count in space.items() if count]
    out["volume"]["problems"] = problems
    out["verdict"] = decide_verdict(folders, problems, out["findings"], smoke=plan.smoke,
                                    proves_one_copy_only=plan.sizing.proves_one_copy_only)
    print(f"[rehearsal] large-import verdict: {out['verdict']}; findings {len(out['findings'])}", flush=True)
