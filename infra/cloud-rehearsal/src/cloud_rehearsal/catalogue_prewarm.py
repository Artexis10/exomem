"""The catalogue-prewarm mode: rehearse `cellctl.cataloguePrewarm` on a disposable cluster
(warm-cell-catalogue task 3.1).

After provisioning, the mode adds a second tenant whose cell holds a synthetic vault, then runs three
scenarios against that cell. Each scenario publishes a release whose cell image has a new catalogue
identity (the parser version, raised one step per release), opens the cell's upgrade window on demand
and watches the cell's namespace until the cell runs the new image:

1. completed build: the idle cell's attempt waits for the running build, the cell adopts the staged
   catalogue, an edit made after the build is caught up at restart, and nothing but the edit changes
   the vault;
2. unfinished build: the forced-attempt boundary falls while the build runs, cellctl cancels the build
   before the stop, and the cell rebuilds cold;
3. insufficient capacity: a filler pod leaves the node too little CPU for the build, the waiting build
   does not defer the idle attempt, and cellctl cancels it before the stop.

The cell's own structured log events (`staged_catalogue_adoption`, `lexical_catalogue_materialized`)
are the adoption and rebuild evidence; they keep their fields through a cloud cell's log redaction.
For each scenario the receipt holds the build Job's start, end and outcome, the stop and ready times,
the build container's CPU and memory peaks and the configured build timeout. The receipt holds counts,
timings, hashes and the synthetic fixture's invented names; it never holds note contents.

After each scenario a gate reads whether the cell is fit for the next one; a gate that stays failed marks
the scenarios left BLOCKED. The harness never resumes the rollout or clears a hold. The run's diagnostics
artifact holds cellctl's log for the whole stage and, for each scenario, the cell's StatefulSet with its
field owners, the namespace's events and the log of each pod that replaced the serving one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
import subprocess
import tempfile
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
    HOLD_ANNOTATION,
    HOLD_STARTED_ANNOTATION,
    JOB_KIND_CATALOGUE_BUILD,
    JOB_KIND_LABEL,
    PREVIOUS_IMAGE_ANNOTATION,
    TARGET_APPLIED_ANNOTATION,
    UPGRADE_TARGET_ANNOTATION,
    UPGRADE_WINDOW_ANNOTATION,
    UPGRADE_WINDOW_AT_ANNOTATION,
    namespace_name,
)

from . import substrate as substrate_mod
from .large_import import SAMPLE_SCRIPT
from .operator_restore_abort import MCP_PROBE
from .platform import CLOUD_NAMESPACE, CataloguePrewarm
from .report import StepFailure
from .scenarios import _running_ready

if TYPE_CHECKING:
    from .scenarios import Context

DEFAULT_NOTES = 12000
# A cold rebuild of more notes would outlast the rehearsal's 10-minute upgrade ready deadline if the build
# time scales from the default's (an unverified estimate until a run measures it).
MAX_NOTES = 30000
SECTIONS_PER_NOTE = 12  # about 4 KiB a note
# One release per scenario. Substrate takes ascending vMAJOR.MINOR.PATCH tags and the P3 steps that use
# v0.0.1 to v0.0.4 do not run in this mode.
RELEASES = ("v0.0.2", "v0.0.3", "v0.0.4")
CELLCTL_DEPLOYMENT = "cellctl"
STATEFULSET = "cell"  # CellManifestSpec.statefulset_name
# cellctl's environment contract (infra/helm/platform/templates/cellctl.yaml): the build profile the chart
# renders, read back rather than assumed, and the window minutes this mode sets.
TIMEOUT_ENV = "CELLCTL_CATALOGUE_BUILD_TIMEOUT_SECONDS"
RESOURCES_ENV = "CELLCTL_CATALOGUE_BUILD_RESOURCES"
FORCE_ENV = "CELLCTL_UPGRADE_WINDOW_FORCE_MINUTES"
CELLCTL_SETTINGS = ("CELLCTL_CATALOGUE_PREWARM", TIMEOUT_ENV, RESOURCES_ENV, "CELLCTL_UPGRADE_WINDOW_IDLE_MINUTES", FORCE_ENV)
# The product's cataloged log events and the closed `route` tokens it emits (log_events.EVENT_CATALOG,
# lexstore._log_catalogue_materialized): protocol names, not a reading of anyone's words.
ADOPTION_EVENT = "staged_catalogue_adoption"
ROUTE_EVENT = "lexical_catalogue_materialized"
DEFER_MARGIN = 35  # idle seconds beyond the idle minutes that cover cellctl's longest wait between passes (30 s)
SHORT_BUILD_SECONDS = 120  # below this the build can end before the deferral has been observed
LONG_BUILD_SECONDS = 330  # above this a cold rebuild risks the 10-minute upgrade ready deadline
WINDOW_BUDGET_SECONDS = LONG_BUILD_SECONDS + 240  # the longest scenario 1 needs inside one window
CAPACITY_BUDGET_SECONDS = 360
FORCE_RESTART_MARGIN = 120  # cellctl's Recreate rollout after its force minutes change
WINDOW_OPEN_MARGIN = 5
DAY_MARGIN = dt.timedelta(minutes=5)  # cellctl re-dates the window annotations after 00:00 UTC; start after it
# Longer than a scenario's span from opening the window to the new pod's readiness (about 20 minutes, an
# estimate); it also ends a watch the harness could not stop.
WATCH_SECONDS = 2700
POLL_SECONDS = 2.0
TOUCH_SECONDS = 15.0  # well inside the idle minute, so a touch keeps the cell busy
BIND_TIMEOUT = 240
READY_TIMEOUT = 1500
SEED_TIMEOUT = 1200
CATCH_UP_TIMEOUT = 300
CATCH_UP_SLACK = 150  # seconds beyond the idle minute within which an undeferred attempt must start
FILLER_FREE_FRACTION = Decimal("0.75")  # the filler leaves this share of the build's CPU request free
MAX_LISTED_PATHS = 20
# cellctl's own names for an upgrade hold's StatefulSet annotations (cellctl.manifests). A start beside a
# catalogue build writes them by merge patch, and one left after the hold ends makes cellctl hold the cell
# again. The harness lists them apart from cellctl's own cleanup, so a key that cellctl forgets to remove still shows.
HOLD_KEYS = (HOLD_ANNOTATION, HOLD_STARTED_ANNOTATION, PREVIOUS_IMAGE_ANNOTATION, UPGRADE_TARGET_ANNOTATION)
GATE_SETTLE_SECONDS = 75  # two of cellctl's 30-s passes and slack: a state that its next pass converges is no block
GATE_POLL_SECONDS = 5.0
ROLLOUT_GATE = "rollout_not_paused"  # only an operator resumes a paused rollout, so waiting cannot pass this gate
DIAGNOSTICS_DIR = "catalogue-prewarm"  # under the run's diagnostics folder, which the workflow uploads
LOG_FOLLOW_SECONDS = 9000  # longer than the workflow's 140-minute step; it also ends a stream the harness could not stop
DEVIATIONS = (
    "the build's resource profile and timeout are the chart's values, read back from cellctl's Deployment; "
    "the upgrade window's idle and force minutes are the schema's 1-minute floor, so a scenario fits inside "
    "one UTC hour",
    "the cell image's parser version is raised by one per release with a derived image, not by a source "
    "change; a release that changes the parser also changes the embedding drain's key, which adds "
    "background work after each start that a real parser release would add too",
    "each scenario pins the cell's upgrade window to the current UTC hour by writing the StatefulSet "
    "annotations cellctl keeps, and parks it twelve hours away between scenarios",
    "scenario 2 reaches the unfinished build through the forced-attempt boundary (`forceMinutes`); the "
    "build timeout path is covered by cellctl's unit tests, and the timeout is recorded, not reached",
    "peaks come from the build container's own cgroup, read with `kubectl exec` about every poll: memory is "
    "the kernel's high-water mark (`memory.peak`, which includes page cache), or the highest sampled "
    "`memory.current` where the kernel keeps no peak; CPU is the highest mean rate between two samples, so "
    "a shorter burst reads lower",
    "the order of the build pod's deletion and the serving pod's stop comes from one pod watch on the cell's "
    "namespace, whose events arrive in commit order; Job times have the Kubernetes API's one-second precision",
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
                  'files': sorted(entry.name for entry in store.path.parent.glob('.lexical*')),
                  'staged': [path.name for path in catalogue_prewarm._staged_copies(store)]}))
"""

# Runs inside the cell, read-only: whether the live catalogue holds a page for each vault-relative path.
CATALOGUED = """
import json, os, sqlite3, sys
from pathlib import Path
from exomem import lexstore
store = lexstore.LexicalStore(Path(os.environ['EXOMEM_VAULT_PATH']))
conn = sqlite3.connect(store.path.as_uri() + '?mode=ro', uri=True)
print(json.dumps({path: conn.execute('SELECT 1 FROM pages WHERE path = ?', (path,)).fetchone() is not None
                  for path in sys.argv[1:]}))
"""

# Runs inside the cell: size, mtime and hash of every vault file, to compare across the scenario.
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

# Runs inside the cell: the current pod's log events named in argv, oldest first. RotatingFileHandler keeps
# backups as exomem.log.1 (newest) to exomem.log.N (oldest), so those come first and the live file last.
LOG = """
import json, os, sys
directory = os.environ.get('EXOMEM_LOG_DIR', '/tmp/exomem-logs')
backups = [name for name in os.listdir(directory) if name.startswith('exomem.log.') and name[11:].isdigit()]
wanted, found = set(sys.argv[1:]), []
for name in [*sorted(backups, key=lambda name: -int(name[11:])), 'exomem.log']:
    try:
        handle = open(os.path.join(directory, name), encoding='utf-8', errors='replace')
    except OSError:
        continue
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('event') in wanted:
                found.append({'ts': record.get('ts'), 'event': record['event'], 'fields': record.get('fields') or {}})
print(json.dumps(found[-400:]))
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


def _statefulset(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((i for i in items if i.get("kind") == "StatefulSet"), None)


def window_annotations(items: list[dict[str, Any]]) -> dict[str, str | None]:
    notes = (_statefulset(items) or {}).get("metadata", {}).get("annotations") or {}
    names = {
        "window": UPGRADE_WINDOW_ANNOTATION,
        "window_at": UPGRADE_WINDOW_AT_ANNOTATION,
        "outcome": CATALOGUE_BUILD_OUTCOME_ANNOTATION,
        "cancelled": CATALOGUE_BUILD_CANCELLED_ANNOTATION,
    }
    return {key: notes.get(annotation) for key, annotation in names.items()}


def statefulset_state(statefulset: dict[str, Any] | None) -> dict[str, Any] | None:
    """What cellctl last wrote to the cell's StatefulSet: images, replicas and the hold annotations it carries."""

    if statefulset is None:
        return None
    notes = statefulset.get("metadata", {}).get("annotations") or {}
    spec = statefulset.get("spec") or {}
    containers = spec.get("template", {}).get("spec", {}).get("containers") or []
    return {"images": {c.get("name"): c.get("image") for c in containers}, "replicas": spec.get("replicas"),
            "annotations": {key: notes[key] for key in (*HOLD_KEYS, TARGET_APPLIED_ANNOTATION) if key in notes}}


def scenario_gate(row: dict[str, Any], statefulset: dict[str, Any] | None, rollout: dict[str, Any],
                  image: str | None) -> dict[str, str]:
    """Each gate the cell fails before the next scenario, with what was read; empty when the next may start.

    `statefulset` is None when it could not be read, and `image` is None when the stage published no release.
    """

    failed: dict[str, str] = {}
    if not _running_ready()(row):
        failed["cell_serving_with_no_hold"] = (f"state {row.get('observed_state')}, ready {row.get('ready')}, "
                                               f"hold {row.get('hold_kind')}")
    if image is not None and row.get("observed_image") != image:
        failed["cell_on_the_released_image"] = f"the cell runs {row.get('observed_image')}, the release is {image}"
    notes = (statefulset or {}).get("metadata", {}).get("annotations") or {}
    held = {key: notes[key] for key in HOLD_KEYS if key in notes}
    if statefulset is None or held:
        failed["statefulset_carries_no_hold"] = json.dumps(held) if held else "the StatefulSet could not be read"
    if rollout.get("paused"):
        failed[ROLLOUT_GATE] = f"paused with error {rollout.get('error_code')} on cell {rollout.get('held_cell_id')}"
    return failed


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
    """CPU and memory peaks from the build container's samples. A figure the samples cannot give is None with its reason."""

    cpu = [s for s in samples if s.get("cpu_usage_usec") is not None]
    rates = [
        (b["cpu_usage_usec"] - a["cpu_usage_usec"]) / 1e6 / (b["mono"] - a["mono"])
        for a, b in zip(cpu, cpu[1:], strict=False)
        if b["mono"] > a["mono"]
    ]
    peaks = [s["memory_peak"] for s in samples if s.get("memory_peak") is not None]
    current = [s["memory_current"] for s in samples if s.get("memory_current") is not None]
    anon = [s["memory_anon"] for s in samples if s.get("memory_anon") is not None]
    span = cpu[-1]["mono"] - cpu[0]["mono"] if len(cpu) > 1 else 0
    unknown = {}
    if not rates:
        unknown["cpu"] = f"{len(cpu)} usable CPU sample(s); a rate needs two"
    if not peaks and not current:
        unknown["memory"] = "no memory counter could be read from the build container"
    return {
        "samples": len(samples),
        "cpu_cores_peak": round(max(rates), 2) if rates else None,
        "cpu_cores_mean": round((cpu[-1]["cpu_usage_usec"] - cpu[0]["cpu_usage_usec"]) / 1e6 / span, 2) if rates and span else None,
        "memory_peak_bytes": max(peaks) if peaks else max(current) if current else None,
        "memory_peak_source": "memory.peak" if peaks else "memory.current, highest sample" if current else None,
        "anon_peak_bytes": max(anon) if anon else None,
        "unknown": unknown,
    }


def catalogue_log(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The adoption outcomes and the catalogue builds in a pod's structured log events, oldest first."""

    routes = [e["fields"] for e in events if e["event"] == ROUTE_EVENT]
    return {
        "adoption": [e["fields"].get("outcome") for e in events if e["event"] == ADOPTION_EVENT],
        "rebuilt": [r for r in routes if r.get("route") == "rebuilt"],
        "reconciled": [r for r in routes if r.get("route") == "reconciled"],
    }


def watch_events(text: str) -> list[dict[str, Any]]:
    """The pod events of a `--watch --output-watch-events --output=json` stream, in arrival order.

    kubectl prints each event as an indented JSON object; a stream cut inside an object ends at the last whole one.
    """

    decoder, events, index = json.JSONDecoder(), [], 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return events
        try:
            event, index = decoder.raw_decode(text, index)
        except ValueError:
            return events
        pod = event.get("object") or {}
        metadata = pod.get("metadata") or {}
        events.append({"type": event.get("type"), "uid": metadata.get("uid"), "name": metadata.get("name"),
                       "build": _labels(pod).get(JOB_KIND_LABEL) == JOB_KIND_CATALOGUE_BUILD,
                       "deleting": bool(metadata.get("deletionTimestamp"))})


def stop_order(events: list[dict[str, Any]], old_uid: str | None) -> tuple[bool, str]:
    """Whether every build pod the watch saw was DELETED before the serving pod's first stopping event.

    Watch events have a total order, so a tie cannot pass; a build pod or stop the watch never saw fails.
    """

    builds = {e["uid"] for e in events if e["build"]}
    stop = next((i for i, e in enumerate(events) if e["uid"] == old_uid and (e["deleting"] or e["type"] == "DELETED")), None)
    deleted = {e["uid"]: i for i, e in enumerate(events) if e["build"] and e["type"] == "DELETED"}
    if not builds:
        return False, "the pod watch saw no build pod"
    if stop is None:
        return False, "the pod watch saw no stop of the serving pod"
    late = [uid for uid in builds if deleted.get(uid, len(events)) > stop]
    return not late, f"{len(late)} of {len(builds)} build pod(s) were not deleted before the serving pod began to stop"


def diff_manifests(before: dict[str, list], after: dict[str, list]) -> dict[str, list[str]]:
    """Paths whose size, mtime or hash differ, and paths that appeared or vanished."""

    return {
        "changed": sorted(path for path in before if path in after and before[path] != after[path]),
        "removed": sorted(path for path in before if path not in after),
        "added": sorted(path for path in after if path not in before),
    }


def seconds_until_day_fits(now: dt.datetime, span_seconds: float) -> float:
    """Zero when `span_seconds` from now end before 00:00 UTC, else the wait until just after it."""

    midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if now + dt.timedelta(seconds=span_seconds) < midnight:
        return 0.0
    return (midnight + DAY_MARGIN - now).total_seconds()


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


def build_profile(environment: dict[str, str | None]) -> dict[str, Any]:
    """The build timeout and CPU request cellctl runs with; a value it does not hold is None."""

    try:
        timeout = int(environment.get(TIMEOUT_ENV) or "")
    except ValueError:
        timeout = None
    try:
        cpu = str(json.loads(environment.get(RESOURCES_ENV) or "")["requests"]["cpu"])
    except (ValueError, KeyError, TypeError):
        cpu = None
    return {"timeout_seconds": timeout, "cpu_request": cpu}


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


# --- the run ---


def _end(process: subprocess.Popen) -> None:
    """Stop a stream the harness started, unless it already ended by itself."""

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class PodWatch:
    """One `kubectl get pods --watch` stream over the cell's namespace, kept in a file until it is stopped."""

    def __init__(self, container: str, namespace: str) -> None:
        self.output = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.process = subprocess.Popen(
            ["docker", "exec", container, "kubectl", "get", "pods", "--namespace", namespace, "--watch",
             "--output-watch-events", "--output=json", f"--request-timeout={WATCH_SECONDS}s"],
            stdout=self.output, stderr=subprocess.DEVNULL)

    def stop(self) -> dict[str, Any]:
        ended = self.process.poll()  # a watch that ended by itself may have missed later events
        _end(self.process)
        self.output.seek(0)
        events = watch_events(self.output.read())
        self.output.close()
        return {"events": events, "ended_by_itself": ended is not None}


class Diagnostics:
    """Files for the run's diagnostics artifact: cellctl's log for the whole stage and each scenario's cluster state.

    Every capture is best effort: a read that fails writes its error to the file and never fails the stage.
    """

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.root = ctx.stack.workdir / "diagnostics" / DIAGNOSTICS_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.streams: dict[str, subprocess.Popen] = {}  # each cellctl pod's log stream, which writes its own file
        self.errors: list[str] = []

    def follow_cellctl(self) -> None:
        """Stream the log of each running cellctl pod not yet followed, one file per pod.

        `kubectl set env` replaces the pod, and the old pod's stream ends with its last line.
        """

        try:
            deployment = self.ctx.kube_json("get", "deployment", CELLCTL_DEPLOYMENT, "--namespace", CLOUD_NAMESPACE)
            labels = deployment["spec"]["selector"]["matchLabels"]
            selector = ",".join(f"{key}={value}" for key, value in labels.items())
            pods = self.ctx.kube_json("get", "pods", "--namespace", CLOUD_NAMESPACE, f"--selector={selector}")["items"]
            for pod in pods:
                name = pod["metadata"]["name"]
                # A pod that is not Running yet has no log to follow; a later call picks it up.
                if name in self.streams or pod["metadata"].get("deletionTimestamp") or pod["status"].get("phase") != "Running":
                    continue
                # A cellctl pod's name starts with its Deployment's, so the file names its role too.
                with (self.root / f"{name}.log").open("w", encoding="utf-8") as output:
                    process = subprocess.Popen(
                        ["docker", "exec", self.ctx.stack.k3s.container, "kubectl", "logs", "--namespace",
                         CLOUD_NAMESPACE, f"pod/{name}", "--follow", "--timestamps",
                         f"--request-timeout={LOG_FOLLOW_SECONDS}s"],
                        stdout=output, stderr=subprocess.STDOUT)
                self.streams[name] = process
        except Exception as error:  # noqa: BLE001 - a diagnostics read never replaces the stage's own outcome
            self.errors.append(f"following cellctl's log: {_where(error)}")

    def stop(self) -> dict[str, Any]:
        """End every stream; the record names the files and any read that failed."""

        for process in self.streams.values():
            _end(process)
        return {"directory": f"{DIAGNOSTICS_DIR} in the diagnostics artifact",
                "cellctl_logs": [f"{name}.log" for name in sorted(self.streams)], "errors": self.errors}

    def capture(self, label: str, namespace: str, pods: list[str]) -> list[str]:
        """The cell's StatefulSet with its field owners, the namespace's events and each named pod's log."""

        reads = {
            f"{label}-statefulset.yaml": ("get", "statefulset", STATEFULSET, "--namespace", namespace,
                                          "--output=yaml", "--show-managed-fields"),
            f"{label}-events.json": ("get", "events", "--namespace", namespace, "--output=json"),
            **{f"{label}-pod-{pod}.log": ("logs", "--namespace", namespace, f"pod/{pod}", "--all-containers",
                                          "--prefix", "--timestamps") for pod in pods},
        }
        written = []
        for name, command in reads.items():
            try:
                result = self.ctx.kubectl(*command, check=False)
                (self.root / name).write_text(result.stdout + result.stderr, encoding="utf-8")
                written.append(name)
            except Exception as error:  # noqa: BLE001 - a diagnostics read never replaces the scenario's own outcome
                written.append(f"{name}: not written, {type(error).__name__}")
        return written


class Run:
    """One scenario's view of the cell: polls, timeline, checks and the cell's in-pod probes."""

    def __init__(self, ctx: Context, plan: CataloguePlan, record: dict[str, Any], name: str,
                 profile: dict[str, Any] | None = None, diagnostics: Diagnostics | None = None) -> None:
        self.ctx, self.plan, self.name = ctx, plan, name
        self.profile = profile or {}
        self.diagnostics = diagnostics
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
        self.window_hour: dt.datetime | None = None
        self.pod_watch: PodWatch | None = None
        self.watched: dict[str, Any] | None = None
        self.unpinned_ticks = 0
        self.ticks = 0
        self.new_pods: list[str] = []  # each pod that replaced the serving one, in the order polls first saw them
        self.last_state: dict[str, Any] | None = None

    # checks

    def observe(self, name: str, ok: bool | None, detail: str) -> None:
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

    def touch(self) -> bool:
        """One MCP request, which restarts the cell's idle clock. False when the cell cannot answer."""

        try:
            self.in_cell(MCP_PROBE, "rehearsal touch", "search", what="a touch")
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
        self._state(tick, _statefulset(items), await self.ctx.rollout())
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
        if serving["new_pod"] and serving["new_pod"] not in self.new_pods:
            self.new_pods.append(serving["new_pod"])
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

    def _state(self, tick: Tick, statefulset: dict[str, Any] | None, rollout: dict[str, Any]) -> None:
        """Record what cellctl wrote whenever it differs from the previous poll, with the poll that first saw it."""

        state = {"statefulset": statefulset_state(statefulset),
                 "row": {key: tick.row[key] for key in ("observed_state", "ready", "hold_kind", "observed_image")},
                 "rollout": {key: rollout.get(key) for key in ("paused", "error_code", "held_cell_id")}}
        if state != self.last_state:
            self.record.setdefault("states", []).append({"at": _iso(tick.wall), "poll": self.ticks, **state})
            self.last_state = state

    def _first(self, name: str, tick: Tick) -> None:
        self.events.setdefault(name, {"mono": tick.mono, "at": _iso(tick.wall)})

    def _sample(self, build: dict[str, Any]) -> None:
        pod = next(p for p in build["pods"] if p["container_running"])
        result = self.ctx.kubectl("exec", "--namespace", self.namespace, pod["name"], "--container",
                                  JOB_KIND_CATALOGUE_BUILD, "--", "python3", "-c", SAMPLE_SCRIPT, check=False)
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
        now = dt.datetime.now(dt.UTC)
        self.window_hour = now.replace(minute=0, second=0, microsecond=0)
        self.pinned = self.annotate_window(now.hour if hour is None else hour)
        self.record["window"] = {"annotation": self.pinned, "pinned_at": _iso(dt.datetime.now(dt.UTC))}

    def set_force_minutes(self, minutes: int) -> None:
        self.ctx.kubectl("set", "env", f"deployment/{CELLCTL_DEPLOYMENT}", "--namespace", CLOUD_NAMESPACE,
                         f"CELLCTL_UPGRADE_WINDOW_FORCE_MINUTES={minutes}")
        self.ctx.kubectl("rollout", "status", f"deployment/{CELLCTL_DEPLOYMENT}", "--namespace", CLOUD_NAMESPACE,
                         "--timeout=180s")
        if self.diagnostics is not None:
            self.diagnostics.follow_cellctl()  # the change replaced cellctl's pod

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
        """The current pod's adoption and catalogue-build events."""

        log = catalogue_log(self.in_cell(LOG, ADOPTION_EVENT, ROUTE_EVENT, what="the catalogue log read"))
        self.record["new_pod_catalogue_log"] = log
        return log

    def watch_pods(self) -> None:
        self.pod_watch = PodWatch(self.ctx.stack.k3s.container, self.namespace)

    def stop_watch(self) -> dict[str, Any] | None:
        """The pod watch's events; the first call stops it."""

        if self.pod_watch is not None and self.watched is None:
            self.watched = self.pod_watch.stop()
        return self.watched

    # evidence

    def finish(self) -> dict[str, Any]:
        build = build_outcome(self.last_view, self.vanished_at)
        build["timeout_seconds"] = self.profile.get("timeout_seconds")
        stop = {
            "hold_started_at": _iso(self.hold_started),
            "old_pod_stop_seen_at": (self.events.get("old_stopping") or {}).get("at"),
            "old_pod_deletion_timestamp": self.old_deleted_at,
            "new_pod_ready_at": self.new_ready_at,
        }
        stop["hold_to_ready_seconds"] = _seconds(stop["hold_started_at"], stop["new_pod_ready_at"])
        stop["stop_seen_to_ready_seconds"] = _seconds(stop["old_pod_stop_seen_at"], stop["new_pod_ready_at"])
        ran = self.samples or (self.last_view or {}).get("bound")
        watched = self.stop_watch()
        if watched is not None:
            relevant = [e for e in watched["events"] if e["build"] or e["uid"] == self.old_uid]
            self.record["pod_watch"] = {"events": len(watched["events"]), "ended_by_itself": watched["ended_by_itself"],
                                        "build_and_serving_events": relevant[:40]}
        self.record.update({"build": build, "stop": stop,
                            "peaks": reduce_samples(self.samples) if ran else {"state": "no build container ran"},
                            "build_annotations": {"outcome": self.outcome_seen, "cancelled": self.cancelled_seen},
                            "polls": self.ticks, "timeline": {k: v["at"] for k, v in self.events.items()}})
        return self.record

    def order(self, earlier: str, later: str) -> bool:
        """A poll before the first to see `later` saw `earlier`; false when either is unseen or one poll first saw both."""

        return earlier in self.events and later in self.events and self.events[earlier]["mono"] < self.events[later]["mono"]


# --- scenarios ---


def _node_capacity(run: Run) -> dict[str, Any]:
    """CPU the node has left, against what the build requests: the figure behind a build that never binds."""

    nodes = run.ctx.kube_json("get", "nodes")["items"]
    pods = run.ctx.kube_json("get", "pods", "--all-namespaces")["items"]
    return {"nodes": len(nodes), "build_cpu_request": run.profile.get("cpu_request"),
            "free_cpu": float(free_cpu(nodes[0], pods)) if len(nodes) == 1 else None}


async def _begin(run: Run, index: int) -> None:
    run.record["phase"] = "release"
    force = _cellctl_environment(run.ctx)[FORCE_ENV]
    if force != str(run.plan.settings.force_minutes):
        raise StepFailure(f"scenario {run.name}: cellctl runs with force minutes {force}, not the mode's "
                          f"{run.plan.settings.force_minutes}; an earlier scenario did not restore them")
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


def _common_checks(run: Run, tick: Tick, *, peaks: bool = True) -> None:
    run.check("cell_runs_the_target_image", tick.row["observed_image"] == run.record["image"])
    run.check("window_stayed_pinned", run.unpinned_ticks == 0, blocking=False,
              said=f"{run.unpinned_ticks} poll(s) saw another window annotation than the one pinned")
    if peaks:
        unknown = reduce_samples(run.samples)["unknown"]
        run.check("peaks_measured", not unknown, blocking=False, said=json.dumps(unknown))


def _manifest(run: Run, when: str) -> dict[str, list]:
    return run.in_cell(MANIFEST, what=f"the vault manifest {when}")


async def _completed_build(run: Run, shared: dict[str, Any]) -> None:
    idle_seconds = run.plan.settings.idle_minutes * 60
    await _begin(run, 0)
    baseline = _manifest(run, "before the window opens")
    await run.open_window(need_seconds=WINDOW_BUDGET_SECONDS)

    # The checks before the stop record their outcome and never end the scenario, so it still watches the
    # stop and the upgrade; any of them that failed fails the scenario at the end.
    run.record["phase"] = "build_start"
    bound = await run.watch(lambda t: t.build["bound"] or t.attempting, timeout=BIND_TIMEOUT, touch=True,
                            what="the build Job's pod to be bound to the node")
    run.check("build_bound_before_any_attempt", bound.build["bound"] and "hold" not in run.events, blocking=False,
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
              blocking=False, said="the attempt began while the build was still running")
    run.check("cell_idle_while_build_running", seen.build["state"] == "running" and age is not None and age >= needed,
              blocking=False, said=f"the build ended ({seen.build['state']}) before the cell had been idle {needed} s, "
                                   "so the deferral was not shown; raise --catalogue-prewarm-notes")
    run.touch()  # idle again would let the attempt follow the build's end before the evidence is read

    run.record["phase"] = "build_end"
    # A pod that has succeeded reads as waiting until the Job controller marks the Job complete; wait for that.
    await run.watch(lambda t: t.build["state"] in ("complete", "failed", "absent"), timeout=LONG_BUILD_SECONDS * 2,
                    touch=True, what="the build Job to end")
    run.touch()
    view = run.last_view or {}
    run.check("build_completed", view.get("state") == "complete", blocking=False, said=f"the build ended as {view.get('state')}")
    seconds = _seconds(view.get("started_at"), view.get("completed_at"))
    shared["build_seconds"] = seconds
    run.observe("build_time_in_range", seconds is not None and SHORT_BUILD_SECONDS <= seconds <= LONG_BUILD_SECONDS,
                f"the build took {seconds} s; {SHORT_BUILD_SECONDS} to {LONG_BUILD_SECONDS} s lets scenario 2 end it "
                "unfinished and keeps a cold rebuild inside the ready deadline")

    run.record["phase"] = "evidence_before_stop"
    prefix, footprint = None, {"added": [], "changed": [], "removed": []}
    try:
        prefix, footprint = _evidence_before_stop(run, view)
    except StepFailure as error:  # an in-cell read failed, as it does once the stop has begun
        run.check("evidence_before_stop_read", False, blocking=False, said=str(error)[:300])

    run.record["phase"] = "attempt"
    attempt = await run.watch(lambda t: t.attempting, timeout=idle_seconds + CATCH_UP_SLACK + 60,
                              what="the idle attempt to start")
    run.record["idle_to_attempt_seconds"] = round(attempt.mono - run.last_touch, 1)
    ready = await _wait_ready_on_target(run)
    _common_checks(run, ready)
    after = _after_restart(run)
    run.check("staged_catalogue_is_the_new_identity", prefix is not None and after["identity"].startswith(prefix),
              said="no single staged copy was read before the stop" if prefix is None else "")
    run.check("live_catalogue_holds_the_new_identity", after["live_matches"])

    run.record["phase"] = "catch_up"
    notes, began, held = [footprint["note"]] if footprint.get("note") else [], time.monotonic(), False
    while notes and not held and time.monotonic() - began < CATCH_UP_TIMEOUT:
        run.touch()  # a request lets a lazy catch-up run, as a user's would
        held = run.in_cell(CATALOGUED, notes[0], what="the live catalogue read")[notes[0]]
        if not held:
            await asyncio.sleep(3)
    run.record["catch_up_seconds"] = round(time.monotonic() - began, 1) if held else None
    log = run.catalogue_log()
    run.check("staged_catalogue_adopted", "adopted" in log["adoption"], said=f"the adoption events read {log['adoption']}")
    run.check("no_full_parse_after_adoption", not log["rebuilt"], said=json.dumps(log["rebuilt"][:3]))
    run.check("edit_caught_up_in_the_live_catalogue", held,
              said=f"the live catalogue held no page for the edit {CATCH_UP_TIMEOUT} s after the restart" if notes
              else "no edit was written before the stop, so none could be caught up")
    parsed = [r.get("reparsed", 0) + r.get("inserted", 0) for r in log["reconciled"]]
    limit = max(10, run.plan.notes // 20)
    run.observe("catch_up_parsed_only_the_changes", all(n <= limit for n in parsed) if parsed else None,
                f"catalogue builds parsed {parsed} pages; the limit is {limit}" if parsed
                else "unknown: the new pod logged no reconciled catalogue build")
    run.check("build_outcome_recorded_succeeded", run.outcome_seen == "succeeded", blocking=False,
              said=f"the outcome annotation read {run.outcome_seen}")

    run.record["phase"] = "unrelated_state"
    changes = diff_manifests(baseline, _manifest(run, "after the restart"))
    edited = set(footprint["added"]) | set(footprint["changed"])
    touched = set(changes["added"]) | set(changes["changed"])
    unexpected = {"removed": changes["removed"], "beyond_the_edit": sorted(touched - edited),
                  "edit_lost": sorted(edited - touched)}
    folder = f"/{shared['fixture_folder']}/"
    run.record["vault_changes"] = {
        "files_before": len(baseline), "edit": sorted(edited)[:MAX_LISTED_PATHS],
        "seeded_notes_changed_or_removed": sum(folder in f"/{path}" for path in [*changes["changed"], *changes["removed"]]),
        "unexpected": {key: value[:MAX_LISTED_PATHS] for key, value in unexpected.items()},
        "counts": {key: len(value) for key, value in unexpected.items()}}
    run.check("vault_changed_only_by_the_edit", not any(unexpected.values()),
              said=json.dumps(run.record["vault_changes"]["counts"]))
    run.finish()


def _evidence_before_stop(run: Run, view: dict[str, Any]) -> tuple[str | None, dict[str, list[str]]]:
    """The staged copy the build left and an edit written after it. Returns the copy's identity prefix, None
    unless exactly one copy was staged, and the edit's footprint on the vault."""

    output = run.ctx.kubectl("logs", "--namespace", run.namespace, f"job/{view.get('job')}", "--tail=2", check=False)
    run.record["build_output"] = output.stdout.strip().splitlines()[-1:] if output.returncode == 0 else "unavailable"
    staged = run.facts("the staged catalogue listing")
    run.record["staged"] = staged
    copies = staged["staged"]  # the product's own reading of its reserved names: the build lock is not a copy
    run.check("one_staged_copy_after_the_build", len(copies) == 1, blocking=False, said=f"staged files: {copies}")
    prefix = copies[0].removeprefix(STAGED_PREFIX).split(".")[0] if len(copies) == 1 else None
    run.check("staged_copy_is_a_new_identity", bool(prefix) and not run.record["before"]["identity"].startswith(prefix),
              blocking=False)
    before_edit = _manifest(run, "before the edit")
    token = secrets.token_hex(4)
    phrase = f"Edited after the build {token}"
    edit = run.in_cell(MCP_PROBE, phrase, "write", what="the edit after the build")
    run.last_touch = time.monotonic()
    run.check("edit_after_the_build_written", not edit["is_error"], blocking=False, said=json.dumps(edit["result"])[:300])
    # The edit's own footprint: what it wrote is the only change the vault may show after the restart.
    footprint = diff_manifests(before_edit, _manifest(run, "after the edit"))
    # A governed write also updates indexes and the log, so the edit is found by the harness's own random title token.
    notes = [path for path in footprint["added"] if path.endswith(f"-{token}.md")]
    footprint["note"] = notes[0] if len(notes) == 1 else None
    run.check("edit_created_its_note", footprint["note"] is not None and not footprint["removed"], blocking=False,
              said=json.dumps({key: value[:MAX_LISTED_PATHS] for key, value in footprint.items() if key != "note"}))
    return prefix, footprint


def _restore_force_minutes(run: Run) -> bool:
    """Put cellctl's force minutes back without raising, so a failed restore never hides the scenario's own failure."""

    try:
        run.set_force_minutes(run.plan.settings.force_minutes)
        return True
    except Exception as error:  # noqa: BLE001 - recorded here; the next scenario refuses to start on the wrong value
        run.record["force_minutes_restore_failure"] = _where(error)
        return False


async def _unfinished_build(run: Run, shared: dict[str, Any]) -> None:
    seconds = shared.get("build_seconds")  # recorded beside the boundary, so a lost race can be read against it
    await _begin(run, 1)
    # The build pod runs about 6-12 s after the window opens. cellctl's attempt follows the boundary within one
    # idle pass (30 s), and a build that ignores SIGTERM can run on through its 30 s grace period. A 35 s lead
    # puts the boundary about 25 s into the build, so even that worst case ends before the fastest observed
    # build (about 97 s); a later boundary let a cancelled build publish its complete copy first.
    lead = 35.0
    window = plan_force_window(dt.datetime.now(dt.UTC), lead)
    run.record["force"] = {"lead_seconds": round(lead), "force_minutes": window.force_minutes,
                           "boundary_at": _iso(window.boundary_at), "opens_at": _iso(window.start_at),
                           "build_seconds_scenario_1": seconds}
    try:
        run.set_force_minutes(window.force_minutes)
        wait = max((window.start_at - dt.datetime.now(dt.UTC)).total_seconds(), 0)
        run.record.setdefault("waits", []).append({"for": "the window to open before the forced boundary", "seconds": round(wait)})
        await asyncio.sleep(wait)
        run.watch_pods()
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
        restored = _restore_force_minutes(run)
    if not restored:
        raise StepFailure(f"scenario unfinished_build: cellctl's force minutes were not restored: "
                          f"{run.record['force_minutes_restore_failure']}")
    _common_checks(run, ready)
    _cold_fallback(run)
    run.finish()


def _cold_fallback(run: Run) -> None:
    """The checks both cancelled-build scenarios share: the build ended cancelled, its pods were deleted
    before the stop, and the new pod rebuilt its catalogue cold without adopting a staged copy."""

    view = run.last_view or {}
    run.check("build_ended_as_cancelled", view.get("state") in ("running", "waiting") and run.vanished_at is not None,
              said=f"last build state {view.get('state')}, gone {run.vanished_at is not None}")
    watched = run.stop_watch() or {"events": [], "ended_by_itself": None}
    gone_first, detail = stop_order(watched["events"], run.old_uid)
    run.check("build_gone_before_the_stop", gone_first, said=detail)
    run.check("build_outcome_recorded_cancelled", run.outcome_seen == "cancelled", blocking=False,
              said=f"the outcome annotation read {run.outcome_seen}")
    named = view.get("job") in (json.loads(run.cancelled_seen).get("jobs", []) if run.cancelled_seen else [])
    run.check("cancelled_job_recorded", named, blocking=False, said=f"the cancellation record read {run.cancelled_seen}")
    after = _after_restart(run)
    log = run.catalogue_log()
    # The build publishes its staged copy in one move once complete, so an adoption here means the cancelled build
    # finished first: correct product behaviour, but this run then never reached the cold path it exists to prove.
    raced = "adopted" in log["adoption"]
    run.check("cold_rebuild_after_cancellation", bool(log["adoption"]) and not raced and bool(log["rebuilt"]),
              said=("the cancelled build published a complete staged copy before it ended, and the cell adopted it; "
                    "the boundary fell too late in the build to reach the cold path" if raced else
                    f"adoption events {log['adoption']}, full catalogue builds {len(log['rebuilt'])}"))
    run.check("live_catalogue_holds_the_new_identity", after["live_matches"])


async def _insufficient_capacity(run: Run, shared: dict[str, Any]) -> None:
    settings, idle_seconds = run.plan.settings, run.plan.settings.idle_minutes * 60
    request = run.profile.get("cpu_request")
    if request is None:
        raise StepFailure(f"scenario insufficient_capacity sizes its filler from the build's CPU request, and "
                          f"cellctl's {RESOURCES_ENV} could not be read")
    await _begin(run, 2)
    namespace = f"exo-filler-{run.ctx.stack.run_id}"
    try:
        if run.record["capacity"]["nodes"] != 1:
            raise StepFailure(f"scenario insufficient_capacity expects one node, found {run.record['capacity']['nodes']}")
        nodes = run.ctx.kube_json("get", "nodes")["items"]
        free = free_cpu(nodes[0], run.ctx.kube_json("get", "pods", "--all-namespaces")["items"])
        size = filler_millicores(free, request)
        run.record["capacity"].update({"free_cpu_before_filler": float(free), "filler_millicores": size})
        if size is not None:
            run.record["phase"] = "filler"
            _create_filler(run, namespace, size, run.ctx.images.cell_v1)
        free_after = free_cpu(nodes[0], run.ctx.kube_json("get", "pods", "--all-namespaces")["items"])
        run.record["capacity"]["free_cpu_after_filler"] = float(free_after)
        run.check("node_cpu_below_the_build_request", free_after < quantity(request),
                  said=f"{free_after} CPU is free against a build request of {request}")

        run.watch_pods()
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
        # The hour's forced boundary starts the attempt whatever the build does, so the idle rule must win first.
        # A stop first seen by a poll is later than the attempt's start, so it bounds it from above.
        began = run.hold_started or _time((run.events.get("old_stopping") or {}).get("at"))
        force_at = run.window_hour + dt.timedelta(minutes=60 - settings.force_minutes) if run.window_hour else None
        run.check("attempt_began_before_the_force_boundary", began is not None and force_at is not None and began < force_at,
                  said=f"the attempt began at {_iso(began)}, the forced boundary is {_iso(force_at)}")
    finally:
        run.ctx.kubectl("delete", "namespace", namespace, "--ignore-not-found", "--wait=false", check=False)
    _common_checks(run, ready, peaks=False)
    _cold_fallback(run)
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


# Each scenario's runner and its longest span from parking the window to the new pod's readiness, which must
# not cross 00:00 UTC (unverified estimates until a run measures them).
SCENARIOS: dict[str, tuple[Callable[[Run, dict[str, Any]], Any], int]] = {
    "completed_build": (_completed_build, 45 * 60),
    "unfinished_build": (_unfinished_build, 55 * 60),
    "insufficient_capacity": (_insufficient_capacity, 35 * 60),
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
            reply = run.in_cell(MCP_PROBE, sentinel, "search", what="the first recall of the seeded notes")
            if sentinel in json.dumps(reply["result"]):
                break
        except StepFailure:
            pass  # the first search may hold the cell for the whole catalogue sync, past the client's timeout
        if time.monotonic() - began > SEED_TIMEOUT:
            raise StepFailure(f"the seeded notes were not searchable within {SEED_TIMEOUT} s")
        await asyncio.sleep(5)
    record["fixture"]["searchable_seconds"] = round(time.monotonic() - began, 1)
    return folder


def _cellctl_environment(ctx: Context) -> dict[str, str | None]:
    """The feature's settings as cellctl's Deployment holds them."""

    deployment = ctx.kube_json("get", "deployment", CELLCTL_DEPLOYMENT, "--namespace", CLOUD_NAMESPACE)
    return {name: cellctl_setting(deployment, name) for name in CELLCTL_SETTINGS}


def _configuration(ctx: Context, plan: CataloguePlan) -> dict[str, Any]:
    environment = _cellctl_environment(ctx)
    return {"asked": asdict(plan.settings), "cellctl_environment": environment, "build_profile": build_profile(environment)}


def _where(error: BaseException) -> str:
    """The innermost frame of an unexpected error, so a failed run can be fixed from its receipt."""

    frame = traceback.extract_tb(error.__traceback__)[-1] if error.__traceback__ else None
    return f"{type(error).__name__} at {frame.name}:{frame.lineno}: {str(error)[:200]}" if frame else type(error).__name__


async def _gate(run: Run, image: str | None) -> dict[str, Any]:
    """Read the cell, its StatefulSet and the rollout until the next scenario may start or the gate settles failed.

    The harness never resumes the rollout or clears a hold: a gate that stays failed blocks the scenarios left.
    """

    began = time.monotonic()
    while True:
        row, rollout = await run.ctx.cell(run.cell_id), await run.ctx.rollout()
        read = run.ctx.kubectl("get", "statefulset", STATEFULSET, "--namespace", run.namespace, "--output=json", check=False)
        failed = scenario_gate(row, json.loads(read.stdout) if read.returncode == 0 else None, rollout, image)
        waited = time.monotonic() - began
        if not failed or ROLLOUT_GATE in failed or waited >= GATE_SETTLE_SECONDS:
            return {"failed": failed, "image": image, "read_at": _iso(dt.datetime.now(dt.UTC)), "waited_seconds": round(waited)}
        await asyncio.sleep(GATE_POLL_SECONDS)


async def run_catalogue_prewarm(ctx: Context, plan: CataloguePlan) -> dict:
    """Provision the scenario cell, run the three scenarios, and fail the stage on any unmet check.

    After each scenario a gate reads whether the cell is fit for the next one; when it is not, the scenarios
    left are BLOCKED at once instead of each waiting for a cell that will not settle.
    """

    if len(plan.images) != len(RELEASES):
        raise StepFailure(f"the mode needs {len(RELEASES)} catalogue images, got {len(plan.images)}")
    record: dict[str, Any] = {"state": "RUNNING", "phase": "provision", "plan": {"notes": plan.notes},
                              "scenarios": {}, "deviations": list(DEVIATIONS)}
    ctx.report.overlays.append(
        "catalogue-prewarm: the cluster's cellctl Deployment gets CELLCTL_UPGRADE_WINDOW_FORCE_MINUTES changed for "
        "scenario 2 and restored; each scenario writes the scenario cell's StatefulSet upgrade-window annotations")

    def save() -> None:
        ctx.report.stages["catalogue_prewarm"]["evidence"] = record

    diagnostics = Diagnostics(ctx)
    failures: list[str] = []
    save()
    try:
        diagnostics.follow_cellctl()
        record["configuration"] = _configuration(ctx, plan)
        shared: dict[str, Any] = {"fixture_folder": await _provision(ctx, plan, record)}
        save()
        blocked: str | None = None
        published: str | None = None  # the last release the stage published, which the cell should run
        for name, (runner, span) in SCENARIOS.items():
            if blocked:
                record["scenarios"][name] = {"state": "BLOCKED", "failure": blocked, "checks": {}}
                failures.append(f"{name}: {blocked}")
                save()
                continue
            scenario: dict[str, Any] = {"state": "RUNNING", "phase": "start", "checks": {}}
            record["scenarios"][name] = scenario
            record["phase"] = name
            save()
            if wait := seconds_until_day_fits(dt.datetime.now(dt.UTC), span):
                # cellctl re-dates the window annotations at 00:00 UTC, which would move the pinned window.
                scenario["waits"] = [{"for": "00:05 UTC, so the scenario does not cross a date change", "seconds": round(wait)}]
                save()
                await asyncio.sleep(wait)
            diagnostics.follow_cellctl()
            run = Run(ctx, plan, scenario, name, record["configuration"]["build_profile"], diagnostics)
            try:
                await runner(run, shared)
                scenario["state"] = "PASS" if not run.problems else "PARTIAL"
            except StepFailure as error:
                scenario["state"] = "FAILED"
                scenario["failure"] = str(error)[:400]
                run.finish()
            except Exception as error:  # noqa: BLE001 - any failure leaves a receipt; cancellation passes through
                scenario["state"] = "FAILED"
                scenario["failure"] = _where(error)
                run.finish()
            save()
            published = run.record.get("image") or published
            gate = scenario["gate_after"] = await _gate(run, published)
            run.check("cell_settled_after_the_scenario", not gate["failed"], blocking=False,
                      said="; ".join(f"{key}: {value}" for key, value in gate["failed"].items()))
            if gate["failed"] and scenario["state"] == "PASS":
                scenario["state"] = "PARTIAL"
            scenario["diagnostics"] = diagnostics.capture(name, run.namespace, run.new_pods)
            if scenario["state"] != "PASS":
                failures.append(f"{name}: {scenario.get('failure') or 'checks failed: ' + ', '.join(run.problems)}")
            if gate["failed"]:
                blocked = f"BLOCKED: the gate after {name} failed: {', '.join(gate['failed'])}"
            save()
    except Exception as error:  # noqa: BLE001 - any failure leaves a PARTIAL receipt; cancellation passes through
        record["state"] = "PARTIAL"
        record["failure"] = str(error)[:400] if isinstance(error, StepFailure) else _where(error)
        if ctx.b.cell_id:
            record["failure_diagnostics"] = diagnostics.capture("stage", namespace_name(ctx.b.cell_id), [])
        raise StepFailure(f"catalogue prewarm stopped during {record['phase']}: {record['failure'][:300]}") from None
    finally:
        record["diagnostics"] = diagnostics.stop()
        save()
    if failures:
        record["state"] = "FAILED"
        raise StepFailure("; ".join(failures)[:900])
    record["state"] = "PASS"
    return record
