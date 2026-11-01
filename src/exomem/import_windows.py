"""The status of an import sent as windows: the cell's budgets, each window's unfiled members, and acknowledgement (D12).

`adoption_studio status` reports what a sender needs to size its next window, and what the person still has to
keep. The sender never computes a budget: every figure here is read in the cell, and one the cell cannot read is
`{"unknown": reason}`, never zero. A cell without the media store reports no window budget, so a sender follows
the older-cell rule.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import (
    adoption_plan,
    adoption_run,
    cloud_import,
    import_stage,
    media_capacity,
    media_store,
    originals,
    vault,
)

# Members that one status reply lists; `next_after` continues the list.
PAGE_MEMBERS = 200
# The label of a member of a discarded window is a status protocol word, not vault vocabulary. The states are
# `import_stage.WINDOW_*`.
DISCARDED_NOT_FILED = "discarded, not filed"
# The longest series ID a demand lease accepts; the sender's IDs are short, and the lease is a row per series.
MAX_SERIES_ID = 128
# What reading a policy value or the media ledger can raise: a value that is no number, an unreadable file or table.
_UNREAD = (OSError, sqlite3.Error, ValueError, TypeError)

# One acknowledgement at a time, so two calls never rewrite the same record from a stale read.
_ACKNOWLEDGE_LOCK = threading.Lock()


class _Unknown(Exception):
    """An input that the cell cannot read; the message is the reason that the status gives."""


@dataclass(frozen=True, slots=True)
class _Reading:
    """One input of the figures, read once: its value, or why it is unknown."""

    value: object = None
    reason: str | None = None

    def need(self):
        if self.reason is not None:
            raise _Unknown(self.reason)
        return self.value


def _read(compute: Callable[[], object]) -> _Reading:
    try:
        return _Reading(compute())
    except _Unknown as why:
        return _Reading(reason=str(why))


def _figure(compute: Callable[[], int]) -> dict:
    try:
        return {"bytes": compute()}
    except _Unknown as why:
        return {"unknown": str(why)}


@dataclass(frozen=True, slots=True)
class Window:
    """One import window that the cell holds a receipt or a walk file for.

    A landed window lists the manifest members that no run filed. A discarded one lists its residual record,
    and the members that the person acknowledged. An unreadable one has a record that cannot be used, or a
    stage and no record, so nothing about its members is known; `staged` says whether its stage folder exists.
    `renames_pending` names the Knowledge Base paths that a refused discard found renamed and not committed.
    """

    import_id: str
    state: str
    members: tuple[dict, ...] = ()
    acknowledged: tuple[dict, ...] = ()
    problem: str | None = None
    staged: bool = False
    renames_pending: tuple[str, ...] = ()


def census(root: Path) -> list[Window]:
    """Every window of the cell in import ID order; raises OSError when the records cannot be listed."""
    runs = adoption_run.AdoptionRunStore(root).runs_by_stage()
    windows = []
    for import_id in import_stage.window_ids(root):
        record = import_stage.window_record(root, import_id)
        if record is None:
            continue  # A window with no stage and no record has nothing left to report.
        common = {"staged": record.staged, "renames_pending": record.renames_pending}
        if record.kind == import_stage.WINDOW_LANDED:
            staged = {path for path, _size in import_stage.staged_files(root, import_id)}
            unfiled = adoption_run.unfiled_members(root, import_id, runs.get(import_id, ()))
            members = tuple({**member, "staged": member["path"] in staged} for member in unfiled)
            windows.append(Window(import_id, import_stage.WINDOW_LANDED, members, **common))
        elif record.kind == import_stage.WINDOW_DISCARDED:
            windows.append(Window(import_id, import_stage.WINDOW_DISCARDED, record.members, record.acknowledged, **common))
        else:
            windows.append(Window(import_id, import_stage.WINDOW_UNREADABLE, problem=record.problem, **common))
    return windows


def _page(windows: list[Window], after: str | None) -> tuple[dict[str, dict[bool, list[dict]]], str | None]:
    """One page of members across the windows, ordered by import ID then path, from the member after `after`.

    A cursor is `<import ID>/<path>`; an import ID has a fixed length, so string order is the order of the pair.
    The acknowledged members page with the others, and a window's record lists a path once (`import_stage` reads a
    record that lists it twice as damaged), so no two keys are equal. A listed member is filed under whether it is acknowledged.
    """
    keyed = sorted(
        (f"{window.import_id}/{member['path']}", window.import_id, acknowledged, member)
        for window in windows
        for acknowledged, members in ((False, window.members), (True, window.acknowledged)) for member in members
    )
    rest = [item for item in keyed if after is None or item[0] > after]
    page, more = rest[:PAGE_MEMBERS], len(rest) > PAGE_MEMBERS
    listed: dict[str, dict[bool, list[dict]]] = {}
    for _key, import_id, acknowledged, member in page:
        listed.setdefault(import_id, {False: [], True: []})[acknowledged].append(member)
    return listed, (page[-1][0] if more else None)


def _window_entry(window: Window, listed: dict[bool, list[dict]]) -> dict:
    total = sum(member["bytes"] for member in window.members)
    entry: dict = {"import_id": window.import_id, "state": window.state}
    if window.problem is not None:
        entry["problem"] = window.problem
    if window.renames_pending:
        entry["renames_pending"] = list(window.renames_pending)
    if window.state == import_stage.WINDOW_LANDED:
        staged = [member for member in window.members if member["staged"]]
        entry["unfiled"] = {"count": len(window.members), "bytes": total}
        entry["unfiled_staged"] = {"count": len(staged), "bytes": sum(member["bytes"] for member in staged)}
        entry["members"] = [{**member, "state": "unfiled"} for member in listed[False]]
    elif window.state == import_stage.WINDOW_DISCARDED:
        entry["residual"] = {"count": len(window.members), "bytes": total}
        entry["members"] = [{**member, "state": DISCARDED_NOT_FILED} for member in listed[False]]
        entry["acknowledged"] = {
            "count": len(window.acknowledged), "bytes": sum(member["bytes"] for member in window.acknowledged),
            "members": listed[True],
        }
    return entry


def _keep_source(windows: list[Window]) -> dict:
    """Whether the person still needs the source folder; a window that cannot be read counts, because it may hold a member."""
    unfiled = sum(len(w.members) for w in windows if w.state == import_stage.WINDOW_LANDED)
    discarded = sum(len(w.members) for w in windows if w.state == import_stage.WINDOW_DISCARDED)
    unread = sum(w.state == import_stage.WINDOW_UNREADABLE for w in windows)
    if not (unfiled or discarded or unread):
        return {"keep": False, "message": None}
    parts = [f"{unfiled} unfiled member(s) in landed windows"] if unfiled else []
    parts += [f"{discarded} member(s) {DISCARDED_NOT_FILED}"] if discarded else []
    parts += [f"{unread} window(s) whose record cannot be read"] if unread else []
    return {"keep": True, "message": "Keep the source folder: " + ", ".join(parts) + "."}


def _windows_section(windows: list[Window], after: str | None) -> dict:
    listed, next_after = _page(windows, after)
    states = (import_stage.WINDOW_LANDED, import_stage.WINDOW_DISCARDED, import_stage.WINDOW_UNREADABLE)
    counts = {state: sum(w.state == state for w in windows) for state in states}
    return {
        "counts": counts,
        # Every window with a record is listed, a fully filed one too: the sender looks a window's receipt up by its ID.
        "windows": [_window_entry(window, listed.get(window.import_id, {False: [], True: []})) for window in windows],
        "keep_source": _keep_source(windows),
        "next_after": next_after,
    }


def _volume(root: Path) -> tuple[int, int]:
    try:
        usage = shutil.disk_usage(root)
    except OSError as error:
        raise _Unknown(f"the volume's size and use cannot be read: {error}") from error
    return usage.total, usage.used


def _growth_threshold(config: dict) -> float:
    threshold = media_capacity.growth_threshold(config)
    if threshold is None:
        raise _Unknown("the media store policy sets no growth threshold")
    return threshold


def _limits(config: dict) -> media_capacity.HydrationLimits:
    limits = media_capacity.hydration_limits(config)
    if limits is None:
        raise _Unknown(media_capacity.hydration_policy_problem(config) or "the media store policy sets no hydration capacity")
    return limits


def _bounds(config: dict, root: Path) -> media_capacity.Bounds:
    _limits(config)
    try:
        bounds = media_capacity.bounds_for(config, root)
    except _UNREAD as error:
        raise _Unknown(f"the media store policy's bounds or the volume's size cannot be read: {error}") from error
    assert bounds is not None  # `_limits` found the capacity that `bounds_for` reads.
    return bounds


def _eviction_idle(config: dict) -> float | None:
    """The minimum idle age of an evictable original, or None when the policy keeps eviction off."""
    if config.get("eviction_enabled") is not True:
        return None
    try:
        return float(config["minimum_idle_seconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise _Unknown("the media store policy sets eviction without a valid minimum idle age") from error


def _occupancy(root: Path, bounds: _Reading, idle: _Reading) -> media_store.Occupancy:
    try:
        return media_store.occupancy(root, bounds.need(), eviction_idle_seconds=idle.need())
    except (media_store.MediaStoreError, *_UNREAD) as error:
        raise _Unknown(str(error)) from error


def _unfiled(windows: _Reading, bounds: _Reading) -> dict[str, int]:
    """The bytes that landed windows have not filed, by the classification rule's classes."""
    rule = bounds.need()
    totals = {media_capacity.TEXT: 0, media_capacity.OFFLOADABLE: 0, media_capacity.RESIDENT: 0}
    for window in windows.need():
        # With no stage left, no byte of the window is on the volume.
        if window.state == import_stage.WINDOW_UNREADABLE and window.staged:
            raise _Unknown(f"window {window.import_id} cannot be read, so its unfiled members are unknown")
        if window.state == import_stage.WINDOW_LANDED:
            for member in window.members:
                size = member["bytes"]
                totals[rule.classify(size, text=adoption_run.reads_as_text(member["path"], size))] += size
    return totals


def _allowance(root: Path, config: dict, *, may_count: bool) -> dict:
    """The allowance and the owner census that it was measured by, with the census's time.

    A census walks every note under the mutation boundary, which blocks filing for as long as the vault is large,
    so a plain status reads the latest census that offload recorded. A call that carries a demand (`may_count`)
    counts afresh and records the result when nothing is recorded, or the record is older than the demand lease.
    A census that could not count, or none at all, is unknown, never 0.
    """
    allowance = config.get("allowance_bytes")
    if type(allowance) is not int or allowance < 0:
        return {"unknown": "the media store policy sets no allowance"}
    try:
        counted = media_store.recorded_census(root)
        if may_count and (counted is None or counted.older_than(media_store.DEMAND_LEASE_SECONDS)):
            counted = media_store.take_census(root)
    except media_store.MediaStoreError as error:
        return {"allowance_bytes": allowance, "unknown": str(error)}
    if counted is None:
        return {"allowance_bytes": allowance, "unknown": "no owner census is recorded yet; the next offload records one"}
    if counted.used_bytes is None:
        return {"allowance_bytes": allowance, "unknown": "the stored versions could not be counted",
                "observed_at": counted.observed_at}
    return {"allowance_bytes": allowance, "used_bytes": counted.used_bytes,
            "remaining_bytes": max(0, allowance - counted.used_bytes), "observed_at": counted.observed_at}


def _budget_section(root: Path, config: dict, windows: _Reading, series: tuple[str, int] | None) -> dict:
    """Every figure of the window budget; D12 fixes each formula."""
    volume, threshold = _read(lambda: _volume(root)), _read(lambda: _growth_threshold(config))
    limits, idle = _read(lambda: _limits(config)), _read(lambda: _eviction_idle(config))
    bounds = _read(lambda: _bounds(config, root))
    unfiled = _read(lambda: _unfiled(windows, bounds))
    occupancy = _read(lambda: _occupancy(root, bounds, idle))

    def largest() -> int:
        return max(0, bounds.need().largest_offloadable())

    def window_budget() -> int:
        total, used = volume.need()
        capacity = limits.need()
        fixed = cloud_import.reserve_bytes_from_env() + capacity.temporary_bytes + capacity.hydrations * largest()
        return int(threshold.need() * total) - used - fixed - unfiled.need()[media_capacity.TEXT]

    def reachable() -> int:
        # With eviction off nothing cached, offloaded or filed ever leaves the volume, so the budget cannot grow.
        if idle.need() is None:
            return window_budget()
        held = occupancy.need()
        # `evictable_bytes` is a sum over ledger rows, so it can exceed what eviction frees: `evict` verifies each
        # candidate again and skips an original that changed. The reachable budget is therefore an upper bound.
        return window_budget() + held.evictable_bytes + held.pending_offload_bytes + unfiled.need()[media_capacity.OFFLOADABLE]

    def effective() -> int:
        # Pinned bytes return to the volume when their jobs end, which only eviction turns into room.
        pinned = 0 if idle.need() is None else occupancy.need().pinned_bytes
        return max(0, min(largest(), reachable() + pinned))

    budget, reach = _figure(window_budget), _figure(reachable)
    section: dict = {
        "window_budget": budget,
        "reachable_budget": reach,
        "job_pinned": _figure(lambda: occupancy.need().pinned_bytes),
        "effective_file_limit": _figure(effective),
        "classification": _classification(bounds),
        "volume": _volume_section(volume),
        "media_allowance": _allowance(root, config, may_count=series is not None),
        "drain": _drain(unfiled, occupancy, idle),
    }
    if series is not None:
        section["demand"] = _demand(root, series, budget, reach)
    return section


def _volume_section(volume: _Reading) -> dict:
    """The bytes that the volume uses, which the sender's admission rule needs; unknown when the volume cannot be read."""
    try:
        return {"used_bytes": volume.need()[1]}
    except _Unknown as why:
        return {"unknown": str(why)}


def _classification(bounds: _Reading) -> dict:
    try:
        rule = bounds.need()
    except _Unknown as why:
        return {"unknown": str(why)}
    return {
        "object_bound": rule.object_bytes,
        "hydration_bytes": rule.hydration_bytes,
        "volume_fraction": rule.volume_fraction,
        "volume_bytes": rule.volume_bytes,
        "inline_text_limit": adoption_plan.INLINE_TEXT_MAX_BYTES,
        "largest_offloadable_file": rule.largest_offloadable(),
    }


def _drain(unfiled: _Reading, occupancy: _Reading, idle: _Reading) -> dict:
    drain: dict = {}
    try:
        classes = unfiled.need()
        drain["unfiled_bytes"] = {"bytes": sum(classes.values()), **classes}
    except _Unknown as why:
        drain["unfiled_bytes"] = {"unknown": str(why)}
    try:
        held = occupancy.need()
        drain["pending_offloads"] = {"count": held.pending_offloads, "bytes": held.pending_offload_bytes}
        drain["evictable"] = {"bytes": held.evictable_bytes, "eviction": "on" if idle.need() is not None else "off"}
    except _Unknown as why:
        drain["pending_offloads"] = drain["evictable"] = {"unknown": str(why)}
    return drain


def _demand(root: Path, series: tuple[str, int], budget: dict, reachable: dict) -> dict:
    """Record the series' demand with the budgets read here; the request never supplies one."""
    series_id, window_bytes = series
    outcome: dict = {"series_id": series_id, "bytes": window_bytes, "recorded": False}
    if "bytes" not in budget or "bytes" not in reachable:
        return {**outcome, "reason": "the window budget or the reachable budget is unknown"}
    try:
        recorded = media_store.record_demand(
            root, series_id, window_bytes, budget=budget["bytes"], reachable_budget=reachable["bytes"])
    except _UNREAD as error:
        return {**outcome, "reason": f"the media ledger cannot record it: {error}"}
    if not recorded:
        return {**outcome, "reason": "the window is above the reachable budget"}
    return {**outcome, "recorded": True}


def _series(series_id: object, next_window_bytes: object) -> tuple[str, int] | None:
    """The demand that a status call carries, or None when it carries none; a half or a malformed one is refused."""
    if series_id is None and next_window_bytes is None:
        return None
    if (not isinstance(series_id, str) or not 0 < len(series_id) <= MAX_SERIES_ID
            or type(next_window_bytes) is not int or next_window_bytes < 0):
        raise adoption_run.AdoptionRunError(
            "IMPORT_REQUEST_INVALID",
            f"a demand takes a series ID of 1 to {MAX_SERIES_ID} characters and the next window's bytes, together")
    return series_id, next_window_bytes


def report(root: Path, *, after: str | None = None, series_id: str | None = None, next_window_bytes: int | None = None) -> dict:
    """The window figures to add to the run-listing status; empty for a vault with no window and no media store."""
    root = Path(root)
    series = _series(series_id, next_window_bytes)
    windows = _read(lambda: _census_or_unknown(root))
    try:
        config = originals.configuration(root)
    except (OSError, ValueError) as error:
        config, config_problem = None, f"the media store configuration cannot be read: {error}"
    else:
        config_problem = None
    reply: dict = {}
    try:
        listed = windows.need()
    except _Unknown as why:
        reply["import_windows"] = {"unknown": str(why), "keep_source": {"keep": True, "message": f"Keep the source folder: {why}."}}
    else:
        if listed:
            reply["import_windows"] = _windows_section(listed, after)
    if config_problem is not None:
        reply["window_budget"] = {"unknown": config_problem}
    elif config is not None and config.get("enabled") is True:
        reply["window_budget"] = _budget_section(root, config, windows, series)
    elif config is not None and config.get("enabled") is False:
        reply["retained_store"] = _retained_section(root, config)
    return reply


def _retained_section(root: Path, config: dict) -> dict:
    """A turned-off store's drain: the shortfall, the census, whether it latched, what it waits for and what cannot hydrate (D12)."""
    def shortfall() -> int:
        try:
            return media_store.retained_shortfall(root, config)
        except media_store.MediaStoreError as error:
            raise _Unknown(str(error)) from error

    waiting = None
    try:
        record = media_store.drain_record(root)
    except media_store.MediaStoreError as error:
        census = drained = {"unknown": str(error)}
    else:
        complete = (None if record is None or record.evicted is None else
                    {"evicted": record.evicted, "bytes": record.evicted_bytes, "captured_at": record.captured_at})
        if record is not None and record.attempt_failed:
            # The latest attempt could not count; the last complete figures stay as history, never as the answer.
            census = {"unknown": "the latest census could not count the evicted originals", "attempted_at": record.attempted_at}
            if complete is not None:
                census["last_complete"] = complete
        else:
            census = complete or {"unknown": "no census has completed"}
        drained = record is not None and record.latched
        waiting = None if record is None else record.waiting
    try:
        above = {"count": media_store.evicted_above_capacity(root, config)}
    except media_store.MediaStoreError as error:
        above = {"unknown": str(error)}
    return {"shortfall": _figure(shortfall), "census": census, "drained": drained, "drain_waiting": waiting,
            "above_hydration_capacity": above}


def _census_or_unknown(root: Path) -> list[Window]:
    try:
        return census(root)
    except OSError as error:
        raise _Unknown(f"the import records cannot be listed: {error}") from error


def acknowledge(root: Path, import_id: str | None, paths: object, *, at: str | None = None) -> dict:
    """Move the named paths of one discarded window's residual record under `acknowledged`; the person decides, never the caller.

    The call refuses without a list of paths, so no call clears a whole record by accident. A path that the
    record does not list changes nothing and is reported. The entries stay in the record with the time (`at`,
    now by default), so the status lists them and a mistaken acknowledgement can be undone by hand.
    """
    root = Path(root)
    if not vault.is_import_id(import_id):
        raise adoption_run.AdoptionRunError("IMPORT_REQUEST_INVALID", "acknowledge takes the import ID of a discarded window")
    named = {path for path in paths if isinstance(path, str) and path} if isinstance(paths, list) else set()
    if not named:
        raise adoption_run.AdoptionRunError(
            "IMPORT_REQUEST_INVALID", "acknowledge takes a list of the paths to clear and never clears a whole record")
    assert import_id is not None
    with _ACKNOWLEDGE_LOCK:
        try:
            cleared = import_stage.clear_residual(root, import_id, named, at or datetime.now(UTC).isoformat(timespec="seconds"))
        except (OSError, ValueError) as error:
            raise adoption_run.AdoptionRunError("IMPORT_RESIDUAL_UNWRITABLE", f"the record cannot be used: {error}") from error
        remaining = import_stage.read_residual(root, import_id)
    if cleared is None:
        raise adoption_run.AdoptionRunError("IMPORT_NO_RESIDUAL", "that window has no residual record to clear")
    return {
        "mode": "adoption",
        "mutated": bool(cleared),
        "import_id": import_id,
        "cleared": cleared,
        "not_in_record": sorted(named - set(cleared)),
        "remaining_members": len(remaining or []),
    }
