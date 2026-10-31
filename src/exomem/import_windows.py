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
from pathlib import Path

from . import (
    adoption_plan,
    adoption_run,
    cloud_import,
    import_stage,
    media_allowance,
    media_capacity,
    media_store,
    originals,
    vault,
)

# Members that one status reply lists; `next_after` continues the list.
PAGE_MEMBERS = 200
# These states and the stage label are the status protocol's words, not vault vocabulary.
LANDED, DISCARDED, UNREADABLE = "landed", "discarded", "unreadable"
DISCARDED_NOT_FILED = "discarded, not filed"
# The longest series ID a demand lease accepts; the sender's IDs are short, and the lease is a row per series.
MAX_SERIES_ID = 128

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

    A landed window lists the manifest members that no run filed. A discarded one lists its residual record.
    An unreadable one has a stage and no record that can be read, so nothing about its members is known.
    """

    import_id: str
    state: str
    members: tuple[dict, ...] = ()
    problem: str | None = None


def _record_members(record: list[dict]) -> tuple[dict, ...] | None:
    """The members of a residual record, or None when an entry is not a path, a byte count and a code."""
    members = []
    for entry in record:
        if not (isinstance(entry.get("path"), str) and type(entry.get("bytes")) is int and isinstance(entry.get("code"), str)):
            return None
        members.append({"path": entry["path"], "bytes": entry["bytes"], "code": entry["code"]})
    return tuple(members)


def census(root: Path) -> list[Window]:
    """Every window of the cell in import ID order; raises OSError when the records cannot be listed."""
    runs = adoption_run.AdoptionRunStore(root).runs_by_stage()
    windows = []
    for import_id in import_stage.window_ids(root):
        stage = import_stage.read(root, import_id)
        if stage is not None and stage.proof == import_stage.CLOUD_PROOF:
            staged = {path for path, _size in import_stage.staged_files(root, import_id)}
            unfiled = adoption_run.unfiled_members(root, import_id, runs.get(import_id, ()))
            windows.append(Window(import_id, LANDED, tuple({**member, "staged": member["path"] in staged} for member in unfiled)))
            continue
        record = import_stage.read_residual(root, import_id)
        if record is None:
            if (root / import_stage.STAGE_DIR / import_id).exists():
                windows.append(Window(import_id, UNREADABLE, problem="the receipt of this window cannot be read"))
            continue  # A window with no stage and no record has nothing left to report.
        members = _record_members(record)
        windows.append(Window(import_id, DISCARDED, members or (), None if members is not None else "its residual record is damaged"))
    return windows


def _page(windows: list[Window], after: str | None) -> tuple[dict[str, list[dict]], str | None]:
    """One page of members across the windows, ordered by import ID then path, from the member after `after`.

    A cursor is `<import ID>/<path>`; an import ID has a fixed length, so string order is the order of the pair.
    """
    keyed = sorted(
        (f"{window.import_id}/{member['path']}", window.import_id, member)
        for window in windows for member in window.members
    )
    rest = [item for item in keyed if after is None or item[0] > after]
    page, more = rest[:PAGE_MEMBERS], len(rest) > PAGE_MEMBERS
    listed: dict[str, list[dict]] = {}
    for _key, import_id, member in page:
        listed.setdefault(import_id, []).append(member)
    return listed, (page[-1][0] if more else None)


def _window_entry(window: Window, listed: list[dict]) -> dict:
    total = sum(member["bytes"] for member in window.members)
    entry: dict = {"import_id": window.import_id, "state": window.state}
    if window.problem is not None:
        entry["problem"] = window.problem
    if window.state == LANDED:
        staged = [member for member in window.members if member["staged"]]
        entry["unfiled"] = {"count": len(window.members), "bytes": total}
        entry["residual"] = {"count": len(staged), "bytes": sum(member["bytes"] for member in staged)}
        entry["members"] = [{**member, "state": "unfiled"} for member in listed]
    elif window.state == DISCARDED:
        entry["residual"] = {"count": len(window.members), "bytes": total}
        entry["members"] = [{**member, "state": DISCARDED_NOT_FILED} for member in listed]
    return entry


def _keep_source(windows: list[Window]) -> dict:
    """Whether the person still needs the source folder; a window that cannot be read counts, because it may hold a member."""
    unfiled = sum(len(w.members) for w in windows if w.state == LANDED)
    discarded = sum(len(w.members) for w in windows if w.state == DISCARDED)
    unread = sum(w.state == UNREADABLE or w.problem is not None for w in windows)
    if not (unfiled or discarded or unread):
        return {"keep": False, "message": None}
    parts = [f"{unfiled} unfiled member(s) in landed windows"] if unfiled else []
    parts += [f"{discarded} member(s) {DISCARDED_NOT_FILED}"] if discarded else []
    parts += [f"{unread} window(s) whose record cannot be read"] if unread else []
    return {"keep": True, "message": "Keep the source folder: " + ", ".join(parts) + "."}


def _windows_section(windows: list[Window], after: str | None) -> dict:
    listed, next_after = _page(windows, after)
    reported = [w for w in windows if w.state == LANDED or w.members or w.problem is not None]
    counts = {state: sum(w.state == state for w in windows) for state in (LANDED, DISCARDED, UNREADABLE)}
    return {
        "counts": counts,
        "windows": [_window_entry(window, listed.get(window.import_id, [])) for window in reported],
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
    value = config.get("growth_threshold")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise _Unknown("the media store policy sets no growth threshold")
    return float(value)


def _limits(config: dict) -> media_capacity.HydrationLimits:
    limits = media_capacity.hydration_limits(config)
    if limits is None:
        raise _Unknown(media_capacity.hydration_policy_problem(config) or "the media store policy sets no hydration capacity")
    return limits


def _bounds(config: dict, root: Path) -> media_capacity.Bounds:
    _limits(config)
    try:
        bounds = media_capacity.bounds_for(config, root)
    except OSError as error:
        raise _Unknown(f"the volume's size cannot be read: {error}") from error
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
    except media_store.MediaStoreError as error:
        raise _Unknown(str(error)) from error


def _unfiled(windows: _Reading, bounds: _Reading) -> dict[str, int]:
    """The bytes that landed windows have not filed, by the classification rule's classes."""
    rule = bounds.need()
    totals = {media_capacity.TEXT: 0, media_capacity.OFFLOADABLE: 0, media_capacity.RESIDENT: 0}
    for window in windows.need():
        if window.state == UNREADABLE:
            raise _Unknown(f"window {window.import_id} cannot be read, so its unfiled members are unknown")
        if window.state == LANDED:
            for member in window.members:
                size = member["bytes"]
                totals[rule.classify(size, text=adoption_run.reads_as_text(member["path"], size))] += size
    return totals


def _allowance(root: Path, config: dict) -> dict:
    allowance = config.get("allowance_bytes")
    if type(allowance) is not int or allowance < 0:
        return {"unknown": "the media store policy sets no allowance"}
    used = media_allowance.reference_usage(root).used_bytes
    if used is None:
        return {"allowance_bytes": allowance, "unknown": "the stored versions cannot be counted"}
    return {"allowance_bytes": allowance, "used_bytes": used, "remaining_bytes": max(0, allowance - used)}


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
        return window_budget() + held.evictable_bytes + held.pending_offload_bytes + unfiled.need()[media_capacity.OFFLOADABLE]

    def effective() -> int:
        return max(0, min(largest(), reachable() + occupancy.need().pinned_bytes))

    budget, reach = _figure(window_budget), _figure(reachable)
    section: dict = {
        "window_budget": budget,
        "reachable_budget": reach,
        "job_pinned": _figure(lambda: occupancy.need().pinned_bytes),
        "effective_file_limit": _figure(effective),
        "classification": _classification(bounds),
        "media_allowance": _allowance(root, config),
        "drain": _drain(unfiled, occupancy, idle),
    }
    if series is not None:
        section["demand"] = _demand(root, series, budget, reach)
    return section


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
    except (OSError, sqlite3.Error) as error:
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
    return reply


def _census_or_unknown(root: Path) -> list[Window]:
    try:
        return census(root)
    except OSError as error:
        raise _Unknown(f"the import records cannot be listed: {error}") from error


def acknowledge(root: Path, import_id: str | None, paths: list[str] | None) -> dict:
    """Clear the named paths from one discarded window's residual record; the person decides, never the caller.

    The call refuses without paths, so no call clears a whole record by accident. A path that the record
    does not list changes nothing and is reported.
    """
    root = Path(root)
    named = {path for path in paths or [] if isinstance(path, str) and path}
    if not vault.is_import_id(import_id):
        raise adoption_run.AdoptionRunError("IMPORT_REQUEST_INVALID", "acknowledge takes the import ID of a discarded window")
    if not named:
        raise adoption_run.AdoptionRunError(
            "IMPORT_REQUEST_INVALID", "acknowledge takes the paths to clear and never clears a whole record")
    assert import_id is not None
    with _ACKNOWLEDGE_LOCK:
        try:
            cleared = import_stage.clear_residual(root, import_id, named)
        except (OSError, ValueError) as error:
            raise adoption_run.AdoptionRunError("IMPORT_RESIDUAL_UNWRITABLE", f"the record cannot be written: {error}") from error
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
