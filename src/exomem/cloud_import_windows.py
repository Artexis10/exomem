"""Cloud import in windows: a folder that exceeds the cell's volume arrives as a series of ordinary imports.

The cell reports every budget in `adoption_studio status` (D12 of add-cloud-media-store); this module never computes
one. It plans a window from the reported reachable budget, reads the budget again right before it creates the
window, and waits or stops as the design says. The series record lives in the checkpoint folder.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastmcp import Client

from . import adoption_run, import_selection, import_stage, media_capacity
from .cell_init import _ensure_private_directory
from .cli_progress import Progress, human_bytes
from .cloud_import import STORAGE_ALLOWANCE_EXCEEDED
from .cloud_import_client import (
    PREFLIGHT,
    CloudImportClientConfig,
    CloudImportError,
    Window,
    _coded,
    _Where,
    import_source,
)
from .cloud_import_job import _private_json

WINDOWS = "windows"
SERIES_RECORD, RESEND_RECORD = "series.json", "series-resend.json"
# D3: a demand is a lease that lapses after 15 minutes, and the sender renews it at least every 5.
DEMAND_RENEWAL_SECONDS = 300
WAIT_POLL_SECONDS = 30
# A series that creates no window for this long stops, so a stalled cell never holds the command forever.
WAIT_LIMIT_SECONDS = 7200
LIST_LIMIT = 50
GIB = 1 << 30

OFFLOADABLE, RESIDENT, TEXT = media_capacity.OFFLOADABLE, media_capacity.RESIDENT, media_capacity.TEXT
# D12 order: offloadable artifacts, resident artifacts, then text, each in path order.
_CLASS_ORDER = {OFFLOADABLE: 0, RESIDENT: 1, TEXT: 2}


class Cell:
    """What the CLI asks of the cell's Adoption Studio. Tests give a synthetic one."""

    async def status(self, *, series_id: str | None = None, next_window_bytes: int | None = None,
                     after: str | None = None) -> dict:
        raise NotImplementedError

    async def acknowledge(self, import_id: str, paths: list[str]) -> dict:
        raise NotImplementedError


class McpCell(Cell):
    """The cell's tools through the Cloud gateway, signed in with the CLI's OAuth client."""

    def __init__(self, config: CloudImportClientConfig, oauth):
        self._client = Client(config.mcp_url, auth=oauth, timeout=120)

    async def __aenter__(self) -> McpCell:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *error) -> None:
        await self._client.__aexit__(*error)

    async def _call(self, arguments: dict) -> dict:
        result = await self._client.call_tool("adoption_studio", arguments)
        if isinstance(result.structured_content, dict):
            return result.structured_content
        return json.loads(result.content[0].text)

    async def status(self, *, series_id=None, next_window_bytes=None, after=None) -> dict:
        arguments = {"action": "status", "series_id": series_id, "next_window_bytes": next_window_bytes, "after": after}
        return await self._call({key: value for key, value in arguments.items() if value is not None})

    async def acknowledge(self, import_id: str, paths: list[str]) -> dict:
        return await self._call({"action": "acknowledge", "import_id": import_id, "paths": paths})


@dataclass(frozen=True)
class Figure:
    """One reported number: its bytes, or why the cell cannot say."""

    bytes: int | None
    why: str | None = None

    @classmethod
    def of(cls, reported: object) -> Figure:
        if isinstance(reported, dict) and type(reported.get("bytes")) is int:
            return cls(reported["bytes"])
        return cls(None, reported.get("unknown") if isinstance(reported, dict) and reported.get("unknown") else "the cell did not report it")


@dataclass(frozen=True)
class Reading:
    """`adoption_studio status`, as far as a sender reads it. An unknown figure stays unknown."""

    older_cell: bool
    budget: Figure
    reachable: Figure
    pinned: Figure
    effective: Figure
    bounds: media_capacity.Bounds | None
    bounds_why: str | None
    allowance: Figure
    volume_used: Figure
    keep_source: dict | None
    windows: list[dict] = field(default_factory=list)
    next_after: str | None = None
    # Why the cell cannot list its window records, or None. An unknown listing is not an empty one.
    windows_unknown: str | None = None
    # The classification the cell reports; None when it reports none, and the sender's own copy of the rule serves.
    largest_offloadable: int | None = None
    inline_text_limit: int | None = None

    @classmethod
    def of(cls, reply: dict) -> Reading:
        section = reply.get("window_budget")
        # `report()` leaves `import_windows` out when the cell holds no window record: that is an empty listing.
        # Only a listing that says it is unknown, or that has no `windows` list, proves nothing.
        listing = reply.get("import_windows", {"windows": []})
        windows = listing if isinstance(listing, dict) else {}
        listing_why = None
        if "windows" not in windows:
            listing_why = windows.get("unknown") or "the cell did not list its window records"
        common = {"keep_source": windows.get("keep_source"), "windows": windows.get("windows") or [],
                  "next_after": windows.get("next_after"), "windows_unknown": str(listing_why) if listing_why else None}
        unknown = Figure(None, "the cell reports no window budget")
        if not isinstance(section, dict):
            return cls(True, unknown, unknown, unknown, unknown, None, unknown.why, unknown, unknown, **common)
        if "window_budget" not in section:
            why = Figure.of(section)
            return cls(False, why, why, why, why, None, why.why, why, why, **common)
        classification = section.get("classification") or {}
        try:
            bounds = media_capacity.Bounds(
                classification["object_bound"], classification["hydration_bytes"], classification["volume_bytes"],
                classification["volume_fraction"])
            bounds_why = None
        except KeyError:
            bounds, bounds_why = None, classification.get("unknown") or "the cell did not report its classification inputs"
        used = (section.get("volume") or {}).get("used_bytes")
        allowance = section.get("media_allowance") or {}
        remaining = {"bytes": allowance["remaining_bytes"]} if "remaining_bytes" in allowance else allowance
        reported = {name: classification.get(key) for name, key in
                    (("largest_offloadable", "largest_offloadable_file"), ("inline_text_limit", "inline_text_limit"))}
        reported = {name: value for name, value in reported.items() if type(value) is int}
        return cls(False, Figure.of(section["window_budget"]), Figure.of(section.get("reachable_budget")),
                   Figure.of(section.get("job_pinned")), Figure.of(section.get("effective_file_limit")),
                   bounds, bounds_why, Figure.of(remaining), Figure.of({"bytes": used}), **common, **reported)

    def unknown_reasons(self) -> list[str]:
        figures = (self.budget, self.reachable, self.pinned)
        return sorted({figure.why for figure in figures if figure.bytes is None and figure.why}
                      | ({self.bounds_why} if self.bounds is None else set()))


@dataclass
class Member:
    path: str
    size: int
    mtime_ns: int
    klass: str

    @property
    def cost(self) -> int:
        """What the member takes of the window budget: text files twice, because the stage keeps a copy."""
        return 2 * self.size if self.klass == TEXT else self.size


@dataclass
class SentWindow:
    paths: list[str]
    dir: str
    state: str = "open"  # open: created or being sent; landed: the cell finished it; settled: the resend cleared its old records
    import_id: str | None = None
    cost: int = 0
    # The import IDs of earlier attempts that ended terminally or declared another member set. A receipt for one lands the window.
    attempts: list[str] = field(default_factory=list)

    def ids(self) -> list[str]:
        return [*self.attempts, *([self.import_id] if self.import_id else [])]

    def renew(self) -> None:
        """Give the window a new import ID: Cloud replays a known ID whatever its state, and refuses a changed declaration."""
        if self.import_id:
            self.attempts.append(self.import_id)
        self.import_id = str(uuid.uuid4())


@dataclass
class Series:
    source: str
    api_base_url: str
    series_id: str
    series_file: str
    members: list[Member]
    windows: list[SentWindow] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    # Resend only: each member's path -> the import ID of the old window whose residual record lists it.
    clears: dict[str, str] = field(default_factory=dict)
    # Members that changed or vanished at the source before their window was sent: never sent, so never planned again.
    skipped: list[str] = field(default_factory=list)

    def not_landed(self) -> list[Member]:
        """The members that the cell has not received: unplanned or skipped ones and those of a window still open."""
        landed = {path for window in self.windows if window.state != "open" for path in window.paths}
        return [member for member in self.members if member.path not in landed]

    @classmethod
    def read(cls, path: Path) -> Series | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return cls(raw["source"], raw["api_base_url"], raw["series_id"], raw["series_file"],
                   [Member(**item) for item in raw["members"]], [SentWindow(**item) for item in raw["windows"]],
                   raw["options"], raw.get("clears", {}), raw.get("skipped", []))

    def write(self, path: Path) -> None:
        _private_json(path, asdict(self))


def not_sent_notice(members: list[Member]) -> str:
    """The sentence that every stop carries (D12): what is not in Exomem yet, and that the person must keep it."""
    count, size = len(members), sum(member.size for member in members)
    return (f"The members not yet sent ({count:,} files, {human_bytes(size)}) are not in Exomem. "
            "You must keep them: do not delete or move your source folder")


class Stopped(CloudImportError):
    """A stop that already says the members not yet sent are not in Exomem."""


def stopped(series: Series, code: str, message: str) -> Stopped:
    return Stopped(WINDOWS, code, f"{message}. {not_sent_notice(series.not_landed())}", resume=True)


def _listed(items: list[str]) -> str:
    shown = items[:LIST_LIMIT]
    more = len(items) - len(shown)
    return "; ".join(shown) + (f"; and {more:,} more" if more else "")


class _Run:
    """One series: its record, the cell and the Cloud API. A run keeps the state that its steps share."""

    def __init__(self, series: Series, record: Path, source: Path, config: CloudImportClientConfig, *, cell: Cell, api,
                 uploads, limits: dict | None, progress: Progress, poll_seconds: float, sleep, clock, on_landed=None):
        self.series, self.record, self.source, self.config = series, record, source, config
        self.cell, self.api, self.uploads, self.limits, self.progress = cell, api, uploads, limits, progress
        self.sleep, self.clock, self.on_landed = sleep, clock, on_landed
        # No wait outlasts a demand's renewal interval, so a waiting sender renews its demand in time.
        self.poll = min(poll_seconds, DEMAND_RENEWAL_SECONDS)
        self.shown: str | None = None
        self.refused: int | None = None

    # -- reading and reporting

    def unplanned(self) -> list[Member]:
        """The members that no window holds yet."""
        planned = {path for window in self.series.windows for path in window.paths} | set(self.series.skipped)
        return [member for member in self.series.members if member.path not in planned]

    def stop(self, code: str, message: str) -> Stopped:
        """Every stop says that the members not yet sent are not in Exomem."""
        return stopped(self.series, code, message)

    def say_once(self, line: str) -> None:
        if line != self.shown:
            self.progress.say(line)
            self.shown = line

    async def read(self, demand: int | None = None) -> Reading:
        arguments = {"series_id": self.series.series_id, "next_window_bytes": demand} if demand is not None else {}
        reply = await self.cell.status(**arguments)
        return Reading.of(reply)

    async def wait(self, since: float, reason: str) -> None:
        self.say_once(f"Waiting: {reason}")
        if self.clock() - since >= WAIT_LIMIT_SECONDS:
            raise self.stop("WAIT_TIMED_OUT", f"the series waited {WAIT_LIMIT_SECONDS // 3600} hours and created no window; last reason: {reason}")
        await self.sleep(self.poll)

    # -- one window

    def admission_bytes(self, reading: Reading) -> tuple[int | None, str | None]:
        """The most plain bytes a window may carry so that admission grows no claim, or None when nothing limits it.

        Admission applies only where the cell publishes its limits: used + `space_factor` x bytes + `reserve_bytes`
        within the cell's current size (D12).
        """
        limits = self.limits
        if limits is None:
            return None, None
        # A published limit that cannot be read is unknown, never "no limit". Both limits are whole numbers.
        if not isinstance(limits, dict):
            return None, "the cell's published admission limits cannot be read"
        values = limits.get("space_factor"), limits.get("reserve_bytes")
        if any(value is not None and (type(value) is not int or value < 0) for value in values):
            return None, "the cell's published admission limits cannot be read"
        if any(value is None for value in values):
            return None, None
        if limits["space_factor"] < 1:
            return None, "the cell's published admission limits cannot be read"
        if reading.volume_used.bytes is None or reading.bounds is None:
            return None, "the cell does not report the volume's use or size, which admission needs"
        room = reading.bounds.volume_bytes - reading.volume_used.bytes - limits["reserve_bytes"]
        return max(0, room // limits["space_factor"]), None

    def plan(self, pool: list[Member], reading: Reading, ceiling: int | None) -> tuple[list[Member], int, str | None]:
        """The members of the next window, its cost against the budget, and why nothing fits yet (else None).

        Raises the stop when not even the next member can land. `ceiling` caps the cost after the Job refused a window.
        """
        series_file = self.series.series_file.encode()
        series_cost = 2 * len(series_file)
        limit = reading.reachable.bytes if ceiling is None else min(reading.reachable.bytes, ceiling)
        admitted, unadmitted = self.admission_bytes(reading)
        if unadmitted:
            return [], 0, unadmitted
        taken, cost, plain = [], series_cost, len(series_file)
        for member in pool:
            if cost + member.cost > limit or (admitted is not None and plain + member.size > admitted):
                break
            taken.append(member)
            cost, plain = cost + member.cost, plain + member.size
        if taken:
            return taken, cost, None
        need, pinned = series_cost + pool[0].cost, reading.pinned.bytes
        if ceiling is not None and need > ceiling:
            raise self.stop("WINDOW_REFUSED_FOR_SPACE", "the Job refused a window for space and the next member cannot form a smaller one")
        if need > reading.reachable.bytes + pinned:
            raise self.stop(
                "BAND_TOO_SMALL",
                f"band too small: the window budget is {reading.budget.bytes} bytes, the reachable budget is "
                f"{reading.reachable.bytes} bytes, {pinned} bytes are job-pinned, and the next member needs {pool[0].cost} bytes")
        if need > reading.reachable.bytes:
            return [], 0, (f"the next member needs {need} bytes; claimed jobs keep {pinned} bytes of originals on the "
                           "volume, which free up when their jobs end")
        return [], 0, "admission would grow the cell for the next member"

    async def create(self, taken: list[Member], cost: int) -> None:
        window = SentWindow([member.path for member in taken], f"window-{uuid.uuid4().hex[:8]}", import_id=str(uuid.uuid4()), cost=cost)
        self.series.windows.append(window)
        self.series.write(self.record)
        await self.send(window)

    async def send(self, window: SentWindow) -> None:
        """Send one window; a window that the Job refuses for space is dropped, so the loop plans it again smaller."""
        kept, gone = [], []
        for path in window.paths:
            member = next(m for m in self.series.members if m.path == path)
            fresh = _kept_now(self.source, member)
            if fresh is None or (fresh.size, fresh.mtime_ns) != (member.size, member.mtime_ns):
                gone.append(path)
            else:
                kept.append(fresh)
        checkpoint = self.record.parent / window.dir
        if gone:
            if not (checkpoint / "checkpoint.json").exists():
                window.renew()  # The member set differs from any earlier attempt's declaration.
            # The record is the partition: a member that changed is not sent and not planned again, and the person keeps it.
            window.paths = [item.rel for item in kept]
            self.series.skipped.extend(gone)
            self.progress.say(f"Not sent, because the source file changed or vanished since the series began ({len(gone):,}); "
                              "keep the source: " + _listed(gone))
            if not kept:
                self.series.windows.remove(window)
                self.series.write(self.record)
                return
            self.series.write(self.record)
        self.progress.say(f"Sending window {self.series.windows.index(window) + 1}: {len(kept):,} files, {human_bytes(sum(i.size for i in kept))}.")
        try:
            result = await import_source(
                self.source, self.config, checkpoint, api=self.api, uploads=self.uploads, poll_seconds=self.poll,
                progress=self.progress, window=Window(import_selection.Selection(kept=kept), self.series.series_file.encode(), window.import_id),
                include_hidden=self.series.options["include_hidden"], use_gitignore=self.series.options["use_gitignore"])
        except CloudImportError as error:
            # The Job refuses a window for lack of space with this code: the import ends `failed` with it.
            if error.code == STORAGE_ALLOWANCE_EXCEEDED:
                self.series.windows.remove(window)
                self.series.write(self.record)
                self.refused = window.cost
                self.progress.say("The Job refused the window for space; planning it again from the current budget.")
                return
            # An attempt that ended and removed its checkpoint is dead on Cloud, which replays its ID; the rerun uses a new one.
            ended = not error.resume and not (checkpoint / "checkpoint.json").exists()
            if ended:
                window.renew()
                self.series.write(self.record)
            raise Stopped(error.phase, error.code, f"{error.message}. {not_sent_notice(self.series.not_landed())}",
                          error.import_id, resume=error.resume or ended) from None
        if window.import_id != result["import_id"]:  # The import resumed from a checkpoint that holds its own ID.
            window.attempts = [import_id for import_id in window.ids() if import_id != result["import_id"]]
            window.import_id = result["import_id"]
        window.state = "landed"
        self.series.write(self.record)
        self.refused = None
        await self.settle(window)

    async def settle(self, window: SentWindow) -> None:
        """A landed window of a resend clears its members from the old residual records, once."""
        if self.on_landed is not None and window.state == "landed":
            await self.on_landed(window)
        window.state = "settled"
        self.series.write(self.record)

    # -- the series

    async def drive(self) -> dict:
        reading = await self.read()
        await self.reconcile(reading)
        since = self.clock()
        while pool := self.unplanned():
            reading = await self.read()
            reasons = reading.unknown_reasons()
            if reasons:
                await self.wait(since, "the cell reports an unknown figure: " + "; ".join(reasons))
                continue
            taken, cost, why = self.plan(pool, reading, None if self.refused is None else self.refused - 1)
            if not taken:
                await self.wait(since, why)
                continue
            offloadable = sum(member.size for member in taken if member.klass == OFFLOADABLE)
            fits = cost <= reading.budget.bytes
            # The demand records the window's bytes for eviction; a cell that has no census yet also counts one then.
            census = offloadable and reading.allowance.bytes is None
            gate = await self.read(cost if (not fits or census) else None)
            if gate.unknown_reasons():
                await self.wait(since, "the cell reports an unknown figure: " + "; ".join(gate.unknown_reasons()))
                continue
            self.check_allowance(gate, offloadable)
            if cost > gate.budget.bytes:
                if fits:
                    await self.read(cost)
                await self.wait(since, f"the next window needs {cost} bytes and the window budget is {gate.budget.bytes} bytes; "
                                       f"the reachable budget {gate.reachable.bytes} bytes covers it")
                continue
            admitted, unadmitted = self.admission_bytes(gate)
            if unadmitted or (admitted is not None and sum(m.size for m in taken) > admitted):
                await self.wait(since, unadmitted or "admission would grow the cell")
                continue
            self.shown = None
            await self.create(taken, cost)
            since = self.clock()
        return await self.finish()

    def check_allowance(self, reading: Reading, offloadable: int) -> None:
        if not offloadable:
            return
        if reading.allowance.bytes is None:
            raise self.stop("MEDIA_USAGE_UNKNOWN", f"media usage unknown: {reading.allowance.why}")
        if offloadable > reading.allowance.bytes:
            raise self.stop("MEDIA_ALLOWANCE_EXHAUSTED",
                            f"allowance exhausted: the next window holds {offloadable} bytes of media and the cell reports "
                            f"{reading.allowance.bytes} bytes remaining")

    async def reconcile(self, reading: Reading) -> None:
        """Resume from the record: a landed window without a receipt is sent again; an open window resumes."""
        if not self.series.windows:
            return
        since = self.clock()
        while reading.windows_unknown:  # An unknown listing proves nothing: waiting, never sending a landed window again.
            await self.wait(since, f"the cell cannot list its window records: {reading.windows_unknown}")
            reading = await self.read()
        receipts = {window.get("import_id") for window in reading.windows}
        for window in list(self.series.windows):
            received = next((import_id for import_id in window.ids() if import_id in receipts), None)
            if window.state != "open" and received is None:
                self.progress.say(f"The cell has no receipt for window {self.series.windows.index(window) + 1}; sending it again.")
                self.series.windows.remove(window)
            elif received is not None:
                window.attempts = [import_id for import_id in window.ids() if import_id != received]
                window.import_id = received
                if window.state == "open":
                    window.state = "landed"  # The import finished, and the stop came before the record said so.
        self.series.write(self.record)
        for window in list(self.series.windows):
            if window.state == "open":
                await self.send(window)
            elif window.state == "landed":
                await self.settle(window)

    async def finish(self) -> dict:
        final = await self.cell.status()
        keep = Reading.of(final).keep_source
        if self.series.skipped:
            cell_says = keep["message"] if keep and keep.get("keep") and keep.get("message") else "Keep the source folder."
            keep = {"keep": True, "message": f"{cell_says} {len(self.series.skipped):,} files were not sent."}
        if keep is None or keep.get("keep"):
            self.progress.say("The upload is done and filing continues. " + ((keep or {}).get("message") or "Keep your source folder until the cell reports that nothing is left to file."))
        else:
            self.progress.say("Every member is filed and discarded, so you no longer need to keep the source folder.")
        landed = [window for window in self.series.windows if window.state != "open"]
        return {"import_id": landed[-1].import_id if landed else None, "windows": len(landed),
                "import_ids": [window.import_id for window in landed], "keep_source": keep,
                "not_sent": list(self.series.skipped)}


def _kept_now(source: Path, member: Member) -> import_selection.Kept | None:
    """The member as the source holds it now, or None when it is gone or no longer a regular file."""
    try:
        info = os.lstat(source / member.path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return import_selection.Kept(member.path, info.st_size, info.st_mtime_ns, (info.st_dev, info.st_ino))


def _series_file(label: str, members: list[Member]) -> str:
    return json.dumps({"label": label, "paths": [member.path for member in members]}, ensure_ascii=False)


async def _preflight(run: _Run, members: list[Member], reading: Reading) -> None:
    """Refuse before any upload a series that cannot land (D12), and list the files concerned."""
    offloadable = [m for m in members if m.klass == OFFLOADABLE]
    limit = reading.effective.bytes
    too_big = [m for m in offloadable if m.size > limit]
    if too_big:
        raise run.stop("FILE_ABOVE_EFFECTIVE_LIMIT", f"the cell can land a file of at most {limit} bytes now; larger files: " + _listed(
            [f"{m.path} ({m.size} bytes)" for m in too_big]))
    text = sum(m.size for m in members if m.klass == TEXT)
    resident = [m for m in members if m.klass == RESIDENT]
    need = 2 * text + sum(m.size for m in resident)
    if need > reading.reachable.bytes:
        raise run.stop("RESIDENT_BYTES_TOO_LARGE", f"2 x the text bytes plus the resident bytes need {need} bytes and the reachable budget "
                       f"is {reading.reachable.bytes} bytes; resident files: " + _listed([f"{m.path} ({m.size} bytes)" for m in resident]))



def _sorted(members: list[Member]) -> list[Member]:
    return sorted(members, key=lambda member: (_CLASS_ORDER[member.klass], member.path))


def _classify(kept: import_selection.Kept, reading: Reading) -> Member:
    """Classify by the cell's reported rule; the sender's own copy serves only where the cell reports none."""
    inline = reading.inline_text_limit
    if inline is None:
        text = adoption_run.reads_as_text(kept.rel, kept.size)
    else:  # The suffix rule alone; the cell's inline limit replaces the local size limit.
        text = adoption_run.has_text_suffix(kept.rel) and kept.size <= inline
    if reading.largest_offloadable is None:
        return Member(kept.rel, kept.size, kept.mtime_ns, reading.bounds.classify(kept.size, text=text))
    klass = TEXT if text else OFFLOADABLE if kept.size <= reading.largest_offloadable else RESIDENT
    return Member(kept.rel, kept.size, kept.mtime_ns, klass)


async def import_folder(source: Path, config: CloudImportClientConfig, checkpoint: Path, *, cell: Cell, api, uploads=None,
                        include_hidden: bool = False, use_gitignore: bool = True, progress: Progress | None = None,
                        poll_seconds: float = WAIT_POLL_SECONDS, sleep=asyncio.sleep, clock=time.monotonic,
                        resend_residual: bool = False) -> dict:
    """Send a folder as windows, or as one import when the cell reports no window budget. See the module docstring."""
    progress, source, checkpoint = progress or Progress(), source.resolve(), checkpoint.resolve()
    where, held = _Where(WINDOWS), []
    try:
        return await _import_folder(source, config, checkpoint, cell=cell, api=api, uploads=uploads, include_hidden=include_hidden,
                                    use_gitignore=use_gitignore, progress=progress, poll_seconds=poll_seconds, sleep=sleep,
                                    clock=clock, resend=resend_residual, held=held)
    except Stopped:
        raise
    except Exception as error:  # noqa: BLE001 -- provider errors may contain presigned URLs
        failure = error if isinstance(error, CloudImportError) else _coded(where, error)
        if not held:  # No series exists yet, so nothing was sent.
            raise failure from (None if failure is not error else error.__cause__)
        # Once a series exists, every stop says that the members not yet sent are not in Exomem.
        raise Stopped(failure.phase, failure.code, f"{failure.message}. {not_sent_notice(held[0].not_landed())}",
                      failure.import_id, resume=failure.resume) from None


async def _import_folder(source, config, checkpoint, *, cell, api, uploads, include_hidden, use_gitignore, progress,
                         poll_seconds, sleep, clock, resend, held) -> dict:
    # nosemgrep: ep-word-membership -- the series file's own reserved name, fixed by D12.
    if os.path.lexists(source / import_stage.SERIES_FILE):
        raise CloudImportError(PREFLIGHT, "SERIES_FILE_IN_SOURCE", f"the source folder holds a file named {import_stage.SERIES_FILE} "
                               "at its top level, a name that Exomem reserves for a series; rename it and run the command again", resume=False)
    if checkpoint.is_relative_to(source):
        raise CloudImportError(PREFLIGHT, "CHECKPOINT_INSIDE_SOURCE", "the checkpoint folder must be outside the source folder")
    # The record is read before any network call, so a failure on the first call still says what is not in Exomem.
    _ensure_private_directory(checkpoint)
    record = checkpoint / (RESEND_RECORD if resend else SERIES_RECORD)
    series = Series.read(record)
    if series is not None and (series.source, series.api_base_url) != (str(source), config.api_base_url):
        raise CloudImportError(PREFLIGHT, "CHECKPOINT_MISMATCH", "the checkpoint belongs to another source or Cloud endpoint")
    if series is not None:
        held.append(series)
    base = config.api_base_url.rstrip("/") + "/api/exomem/cloud/imports"
    response = await api.request("GET", base)
    response.raise_for_status()
    limits = response.json().get("limits")
    first = Reading.of(await cell.status())
    options = {"include_hidden": include_hidden, "use_gitignore": use_gitignore}
    settings = {"cell": cell, "api": api, "uploads": uploads, "limits": limits, "progress": progress,
                "poll_seconds": poll_seconds, "sleep": sleep, "clock": clock}
    if resend:
        return await _resend(source, config, checkpoint, record, series, first, options, settings, held)
    if first.older_cell:
        if series is not None:  # A series exists: sending the folder as one import would send every landed member again.
            raise stopped(series, "NO_WINDOW_BUDGET", "the cell reports no window budget now (its media store may be off or rolled back), "
                                                       "so the series cannot go on")
        return await _older_cell(source, config, checkpoint, limits, options, **{k: settings[k] for k in ("api", "uploads", "progress", "poll_seconds")})
    if series is None:
        series = await _begin(source, config, record, first, options, settings)
    if series not in held:
        held.append(series)
    run = _Run(series, record, source, config, **settings)
    return await run.drive()


async def _begin(source, config, record, first, options, settings) -> Series:
    """Select, classify and refuse before any upload; then record the series."""
    progress = settings["progress"]
    selection = import_selection.select(source, include_hidden=options["include_hidden"], use_gitignore=options["use_gitignore"])
    for line in selection.report_lines():
        progress.say(line)
    if not selection.kept:
        raise CloudImportError(PREFLIGHT, "NOTHING_TO_IMPORT", "no file is left to import; pass --include-hidden or --no-gitignore to take more")
    # The preflight needs classes, which need the cell's classification inputs; an unknown input waits.
    placeholders = [Member(kept.rel, kept.size, kept.mtime_ns, TEXT) for kept in selection.kept]
    series = Series(str(source), config.api_base_url, str(uuid.uuid4()), "", placeholders, options=options)
    run = _Run(series, record, source, config, **settings)
    reading, since = first, run.clock()
    while reading.bounds is None or reading.reachable.bytes is None or reading.effective.bytes is None:
        await run.wait(since, "the cell reports an unknown figure: " + "; ".join(reading.unknown_reasons() or ["its limits"]))
        reading = Reading.of(await settings["cell"].status())
    members = _sorted([_classify(kept, reading) for kept in selection.kept])
    series.members, series.series_file = members, _series_file(source.name, members)
    await _preflight(run, members, reading)
    offloadable = sum(m.size for m in members if m.klass == OFFLOADABLE)
    if offloadable:
        if reading.allowance.bytes is None:
            # The demand makes the cell count the media usage when none is recorded; it follows the refusals above.
            reading = await run.read(members[0].cost)
        run.check_allowance(reading, offloadable)
    series.write(record)
    return series


async def _older_cell(source, config, checkpoint, limits, options, *, api, uploads, progress, poll_seconds) -> dict:
    """A cell without a window budget: refuse a folder above its known size, else send one import as before."""
    maximum = (limits or {}).get("max_storage_gib")
    if type(maximum) is int:
        selection = import_selection.select(source, include_hidden=options["include_hidden"], use_gitignore=options["use_gitignore"])
        if selection.kept_bytes > maximum * GIB:
            raise CloudImportError(PREFLIGHT, "FOLDER_ABOVE_CELL_SIZE", f"the folder declares {selection.kept_bytes} bytes and the "
                                   f"cell's known size is {maximum * GIB} bytes ({maximum} GiB); nothing is sent", resume=False)
    return await import_source(source, config, checkpoint, api=api, uploads=uploads, poll_seconds=poll_seconds,
                               progress=progress, include_hidden=options["include_hidden"], use_gitignore=options["use_gitignore"])


async def _residual(cell: Cell) -> dict[str, tuple[str, int]]:
    """Every member of the discarded windows' residual records: path -> (import ID of the record, bytes)."""
    found: dict[str, tuple[str, int]] = {}
    after = None
    while True:
        reading = Reading.of(await cell.status(after=after))
        if reading.windows_unknown:
            raise CloudImportError(WINDOWS, "RECEIPTS_UNKNOWN", f"the cell cannot list its window records ({reading.windows_unknown}), so "
                                   "nothing was resent. Keep your source folder, and run the command again later", resume=False)
        for window in reading.windows:
            if window.get("state") == import_stage.WINDOW_DISCARDED:
                for member in window.get("members", []):
                    found[member["path"]] = (window["import_id"], member["bytes"])
        after = reading.next_after
        if after is None:
            return found


async def _resend(source, config, checkpoint, record, series, first, options, settings, held) -> dict:
    """Send the residual of a finished series as a new series that carries the first series' file unchanged."""
    progress = settings["progress"]
    original = Series.read(checkpoint / SERIES_RECORD)
    if original is None:
        raise CloudImportError(PREFLIGHT, "NO_SERIES", "this folder has no series to resend", resume=False)
    if series is None:
        if first.bounds is None:
            raise CloudImportError(PREFLIGHT, "CELL_FIGURES_UNKNOWN", "the cell reports no classification inputs; try again later", resume=False)
        recorded = {member.path: member for member in original.members}
        sent_windows = {window.import_id for window in original.windows}
        found = {path: where for path, where in (await _residual(settings["cell"])).items() if where[0] in sent_windows and path in recorded}
        if not found:
            progress.say("No discarded window lists a member to resend.")
            return {"import_id": None, "windows": 0, "import_ids": [], "keep_source": None}
        members, skipped = [], []
        for path in sorted(found):
            old = recorded[path]
            fresh = _kept_now(source, old)
            if fresh is None or (fresh.size, fresh.mtime_ns) != (old.size, old.mtime_ns):
                skipped.append(path)
            else:
                members.append(_classify(fresh, first))
        if skipped:
            progress.say(f"Not resent, because the source file changed since the series began ({len(skipped):,}): " + _listed(skipped))
        if not members:
            return {"import_id": None, "windows": 0, "import_ids": [], "keep_source": None}
        series = Series(str(source), config.api_base_url, str(uuid.uuid4()), original.series_file, _sorted(members), options=options,
                        clears={member.path: found[member.path][0] for member in members})
        series.write(record)
    cell = settings["cell"]

    async def clear(window: SentWindow) -> None:
        by_record: dict[str, list[str]] = {}
        for path in window.paths:
            by_record.setdefault(series.clears[path], []).append(path)
        for import_id, paths in by_record.items():
            await cell.acknowledge(import_id, paths)

    if series not in held:
        held.append(series)
    run = _Run(series, record, source, config, on_landed=clear, **settings)
    result = await run.drive()
    record.unlink(missing_ok=True)
    return result

