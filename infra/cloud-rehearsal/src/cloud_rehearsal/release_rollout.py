"""The release mode: automate-cloud-releases tasks 1.13, 1.15, 2.1 and 3.1 on the disposable cluster.

Three cells run on the local storage domain: the canary A and two disposable cells, B (left idle)
and C (kept busy, then left idle). The harness sets the release image as the cloud-deploy workflow
does, through the workflow's own Substrate client and the deploy token, and each proof below goes
into the report as a step record (numbers 13 and up) that passes, fails or is blocked by name.

The order differs from the design's sketch in a single place, because cellctl rolls a single cell at a time
and pauses the whole rollout on a failed canary: B and C roll first, on v2, and the broken release
arrives while C's attempt runs. The canary then attempts it, fails readiness and is restored.

Every cell is held back by hand-set window annotations, not by moving the default range: cellctl
caches a cell's window per UTC day, so a changed default would not reach a cell that already has a window.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

import httpx
from cellctl.alerts import ROLLOUT_PAUSED_ALERT
from cellctl.capacity import snapshot_reserve_bytes
from cellctl.manifests import (
    BACKUP_PATHS,
    PRE_UPGRADE_VOLUME_SNAPSHOT_ANNOTATION,
    TARGET_APPLIED_ANNOTATION,
    UPGRADE_TARGET_ANNOTATION,
    UPGRADE_WINDOW_ANNOTATION,
    UPGRADE_WINDOW_AT_ANNOTATION,
    clone_claim_name,
    namespace_name,
)
from cellctl.storage_config import GIB, LocalStorage
from mcp.shared.exceptions import McpError

from . import build, images, tls
from .mcp_client import MeasuredSession, refusal_text, session_gone
from .platform import CLOUD_NAMESPACE
from .report import (
    BLOCKED,
    FAILED,
    PASSED,
    CrossLaneDefect,
    StepFailure,
    StepRecord,
    failure_record,
    innermost,
    percentile,
)
from .runbook_commands import (
    BLOCK_TIMEOUT_SECONDS,
    FORWARD_ONLY_HOLD,
    PAUSED_ROLLOUT,
    ROTATE_KEY,
    RUNBOOK,
    RunbookRunner,
    redact,
    runbook_blocks,
)
from .scenarios import (
    RELEASE_BROKEN,
    RELEASE_RESUMED,
    RELEASE_V2,
    Tenant,
    TenantClient,
    _flatten,
    _running_ready,
    _safe_body,
    activate_subscription,
    first_session,
    redeem_invite,
)
from .substrate import PUBLIC_BASE_URL, seed_invite

if TYPE_CHECKING:
    from .scenarios import Context

DEPLOY_SCRIPT = images.REPO_ROOT / "infra/scripts/cloud_deploy.py"

# The platform chart's own local-storage settings, which the rehearsal keeps but for the cell volume cap.
LOCAL = LocalStorage()
# Substrate admits an address while the published slots exceed its live cells. cellctl reserves a snapshot
# and a clone of its largest cell per backup and charges a cell above the default size extra slots
# (capacity.compute_local_capacity). Substrate inserts a cell row at 10 GiB, so size_cell sets the row to this
# size before the cell's claim is rendered: left alone, the first cell leaves this pool no slot.
CELL_GIB = LOCAL.default_cell_gib
# TopoLVM counts every thin volume at its virtual size, so the pool covers the three cells, the failed
# attempts' snapshot and clone beside the canary (two of them, in case a second attempt overlaps the
# first), and the snapshot reserve that cellctl keeps back from the published slots.
POOL_GIB = 3 * CELL_GIB + 2 * (2 * CELL_GIB) + snapshot_reserve_bytes(LOCAL, CELL_GIB) // GIB
# What cellctl publishes with no cell larger than CELL_GIB, whatever the cells and their attempts hold.
SLOTS_AT_REST = (POOL_GIB - snapshot_reserve_bytes(LOCAL, CELL_GIB) // GIB) // CELL_GIB

# The window rule under test (D4): a cell rolls once it has been quiet for this long. A minute
# is the chart's smallest value, so each proof waits minutes, not the production five.
IDLE_MINUTES = 1
FORCE_MINUTES = 1
# D5's first value. The chart's own default is 0, which turns the hold off, so the rehearsal sets it.
HOLD_BUDGET_SECONDS = 40
# A window must stay open through B's and C's attempts, so it opens only with this much of the UTC hour left.
MIN_MINUTES_LEFT = 20
CLOSED_OBSERVATION_SECONDS = 120
BUSY_POLL_SECONDS = 20
BUSY_HOLD_SECONDS = 75
# cellctl reads a cell's idle age on a pass, so a cell can roll up to a pass after it turns idle, and the
# harness stamps its last request when the call returns: the rule is only violated by a hold this much early.
IDLE_TOLERANCE_SECONDS = 5
POLL_QUERY = "release rollout poll"
# What the broken target is expected to leave in the cell's backed-up paths (build.BROKEN_SHIM writes it).
MARKER_PROBE = (
    "import json, os, sys; print(json.dumps({path: os.path.exists(os.path.join(path, sys.argv[1])) "
    f"for path in {BACKUP_PATHS!r}}}))"
)
# The proofs that observe a roll take as long as the roll does; these bound each wait.
HOLD_TIMEOUT = 300
ROLL_TIMEOUT = 900
FAILED_ATTEMPT_TIMEOUT = 1800


# --- the cloud-deploy workflow's own client ---------------------------------------------------


def load_cloud_deploy() -> ModuleType:
    """The workflow's script, loaded the way its own tests load it, so the rehearsal runs its Substrate
    client and its paused-rollout stop rather than a copy of either."""

    loaded = sys.modules.get("cloud_deploy")
    if loaded is not None and getattr(loaded, "__file__", None) == str(DEPLOY_SCRIPT):
        return loaded
    spec = importlib.util.spec_from_file_location("cloud_deploy", DEPLOY_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["cloud_deploy"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def deploy_settings(deploy: ModuleType, deploy_token: str) -> Any:
    """The workflow's settings, read from its own environment keys. The GitHub keys are required by the
    parser and unused by the Substrate client."""

    return deploy.Settings.from_env({
        "GITHUB_REPOSITORY": "rehearsal/rehearsal", "GH_TOKEN": "not-used-by-the-rehearsal",
        "EXOMEM_SUBSTRATE_URL": PUBLIC_BASE_URL, "EXOMEM_DEPLOY_TOKEN": deploy_token,
    })


def deploy_transport(client: httpx.Client):
    """The workflow's transport contract over the rehearsal's resolver: a network failure is an OSError."""

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
        try:
            response = client.request(method, url, headers=headers, content=body)
        except httpx.TransportError as error:
            raise OSError(str(error)) from error
        return response.status_code, response.content

    return send


# --- the switch poller ----------------------------------------------------------------------


@dataclass
class Call:
    seconds: float
    error: str | None


@dataclass
class SwitchPoller:
    """Sequential `search_memory` calls through a tenant's switch, each timed from the call to its answer.

    Calls never overlap: a call held behind a restart delays the next call, as a single client's would be.
    The poller keeps its MCP session open across calls, as a connector does, because the gateway counts every
    request from the harness's address (initialize and its notification too) against a bucket of 120 a minute.
    The SDK ends a session when a request fails, so the next poll opens another; `sessions` counts them.
    """

    label: str
    tenant: Tenant
    interval: float
    calls: list[Call] = field(default_factory=list)
    sessions: int = 0
    last_completed_at: dt.datetime | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _first: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None
    _started: float = 0.0

    def start(self) -> None:
        self._started = time.perf_counter()
        self._task = asyncio.create_task(self._run())

    async def first_call(self) -> None:
        await self._first.wait()

    async def stop(self, *, timeout: float = 150.0) -> dict[str, Any]:
        """Stop after the call in flight, and return what the poller saw."""

        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._task, timeout)
            self._task = None
        polled = round(time.perf_counter() - self._started)
        return {**summarize_calls(self.calls), "sessions": self.sessions, "polled_seconds": polled}

    async def _run(self) -> None:
        assert self.tenant.client is not None
        began = time.perf_counter()
        # The server said the session is gone: a client initializes another and repeats the call, once.
        retrying = False
        while not self._stop.is_set():
            if not retrying:
                # A poll that opens a session also waits for its initialize, as a client's first call does.
                began = time.perf_counter()
            in_flight = True
            try:
                async with self.tenant.client.mcp() as session:
                    self.sessions += 1
                    while not self._stop.is_set():
                        in_flight = True
                        error, gone = await self._call(session)
                        in_flight = False
                        if gone and not retrying:
                            retrying = True
                            break
                        retrying = False
                        self._record(began, error)
                        await self._pause(began)
                        began = time.perf_counter()
            except Exception as raised:  # noqa: BLE001 - an errored call is the measurement
                if not in_flight and self._stop.is_set():
                    break  # the session closing after the last call, with no call to blame
                retrying = False
                self._record(began, refusal_text(innermost(raised)) or _flatten(innermost(raised))[:200])
                await self._pause(began)

    async def _call(self, session: MeasuredSession) -> tuple[str | None, bool]:
        """The error a poll saw, if any, and whether it says the session is gone."""

        try:
            result = await session.call("search_memory", {"query": POLL_QUERY})
        except McpError as error:  # a JSON-RPC error answered on a live session; an HTTP failure ends it instead
            return _flatten(error)[:200], session_gone(error)
        if result.is_error:
            return f"tool error {result.error_code or 'unknown'}", False
        return None, False

    def _record(self, began: float, error: str | None) -> None:
        self.calls.append(Call(time.perf_counter() - began, error))
        self.last_completed_at = dt.datetime.now(dt.UTC)
        self._first.set()

    async def _pause(self, began: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), max(0.0, self.interval - (time.perf_counter() - began)))


def summarize_calls(calls: list[Call]) -> dict[str, Any]:
    waits = [call.seconds for call in calls]
    errors = [call for call in calls if call.error]
    return {
        "calls": len(calls),
        "errors": len(errors),
        "max_wait_seconds": round(max(waits), 2) if waits else None,
        "p95_wait_seconds": round(percentile(waits, 0.95), 2) if waits else None,
        # A wait over the gateway's retry interval is a call held behind a restart.
        "held_calls": sum(1 for wait in waits if wait > 2.0),
        "error_samples": [{"waited_seconds": round(call.seconds, 2), "error": call.error} for call in errors[:5]],
        # How often each kind of error came, since the samples keep only the first few.
        "error_kinds": dict(Counter((call.error or "").splitlines()[0][:100] for call in errors)),
    }


def poll_failures(label: str, summary: dict[str, Any], budget: float = HOLD_BUDGET_SECONDS) -> list[str]:
    """Why a switch's polls fail the proof, a message per kind. A call that errored after waiting out the
    hold budget says the cell was down longer than the budget; a call that errored sooner says the hold did not hold."""

    if not summary["calls"]:
        return [f"switch {label} was never polled"]
    problems = []
    late = [sample for sample in summary["error_samples"] if sample["waited_seconds"] >= budget - IDLE_TOLERANCE_SECONDS]
    early = [sample for sample in summary["error_samples"] if sample not in late]
    if early:
        problems.append(f"switch {label}: {len(early)} call(s) errored before the {budget:.0f} s hold budget ran out "
                        f"(first: {early[0]['error']})")
    if late:
        problems.append(f"switch {label}: {len(late)} call(s) errored after waiting out the {budget:.0f} s hold budget, "
                        f"so the cell was down longer than the hold (first: {late[0]['error']})")
    if summary["errors"] and not problems:
        problems.append(f"switch {label}: {summary['errors']} call(s) errored")
    return problems


def pause_alert_lines(log: str, cell_id: str) -> int:
    """The lines where cellctl logs that the pause alert fires for `cell_id`. This rehearsal has no alert
    receiver, so the log is the only record; cellctl formats the line itself as `cellctl: <alert>: <cells>`
    (reconcile.py), and a failed delivery names the alert with no colon after it."""

    return sum(1 for line in log.splitlines() if f"cellctl: {ROLLOUT_PAUSED_ALERT}: {cell_id}" in line)


# What each runbook command is expected to print. Each check returns the problem it found, or None.


def view_shows_paused(stdout: str, paused: bool) -> str | None:
    try:
        view = json.loads(stdout)
    except ValueError:
        return f"printed no JSON view: {stdout.strip()[:200]!r}"
    shown = view.get("paused") if isinstance(view, dict) else None
    return None if shown is paused else f"the view shows paused={shown!r}, not {paused}"


def prints_true(stdout: str) -> str | None:
    return None if stdout.strip() == "true" else f"printed {stdout.strip()[:200]!r}, not true"


def lists_start_times(stdout: str, cell_ids: list[str]) -> str | None:
    """Every cell's namespace shows a pod with a start time. The listing is kubectl's two custom columns."""

    started: dict[str, list[str]] = {}
    for line in stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) == 2:
            started.setdefault(columns[0], []).append(columns[1])
    unlisted = []
    for cell_id in cell_ids:
        try:
            parsed = [dt.datetime.fromisoformat(value) for value in started.get(namespace_name(cell_id), [])]
        except ValueError:
            parsed = []
        if not parsed:
            unlisted.append(namespace_name(cell_id))
    return f"no pod with a start time in {unlisted}" if unlisted else None


# --- the upgrade window, held by hand ------------------------------------------------------


def window_annotations(hour: int, day: dt.date) -> list[str]:
    """`kubectl annotate` arguments for a cell's window: the hour, and the UTC day cellctl caches it for.

    cellctl recomputes a window only when it has none or when the day moved on, so an annotation
    stamped today stays until the day changes."""

    return [f"{UPGRADE_WINDOW_ANNOTATION}={hour} default", f"{UPGRADE_WINDOW_AT_ANNOTATION}={day.isoformat()}"]


def seconds_until_quiet_hour(now: dt.datetime, minutes_needed: int = MIN_MINUTES_LEFT) -> float:
    """0 when this UTC hour has `minutes_needed` left, else the wait to the start of the next hour."""

    next_hour = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    remaining = (next_hour - now).total_seconds()
    return 0.0 if remaining >= minutes_needed * 60 else remaining + 5.0


async def set_windows(ctx: Context, tenants: list[Tenant], hour: int) -> None:
    day = dt.datetime.now(dt.UTC).date()
    for tenant in tenants:
        await asyncio.to_thread(
            ctx.kubectl, "annotate", "statefulset", "cell", "--namespace", namespace_name(tenant.cell_id),
            *window_annotations(hour, day), "--overwrite",
        )


# --- reading the cluster while a cell rolls -------------------------------------------------


async def annotations_of(ctx: Context, cell_id: str) -> dict[str, str]:
    statefulset = await asyncio.to_thread(ctx.kube_json, "get", "statefulset", "cell", "--namespace", namespace_name(cell_id))
    return statefulset["metadata"].get("annotations") or {}


async def pod_images(ctx: Context, cell_id: str) -> list[str]:
    pod = await asyncio.to_thread(ctx.runtime_pod, cell_id)
    return [container["image"] for container in pod["spec"]["containers"]] if pod else []


async def wait_until(description: str, timeout: float, probe, *, interval: float = 2.0) -> Any:
    """The first truthy answer of the async `probe`, or a StepFailure naming the wait and its last answer."""

    deadline = time.monotonic() + timeout
    answer = None
    while time.monotonic() < deadline:
        answer = await probe()
        if answer:
            return answer
        await asyncio.sleep(interval)
    raise StepFailure(f"timed out after {timeout:.0f}s waiting for {description}")


# --- admission: the slots cellctl publishes and Substrate counts ---------------------------------


async def size_cell(ctx: Context, tenant: Tenant) -> None:
    """Set the cell's volume to CELL_GIB. The row is stopped until checkout, so no claim is rendered yet."""

    await ctx.execute("UPDATE exomem_cloud_cells SET storage_gib = $2 WHERE cell_id = $1", tenant.cell_id, CELL_GIB)


# Substrate's own count (cloud-capacity.ts): the slots of the observations from the last five minutes, and
# the cells whose deletion has not finished. cell_gib is the size each cell's claim is rendered at.
ADMISSION_SQL = """
SELECT
  (SELECT COALESCE(SUM(cell_slots), 0) FROM exomem_cloud_capacity
   WHERE observed_at > statement_timestamp() - interval '5 minutes'
     AND observed_at <= statement_timestamp()) AS published_slots,
  (SELECT COUNT(*) FROM exomem_cloud_cells AS cell WHERE NOT exomem_cloud_cell_deletion_finished(cell)) AS live_cells,
  (SELECT jsonb_object_agg(cell_id, GREATEST(storage_gib, COALESCE(grown_storage_gib, 0)))
   FROM exomem_cloud_cells) AS cell_gib
"""


async def admission_state(ctx: Context) -> dict[str, Any]:
    row = await ctx.fetchrow(ADMISSION_SQL) or {}
    gib = row.get("cell_gib")
    return {"published_slots": int(row.get("published_slots") or 0), "live_cells": int(row.get("live_cells") or 0),
            "cell_gib": json.loads(gib) if isinstance(gib, str) else (gib or {})}


async def require_admission_room(ctx: Context, record: StepRecord, label: str, *, room: int, timeout: float = 60.0) -> None:
    """Fail before a redemption that Substrate would refuse with 503 HOSTED_ADMISSION_CLOSED, naming the slots
    cellctl published. A cell's corrected size reaches Substrate on cellctl's next pass, so the wait is bounded."""

    deadline = time.monotonic() + timeout
    while True:
        state = await admission_state(ctx)
        state["room"] = state["published_slots"] - state["live_cells"]
        record.evidence.setdefault("admission", {})[label] = state
        if state["room"] >= room:
            return
        if time.monotonic() > deadline:
            raise StepFailure(
                f"admission has no room for {label}: cellctl published {state['published_slots']} slot(s) for "
                f"{state['live_cells']} live cell(s), and {room} more cell(s) need room. The pool holds {SLOTS_AT_REST} "
                f"slot(s) at rest for {CELL_GIB} GiB cells; cell volume sizes in GiB: {state['cell_gib']}")
        await asyncio.sleep(2)


async def wait_for_hold(ctx: Context, tenant: Tenant, *, timeout: float = HOLD_TIMEOUT) -> dict[str, Any]:
    """The row of a cell once its upgrade hold has started."""

    return await ctx.wait_cell(
        tenant.cell_id, lambda row: row.get("hold_kind") == "upgrade", timeout=timeout,
        description=f"cell {tenant.label}'s upgrade hold to start", interval=1.0,
    )


async def wait_rolled(ctx: Context, tenant: Tenant, image: str, *, timeout: float = ROLL_TIMEOUT) -> dict[str, Any]:
    return await ctx.wait_cell(
        tenant.cell_id, _running_ready(image), timeout=timeout,
        description=f"cell {tenant.label} to run its target and be Ready with the hold gone", interval=2.0,
    )


async def hold_cells_unmoved(ctx: Context, tenants: list[Tenant], image: str, seconds: float, why: str) -> None:
    """Every sampled row stays on `image` with no hold for `seconds`, and so does each pod at the end."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for tenant in tenants:
            row = await ctx.cell(tenant.cell_id)
            if row.get("hold_kind") or row.get("observed_image") != image:
                raise StepFailure(
                    f"cell {tenant.label} began to roll while {why}: hold {row.get('hold_kind')}, "
                    f"image {row.get('observed_image')}")
        await asyncio.sleep(5)
    for tenant in tenants:
        if image not in await pod_images(ctx, tenant.cell_id):
            raise StepFailure(f"cell {tenant.label}'s pod does not run {image} while {why}")


async def touch(tenant: Tenant) -> dt.datetime:
    """A single request, so the cell's idle clock starts now. Returns when the answer came back."""

    assert tenant.client is not None
    async with tenant.client.mcp() as session:
        result = await session.call("search_memory", {"query": POLL_QUERY})
    if result.is_error:
        raise StepFailure(f"cell {tenant.label} answered a request with {result.error_code or 'an error'}")
    return dt.datetime.now(dt.UTC)


def seconds_between(later: dt.datetime, earlier: dt.datetime) -> float:
    return (later - earlier).total_seconds()


# --- the proofs ---------------------------------------------------------------------------------


@dataclass
class Rollout:
    """What the proofs share: the pollers, the instants they compare, and what the attempts left behind."""

    ctx: Context
    pollers: dict[str, SwitchPoller] = field(default_factory=dict)
    switches: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_request: dict[str, dt.datetime] = field(default_factory=dict)
    note_title: str = ""
    note_ack: dict[str, Any] = field(default_factory=dict)
    broken_attempt: dict[str, Any] = field(default_factory=dict)
    closed_hour: int = 0
    put_at: dt.datetime | None = None
    c_hold_started_at: dt.datetime | None = None
    outcome: dict[int, str] = field(default_factory=dict)

    def start_poller(self, label: str, tenant: Tenant, interval: float = 1.0) -> SwitchPoller:
        poller = self.pollers[label] = SwitchPoller(label, tenant, interval)
        poller.start()
        return poller

    async def stop_poller(self, label: str) -> dict[str, Any]:
        summary = await self.pollers[label].stop()
        self.switches[label] = summary
        return summary

    async def stop_all_pollers(self) -> None:
        for label, poller in self.pollers.items():
            if label not in self.switches:
                self.switches[label] = await poller.stop(timeout=30.0)

    # 13
    async def provision_disposable_cells(self, record: StepRecord) -> None:
        ctx = self.ctx
        if not (ctx.a.client and ctx.a.cell_id):
            raise StepFailure("the canary's connector is missing: steps 2 and 3 did not complete")
        # B and C need a slot each; the check also names the published slots when cellctl has none to give.
        await require_admission_room(ctx, record, "B and C", room=2)
        for tenant in (ctx.b, ctx.c):
            started = time.monotonic()
            if tenant is ctx.c:
                # B's row was inserted at Substrate's size until size_cell corrected it, and cellctl publishes that.
                await require_admission_room(ctx, record, "C", room=1)
            # A paid invite admits a stopped cell, so its volume is sized before checkout starts it, as for A.
            tenant.paid = True
            await redeem_invite(ctx, tenant, await seed_invite(ctx.substrate, tenant.email, paid=tenant.paid))
            await size_cell(ctx, tenant)
            await activate_subscription(ctx, tenant)
            await ctx.wait_cell(tenant.cell_id, _running_ready(ctx.images.cell_v1), timeout=600,
                                description=f"cell {tenant.label} to run v1 and be ready")
            tenant.client = TenantClient(ctx.resolver, ctx.browser, tenant.session, ctx.substrate.redirect_uri)
            await first_session(tenant.client, StepRecord(0, f"first_session_{tenant.label}"))
            record.evidence[f"{tenant.label}_provision_seconds"] = round(time.monotonic() - started, 1)
        images_now = {t.label: (await ctx.cell(t.cell_id))["observed_image"] for t in (ctx.a, ctx.b, ctx.c)}
        record.evidence["observed_images"] = images_now
        wrong = [label for label, image in images_now.items() if image != ctx.images.cell_v1]
        if wrong:
            raise StepFailure(f"cell(s) {wrong} do not run the baseline release the deploy token set")
        # The windows are closed for B and C from the start: twelve hours from now.
        self.closed_hour = (dt.datetime.now(dt.UTC).hour + 12) % 24
        await set_windows(ctx, [ctx.b, ctx.c], self.closed_hour)

    # 14 (task 1.15)
    async def deploy_token_sets_the_release_image(self, record: StepRecord) -> None:
        ctx = self.ctx
        # Stamped again in case a UTC midnight passed since provisioning, which would let cellctl pick new hours.
        await set_windows(ctx, [ctx.b, ctx.c], self.closed_hour)
        self.start_poller("A", ctx.a)
        self.put_at = dt.datetime.now(dt.UTC)
        await ctx.deploy_release(ctx.images.cell_v2, RELEASE_V2)
        view = await ctx.deploy_rollout()
        record.evidence["view_after_put"] = {key: view.get(key) for key in ("release", "paused", "canary_on_target")}
        if view.get("cell_image") != ctx.images.cell_v2 or view.get("release") != RELEASE_V2:
            raise StepFailure(f"the rollout view does not show the release the deploy token set: {record.evidence['view_after_put']}")
        if view.get("paused") is not False:
            raise StepFailure("the rollout is paused after a fresh release")

    # 15 (bullet 2, first half)
    async def canary_rolls_at_once_and_the_rest_wait(self, record: StepRecord) -> None:
        ctx = self.ctx
        held = await wait_for_hold(ctx, ctx.a, timeout=180)
        record.evidence["seconds_from_put_to_canary_hold"] = round(seconds_between(held["hold_started_at"], self.put_at), 1)
        row = await wait_rolled(ctx, ctx.a, ctx.images.cell_v2)
        record.evidence["canary_attempt_seconds"] = round(seconds_between(dt.datetime.now(dt.UTC), held["hold_started_at"]), 1)
        record.evidence["canary"] = {key: row.get(key) for key in ("observed_image", "ready", "hold_kind")}
        await hold_cells_unmoved(ctx, [ctx.b, ctx.c], ctx.images.cell_v1, CLOSED_OBSERVATION_SECONDS,
                                 "the window excludes now")
        record.evidence["closed_window_observed_seconds"] = CLOSED_OBSERVATION_SECONDS
        await self.stop_poller("A")

    # 16 (bullet 2, idle cell)
    async def idle_cell_rolls_after_its_idle_minute(self, record: StepRecord) -> None:
        ctx = self.ctx
        now = dt.datetime.now(dt.UTC)
        wait = seconds_until_quiet_hour(now)
        record.evidence["waited_for_hour_seconds"] = round(wait)
        if wait:
            await asyncio.sleep(wait)
        busy = self.start_poller("C_busy", ctx.c, BUSY_POLL_SECONDS)
        await busy.first_call()
        # B's clock starts with this request and the window opens straight after it, so B is not
        # idle at the moment its window opens.
        self.last_request["B"] = await touch(ctx.b)
        hour = dt.datetime.now(dt.UTC).hour
        await set_windows(ctx, [ctx.b, ctx.c], hour)
        record.evidence["window_hour_utc"] = hour
        held = await wait_for_hold(ctx, ctx.b)
        idle_gap = seconds_between(held["hold_started_at"], self.last_request["B"])
        record.evidence["seconds_idle_before_hold"] = round(idle_gap, 1)
        if idle_gap < IDLE_MINUTES * 60 - IDLE_TOLERANCE_SECONDS:
            raise StepFailure(f"cell B began to roll {idle_gap:.0f} s after its last request, before its idle minute")
        # Once its attempt has started, B is polled through its switch.
        self.start_poller("B", ctx.b)
        row = await wait_rolled(ctx, ctx.b, ctx.images.cell_v2)
        record.evidence["attempt_seconds"] = round(seconds_between(dt.datetime.now(dt.UTC), held["hold_started_at"]), 1)
        record.evidence["cell"] = {key: row.get(key) for key in ("observed_image", "ready", "hold_kind")}
        await asyncio.sleep(5)
        await self.stop_poller("B")

    # 17 (bullet 2, busy cell)
    async def busy_cell_waits_then_rolls_when_idle(self, record: StepRecord) -> None:
        ctx = self.ctx
        await hold_cells_unmoved(ctx, [ctx.c], ctx.images.cell_v1, BUSY_HOLD_SECONDS, "it is still being used")
        record.evidence["busy_seconds_observed"] = BUSY_HOLD_SECONDS
        busy = await self.stop_poller("C_busy")
        record.evidence["busy_polls"] = busy["calls"]
        self.last_request["C"] = self.pollers["C_busy"].last_completed_at
        held = await wait_for_hold(ctx, ctx.c)
        idle_gap = seconds_between(held["hold_started_at"], self.last_request["C"])
        record.evidence["seconds_idle_before_hold"] = round(idle_gap, 1)
        if idle_gap < IDLE_MINUTES * 60 - IDLE_TOLERANCE_SECONDS:
            raise StepFailure(f"cell C began to roll {idle_gap:.0f} s after its last request, before its idle minute")
        self.c_hold_started_at = held["hold_started_at"]
        self.start_poller("C", ctx.c)

    # 18 (bullet 4)
    async def second_release_leaves_the_running_attempt_pinned(self, record: StepRecord) -> None:
        ctx = self.ctx
        # 3.1: the acknowledged write, made while the canary serves v2 and before anything stops it. It comes
        # first so the second release can land as soon as C's target is in effect, with the attempt still running.
        await self.write_note()

        async def applied() -> str | None:
            row = await ctx.cell(ctx.c.cell_id)
            if _running_ready(ctx.images.cell_v2)(row):
                raise StepFailure("cell C's attempt ended before the next release arrived; nothing was left to pin")
            annotations = await annotations_of(ctx, ctx.c.cell_id)
            return annotations.get(UPGRADE_TARGET_ANNOTATION) if TARGET_APPLIED_ANNOTATION in annotations else None

        # The second release arrives once the attempt's target is in effect and the attempt is not over.
        record.evidence["pin_when_second_release_arrived"] = await wait_until(
            "cell C's attempt to apply its target", HOLD_TIMEOUT, applied)
        if record.evidence["pin_when_second_release_arrived"] != ctx.images.cell_v2:
            raise StepFailure("cell C's attempt is pinned to something other than the release under test")
        await ctx.deploy_release(ctx.images.cell_broken, RELEASE_BROKEN)
        # An attempt still running after the request landed was running when it landed.
        during = await ctx.cell(ctx.c.cell_id)
        record.evidence["c_hold_after_second_release_landed"] = during.get("hold_kind")
        if during.get("hold_kind") != "upgrade":
            raise StepFailure("cell C's attempt was over before the second release landed, so the pin was not tested")
        view = await ctx.deploy_rollout()
        record.evidence["view_after_second_release"] = {key: view.get(key) for key in ("release", "paused")}
        if view.get("release") != RELEASE_BROKEN:
            raise StepFailure("Substrate did not record the second release")
        row = await wait_rolled(ctx, ctx.c, ctx.images.cell_v2)
        record.evidence["row_desired_image_is_second_release"] = row.get("desired_image") == ctx.images.cell_broken
        record.evidence["c_attempt_seconds"] = round(seconds_between(dt.datetime.now(dt.UTC), self.c_hold_started_at), 1)
        images_now = await pod_images(ctx, ctx.c.cell_id)
        record.evidence["c_pod_images_after"] = images_now
        if ctx.images.cell_v2 not in images_now:
            raise StepFailure(f"cell C does not run the release its attempt was pinned to: {images_now}")
        await asyncio.sleep(5)
        await self.stop_poller("C")

    async def write_note(self) -> None:
        ctx = self.ctx
        self.note_title = f"Release rollout note {ctx.a.phrase}"
        assert ctx.a.client is not None
        async with ctx.a.client.mcp() as session:
            write = await session.governed_write({
                "title": self.note_title,
                "content": f"{self.note_title}: written before the canary's failed attempt.\n\n"
                           "## Observations\n\n- [rehearsal] acknowledged write #rehearsal\n",
            })
        if write.error_code:
            raise StepFailure(f"the acknowledged write to the canary failed: {write.error_code}")
        self.note_ack = {"acknowledged": True, "response_keys": sorted(write.structured)}

    # 19 (bullet 5, 3.1)
    async def forced_readiness_failure_pauses_and_restores_the_canary(self, record: StepRecord) -> None:
        ctx = self.ctx
        a = ctx.a.cell_id
        namespace = namespace_name(a)
        started = time.monotonic()
        seen: set[str] = set()
        snapshots: set[str] = set()
        claims_in_restore: set[str] = set()
        marker: dict[str, bool] = {}
        deadline = started + FAILED_ATTEMPT_TIMEOUT
        while True:
            row = await ctx.cell(a)
            if row.get("hold_kind"):
                seen.add(row["hold_kind"])
            annotations = await annotations_of(ctx, a)
            if annotations.get(PRE_UPGRADE_VOLUME_SNAPSHOT_ANNOTATION):
                snapshots.add(annotations[PRE_UPGRADE_VOLUME_SNAPSHOT_ANNOTATION])
            if row.get("hold_kind") == "restore":
                jobs = await asyncio.to_thread(ctx.kube_json, "get", "jobs", "--namespace", namespace)
                for job in jobs["items"]:
                    for volume in job["spec"]["template"]["spec"].get("volumes", []):
                        claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
                        if claim:
                            claims_in_restore.add(claim)
            if not any(marker.values()):
                marker = await self.broken_target_marker(ctx.images.cell_broken) or marker
            if "restore" in seen and _running_ready(ctx.images.cell_v2)(row):
                break
            if time.monotonic() > deadline:
                raise StepFailure(f"timed out after {FAILED_ATTEMPT_TIMEOUT}s: the canary's failed attempt did not end "
                                  f"restored on v2; holds seen {sorted(seen)}, last row {row.get('observed_state')}")
            await asyncio.sleep(3)
        rollout = await ctx.rollout()
        clones = {clone_claim_name(snapshot) for snapshot in snapshots}
        self.broken_attempt = {"snapshots": sorted(snapshots), "marker_paths_seen": sorted(path for path, there in marker.items() if there)}
        record.evidence.update({
            "seconds_to_restored": round(time.monotonic() - started, 1),
            "holds_observed": sorted(seen),
            f"{PRE_UPGRADE_VOLUME_SNAPSHOT_ANNOTATION}_values": sorted(snapshots),
            "restore_job_mounted_the_attempts_clone": bool(clones & claims_in_restore) if claims_in_restore else None,
            "restore_job_claims": sorted(claims_in_restore),
            "rollout": {key: rollout.get(key) for key in ("paused", "error_code", "held_cell_id")},
        })
        problems = []
        if not (rollout.get("paused") and rollout.get("error_code") == "UPGRADE_READINESS_TIMEOUT"
                and rollout.get("held_cell_id") == a):
            problems.append(f"the rollout is not paused on the canary with UPGRADE_READINESS_TIMEOUT: {record.evidence['rollout']}")
        if not snapshots:
            problems.append(f"{PRE_UPGRADE_VOLUME_SNAPSHOT_ANNOTATION} was never set on the canary's attempt")
        logs = await asyncio.to_thread(ctx.kubectl, "logs", "deployment/cellctl", "--namespace", CLOUD_NAMESPACE, "--tail=-1")
        alert_lines = pause_alert_lines(logs.stdout, a)
        record.evidence["cellctl_log_lines_for_the_pause_alert"] = alert_lines
        if not alert_lines:
            problems.append(f"cellctl's log has no line saying {ROLLOUT_PAUSED_ALERT} fires for the canary {a}")
        if problems:
            raise StepFailure("; ".join(problems))

    async def broken_target_marker(self, image: str) -> dict[str, bool] | None:
        """Which backed-up paths hold the broken target's marker, read while its pod runs; None when it does not."""

        ctx = self.ctx
        pod = await asyncio.to_thread(ctx.runtime_pod, ctx.a.cell_id)
        if not pod or pod["status"].get("phase") != "Running" or image not in [c["image"] for c in pod["spec"]["containers"]]:
            return None
        probe = await asyncio.to_thread(
            ctx.exec_in, namespace_name(ctx.a.cell_id), pod["metadata"]["name"], "python3", "-c", MARKER_PROBE,
            build.BROKEN_MARKER, check=False)
        if probe.returncode != 0:
            return None
        try:
            return {path: bool(there) for path, there in json.loads(probe.stdout).items()}
        except ValueError:
            return None

    # 20 (bullet 5, the workflow)
    async def workflow_fails_with_the_code_alone(self, record: StepRecord) -> None:
        ctx = self.ctx
        deploy = load_cloud_deploy()
        view = await ctx.deploy_rollout()
        printed = io.StringIO()
        try:
            deploy.refuse_paused(view)
        except deploy.Stop as stop:
            with contextlib.redirect_stdout(printed):
                code = deploy.finish(stop)
        else:
            raise StepFailure("the workflow's paused-rollout check let a paused rollout through")
        line = printed.getvalue().strip()
        record.evidence.update({"printed": line, "exit_code": code})
        ids = [str(value) for tenant in ctx.tenants for value in (tenant.cell_id, tenant.tenant_id, tenant.user_id) if value]
        problems = []
        if code == 0:
            problems.append("the workflow would exit 0 on a paused rollout")
        if line != "cloud-deploy: ROLLOUT_PAUSED UPGRADE_READINESS_TIMEOUT":
            problems.append(f"the workflow printed {line!r}, not the pause's code alone")
        if any(identifier in printed.getvalue() for identifier in ids):
            problems.append("the workflow's output names a cell, tenant or user")
        if problems:
            raise StepFailure("; ".join(problems))

    # 21 (3.1)
    async def acknowledged_write_survives_and_no_broken_file_remains(self, record: StepRecord) -> None:
        ctx = self.ctx
        assert ctx.a.client is not None
        async with ctx.a.client.mcp() as session:
            recall = await session.call("search_memory", {"query": ctx.a.phrase})
        record.evidence.update({"write_ack": self.note_ack, "note_found_after_restore": ctx.a.phrase in recall.text(),
                                "marker_paths_seen_during_attempt": self.broken_attempt["marker_paths_seen"]})
        problems = []
        if ctx.a.phrase not in recall.text():
            problems.append("the acknowledged note is not in the restored canary")
        seen = self.broken_attempt["marker_paths_seen"]
        if not seen:
            problems.append("the broken target's marker was never seen during its attempt, so its absence proves nothing")
        pod = await asyncio.to_thread(ctx.runtime_pod, ctx.a.cell_id)
        if pod is None:
            problems.append("the restored canary has no running pod to read")
        else:
            probe = await asyncio.to_thread(
                ctx.exec_in, namespace_name(ctx.a.cell_id), pod["metadata"]["name"], "python3", "-c", MARKER_PROBE,
                build.BROKEN_MARKER, check=False)
            record.evidence["marker_probe_after_restore"] = probe.stdout.strip()[-200:]
            remaining = [path for path in seen if f'"{path}": true' in probe.stdout]
            if probe.returncode != 0:
                problems.append("the restored canary's marker probe failed")
            if remaining:
                problems.append(f"the broken target's file remains after the restore in {remaining}")
        if problems:
            raise StepFailure("; ".join(problems))

    # 22 (bullet 6, the deploy token's half; step 24's runbook clear is the admin token's)
    async def deploy_token_cannot_clear_the_pause(self, record: StepRecord) -> None:
        ctx = self.ctx
        before = await ctx.deploy_rollout()
        payload = {"cellImage": ctx.images.cell_v2, "release": RELEASE_RESUMED, "clearRolloutPause": True}
        refused = await ctx.release_request(ctx.substrate.secrets.deploy_token, payload)
        after = await ctx.deploy_rollout()
        record.evidence["deploy_token_status"] = refused.status_code
        record.evidence["deploy_token_body"] = _safe_body(refused)
        problems = []
        if before.get("paused") is not True:
            problems.append("the rollout was not paused before the deploy token tried to clear it")
        if refused.status_code == 200:
            problems.append("Substrate accepted a deploy-token request that clears the pause")
        if after.get("paused") is not True or after.get("cell_image") != before.get("cell_image"):
            problems.append("the pause or the release image changed after the refused request")
        if problems:
            raise StepFailure("; ".join(problems))

    # 23 (bullet 3)
    async def switch_polls_never_failed(self, record: StepRecord) -> None:
        await self.stop_all_pollers()
        record.evidence["hold_budget_seconds"] = HOLD_BUDGET_SECONDS
        record.evidence["switches"] = self.switches
        problems = []
        not_polled = {}
        for label, step in POLLER_STARTED_BY.items():
            if label in self.switches:
                problems += poll_failures(label, self.switches[label])
            elif self.outcome.get(step) == PASSED:
                problems.append(f"switch {label} was never polled, though step {step}, which starts it, passed")
            else:
                # The step that starts the poller did not pass and records why; a missing poll is no second finding.
                not_polled[label] = f"step {step} did not pass"
        record.evidence["not_polled"] = not_polled
        if problems:
            raise StepFailure("; ".join(problems))


    # 24 (task 1.13)
    async def runbook_commands_run_as_written(self, record: StepRecord) -> None:
        """The runbook's commands for a paused rollout, a forward-only hold and a key rotation, run unedited.

        The pause from step 19 must still be set: the read, the fix forward and the clear are the operator's
        answer to it. Step 22 leaves it set, and the final read shows the admin token's clear.
        """

        ctx = self.ctx
        a, b, c = ctx.a.cell_id, ctx.b.cell_id, ctx.c.cell_id
        kubeconfig = ctx.stack.k3s.kubeconfig
        missing = [tool for tool in ("bash", "curl", "jq", "kubectl") if shutil.which(tool) is None]
        if missing or kubeconfig is None:
            raise StepFailure(f"the runbook's commands need {missing or 'the cluster kubeconfig'} on the runner")
        blocks = runbook_blocks(RUNBOOK.read_text())
        record.evidence["blocks_per_heading"] = {heading: len(found) for heading, found in blocks.items()}
        # Choose only a release the canary already runs (the runbook's rule for a cell's own image).
        canary_image = (await ctx.cell(a))["observed_image"]
        if canary_image != ctx.images.cell_v2:
            raise StepFailure(f"the canary runs {canary_image!r} after the restore, not the v2 image: "
                              "there is no release it already runs to fix forward to")
        secrets = ctx.substrate.secrets
        runner = RunbookRunner(blocks, kubeconfig, ctx.stack.workdir / "runbook-home", os.environ["PATH"],
                               (secrets.admin_token, secrets.deploy_token))
        runner.configure_curl(ctx.resolver, tls.SUBSTRATE_HOST)
        loaded = {"EXOMEM_ADMIN_TOKEN": secrets.admin_token, "EXOMEM_SUBSTRATE_URL": PUBLIC_BASE_URL}
        fix_forward = {**loaded, "FIXED_IMAGE": canary_image, "FIXED_TAG": RELEASE_RESUMED}
        held_cell = {**loaded, "CELL_ID": b, "FIXED_IMAGE": canary_image}
        # (heading, block counted from 1, the variables loaded, what it must print)
        sequence: list[tuple[str, int, dict[str, str], Callable[[str], str | None]]] = [
            (PAUSED_ROLLOUT, 1, loaded, lambda out: view_shows_paused(out, True)),
            (PAUSED_ROLLOUT, 2, loaded, lambda out: None if pause_alert_lines(out, a) else
                f"no log line says {ROLLOUT_PAUSED_ALERT} fires for the canary {a}"),
            (PAUSED_ROLLOUT, 3, fix_forward, prints_true),
            (PAUSED_ROLLOUT, 4, loaded, prints_true),
            (PAUSED_ROLLOUT, 1, loaded, lambda out: view_shows_paused(out, False)),
            (FORWARD_ONLY_HOLD, 1, held_cell, prints_true),
            (FORWARD_ONLY_HOLD, 2, held_cell, prints_true),
            (ROTATE_KEY, 1, loaded, lambda out: lists_start_times(out, [a, b, c])),
        ]
        findings: list[str] = []
        record.evidence["runbook_findings"] = findings
        record.evidence["fixed_image"] = canary_image
        for heading, number, variables, check in sequence:
            where = f"{heading!r} block {number}"
            try:
                ran = await asyncio.to_thread(runner.run, heading, number, variables)
            except subprocess.TimeoutExpired:
                findings.append(f"{where} did not finish in {BLOCK_TIMEOUT_SECONDS}s")
                continue
            record.evidence["blocks"] = [shown.evidence(runner.secrets) for shown in runner.runs]
            if ran.exit_code != 0:
                findings.append(f"{where} exited {ran.exit_code}: {redact(ran.stderr.strip()[-200:], runner.secrets)}")
            elif problem := check(ran.stdout):
                findings.append(f"{where} {problem}")
        if findings:
            raise StepFailure("a runbook command does not run as written (a runbook finding; no command was adapted): "
                              + "; ".join(findings))


# The step that starts each switch's poller.
POLLER_STARTED_BY = {"A": 14, "C_busy": 16, "B": 16, "C": 17}

# (number, the proof, the proofs it needs). A step record takes its name from the method.
PROOFS: list[tuple[int, Callable[[Rollout, StepRecord], Awaitable[None]], tuple[int, ...]]] = [
    (13, Rollout.provision_disposable_cells, ()),
    (14, Rollout.deploy_token_sets_the_release_image, (13,)),
    (15, Rollout.canary_rolls_at_once_and_the_rest_wait, (14,)),
    (16, Rollout.idle_cell_rolls_after_its_idle_minute, (15,)),
    (17, Rollout.busy_cell_waits_then_rolls_when_idle, (16,)),
    (18, Rollout.second_release_leaves_the_running_attempt_pinned, (17,)),
    (19, Rollout.forced_readiness_failure_pauses_and_restores_the_canary, (18,)),
    (20, Rollout.workflow_fails_with_the_code_alone, (19,)),
    (21, Rollout.acknowledged_write_survives_and_no_broken_file_remains, (19,)),
    (22, Rollout.deploy_token_cannot_clear_the_pause, (19,)),
    # It reads what the pollers saw, whichever later proof failed, so it needs only the step that starts the first poller.
    (23, Rollout.switch_polls_never_failed, (14,)),
    # The runbook's clear, in its own order: after step 22, with the pause from step 19 still set.
    (24, Rollout.runbook_commands_run_as_written, (19,)),
]


async def run_release_rollout(ctx: Context) -> None:
    """Run every proof, recording each as a step. A proof whose predecessor did not pass is blocked, naming it."""

    out = ctx.report.stages["release_rollout"] = {
        "status": "running", "principal": "deploy token (cloud-deploy's Substrate client)",
        "hold_budget_seconds": HOLD_BUDGET_SECONDS, "idle_minutes": IDLE_MINUTES, "force_minutes": FORCE_MINUTES,
        "cells": {"canary": "A", "idle": "B", "busy_then_idle": "C"},
        "pool": {"pool_gib": POOL_GIB, "cell_gib": CELL_GIB, "slots_at_rest": SLOTS_AT_REST},
    }
    rollout = Rollout(ctx)
    outcome = rollout.outcome
    # The step whose failure started each chain of blocked steps, so a blocked proof names where to look.
    origin: dict[int, set[int]] = {}
    try:
        for number, proof, needs in PROOFS:
            name = proof.__name__
            record = StepRecord(number=number, name=name)
            ctx.report.steps.append(record)
            unmet = [n for n in needs if outcome.get(n) != PASSED]
            if unmet:
                origin[number] = set().union(*({n} if outcome[n] == FAILED else origin[n] for n in unmet))
                record.failure = {"type": "Blocked", "message": f"depends on step(s) {unmet}, which did not pass; "
                                                                f"the failed step is {sorted(origin[number])}"}
                outcome[number] = BLOCKED
                print(f"[rehearsal] step {number} {name}: BLOCKED by {unmet}", flush=True)
                continue
            record.started_at = dt.datetime.now(dt.UTC).isoformat()
            print(f"[rehearsal] step {number} {name}: start", flush=True)
            started = time.monotonic()
            try:
                await proof(rollout, record)
                record.status = PASSED
            except Exception as raised:  # noqa: BLE001 - recorded, and the run continues with what does not depend on it
                error = innermost(raised)
                record.status = FAILED
                record.failure = failure_record(error)
                if isinstance(error, CrossLaneDefect):
                    ctx.report.defects.append({"step": number, **record.failure})
            record.seconds = round(time.monotonic() - started, 2)
            outcome[number] = record.status
            detail = "" if record.status == PASSED else f" -- {record.failure.get('message', '')[:300]}"  # type: ignore[union-attr]
            print(f"[rehearsal] step {number} {name}: {record.status.upper()} in {record.seconds}s{detail}", flush=True)
    finally:
        await rollout.stop_all_pollers()
        out["switches"] = rollout.switches
        out["status"] = "passed" if all(status == PASSED for status in outcome.values()) and len(outcome) == len(PROOFS) else "failed"
