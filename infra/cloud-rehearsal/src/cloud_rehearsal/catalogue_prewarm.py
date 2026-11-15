"""The catalogue-prewarm mode: rehearse `cellctl.cataloguePrewarm` on a disposable cluster
(warm-cell-catalogue task 3.1).

After provisioning, the mode adds a second tenant whose cell holds a synthetic vault, then runs three
scenarios against that cell. Each scenario publishes a release whose cell image has a new catalogue
identity (the parser version, raised one step per release), opens the cell's upgrade window on demand
and watches the cell's namespace until the cell runs the new image:

1. completed build: the idle cell's attempt waits for the running build, the cell adopts the staged
   catalogue, an edit made after the build is caught up at restart, and no other vault file changes;
2. unfinished build: the forced-attempt boundary falls while the build runs, cellctl cancels the build
   before the stop, and the cell rebuilds cold;
3. insufficient capacity: a filler pod leaves the node too little CPU for the build, the waiting build
   does not defer the idle attempt, and cellctl cancels it before the stop.

For each scenario the receipt holds the build Job's start, end and outcome, the stop and ready times,
the build container's CPU and memory peaks and the configured build timeout. The receipt holds counts,
timings, hashes and the synthetic fixture's invented names; it never holds note contents.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import secrets
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from cellctl.capacity import quantity
from cellctl.cell_readiness import CELL_HEALTH_PORT, CELL_READINESS_PATH
from cellctl.manifests import (
    CATALOGUE_BUILD_CANCELLED_ANNOTATION,
    CATALOGUE_BUILD_OUTCOME_ANNOTATION,
    JOB_KIND_CATALOGUE_BUILD,
    JOB_KIND_LABEL,
    UPGRADE_WINDOW_ANNOTATION,
    UPGRADE_WINDOW_AT_ANNOTATION,
    namespace_name,
)

from . import substrate as substrate_mod
from .platform import CLOUD_NAMESPACE, CataloguePrewarm
from .report import StepFailure
from .scenarios import _running_ready

if TYPE_CHECKING:
    from .scenarios import Context

DEFAULT_NOTES = 12000
SECTIONS_PER_NOTE = 12  # about 4 KiB a note, the size the build time was calibrated on
# One release per scenario. Substrate takes ascending vMAJOR.MINOR.PATCH tags and the P3 steps that use
# v0.0.1 to v0.0.4 do not run in this mode.
RELEASES = ("v0.0.2", "v0.0.3", "v0.0.4")
SCENARIOS = ("completed_build", "unfinished_build", "insufficient_capacity")
CELLCTL_DEPLOYMENT = "cellctl"
STATEFULSET = "cell"  # CellManifestSpec.statefulset_name
DEFER_MARGIN = 35  # idle seconds beyond the idle minutes that cover cellctl's longest wait between passes (30 s)
SHORT_BUILD_SECONDS = 120  # below this the build can end before the deferral has been observed
LONG_BUILD_SECONDS = 330  # above this a cold rebuild risks the 10-minute upgrade ready deadline
WINDOW_BUDGET_SECONDS = LONG_BUILD_SECONDS + 240  # the longest scenario 1 needs inside one window
CAPACITY_BUDGET_SECONDS = 360
FORCE_RESTART_MARGIN = 120  # cellctl's Recreate rollout after its force minutes change
WINDOW_OPEN_MARGIN = 5
POLL_SECONDS = 2.0
TOUCH_SECONDS = 15.0  # well inside the idle minute, so a touch keeps the cell busy
BIND_TIMEOUT = 240
READY_TIMEOUT = 1500
SEED_TIMEOUT = 1200
CATCH_UP_TIMEOUT = 300
CATCH_UP_SLACK = 150  # seconds beyond the idle minute within which an undeferred attempt must start
FILLER_FREE_FRACTION = Decimal("0.75")  # the filler leaves this share of the build's CPU request free
MAX_LISTED_PATHS = 20
DEVIATIONS = (
    "the build's resource profile and timeout are the chart's shipped defaults, and the upgrade window's "
    "idle and force minutes are the schema's 1-minute floor, so a scenario fits inside one UTC hour",
    "the cell image's parser version is raised by one per release with a derived image, not by a source "
    "change; a release that changes the parser also changes the embedding drain's key, which adds "
    "background work after each start that a real parser release would add too",
    "each scenario pins the cell's upgrade window to the current UTC hour by writing the StatefulSet "
    "annotations cellctl keeps, and parks it twelve hours away between scenarios",
    "scenario 2 reaches the unfinished build through the forced-attempt boundary (`forceMinutes`); the "
    "build timeout path is covered by cellctl's unit tests, and the timeout is recorded, not reached",
    "peaks come from the build container's cgroup counters sampled with `kubectl exec` about every poll, "
    "so they are lower bounds; the memory figure is the kernel's peak counter, which includes page cache",
    "the order of the build's cancellation and the stop is observed at poll precision (a few seconds), "
    "and Job times have the Kubernetes API's one-second precision",
    "the cell serves no tenant traffic besides the rehearsal's own in-pod MCP calls, which keep it busy "
    "or idle on purpose",
)

# Runs inside the cell: the synthetic vault. Every note has several sections and observation bullets, so
# the catalogue parses a realistic shape. The sentinel sits in note 0 for the recall check.
SEED = r"""
import json, os, random, sys
from pathlib import Path
from exomem.vault import kb_root
count, folder, sentinel, sections = int(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
root = kb_root(Path(os.environ['EXOMEM_VAULT_PATH'])) / 'Notes' / folder
root.mkdir(parents=True, exist_ok=True)
words = ('amber basalt cedar delta ember fjord glacier harbor island juniper kestrel lantern meadow nebula '
         'orchard pebble quartz ridge summit tundra umber valley willow xenon yarrow zephyr almond birch '
         'cobalt dune estuary fern grove heath iris jasmine kelp lagoon marsh nectar oasis prairie quill '
         'reef sage thicket upland vine wharf yew zenith anchor bridge canal dock').split()
size = 0
for index in range(count):
    rng = random.Random(index)
    lines = [f'# Rehearsal note {index:05d}', '', f'A synthetic note about {rng.choice(words)} and {rng.choice(words)}.', '']
    if index == 0:
        lines += [f'Sentinel phrase: {sentinel}', '']
    for section in range(sections):
        lines += [f'## Section {section}', '', ' '.join(rng.choice(words) for _ in range(40)) + '.', '',
                  f'- [rehearsal] {rng.choice(words)} observed near the {rng.choice(words)} #rehearsal', '']
    text = chr(10).join(lines)
    (root / f'note-{index:05d}.md').write_text(text)
    size += len(text)
print(json.dumps({'notes': count, 'bytes': size}))
"""

# Runs inside the cell with its own bearer: one search, or one governed note write.
MCP = """
import asyncio, json, os, sys
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    operation, first, second = sys.argv[1], sys.argv[2], sys.argv[3]
    headers = {"Authorization": "Bearer " + os.environ["EXOMEM_CLOUD_CELL_TOKEN"]}
    async with httpx.AsyncClient(headers=headers, timeout=120) as http, streamable_http_client(
        "http://127.0.0.1:8765/mcp", http_client=http
    ) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            if operation == "search":
                result = await session.call_tool("search_memory", {"query": first})
            else:
                result = await session.call_tool(
                    "create_note", {"title": first, "content": second, "unlinked_reason": "A rehearsal note that links nothing."})
            structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None) or {}
            text = " ".join(getattr(part, "text", "") for part in result.content)
            error = structured.get("error") if isinstance(structured, dict) else None
            if getattr(result, "isError", False):
                error = error or text[:200] or "the tool reported an error"
            print(json.dumps({"found": first in json.dumps(structured, default=str) or first in text, "error": error}))

asyncio.run(main())
"""

# Runs inside the cell: its catalogue identity, whether the live catalogue holds it, and the catalogue files.
FACTS = """
import json, os
from pathlib import Path
from exomem import catalogue_prewarm, lexstore
vault = Path(os.environ['EXOMEM_VAULT_PATH'])
store = lexstore.LexicalStore(vault)
identity = lexstore.catalog_semantic_identity(vault)
print(json.dumps({'identity': identity,
                  'live_matches': catalogue_prewarm._catalog_valid(store, store.path, 'lexical-store', identity),
                  'files': sorted(entry.name for entry in store.path.parent.glob('.lexical*'))}))
"""

# Runs inside the cell: size, mtime and hash of every vault file, to compare before and after the restart.
MANIFEST = """
import hashlib, json, os
from pathlib import Path
root = Path(os.environ['EXOMEM_VAULT_PATH'])
files = {}
for directory, _, names in os.walk(root):
    for name in names:
        path = Path(directory) / name
        try:
            stat = path.stat()
            files[str(path.relative_to(root))] = [stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()]
        except OSError:
            continue
print(json.dumps(files, separators=(',', ':')))
"""

# Runs inside the cell: the log lines that mention the lexical catalogue, from the current pod's log files.
LOG = """
import glob, json, os
directory = os.environ.get('EXOMEM_LOG_DIR', '/tmp/exomem-logs')
lines = []
for path in sorted(glob.glob(os.path.join(directory, 'exomem.log*'))):
    with open(path, encoding='utf-8', errors='replace') as handle:
        lines += [line.strip() for line in handle if 'lexical' in line]
print(json.dumps(lines[-400:]))
"""

# Runs inside the cell: the `traffic` block of the readiness answer cellctl reads for its idle rule.
TRAFFIC = """
import json, sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(f'http://127.0.0.1:{sys.argv[1]}{sys.argv[2]}', timeout=10) as reply:
        body = reply.read()
except urllib.error.HTTPError as refused:  # a cell that is not ready still answers with its readiness body
    body = refused.read()
print(json.dumps({'traffic': json.loads(body).get('traffic')}))
"""

# Runs inside the build container: its cgroup's CPU time and memory counters (cgroup v2, then v1).
CGROUP = """
import json, time

def read(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return None

def number(*paths):
    for path in paths:
        text = read(path)
        if text and text.strip().isdigit():
            return int(text)
    return None

usage = None
stat = read('/sys/fs/cgroup/cpu.stat')
if stat:
    usage = next((int(line.split()[1]) for line in stat.splitlines() if line.startswith('usage_usec ')), None)
else:
    legacy = number('/sys/fs/cgroup/cpuacct/cpuacct.usage')
    usage = None if legacy is None else legacy // 1000
anon = None
memory = read('/sys/fs/cgroup/memory.stat')
if memory:
    anon = next((int(line.split()[1]) for line in memory.splitlines() if line.startswith('anon ')), None)
print(json.dumps({'mono': time.monotonic(), 'usage_usec': usage, 'anon': anon,
                  'current': number('/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory/memory.usage_in_bytes'),
                  'peak': number('/sys/fs/cgroup/memory.peak', '/sys/fs/cgroup/memory/memory.max_usage_in_bytes')}))
"""

# The two messages a cold catalogue rebuild logs and the one a delta catch-up logs are the product's own
# fixed text (lexstore.py), so this is a check of our own log format, not a reading of anyone's words.
COLD_PREFIXES = ("lexical sync: rebuilding ", "lexical catch-up declined")
PAGES_PREFIX = "lexical catalogue pages materialized"
PAGES_COUNTS = re.compile(r"\b(reparsed|removed|inserted|unchanged|elapsed_ms)=(\d+)")
STAGED_PREFIX = ".lexical-staged-"  # reserved_paths' staged-copy name, a closed set the product fixes


@dataclass(frozen=True)
class CataloguePlan:
    notes: int
    images: tuple[str, ...]
    settings: CataloguePrewarm = field(default_factory=CataloguePrewarm)


@dataclass(frozen=True)
class ForceWindow:
    """A forced-attempt boundary on a whole minute, and when to open the window before it."""

    start_at: dt.datetime
    boundary_at: dt.datetime
    force_minutes: int
    window_hour: int


@dataclass
class Tick:
    """One poll of the cell's namespace, its control row and (when asked) its readiness."""

    mono: float
    wall: dt.datetime
    build: dict[str, Any]
    serving: dict[str, Any]
    annotations: dict[str, str | None]
    row: dict[str, Any]
    idle: dict[str, Any] | None = None

    @property
    def attempting(self) -> bool:
        """The upgrade attempt has begun: the cell's row holds it for an upgrade, or its pod is stopping.

        A backup hold is not the attempt; the hold kinds are the control database's own closed set.
        """

        return self.row["hold_kind"] == "upgrade" or self.serving["old"] != "serving"


# --- pure helpers: Kubernetes JSON, timings, logs, plans ---


def _labels(item: dict[str, Any]) -> dict[str, str]:
    return item.get("metadata", {}).get("labels") or {}


def _time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    moment = dt.datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def _seconds(start: str | None, end: str | None) -> float | None:
    first, last = _time(start), _time(end)
    return None if first is None or last is None else (last - first).total_seconds()


def pod_view(pod: dict[str, Any]) -> dict[str, Any]:
    status = pod.get("status", {})
    scheduled = next((c for c in status.get("conditions") or [] if c.get("type") == "PodScheduled"), {})
    container = next(iter(status.get("containerStatuses") or []), {})
    state = container.get("state") or {}
    unschedulable = scheduled.get("status") == "False" and scheduled.get("reason") == "Unschedulable"
    return {
        "name": pod["metadata"]["name"],
        "node": pod.get("spec", {}).get("nodeName"),
        "phase": status.get("phase"),
        "unschedulable": unschedulable,
        "message": (scheduled.get("message") or "")[:200] if unschedulable else None,
        "container_running": "running" in state,
        "started_at": (state.get("running") or state.get("terminated") or {}).get("startedAt"),
        "finished_at": (state.get("terminated") or {}).get("finishedAt"),
        "exit_code": (state.get("terminated") or {}).get("exitCode"),
    }


def build_view(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The catalogue build Job and its pods, in cellctl's own terms.

    `running` is cellctl's `BuildJob.running` (a pod bound to a node and not finished); `waiting` is a
    Job that has nothing bound, which is what cellctl yields to a stop.
    """

    def is_build(item: dict[str, Any]) -> bool:
        return _labels(item).get(JOB_KIND_LABEL) == JOB_KIND_CATALOGUE_BUILD

    jobs = [i for i in items if i.get("kind") == "Job" and is_build(i)]
    pods = [pod_view(i) for i in items if i.get("kind") == "Pod" and is_build(i)]
    if not jobs:
        return {"state": "absent", "job": None, "pods": pods, "bound": False}
    job = max(jobs, key=lambda item: item["metadata"].get("creationTimestamp", ""))
    status = job.get("status", {})
    done = {c["type"]: c for c in status.get("conditions") or [] if c.get("status") == "True"}
    bound = any(p["node"] for p in pods)
    if "Complete" in done or status.get("succeeded"):
        state = "complete"
    elif "Failed" in done or status.get("failed"):
        state = "failed"
    elif any(p["node"] and p["phase"] in ("Pending", "Running") for p in pods):
        state = "running"
    else:
        state = "waiting"
    return {
        "state": state,
        "job": job["metadata"]["name"],
        "created_at": job["metadata"].get("creationTimestamp"),
        "started_at": status.get("startTime"),
        "completed_at": status.get("completionTime"),
        "failure_reason": (done.get("Failed") or {}).get("reason"),
        "active_deadline_seconds": job.get("spec", {}).get("activeDeadlineSeconds"),
        "pods": pods,
        "bound": bound,
    }


def serving_view(items: list[dict[str, Any]], old_uid: str | None) -> dict[str, Any]:
    """The cell's own pods: the one that served at the start, and its replacement."""

    pods = [i for i in items if i.get("kind") == "Pod" and JOB_KIND_LABEL not in _labels(i)]
    old = next((p for p in pods if p["metadata"].get("uid") == old_uid), None)
    fresh = next((p for p in pods if p["metadata"].get("uid") != old_uid and not p["metadata"].get("deletionTimestamp")), None)
    ready = next((c for c in (fresh or {}).get("status", {}).get("conditions") or [] if c.get("type") == "Ready"), {})
    return {
        "old": "gone" if old is None else "terminating" if old["metadata"].get("deletionTimestamp") else "serving",
        "old_deletion_timestamp": (old or {}).get("metadata", {}).get("deletionTimestamp"),
        "new_pod": (fresh or {}).get("metadata", {}).get("name"),
        "new_ready_at": ready.get("lastTransitionTime") if ready.get("status") == "True" else None,
    }


def window_annotations(items: list[dict[str, Any]]) -> dict[str, str | None]:
    sets = next((i for i in items if i.get("kind") == "StatefulSet"), {})
    notes = sets.get("metadata", {}).get("annotations") or {}
    names = {
        "window": UPGRADE_WINDOW_ANNOTATION,
        "window_at": UPGRADE_WINDOW_AT_ANNOTATION,
        "outcome": CATALOGUE_BUILD_OUTCOME_ANNOTATION,
        "cancelled": CATALOGUE_BUILD_CANCELLED_ANNOTATION,
    }
    return {key: notes.get(annotation) for key, annotation in names.items()}


def build_outcome(last: dict[str, Any] | None, vanished_at: dt.datetime | None) -> dict[str, Any]:
    """The build's start, end and outcome from the last view in which its Job existed.

    A Job that was unfinished when it vanished was cancelled by cellctl; its end is the first poll that
    found it gone, so it is an upper bound at poll precision.
    """

    if last is None or last["job"] is None:
        return {"outcome": "never_created"}
    state = last["state"]
    ended_at = last.get("completed_at")
    if state == "complete":
        outcome = "completed"
    elif state == "failed":
        outcome = "timed_out" if last.get("failure_reason") == "DeadlineExceeded" else "failed"
        ended_at = next((p["finished_at"] for p in last["pods"] if p["finished_at"]), None)
    elif vanished_at is not None:
        outcome, ended_at = "cancelled", vanished_at.isoformat()
    else:
        outcome, ended_at = "still_" + state, None
    return {
        "outcome": outcome,
        "job": last["job"],
        "created_at": last.get("created_at"),
        "started_at": last.get("started_at"),
        "ended_at": ended_at,
        "seconds": _seconds(last.get("started_at"), ended_at),
        "ever_bound": last["bound"],
        "active_deadline_seconds": last.get("active_deadline_seconds"),
    }


def reduce_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """CPU and memory peaks from cgroup samples. A figure the samples cannot give is None with its reason."""

    cpu = [s for s in samples if s.get("usage_usec") is not None]
    rates = [
        (b["usage_usec"] - a["usage_usec"]) / 1e6 / (b["mono"] - a["mono"])
        for a, b in zip(cpu, cpu[1:], strict=False)
        if b["mono"] > a["mono"]
    ]
    peaks = [s["peak"] for s in samples if s.get("peak") is not None]
    current = [s["current"] for s in samples if s.get("current") is not None]
    anon = [s["anon"] for s in samples if s.get("anon") is not None]
    span = cpu[-1]["mono"] - cpu[0]["mono"] if len(cpu) > 1 else 0
    unknown = {}
    if not rates:
        unknown["cpu"] = f"{len(cpu)} usable CPU sample(s); a rate needs two"
    if not peaks and not current:
        unknown["memory"] = "no memory counter could be read from the build container"
    return {
        "samples": len(samples),
        "cpu_cores_peak": round(max(rates), 2) if rates else None,
        "cpu_cores_mean": round((cpu[-1]["usage_usec"] - cpu[0]["usage_usec"]) / 1e6 / span, 2) if rates and span else None,
        "memory_peak_bytes": max(peaks) if peaks else max(current) if current else None,
        "memory_peak_source": "memory.peak" if peaks else "memory.current, highest sample" if current else None,
        "anon_peak_bytes": max(anon) if anon else None,
        "unknown": unknown,
        "kind": "lower bounds from samples of the build container's cgroup, taken with kubectl exec about every poll",
    }


def catalogue_log(lines: list[str]) -> dict[str, Any]:
    """The cold-rebuild and catch-up evidence in a cell's catalogue log lines (JSONL, newest last)."""

    cold: list[str] = []
    pages: list[dict[str, Any]] = []
    for raw in lines:
        try:
            message = str(json.loads(raw).get("message", ""))
        except ValueError:
            continue
        if message.startswith(COLD_PREFIXES) or (message.startswith(PAGES_PREFIX) and "route=rebuilt" in message):
            cold.append(message[:240])
        elif message.startswith(PAGES_PREFIX):
            pages.append({key: int(value) for key, value in PAGES_COUNTS.findall(message)})
    elapsed = [int(m.group(1)) for line in cold if (m := re.search(r"\belapsed_ms=(\d+)", line))]
    return {"cold_rebuild_lines": cold, "cold_rebuild_ms": max(elapsed) if elapsed else None, "reconciled": pages}


def diff_manifests(before: dict[str, list], after: dict[str, list]) -> dict[str, list[str]]:
    """Paths whose size, mtime or hash differ, and paths that appeared or vanished."""

    return {
        "changed": sorted(path for path in before if path in after and before[path] != after[path]),
        "removed": sorted(path for path in before if path not in after),
        "added": sorted(path for path in after if path not in before),
    }


def seconds_until_window_fits(now: dt.datetime, need_seconds: float, force_minutes: int) -> float:
    """Zero when `need_seconds` of work end before this UTC hour's forced boundary, else the wait until
    just after the next hour opens."""

    if need_seconds > (59 - force_minutes) * 60:
        raise ValueError("the work cannot fit inside one upgrade window")
    hour = now.replace(minute=0, second=0, microsecond=0)
    boundary = hour + dt.timedelta(hours=1, minutes=-force_minutes)
    if now + dt.timedelta(seconds=need_seconds) <= boundary:
        return 0.0
    return (hour + dt.timedelta(hours=1) - now).total_seconds() + WINDOW_OPEN_MARGIN


def plan_force_window(now: dt.datetime, lead_seconds: float, *, margin_seconds: float = FORCE_RESTART_MARGIN) -> ForceWindow:
    """The earliest forced boundary on a whole minute from :30 to :59, with `lead_seconds` between opening
    the window and the boundary, after the margin cellctl needs to restart with the new force minutes.

    `forceMinutes` runs 1 to 30 and counts back from the hour's end, so the boundary minute is 60 minus it.
    """

    earliest = now + dt.timedelta(seconds=margin_seconds + lead_seconds)
    hour = earliest.replace(minute=0, second=0, microsecond=0)
    for hours in (0, 1):
        for minute in range(30, 60):
            boundary = hour + dt.timedelta(hours=hours, minutes=minute)
            if boundary >= earliest:
                return ForceWindow(boundary - dt.timedelta(seconds=lead_seconds), boundary, 60 - minute, boundary.hour)
    raise AssertionError("a boundary minute always follows within the next hour")


def pod_cpu_request(pod: dict[str, Any]) -> Decimal:
    """What the scheduler counts for a pod: its containers' sum, or its largest init container."""

    def cpu(container: dict[str, Any]) -> Decimal:
        return quantity(((container.get("resources") or {}).get("requests") or {}).get("cpu", "0"))

    spec = pod.get("spec", {})
    return max(sum((cpu(c) for c in spec.get("containers") or []), Decimal(0)),
               max((cpu(c) for c in spec.get("initContainers") or []), default=Decimal(0)))


def free_cpu(node: dict[str, Any], pods: list[dict[str, Any]]) -> Decimal:
    """The node's allocatable CPU less the requests of the pods bound to it that have not finished."""

    name = node["metadata"]["name"]
    held = [pod_cpu_request(p) for p in pods
            if p.get("spec", {}).get("nodeName") == name and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    return quantity(node["status"]["allocatable"]["cpu"]) - sum(held, Decimal(0))


def filler_millicores(free: Decimal, build_request: str) -> int | None:
    """The CPU for a filler pod that leaves less than the build requests, or None when no filler is needed."""

    leave = quantity(build_request) * FILLER_FREE_FRACTION
    size = free - leave
    return int(size * 1000) if size >= Decimal("0.05") else None


def cellctl_setting(deployment: dict[str, Any], name: str) -> str | None:
    """One environment variable of cellctl's Deployment, as the live cluster holds it."""

    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next((e.get("value") for c in containers for e in c.get("env") or [] if e["name"] == name), None)


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


# --- the run ---


class Run:
    """One scenario's view of the cell: polls, timeline, checks and the cell's in-pod probes."""

    def __init__(self, ctx: Context, plan: CataloguePlan, record: dict[str, Any], name: str) -> None:
        self.ctx, self.plan, self.name = ctx, plan, name
        self.cell_id = ctx.b.cell_id or ""
        self.namespace = namespace_name(self.cell_id)
        self.record = record
        self.checks: dict[str, bool] = record["checks"]
        self.problems: list[str] = []
        self.events: dict[str, dict[str, Any]] = {}
        self.samples: list[dict[str, Any]] = []
        self.old_uid: str | None = None
        self.last_view: dict[str, Any] | None = None
        self.vanished_at: dt.datetime | None = None
        self.hold_started: dt.datetime | None = None
        self.old_deleted_at: str | None = None
        self.new_ready_at: str | None = None
        self.outcome_seen: str | None = None
        self.cancelled_seen: str | None = None
        self.last_touch = 0.0
        self.pinned: str | None = None
        self.unpinned_ticks = 0
        self.ticks = 0

    # checks

    def observe(self, name: str, ok: bool, detail: str) -> None:
        """A figure worth reading in the receipt that does not decide the scenario: a failure is recorded, never raised."""

        self.record.setdefault("observations", {})[name] = {"ok": ok, "detail": None if ok else detail}

    def check(self, name: str, passed: bool, *, blocking: bool = True, said: str = "") -> None:
        self.checks[name] = passed
        if not passed:
            self.problems.append(name)
            if blocking:
                raise StepFailure(f"scenario {self.name}: check {name!r} failed{': ' + said if said else ''}")

    # the cell's own probes

    def in_cell(self, script: str, *args: str, what: str) -> Any:
        pod = self.ctx.runtime_pod(self.cell_id)
        if pod is None:
            raise StepFailure(f"the runtime is absent during {what}")
        result = self.ctx.exec_in(self.namespace, pod["metadata"]["name"], "python3", "-c", script, *args, check=False)
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()[-1:] or [""]
            raise StepFailure(f"{what} exited {result.returncode}: {tail[0][:300]}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def mcp(self, operation: str, first: str, second: str = "", *, what: str) -> dict[str, Any]:
        return self.in_cell(MCP, operation, first, second, what=what)

    def touch(self) -> bool:
        """One MCP request, which restarts the cell's idle clock. False when the cell cannot answer."""

        try:
            self.mcp("search", "rehearsal touch", what="a touch")
        except StepFailure:
            return False
        self.last_touch = time.monotonic()
        return True

    # polling

    async def poll(self, *, idle: bool = False, sample: bool = True) -> Tick:
        mono, wall = time.monotonic(), dt.datetime.now(dt.UTC)
        items = self.ctx.kube_json("get", "jobs,pods,statefulsets", "--namespace", self.namespace)["items"]
        build, serving = build_view(items), serving_view(items, self.old_uid)
        row = await self.ctx.cell(self.cell_id)
        tick = Tick(mono, wall, build, serving, window_annotations(items), {
            "hold_kind": row.get("hold_kind"), "hold_started_at": row.get("hold_started_at"),
            "observed_image": row.get("observed_image"), "ready": row.get("ready"),
            "observed_state": row.get("observed_state"),
        })
        self.ticks += 1
        if self.pinned is not None and tick.annotations["window"] != self.pinned:
            self.unpinned_ticks += 1
        self._mark(tick)
        if sample and build["state"] in ("running", "waiting") and any(p["container_running"] for p in build["pods"]):
            self._sample(build)
        if idle and serving["old"] == "serving":
            try:
                tick.idle = self.in_cell(TRAFFIC, str(CELL_HEALTH_PORT), CELL_READINESS_PATH, what="the traffic read")["traffic"]
            except StepFailure:
                tick.idle = None
        return tick

    def _mark(self, tick: Tick) -> None:
        build, serving, row = tick.build, tick.serving, tick.row
        if build["state"] != "absent":
            self.last_view, self.vanished_at = build, None
        elif self.last_view is not None and self.vanished_at is None and not build["pods"]:
            # cellctl deletes a build in the foreground, so the Job lasts until its pods end; gone is both.
            self.vanished_at = tick.wall
            self._first("build_gone", tick)
        if row["hold_kind"] == "upgrade" and self.hold_started is None and row["hold_started_at"] is not None:
            started = row["hold_started_at"]
            self.hold_started = started if started.tzinfo else started.replace(tzinfo=dt.UTC)
        self.old_deleted_at = self.old_deleted_at or serving["old_deletion_timestamp"]
        self.new_ready_at = self.new_ready_at or serving["new_ready_at"]
        # The annotations may be rewritten when the new pod is applied, so the last value seen is the record.
        self.outcome_seen = tick.annotations["outcome"] or self.outcome_seen
        self.cancelled_seen = tick.annotations["cancelled"] or self.cancelled_seen
        seen = {
            "build_seen": build["state"] != "absent",
            "build_bound": build["bound"],
            "build_running": build["state"] == "running",
            "build_unschedulable": any(p["unschedulable"] for p in build["pods"]),
            "build_complete": build["state"] == "complete",
            "build_failed": build["state"] == "failed",
            "hold": row["hold_kind"] == "upgrade",
            "old_stopping": serving["old"] != "serving",
            "new_ready": serving["new_ready_at"] is not None,
        }
        for name, happened in seen.items():
            if happened:
                self._first(name, tick)

    def _first(self, name: str, tick: Tick) -> None:
        self.events.setdefault(name, {"mono": tick.mono, "at": _iso(tick.wall)})

    def _sample(self, build: dict[str, Any]) -> None:
        pod = next(p for p in build["pods"] if p["container_running"])
        result = self.ctx.kubectl("exec", "--namespace", self.namespace, pod["name"], "--container",
                                  JOB_KIND_CATALOGUE_BUILD, "--", "python3", "-c", CGROUP, check=False)
        if result.returncode == 0 and result.stdout.strip():
            self.samples.append(json.loads(result.stdout.strip().splitlines()[-1]))

    async def watch(self, done: Callable[[Tick], bool], *, timeout: float, what: str, touch: bool = False,
                    idle: bool = False) -> Tick:
        deadline = time.monotonic() + timeout
        while True:
            tick = await self.poll(idle=idle)
            if done(tick):
                return tick
            if time.monotonic() > deadline:
                raise StepFailure(
                    f"scenario {self.name}: timed out after {timeout:.0f} s waiting for {what}; build "
                    f"{tick.build['state']}, old pod {tick.serving['old']}, hold {tick.row['hold_kind']}, "
                    f"image {tick.row['observed_image']}")
            if touch and time.monotonic() - self.last_touch >= TOUCH_SECONDS:
                self.touch()
            await asyncio.sleep(POLL_SECONDS)

    # the cell and its window

    async def settle(self) -> None:
        """Wait until the cell serves with no hold, and take its serving pod as the one to watch."""

        await self.ctx.wait_cell(self.cell_id, _running_ready(), timeout=READY_TIMEOUT, interval=3,
                                 description=f"scenario {self.name}'s cell to serve with no hold")
        pod = self.ctx.runtime_pod(self.cell_id)
        if pod is None:
            raise StepFailure("the runtime is absent after the cell reported ready")
        self.old_uid = pod["metadata"]["uid"]
        self.record["image_before"] = (await self.ctx.cell(self.cell_id)).get("observed_image")

    def annotate_window(self, hour: int) -> str:
        value = f"{hour} default"
        today = dt.datetime.now(dt.UTC).date().isoformat()
        self.ctx.kubectl("annotate", "statefulset", STATEFULSET, "--namespace", self.namespace, "--overwrite",
                         f"{UPGRADE_WINDOW_ANNOTATION}={value}", f"{UPGRADE_WINDOW_AT_ANNOTATION}={today}")
        return value

    def park_window(self) -> None:
        self.pinned = self.annotate_window((dt.datetime.now(dt.UTC).hour + 12) % 24)

    async def open_window(self, *, need_seconds: float, hour: int | None = None) -> None:
        """Pin the window to the current UTC hour once the scenario's work fits before the force boundary.

        The cell is touched first: cellctl creates the build Job and judges the attempt in one pass, and
        an idle cell with no running build is due at once.
        """

        if hour is None:
            wait = seconds_until_window_fits(dt.datetime.now(dt.UTC), need_seconds, self.plan.settings.force_minutes)
            if wait:
                self.record.setdefault("waits", []).append({"for": "the next UTC hour", "seconds": round(wait)})
                await asyncio.sleep(wait)
        if not self.touch():
            raise StepFailure(f"scenario {self.name}: the cell could not answer the touch that precedes opening its window")
        self.pinned = self.annotate_window(dt.datetime.now(dt.UTC).hour if hour is None else hour)
        self.record["window"] = {"annotation": self.pinned, "pinned_at": _iso(dt.datetime.now(dt.UTC))}

    def set_force_minutes(self, minutes: int) -> None:
        self.ctx.kubectl("set", "env", f"deployment/{CELLCTL_DEPLOYMENT}", "--namespace", CLOUD_NAMESPACE,
                         f"CELLCTL_UPGRADE_WINDOW_FORCE_MINUTES={minutes}")
        self.ctx.kubectl("rollout", "status", f"deployment/{CELLCTL_DEPLOYMENT}", "--namespace", CLOUD_NAMESPACE,
                         "--timeout=180s")

    async def release(self, index: int) -> None:
        """Publish the release for this scenario and wait for the canary to run it, so the cell is next."""

        image = self.plan.images[index]
        self.record["release"], self.record["image"] = RELEASES[index], image
        await self.ctx.admin_release({"cellImage": image, "release": RELEASES[index]})
        await self.ctx.wait_cell(self.ctx.a.cell_id or "", _running_ready(image), timeout=READY_TIMEOUT, interval=3,
                                 description=f"the canary to run release {RELEASES[index]}")

    def facts(self, what: str) -> dict[str, Any]:
        return self.in_cell(FACTS, what=what)

    def catalogue_log(self) -> dict[str, Any]:
        return catalogue_log(self.in_cell(LOG, what="the catalogue log read"))

    # evidence

    def finish(self) -> dict[str, Any]:
        build = build_outcome(self.last_view, self.vanished_at)
        build["timeout_seconds"] = self.plan.settings.timeout_seconds
        stop = {
            "hold_started_at": _iso(self.hold_started),
            "old_pod_stop_seen_at": (self.events.get("old_stopping") or {}).get("at"),
            "old_pod_deletion_timestamp": self.old_deleted_at,
            "new_pod_ready_at": self.new_ready_at,
        }
        stop["hold_to_ready_seconds"] = _seconds(stop["hold_started_at"], stop["new_pod_ready_at"])
        stop["stop_seen_to_ready_seconds"] = _seconds(stop["old_pod_stop_seen_at"], stop["new_pod_ready_at"])
        self.record.update({"build": build, "stop": stop, "peaks": reduce_samples(self.samples),
                            "build_annotations": {"outcome": self.outcome_seen, "cancelled": self.cancelled_seen},
                            "polls": self.ticks, "timeline": {k: v["at"] for k, v in self.events.items()}})
        return self.record

    def order(self, earlier: str, later: str) -> bool:
        """The first poll to see `earlier` is not after the first to see `later`; false when either is unseen."""

        return earlier in self.events and later in self.events and self.events[earlier]["mono"] <= self.events[later]["mono"]


# --- scenarios ---


def _node_capacity(run: Run) -> dict[str, Any]:
    """CPU the node has left, against what the build requests: the figure behind a build that never binds."""

    nodes = run.ctx.kube_json("get", "nodes")["items"]
    pods = run.ctx.kube_json("get", "pods", "--all-namespaces")["items"]
    return {"nodes": len(nodes), "build_cpu_request": run.plan.settings.cpu,
            "free_cpu": float(free_cpu(nodes[0], pods)) if len(nodes) == 1 else None}


async def _begin(run: Run, index: int) -> None:
    run.record["phase"] = "release"
    await run.settle()
    run.park_window()
    run.record["before"] = run.facts("the catalogue facts before the release")
    run.record["capacity"] = _node_capacity(run)
    await run.release(index)
    run.record["phase"] = "window"


def _after_restart(run: Run) -> dict[str, Any]:
    """The new pod's catalogue state, read once the row reports the cell ready on the target image."""

    run.record["phase"] = "after_restart"
    after = run.facts("the catalogue facts after the restart")
    run.record["after"] = after
    run.record["identity_changed"] = after["identity"] != run.record["before"]["identity"]
    run.check("catalogue_identity_changed", run.record["identity_changed"],
              said="the release did not change the catalogue identity, so the scenario proves nothing")
    return after


async def _wait_ready_on_target(run: Run) -> Tick:
    image = run.record["image"]
    return await run.watch(
        lambda t: t.row["observed_image"] == image and t.row["ready"] is True and t.row["hold_kind"] is None
        and t.serving["new_ready_at"] is not None,
        timeout=READY_TIMEOUT, what="the cell to run the target image and be ready")


def _common_checks(run: Run, tick: Tick) -> None:
    run.check("cell_runs_the_target_image", tick.row["observed_image"] == run.record["image"])
    run.check("window_stayed_pinned", run.unpinned_ticks == 0, blocking=False,
              said=f"{run.unpinned_ticks} poll(s) saw another window annotation than the one pinned")
    run.check("peaks_measured", not reduce_samples(run.samples)["unknown"], blocking=False,
              said=json.dumps(reduce_samples(run.samples)["unknown"]))


def _staged_names(files: list[str]) -> list[str]:
    return [name for name in files if name.startswith(STAGED_PREFIX)]


async def _completed_build(run: Run, shared: dict[str, Any]) -> None:
    idle_seconds = run.plan.settings.idle_minutes * 60
    await _begin(run, 0)
    await run.open_window(need_seconds=WINDOW_BUDGET_SECONDS)

    run.record["phase"] = "build_start"
    await run.watch(lambda t: t.build["bound"], timeout=BIND_TIMEOUT, touch=True,
                    what="the build Job's pod to be bound to the node")
    run.check("build_bound_before_any_attempt", "hold" not in run.events,
              said="the attempt began before the build ran")

    run.record["phase"] = "idle_during_build"
    needed = idle_seconds + DEFER_MARGIN

    def idle_long_enough(tick: Tick) -> bool:
        age = (tick.idle or {}).get("last_request_age_seconds")
        return tick.build["state"] != "running" or tick.attempting or (age is not None and age >= needed)

    run.touch()  # the idle age counts from here, and the cell has been touched since the build was bound
    seen = await run.watch(idle_long_enough, timeout=needed + 240, idle=True, what="the cell to be idle while the build runs")
    age = (seen.idle or {}).get("last_request_age_seconds")
    run.record["deferral"] = {"build_state": seen.build["state"], "idle_age_seconds": age, "idle_seconds_needed": needed,
                              "hold_kind": seen.row["hold_kind"], "old_pod": seen.serving["old"]}
    run.check("attempt_deferred_while_build_ran", not (seen.attempting and seen.build["state"] == "running"),
              said="the attempt began while the build was still running")
    run.check("cell_idle_while_build_running", seen.build["state"] == "running" and age is not None and age >= needed,
              said=f"the build ended ({seen.build['state']}) before the cell had been idle {needed} s, so the "
                   "deferral was not shown; raise --catalogue-prewarm-notes")
    run.touch()  # idle again would let the attempt follow the build's end before the evidence is read

    run.record["phase"] = "build_end"
    await run.watch(lambda t: t.build["state"] != "running", timeout=LONG_BUILD_SECONDS * 2, touch=True,
                    what="the build Job to end")
    run.touch()
    view = run.last_view or {}
    run.check("build_completed", view.get("state") == "complete", said=f"the build ended as {view.get('state')}")
    seconds = _seconds(view.get("started_at"), view.get("completed_at"))
    shared["build_seconds"] = seconds
    run.check("build_time_in_range", seconds is not None and SHORT_BUILD_SECONDS <= seconds <= LONG_BUILD_SECONDS,
              said=f"the build took {seconds} s; it must take {SHORT_BUILD_SECONDS} to {LONG_BUILD_SECONDS} s so "
                   "scenario 2 can end it unfinished and a cold rebuild stays inside the ready deadline")

    run.record["phase"] = "evidence_before_stop"
    output = run.ctx.kubectl("logs", "--namespace", run.namespace, f"job/{view.get('job')}", "--tail=2", check=False)
    run.record["build_output"] = output.stdout.strip().splitlines()[-1:] if output.returncode == 0 else "unavailable"
    staged = run.facts("the staged catalogue listing")
    run.record["staged"] = staged
    copies = _staged_names(staged["files"])
    run.check("one_staged_copy_after_the_build", len(copies) == 1, said=f"staged files: {copies}")
    prefix = copies[0].removeprefix(STAGED_PREFIX).split(".")[0]
    run.check("staged_copy_is_a_new_identity", bool(prefix) and not run.record["before"]["identity"].startswith(prefix))
    before = run.in_cell(MANIFEST, what="the vault manifest before the stop")
    phrase = f"Edited after the build {secrets.token_hex(4)}"
    edit = run.mcp("create", phrase, f"{phrase}.\n\n## Observations\n\n- [rehearsal] an edit after the build #rehearsal\n",
                   what="the edit after the build")
    run.last_touch = time.monotonic()
    run.check("edit_after_the_build_written", edit["error"] is None, said=str(edit["error"]))

    run.record["phase"] = "attempt"
    attempt = await run.watch(lambda t: t.attempting, timeout=idle_seconds + CATCH_UP_SLACK + 60,
                              what="the idle attempt to start")
    run.record["idle_to_attempt_seconds"] = round(attempt.mono - run.last_touch, 1)
    ready = await _wait_ready_on_target(run)
    _common_checks(run, ready)
    after = _after_restart(run)

    log = run.catalogue_log()
    run.record["new_pod_catalogue_log"] = log
    run.check("staged_catalogue_is_the_new_identity", after["identity"].startswith(prefix))
    run.check("live_catalogue_holds_the_new_identity", after["live_matches"])
    run.check("staged_copy_consumed", not _staged_names(after["files"]), said=f"files: {after['files']}")
    run.check("no_cold_rebuild_after_adoption", not log["cold_rebuild_lines"], said="; ".join(log["cold_rebuild_lines"][:3]))
    parsed = [c.get("reparsed", 0) + c.get("inserted", 0) for c in log["reconciled"]]
    limit = max(10, run.plan.notes // 20)
    run.record["catch_up_parsed_pages"] = parsed
    run.check("catch_up_parsed_only_the_changes", all(n <= limit for n in parsed), blocking=False,
              said=f"a catch-up parsed {max(parsed, default=0)} pages; the limit is {limit}")
    began, found = time.monotonic(), False
    while not found and time.monotonic() - began < CATCH_UP_TIMEOUT:
        found = run.mcp("search", phrase, what="the recall of the edit")["found"]
        if not found:
            await asyncio.sleep(3)
    run.record["catch_up_seconds"] = round(time.monotonic() - began, 1) if found else None
    run.check("edit_after_the_build_recalled", found, said=f"not found within {CATCH_UP_TIMEOUT} s of the restart")
    run.check("build_outcome_recorded_succeeded", run.outcome_seen == "succeeded", blocking=False,
              said=f"the outcome annotation read {run.outcome_seen}")

    run.record["phase"] = "unrelated_state"
    changes = diff_manifests(before, run.in_cell(MANIFEST, what="the vault manifest after the restart"))
    folder = f"/{shared['fixture_folder']}/"
    seeded = [path for path in [*changes["changed"], *changes["removed"]] if folder in f"/{path}"]
    run.record["vault_changes"] = {"seeded_notes_changed": len(seeded), "changed": changes["changed"][:MAX_LISTED_PATHS],
                                   "removed": changes["removed"][:MAX_LISTED_PATHS], "added": changes["added"][:MAX_LISTED_PATHS],
                                   "counts": {key: len(value) for key, value in changes.items()}}
    run.check("seeded_notes_unchanged", not seeded, said=f"{len(seeded)} seeded note(s) changed or vanished: {seeded[:3]}")
    run.observe("vault_gained_only_the_edit", len(changes["added"]) == 1,
                f"{len(changes['added'])} paths were added: {changes['added'][:MAX_LISTED_PATHS]}")
    run.finish()


async def _unfinished_build(run: Run, shared: dict[str, Any]) -> None:
    seconds = shared.get("build_seconds")
    if not seconds:
        raise StepFailure("scenario unfinished_build sizes its forced boundary from the build time that scenario "
                          "completed_build measures, and that scenario measured none")
    await _begin(run, 1)
    # The build starts within about 45 s of the window opening and takes `seconds`, so a boundary between
    # 75 s and 70 percent of that falls while it runs.
    lead = min(max(seconds * 0.5, 75.0), seconds * 0.7)
    window = plan_force_window(dt.datetime.now(dt.UTC), lead)
    run.record["force"] = {"lead_seconds": round(lead), "force_minutes": window.force_minutes,
                           "boundary_at": _iso(window.boundary_at), "opens_at": _iso(window.start_at),
                           "build_seconds_scenario_1": seconds}
    try:
        run.set_force_minutes(window.force_minutes)
        wait = max((window.start_at - dt.datetime.now(dt.UTC)).total_seconds(), 0)
        run.record["waits"] = [{"for": "the window to open before the forced boundary", "seconds": round(wait)}]
        await asyncio.sleep(wait)
        await run.open_window(need_seconds=0, hour=window.window_hour)

        run.record["phase"] = "forced_attempt"
        await run.watch(lambda t: t.attempting, timeout=lead + 360, touch=True, what="the forced attempt to start")
        run.check("build_ran_before_the_attempt", run.order("build_running", "hold") or run.order("build_running", "old_stopping"),
                  said="no poll saw the build running before the attempt began")
        began = run.hold_started or _time((run.events.get("hold") or run.events["old_stopping"])["at"])
        run.record["force"]["attempt_started_at"] = _iso(began)
        run.check("attempt_started_at_the_force_boundary", began >= window.boundary_at - dt.timedelta(seconds=2),
                  said=f"the attempt began at {_iso(began)}, before the boundary {_iso(window.boundary_at)}, "
                       "while the cell was kept busy and the build still ran")
        ready = await _wait_ready_on_target(run)
    finally:
        run.set_force_minutes(run.plan.settings.force_minutes)
    _common_checks(run, ready)
    _cold_fallback(run, ready)
    run.finish()


def _cold_fallback(run: Run, ready: Tick) -> None:
    """The checks both cancelled-build scenarios share: the build ended cancelled and gone before the stop,
    no staged copy was adopted, and the cell rebuilt its catalogue cold."""

    view = run.last_view or {}
    run.check("build_ended_as_cancelled", view.get("state") in ("running", "waiting") and run.vanished_at is not None,
              said=f"last build state {view.get('state')}, gone {run.vanished_at is not None}")
    run.check("build_gone_before_the_stop", run.order("build_gone", "old_stopping"),
              said="a poll saw the old pod stopping while the build Job or its pods still existed")
    run.check("build_outcome_recorded_cancelled", run.outcome_seen == "cancelled", blocking=False,
              said=f"the outcome annotation read {run.outcome_seen}")
    named = view.get("job") in (json.loads(run.cancelled_seen).get("jobs", []) if run.cancelled_seen else [])
    run.check("cancelled_job_recorded", named, blocking=False, said=f"the cancellation record read {run.cancelled_seen}")
    after = _after_restart(run)
    run.check("no_staged_copy_adopted", not _staged_names(after["files"]), said=f"files: {after['files']}")
    log = run.catalogue_log()
    run.record["new_pod_catalogue_log"] = log
    run.check("cold_rebuild_after_cancellation", bool(log["cold_rebuild_lines"]),
              said="the new pod's log shows no cold rebuild; either the log detector or the fallback failed")
    run.check("live_catalogue_holds_the_new_identity", after["live_matches"])


async def _insufficient_capacity(run: Run, shared: dict[str, Any]) -> None:
    settings, idle_seconds = run.plan.settings, run.plan.settings.idle_minutes * 60
    await _begin(run, 2)
    namespace = f"exo-filler-{run.ctx.stack.run_id}"
    try:
        if run.record["capacity"]["nodes"] != 1:
            raise StepFailure(f"scenario insufficient_capacity expects one node, found {run.record['capacity']['nodes']}")
        nodes = run.ctx.kube_json("get", "nodes")["items"]
        free = free_cpu(nodes[0], run.ctx.kube_json("get", "pods", "--all-namespaces")["items"])
        size = filler_millicores(free, settings.cpu)
        run.record["capacity"].update({"free_cpu_before_filler": float(free), "filler_millicores": size})
        if size is not None:
            run.record["phase"] = "filler"
            _create_filler(run, namespace, size, run.ctx.images.cell_v1)
        free_after = free_cpu(nodes[0], run.ctx.kube_json("get", "pods", "--all-namespaces")["items"])
        run.record["capacity"]["free_cpu_after_filler"] = float(free_after)
        run.check("node_cpu_below_the_build_request", free_after < quantity(settings.cpu),
                  said=f"{free_after} CPU is free against a build request of {settings.cpu}")

        await run.open_window(need_seconds=CAPACITY_BUDGET_SECONDS)
        run.record["phase"] = "waiting_build"
        await run.watch(lambda t: any(p["unschedulable"] for p in t.build["pods"]) or t.attempting,
                        timeout=BIND_TIMEOUT, touch=True, what="the build Job's pod to wait for CPU")
        run.check("build_waited_for_capacity", "build_unschedulable" in run.events and "hold" not in run.events,
                  said="the build pod did not wait Unschedulable before the attempt began")
        run.record["unschedulable_message"] = next(
            (p["message"] for p in (run.last_view or {}).get("pods", []) if p["unschedulable"]), None)

        run.record["phase"] = "attempt"
        run.touch()
        attempt = await run.watch(lambda t: t.attempting, timeout=idle_seconds + CATCH_UP_SLACK + 60,
                                  what="the idle attempt to start")
        run.record["idle_to_attempt_seconds"] = round(attempt.mono - run.last_touch, 1)
        run.check("waiting_build_did_not_defer_the_attempt", run.record["idle_to_attempt_seconds"] <= idle_seconds + CATCH_UP_SLACK,
                  said=f"the attempt began {run.record['idle_to_attempt_seconds']} s after the last request")
        run.check("build_never_bound", not (run.last_view or {}).get("bound"), said="the build was scheduled during the scenario")
        ready = await _wait_ready_on_target(run)
    finally:
        run.ctx.kubectl("delete", "namespace", namespace, "--ignore-not-found", "--wait=false", check=False)
    _common_checks(run, ready)
    _cold_fallback(run, ready)
    run.finish()


def _create_filler(run: Run, namespace: str, millicores: int, image: str) -> None:
    """A pod that holds CPU on the node so the build cannot be scheduled beside the cell."""

    container = {
        "name": "filler", "image": image, "imagePullPolicy": "IfNotPresent", "command": ["sleep", "7200"],
        "resources": {"requests": {"cpu": f"{millicores}m", "memory": "16Mi"}, "limits": {"cpu": f"{millicores}m", "memory": "64Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
    }
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "cpu-filler", "namespace": namespace},
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 0,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [container]}}
    run.ctx.kubectl("create", "namespace", namespace)
    run.ctx.kubectl("apply", "--filename=-", input_text=json.dumps(pod))
    run.ctx.kubectl("wait", "--namespace", namespace, "--for=condition=Ready", "pod/cpu-filler", "--timeout=180s")


SCENARIO_RUNNERS = {
    "completed_build": _completed_build,
    "unfinished_build": _unfinished_build,
    "insufficient_capacity": _insufficient_capacity,
}


async def _provision(ctx: Context, plan: CataloguePlan, record: dict[str, Any]) -> str:
    """Tenant B's cell with the synthetic vault, searchable, on the release under test. Returns the fixture folder."""

    token = await substrate_mod.seed_invite(ctx.substrate, ctx.b.email, paid=False)
    ctx.b.session = await ctx.browser.redeem_invite(token)
    await ctx.identify(ctx.b)
    if not ctx.b.cell_id:
        raise StepFailure("tenant B's redemption left no cell")
    await ctx.wait_cell(ctx.b.cell_id, _running_ready(), timeout=600, description="tenant B's cell to be running and ready")
    run = Run(ctx, plan, {"checks": {}}, "provision")
    record["cell_id"] = ctx.b.cell_id
    sentinel = f"sentinel-{secrets.token_hex(4)}"
    folder = f"Catalogue {ctx.stack.run_id}"
    began = time.monotonic()
    seeded = run.in_cell(SEED, str(plan.notes), folder, sentinel, str(SECTIONS_PER_NOTE), what="the note seed")
    record["fixture"] = {**seeded, "folder": folder, "seed_seconds": round(time.monotonic() - began, 1)}
    began = time.monotonic()
    while True:
        try:
            if run.mcp("search", sentinel, what="the first recall of the seeded notes")["found"]:
                break
        except StepFailure:
            pass  # the first search may hold the cell for the whole catalogue sync, past the client's timeout
        if time.monotonic() - began > SEED_TIMEOUT:
            raise StepFailure(f"the seeded notes were not searchable within {SEED_TIMEOUT} s")
        await asyncio.sleep(5)
    record["fixture"]["searchable_seconds"] = round(time.monotonic() - began, 1)
    return folder


def _configuration(ctx: Context, plan: CataloguePlan) -> dict[str, Any]:
    """What the cluster holds for the feature, read back from cellctl's Deployment."""

    deployment = ctx.kube_json("get", "deployment", CELLCTL_DEPLOYMENT, "--namespace", CLOUD_NAMESPACE)
    names = ("CELLCTL_CATALOGUE_PREWARM", "CELLCTL_CATALOGUE_BUILD_TIMEOUT_SECONDS", "CELLCTL_CATALOGUE_BUILD_RESOURCES",
             "CELLCTL_UPGRADE_WINDOW_IDLE_MINUTES", "CELLCTL_UPGRADE_WINDOW_FORCE_MINUTES")
    return {"asked": asdict(plan.settings), "cellctl_environment": {name: cellctl_setting(deployment, name) for name in names}}


def _where(error: BaseException) -> str:
    """The innermost frame of an unexpected error, so a failed run can be fixed from its receipt."""

    frame = traceback.extract_tb(error.__traceback__)[-1] if error.__traceback__ else None
    return f"{type(error).__name__} at {frame.name}:{frame.lineno}: {str(error)[:200]}" if frame else type(error).__name__


async def run_catalogue_prewarm(ctx: Context, plan: CataloguePlan) -> dict:
    """Provision the scenario cell, run the three scenarios, and fail the stage on any unmet check."""

    if len(plan.images) != len(RELEASES):
        raise StepFailure(f"the mode needs {len(RELEASES)} catalogue images, got {len(plan.images)}")
    record: dict[str, Any] = {"state": "RUNNING", "phase": "provision", "plan": {"notes": plan.notes},
                              "scenarios": {}, "deviations": list(DEVIATIONS)}
    ctx.report.overlays.append(
        "catalogue-prewarm: the cluster's cellctl Deployment gets CELLCTL_UPGRADE_WINDOW_FORCE_MINUTES changed for "
        "scenario 2 and restored; each scenario writes the scenario cell's StatefulSet upgrade-window annotations")

    def save() -> None:
        ctx.report.stages["catalogue_prewarm"]["evidence"] = record

    failures: list[str] = []
    save()
    try:
        record["configuration"] = _configuration(ctx, plan)
        shared: dict[str, Any] = {"fixture_folder": await _provision(ctx, plan, record)}
        save()
        for name in SCENARIOS:
            scenario: dict[str, Any] = {"state": "RUNNING", "phase": "start", "checks": {}}
            record["scenarios"][name] = scenario
            record["phase"] = name
            run = Run(ctx, plan, scenario, name)
            try:
                await SCENARIO_RUNNERS[name](run, shared)
                scenario["state"] = "PASS" if not run.problems else "PARTIAL"
            except StepFailure as error:
                scenario["state"] = "FAILED"
                scenario["failure"] = str(error)[:400]
                run.finish()
            except Exception as error:  # noqa: BLE001 - any failure leaves a receipt; cancellation passes through
                scenario["state"] = "FAILED"
                scenario["failure"] = _where(error)
                run.finish()
            if scenario["state"] != "PASS":
                failures.append(f"{name}: {scenario.get('failure') or 'checks failed: ' + ', '.join(run.problems)}")
            save()
    except Exception as error:  # noqa: BLE001 - any failure leaves a PARTIAL receipt; cancellation passes through
        record["state"] = "PARTIAL"
        record["failure"] = str(error)[:400] if isinstance(error, StepFailure) else _where(error)
        raise StepFailure(f"catalogue prewarm stopped during {record['phase']}: {record['failure'][:300]}") from None
    finally:
        save()
    if failures:
        record["state"] = "FAILED"
        raise StepFailure("; ".join(failures)[:900])
    record["state"] = "PASS"
    return record
