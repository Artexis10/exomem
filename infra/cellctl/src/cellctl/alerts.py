"""The platform alerts cellctl raises (move-cloud-cells-to-local-storage).

One alert fires while a running cell's last successful backup is older than
its schedule allows (task 2.8), and one while a local cell past 80% use cannot
grow, because its node has no room or it is at its cap (D10). Two more fire
for a dedicated host before any cell runs there: its cell array is degraded
(task 5.5) or its thin pool is nearly full (task 5.6). Three more fire for a
serving cell, with thresholds from the observability contract
(cloud-platform-operations, "Monitoring spans independent failure domains"):
it has stayed unready, its volume has little free space left, or its recall
serves a catalogue that has stopped advancing. Each goes
through the existing alert receiver as the same content-free transition the
hosted scheduler evaluator sends (`infra/helm/platform/files/scheduler_runtime.py`).
Substrate's receiver (`alert-receiver.ts`) is the contract both senders follow:
it accepts exactly these five keys, and it emails only when an alert's state
differs from the last one it delivered. So a restarted cellctl can resend the current state without a
duplicate email.

They are alerts, never gates: nothing here changes what a pass does. The cells
and nodes themselves go to the log, by id, because the transition carries no content.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from prometheus_client.parser import text_string_to_metric_families

from .state import GROWTH_AT_CAP, GROWTH_NO_ROOM, CellRow, ClusterObservation
from .storage_config import StorageConfig

JOB = "exomem-cloud-cells"
BACKUP_ALERT = "backup-stale"
# move-cloud-cells-to-local-storage D10: a local cell past 80% use that cannot
# grow, because its node has no room or it is at its cap.
GROWTH_ALERT = "storage-growth-blocked"
GROWTH_BLOCKED = frozenset({GROWTH_NO_ROOM, GROWTH_AT_CAP})
# Tasks 5.5 and 5.6: a dedicated host's md array has lost a member, and its
# thin pool's data or metadata use has reached THIN_POOL_ALERT_PERCENT.
ARRAY_ALERT = "cell-array-degraded"
THIN_POOL_ALERT = "thin-pool-high"
# Design D1: "An alert at 80% data or metadata use".
THIN_POOL_ALERT_PERCENT = 80.0
# The metric and label names are protocol tokens that the two exporters document and version, not
# vocabulary: TopoLVM 0.41.1 (docs/topolvm-node.md) and node-exporter 1.12.1 (collector/mdadm_linux.go).
THIN_POOL_METRICS = ("topolvm_thinpool_data_percent", "topolvm_thinpool_metadata_percent")
ARRAY_STATE = "node_md_state"
ARRAY_DISKS = "node_md_disks"
ARRAY_DISKS_REQUIRED = "node_md_disks_required"
# A serving cell that stayed unready, and one whose volume is nearly full.
# Protocol tokens: the receiver keys on them and the runbook names them; they are not vocabulary.
UNAVAILABLE_ALERT = "cell-unavailable"
DISK_LOW_ALERT = "cell-disk-low"
# A serving cell whose recall serves a catalogue that has not advanced, read
# from the cell's own readiness answer.
RECALL_LAG_ALERT = "cell-recall-lagging"
# `retrieval.state` values of that answer (exomem's runtime_readiness), a
# closed protocol set this package cannot import; any other value is unread.
RECALL_READY = "ready"
RECALL_LAGGING = "lagging"
# An hourly backup missed twice, or a nightly one missed once with two hours'
# grace for the window.
HOURLY_ALERT_AFTER = timedelta(hours=2)
NIGHTLY_ALERT_AFTER = timedelta(hours=26)
SERVING_STATES = ("running", "read_only")


@dataclass(frozen=True)
class AlertThresholds:
    """The observability contract's `alerts.*` values the cell alerts read.
    The chart renders them into cellctl's environment from the contract file,
    and main.py refuses to start without them: no second copy lives in code."""

    unavailable_after: timedelta  # pending_operation_warn_seconds
    disk_free_floor_bytes: int  # pvc_reserved_free_bytes
    recall_lag_after: timedelta  # recall_lag_warn_seconds


def stale_backups(
    rows: Iterable[CellRow],
    observations: Mapping[str, ClusterObservation],
    now: datetime,
    *,
    storage: StorageConfig,
) -> list[str]:
    """Cells that should be serving and have no backup within their schedule.

    A cell on local storage is backed up hourly (D3), any other nightly. A cell
    that could not be observed this pass is held to the nightly bound, so an
    observation outage alone never fires the alert. A cell never backed up is
    aged from its creation."""

    stale = []
    for row in rows:
        if row.desired_state not in SERVING_STATES:
            continue
        observation = observations.get(row.cell_id)
        hourly = observation is not None and storage.is_local(observation.pv_storage_class)
        since = row.last_backup_at or row.created_at
        if since is not None and now - since > (HOURLY_ALERT_AFTER if hourly else NIGHTLY_ALERT_AFTER):
            stale.append(row.cell_id)
    return sorted(stale)


def unavailable_cells(unready_since: Mapping[str, datetime], now: datetime, *, after: timedelta) -> list[str]:
    """Serving cells that have been observed unready for at least `after`.
    `unready_since` is cellctl's per-cell clock (LoopMemory)."""

    return sorted(cell for cell, since in unready_since.items() if now - since >= after)


def cells_unavailable(
    rows: Iterable[CellRow],
    observations: Mapping[str, ClusterObservation],
    unready_since: Mapping[str, datetime],
    now: datetime,
    *,
    after: timedelta,
) -> bool | None:
    """Whether any serving cell has stayed unready for `after`. The answer is
    unknown (None), never "resolved", while a serving cell is unready for less
    than that, or could not be observed and has no clock: a cellctl restart
    forgets every clock, and a RESOLVED sent then would email an operator
    about an outage that is still going on. False only when every serving
    cell is ready, or held by an upgrade, backup or restore."""

    if unavailable_cells(unready_since, now, after=after):
        return True
    serving = [row for row in rows if row.desired_state in SERVING_STATES]
    pending = any(row.cell_id in unready_since or row.cell_id not in observations for row in serving)
    return None if pending else False


def low_disk_cells(rows: Iterable[CellRow], free_bytes: Mapping[str, int], *, floor: int) -> list[str]:
    """Serving cells whose last measured free space is below `floor`.
    `free_bytes` holds what each cell's latest backup reported (total minus
    used): hourly on local storage, nightly otherwise, so a reading can be a
    day old. It lives in cellctl's memory only."""

    return sorted(
        row.cell_id for row in rows
        if row.desired_state in SERVING_STATES and row.cell_id in free_bytes and free_bytes[row.cell_id] < floor
    )


def disk_low(rows: Iterable[CellRow], free_bytes: Mapping[str, int], *, floor: int) -> bool | None:
    """Whether any serving cell is below `floor`. Unknown (None), never
    "resolved", while a serving cell has no reading and none is low: after a
    restart until each cell has been measured again (like growth_blocked), and
    for as long as a cell is never measured; the backup-age alert fires for that."""

    rows = list(rows)
    if low_disk_cells(rows, free_bytes, floor=floor):
        return True
    unmeasured = any(row.desired_state in SERVING_STATES and row.cell_id not in free_bytes for row in rows)
    return None if unmeasured else False


def recall_lag_verdict(body: bytes, *, after: timedelta) -> bool | None:
    """Whether one cell's readiness answer shows its recall serving a
    catalogue unchanged for at least `after`. Only `retrieval.state` and
    `retrieval.catalogue_unchanged_seconds` are read, and the body is never logged.

    False only when recall reads strictly ready, which the cell answers only
    once its catalogue has caught up. A lag younger than `after`, or with no
    age, is unknown (None): the cell measures the age from when its own
    process first saw that catalogue, so a cell restarted during a stall
    reports a young age for an old stall, and a RESOLVED then would end an
    alert while the stall goes on. An answer that cannot be read is unknown too."""

    try:
        payload = json.loads(body)
    # RecursionError: a cell's answer is not trusted, and deep nesting must not take the pass down.
    except (ValueError, RecursionError):
        return None
    retrieval = payload.get("retrieval") if isinstance(payload, dict) else None
    if not isinstance(retrieval, dict):
        return None
    state = retrieval.get("state")
    if state == RECALL_READY:
        return False
    if state != RECALL_LAGGING:
        return None
    age = retrieval.get("catalogue_unchanged_seconds")
    if isinstance(age, bool) or not isinstance(age, int | float) or not math.isfinite(age):
        return None
    return True if age >= after.total_seconds() else None


def recall_lagging_cells(rows: Iterable[CellRow], verdicts: Mapping[str, bool | None]) -> list[str]:
    """Serving cells whose last readiness answer showed recall lagging past
    the threshold (recall_lag_verdict). `verdicts` lives in cellctl's memory."""

    return sorted(row.cell_id for row in rows if row.desired_state in SERVING_STATES and verdicts.get(row.cell_id))


def recall_lagging(rows: Iterable[CellRow], verdicts: Mapping[str, bool | None]) -> bool | None:
    """Whether any serving cell's recall has lagged past the threshold.
    Unknown (None), never "resolved", while a serving cell has no verdict or
    an unknown one: after a cellctl restart until each cell is read again,
    while a cell has no ready pod or does not answer, and while a lag is too
    young to judge. False only when every serving cell reads strictly ready."""

    serving = [verdicts.get(row.cell_id) for row in rows if row.desired_state in SERVING_STATES]
    if True in serving:
        return True
    return None if None in serving else False


def growth_blocked(
    rows: Iterable[CellRow],
    observations: Mapping[str, ClusterObservation],
    verdicts: Mapping[str, str],
    *,
    storage: StorageConfig,
) -> bool | None:
    """D10: whether a serving cell past 80% use cannot grow, because its
    node has no room or it is at its cap. `verdicts` holds what each local
    cell's latest measured backup found, and lives only in cellctl's memory.

    The answer is unknown (None), never "resolved", while any serving local
    cell has no verdict: after a restart until each has been measured again,
    and for as long as one is never measured, such as a cell stuck in its
    init or whose backups keep failing. So no RESOLVED is sent while such a
    cell exists; the backup-age alert is what fires for it. A cell that could
    not be observed this pass may be local, so it counts as unmeasured unless
    it has a verdict."""

    serving = [row for row in rows if row.desired_state in SERVING_STATES]
    if any(verdicts.get(row.cell_id) in GROWTH_BLOCKED for row in serving):
        return True
    unmeasured = [
        row for row in serving
        if row.cell_id not in verdicts
        and ((observation := observations.get(row.cell_id)) is None or storage.is_local(observation.pv_storage_class))
    ]
    return None if unmeasured else False


def _any(verdicts: Iterable[bool | None]) -> bool | None:
    """True when any verdict is True. Otherwise None when any is unknown, or
    there are none to read, and False only when every one reads clean."""

    seen = list(verdicts)
    if True in seen:
        return True
    return None if not seen or None in seen else False


def _samples(text: str | None) -> list | None:
    """The samples of one scrape, or None when there is no scrape or it is not
    Prometheus text."""

    if text is None:
        return None
    try:
        return [sample for family in text_string_to_metric_families(text) for sample in family.samples]
    except ValueError:
        return None


def _gauge(samples: list, name: str, **labels: str) -> float | None:
    """The largest finite value of the named series, or None when it is absent
    or not a number. The largest, because the alert is about the worst reading."""

    values = [
        sample.value for sample in samples
        if sample.name == name and all(sample.labels.get(key) == value for key, value in labels.items())
    ]
    return max(values) if values and all(math.isfinite(value) for value in values) else None


def thin_pool_states(
    expected: Iterable[str], scrapes: Mapping[str, str], *, device_class: str
) -> dict[str, bool | None]:
    """Task 5.6, D1: per expected node, whether its thin pool's data or
    metadata use has reached THIN_POOL_ALERT_PERCENT. `scrapes` holds the
    TopoLVM node plugin's `/metrics` text for each node that could be read; a
    node with none, or without both series for the device class, is None."""

    def high(samples: list, name: str) -> bool | None:
        use = _gauge(samples, name, device_class=device_class)
        return None if use is None else use >= THIN_POOL_ALERT_PERCENT

    states: dict[str, bool | None] = {}
    for node in expected:
        samples = _samples(scrapes.get(node))
        states[node] = None if samples is None else _any(high(samples, name) for name in THIN_POOL_METRICS)
    return states


def array_states(expected: Iterable[str], scrapes: Mapping[str, str]) -> dict[str, bool | None]:
    """Task 5.5: per expected node, whether any md array is inactive, has a
    failed member, or has fewer active members than it requires. `scrapes`
    holds node-exporter's mdadm `/metrics` text for each node that could be
    read. A node whose text shows no array is None, never clean, so a node with
    no array by design (the Cloud recovery agent, tasks 6.3 and 9.1) must not be
    among the expected nodes."""

    states: dict[str, bool | None] = {}
    for node in expected:
        samples = _samples(scrapes.get(node))
        if samples is None:
            states[node] = None
            continue
        devices = sorted({s.labels["device"] for s in samples if s.name == ARRAY_DISKS_REQUIRED and "device" in s.labels})
        verdicts = []
        for device in devices:
            required = _gauge(samples, ARRAY_DISKS_REQUIRED, device=device)
            active = _gauge(samples, ARRAY_DISKS, device=device, state="active")
            failed = _gauge(samples, ARRAY_DISKS, device=device, state="failed")
            # An inactive array reports no required, active or failed disks at all.
            inactive = _gauge(samples, ARRAY_STATE, device=device, state="inactive")
            verdicts.append(_any([
                None if inactive is None else inactive > 0,
                None if failed is None else failed > 0,
                None if active is None or required is None else active < required,
            ]))
        states[node] = _any(verdicts)
    return states


def host_alert_firing(states: Mapping[str, bool | None]) -> bool | None:
    """True when any expected node reads bad. None, which sends nothing and
    never resolves, while any node cannot be read, and while there is no node
    to read: a first "resolved" would email an operator about an alert that
    never fired. False only when every expected node reads clean."""

    return _any(states.values())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def deliver(webhook_url: str, *, alert: str, active: bool, observed_at: datetime) -> None:
    """POST one transition to the receiver. The id binds the evaluation time,
    so a later firing is always new; the receiver folds a repeated state."""

    parsed = urlsplit(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("alert delivery target is not an exact HTTPS URL")
    transition = {"job": JOB, "alert": alert, "active": active}
    transition_id = hashlib.sha256(
        json.dumps({**transition, "observed_at": observed_at.isoformat()}, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"schema_version": 1, "transition_id": transition_id, **transition},
                        separators=(",", ":"), sort_keys=True).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-Exomem-Alert-Transition": transition_id},
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if not 200 <= response.status < 300 or response.geturl() != webhook_url:
                raise RuntimeError("alert delivery failed")
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise RuntimeError("alert delivery failed") from error
