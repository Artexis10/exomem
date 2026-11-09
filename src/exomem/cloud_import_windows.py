"""Cloud import in windows: a folder that exceeds the cell's volume arrives as a series of ordinary imports.

The cell reports every budget in `adoption_studio status` (D12 of add-cloud-media-store); this module never computes
one. It plans a window from the reported reachable budget, reads the budget again right before it creates the
window, and waits or stops as the design says. The series record lives in the checkpoint folder.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx2
from fastmcp import Client
from fastmcp.exceptions import ToolError

from . import adoption_run, import_selection, import_stage, media_capacity
from .cell_init import _ensure_private_directory
from .cli_progress import Progress, human_bytes
from .cloud_import import STORAGE_ALLOWANCE_EXCEEDED
from .cloud_import_client import (
    _STOP_ON_CLOUD,
    CHECKPOINT_RECORD,
    PREFLIGHT,
    REGISTER,
    CloudImportClientConfig,
    CloudImportError,
    Window,
    _coded,
    _Where,
    import_source,
    lands_declared,
    safe_code,
    server_code,
)
from .cloud_import_job import _private_json

WINDOWS = "windows"
SERIES_RECORD, RESEND_RECORD = "series.json", "series-resend.json"
# Each window's own folder in the checkpoint folder: this prefix and hex. The series record keeps the name.
WINDOW_DIR = "window-"
# D3: a demand is a lease that lapses after 15 minutes, and the sender renews it at least every 5.
DEMAND_RENEWAL_SECONDS = 300
WAIT_POLL_SECONDS = 30
# A series that creates no window for this long stops, so a stalled cell never holds the command forever.
WAIT_LIMIT_SECONDS = 7200
LIST_LIMIT = 50
GIB = 1 << 30
# Filing a window to its end gives up on a window whose replies show no movement this many times in a row: no count
# moved, and the page's position (its number, its phase and the files planned) stayed the same.
FILING_STALL_CALLS = 5
# The states of the cell's `file` reply, from the module that builds it.
WORKING, STOPPED, DONE = adoption_run.FILING_STATES
# The CLI's own state for a `file` call that the cell refused with an error; no `file` reply has it.
CALL_ERROR = "error"
# The cell reconciles a `file` call whose outcome it could not report only under the same call, so the CLI repeats that
# call: once after an unknown outcome, and after a wait while the acknowledgement is pending. The codes are the writer
# lease's own; `cli_ops` names them only as keys of its private remediation table, so there is no name to import.
_REPEAT_ONCE = frozenset({"MUTATION_OUTCOME_UNKNOWN", "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"})  # nosemgrep: ep-word-set -- the cell's closed outcome codes.
_REPEAT_AFTER_WAIT = "MUTATION_ACKNOWLEDGEMENT_PENDING"
# The code with which a cell refuses an action that it does not know.
_UNKNOWN_ACTION = "INVALID_MODE"
# The code that the CLI gives a tool error, because the transport carries no code of the cell's own.
_TOOL_ERROR = "TOOL_ERROR"

OFFLOADABLE, RESIDENT, TEXT = media_capacity.OFFLOADABLE, media_capacity.RESIDENT, media_capacity.TEXT
# D12 order: offloadable artifacts, resident artifacts, then text, each in path order.
_CLASS_ORDER = {OFFLOADABLE: 0, RESIDENT: 1, TEXT: 2}


class FilingUnavailable(Exception):
    """The cell does not file in one call (an older cell refuses `file`), so the staged windows wait for an assistant."""


class Cell:
    """What the CLI asks of the cell's Adoption Studio. Tests give a synthetic one."""

    async def status(self, *, series_id: str | None = None, next_window_bytes: int | None = None,
                     after: str | None = None) -> dict:
        raise NotImplementedError

    async def acknowledge(self, import_id: str, paths: list[str]) -> dict:
        raise NotImplementedError

    async def file(self, path: str, *, collection: str | None = None, continuation: str | None = None) -> dict:
        """One `file` call on a staged window; a cell that cannot file in one call raises FilingUnavailable."""
        raise FilingUnavailable


class McpCell(Cell):
    """The cell's tools through the Cloud gateway, signed in with the CLI's OAuth client."""

    def __init__(self, config: CloudImportClientConfig, oauth):
        self._client = Client(config.mcp_url, auth=oauth, timeout=120)
        self._entered = False

    async def __aenter__(self) -> McpCell:
        return self

    async def __aexit__(self, *error) -> None:
        if self._entered:
            self._entered = False
            await self._client.__aexit__(*error)

    async def _call(self, arguments: dict) -> dict:
        if not self._entered:
            await self._client.__aenter__()
            self._entered = True
        result = await self._client.call_tool("adoption_studio", arguments)
        if isinstance(result.structured_content, dict):
            return result.structured_content
        return json.loads(result.content[0].text)

    async def status(self, *, series_id=None, next_window_bytes=None, after=None) -> dict:
        arguments = {"action": "status", "series_id": series_id, "next_window_bytes": next_window_bytes, "after": after}
        return await self._call({key: value for key, value in arguments.items() if value is not None})

    async def acknowledge(self, import_id: str, paths: list[str]) -> dict:
        return await self._call({"action": "acknowledge", "import_id": import_id, "paths": paths})

    async def file(self, path: str, *, collection: str | None = None, continuation: str | None = None) -> dict:
        arguments = {"action": "file", "path": path, "collection": collection, "continuation": continuation}
        try:
            return await self._call({key: value for key, value in arguments.items() if value is not None})
        except ToolError as error:
            # An older cell refuses the unknown action in argument validation, which reaches the client as a tool error.
            if await self._lacks_file():
                raise FilingUnavailable from error
            return {"success": False, "error": {"code": _TOOL_ERROR, "message": str(error)}}

    async def _lacks_file(self) -> bool:
        """Whether the cell's published schema lists the kinds of `import_memory`, and `file` is not one of them.

        A schema that cannot be read, or that lists no kinds, proves nothing, so the caller keeps the error as a stop.
        """
        kinds: set[str] = set()
        try:
            for tool in await self._client.list_tools():
                operation = ((tool.inputSchema or {}).get("properties") or {}).get("operation") or {}
                for variant in (operation.get("oneOf") or operation.get("anyOf") or ()) if tool.name == "import_memory" else ():
                    kind = ((variant if isinstance(variant, dict) else {}).get("properties") or {}).get("kind") or {}
                    kinds.update([kind["const"]] if "const" in kind else kind.get("enum") or ())
        except Exception:  # noqa: BLE001 -- a listing or a schema that cannot be read says nothing about the action
            return False
        return bool(kinds) and "file" not in kinds


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


def _bounded(reason: object) -> str | None:
    """A cell's reason, stripped and of bounded length, or None when it gives none."""
    return (reason.strip()[:400] or None) if isinstance(reason, str) else None


@dataclass(frozen=True)
class FilingReply:
    """A `file` reply, as far as the CLI reads it: where filing stands, the counts, and what only the person can do.

    `state` is one of the cell's `FILING_STATES`, or `CALL_ERROR` when the cell refused the call. `counts` follow
    `FILING_COUNTS`. `position` is the page's position as the cell reports it, compared whole to see movement.
    """

    state: str
    continuation: str | None
    counts: tuple[int, ...]
    position: str
    code: str | None
    reason: str | None
    needs: frozenset[str]

    def count(self, name: str) -> int:
        return self.counts[adoption_run.FILING_COUNTS.index(name)]

    @classmethod
    def refused(cls, code: str | None, reason: object) -> FilingReply:
        return cls(CALL_ERROR, None, (), "", code, _bounded(reason), frozenset())

    @classmethod
    def of(cls, reply: dict) -> FilingReply:
        """Only a refusal of the action itself is an older cell; any other error, or a reply this CLI cannot read, is a stop."""
        # A current cell refuses with `{"error": {...}}` alone; an older cell, and this CLI's tool-error path, add `success`.
        error = reply.get("error")
        if reply.get("success") is False or isinstance(error, dict):
            error = error if isinstance(error, dict) else {}
            code = safe_code(error.get("code"))
            if code == _UNKNOWN_ACTION:
                raise FilingUnavailable
            return cls.refused(code, error.get("message"))
        progress, stop = reply.get("progress"), reply.get("stop")
        if reply.get("filing") not in adoption_run.FILING_STATES or not isinstance(progress, dict) or any(
                type(progress.get(name)) is not int for name in adoption_run.FILING_COUNTS):
            return cls.refused("FILING_REPLY_UNREADABLE", "the reply lacks the state or the counts of a file reply")
        stop = stop if isinstance(stop, dict) else {}
        person = reply.get("person") if isinstance(reply.get("person"), list) else []
        return cls(reply["filing"], reply.get("continuation") if isinstance(reply.get("continuation"), str) else None,
                   tuple(progress[name] for name in adoption_run.FILING_COUNTS),
                   json.dumps(progress.get("page"), sort_keys=True), safe_code(stop.get("code")), _bounded(stop.get("reason")),
                   frozenset(item["need"] for item in person if isinstance(item, dict) and isinstance(item.get("need"), str)))


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


# A window's import identity. Cloud's create replays a known import ID whatever the row's state, refuses a known ID with
# another declaration (IMPORT_CONFLICT), and refuses a new row while another import holds the cell (IMPORT_CONFLICT,
# from the unique index of open imports in Substrate migration 0061). Cloud cannot show a row for an ID when it answers
# 404 IMPORT_NOT_FOUND for it; that answer is per principal, so it does not prove that no row exists. A network error
# or any other answer proves nothing, and the run stops with the notice. A rerun reads the row of each open window
# before it sends the window. So:
# (a) A window keeps its import ID while Cloud cannot show a row for it, while its row is open or done with a receipt,
#     after the client gives up its checkpoint, after any failure that Cloud did not end, and after a member change: the
#     create decides, as Cloud inserts the new declaration under an ID that it never saw.
# (b) A window gets a new import ID only when its own attempt is over (rule c), as a rerun's read of the row or a read of
#     the row after IMPORT_CONFLICT at register finds it, or when the client's wait sees Cloud end the attempt. That wait
#     reads no listing, and that is safe: the run stops, and the next run reads the listing, after that terminal row,
#     before it sends anything. The old ID moves to `attempts`, and a receipt for any of the IDs lands the window. After a
#     renewal at register, the send is retried once, never more.
# (c) An attempt is over when two things hold: its row is `done`, or is `failed`, `expired` or `discarded` and no longer
#     holds the cell; and a known receipt listing, read after the row, holds none of the window's IDs. A record for one
#     of the IDs lands the window instead. Only a listing read after the row is proof: the cell records a receipt before
#     Cloud reports the import done, and the discard of an import that the Job already imported leaves a record. A
#     `done` row with no receipt means that a restore lost the receipt. While the listing is unknown, the run stops, and
#     for a `done` row it names the discard route. A row whose upload never completed (`requested`) still holds the cell
#     until Cloud ends it.
# (d) IMPORT_CONFLICT at register while Cloud cannot show a row for the window's ID means that another import holds the
#     cell: the run stops, and resumes under the same ID once that import ends. The stop names the window's last earlier
#     attempt only when a read of that attempt's row shows that it still holds the cell, which only a pending discard
#     does; the stop then names no cancel, and a failed read only leaves the name out. IMPORT_CONFLICT with the window's
#     own row holding the cell, other than in rule (f), means its earlier attempt with another member set holds the cell:
#     the run stops, and resumes. Cloud accepts a cancel only of a row that holds the cell and is neither cancelled nor
#     being discarded, so only for such a row does that stop name Cloud's cancel route. For a cancelled row, it says that
#     Cloud is ending it, and for a row with a pending discard, that Cloud is discarding it.
# (e) A window's checkpoint is stale when Cloud cannot show a row for the window's ID: a rerun drops it and sends the
#     window afresh under the same ID (rule a), because the checkpoint may hold a copy for a key or upload that no row knows.
# (f) The window's own attempt that is `key_ready` and never completed its upload (`requested`) holds the cell and can
#     leave no receipt. The CLI cancels it through Cloud's cancel route, the one that Home uses, in two cases: IMPORT_CONFLICT
#     at register meets it as an earlier attempt with another member set, or a member change empties the window, so the
#     record is about to forget its ID. The CLI then waits until Cloud ends the attempt, and at register it renews (rule b).
#     It cancels only an ID that the series record holds, and it waits only on its own cancel: a row that someone else
#     cancelled follows rules (c) and (d).
# (g) The window's attempt lands its declared paths, whatever the source holds now, when a rerun resumes a checkpoint
#     that holds the encrypted copy or its completed upload, or when Cloud holds the attempt's whole upload. So the
#     record keeps every declared path, also when a change at the source empties the window. With no copy to resume,
#     nothing is left to send, so the run stops until Cloud ends the import. When a window lands, each declared path that
#     changed or vanished since the series recorded it landed with its content from before the change: the run reports
#     it so, whenever the change came.


@dataclass
class SentWindow:
    paths: list[str]
    dir: str
    state: str = "open"  # open: created or being sent; landed: the cell finished it; settled: the resend cleared its old records
    import_id: str | None = None
    cost: int = 0
    # The import IDs of earlier attempts that ended terminally or declared another member set. A receipt for one lands the window.
    attempts: list[str] = field(default_factory=list)
    # Rule (g): the paths that differed from the series record when the window landed, so they landed with their old content.
    changed: list[str] = field(default_factory=list)

    def ids(self) -> list[str]:
        return [*self.attempts, *([self.import_id] if self.import_id else [])]

    def renew(self) -> None:
        """Give the window a new import ID: Cloud replays a known ID whatever its state, and refuses a changed declaration."""
        if self.import_id:
            self.attempts.append(self.import_id)
        self.import_id = str(uuid.uuid4())

    def land(self, import_id: str) -> None:
        """The cell holds a record for one of the window's IDs: that ID lands the window, and the others stay attempts."""
        self.attempts = [other for other in self.ids() if other != import_id]
        self.import_id = import_id
        if self.state == "open":
            self.state = "landed"


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
        series = cls(raw["source"], raw["api_base_url"], raw["series_id"], raw["series_file"],
                     [Member(**item) for item in raw["members"]], [SentWindow(**item) for item in raw["windows"]],
                     raw["options"], raw.get("clears", {}), raw.get("skipped", []))
        # A format check on the CLI's own field: each removal of a window's folder joins `dir` to the checkpoint folder.
        damaged = [n for n, window in enumerate(series.windows, 1)
                   if not (isinstance(window.dir, str) and re.fullmatch(re.escape(WINDOW_DIR) + "[0-9a-f]+", window.dir))]
        if damaged:
            raise Stopped(PREFLIGHT, "SERIES_RECORD_DAMAGED", (
                f"the series record {path.name} is damaged: window {damaged[0]} does not name its folder as the CLI does, "
                f"{WINDOW_DIR} and hex, so the run stops before it sends or removes anything. Restore the record from a "
                f"backup, or put back the window's folder name. Do not delete the checkpoint folder: a new series sends "
                f"every landed window again. {not_sent_notice(series.not_landed())}"), resume=False)
        return series

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
                 uploads, limits: dict | None, progress: Progress, poll_seconds: float, sleep, clock, on_landed=None,
                 file: bool = False, collection: str | None = None):
        self.series, self.record, self.source, self.config = series, record, source, config
        self.members = {member.path: member for member in series.members}
        self.cell, self.api, self.uploads, self.limits, self.progress = cell, api, uploads, limits, progress
        self.sleep, self.clock, self.on_landed = sleep, clock, on_landed
        # Filing: the CLI files each landed window through the cell's `file`. `unfiled` maps a landed window's import ID to
        # the continuation of its last `file` reply. `marks` keeps a window's last counts and page position, and
        # `stopped` its last stop code.
        self.filing, self.collection = file, collection
        self.unfiled: dict[str, str | None] = {}
        self.marks: dict[str, tuple[tuple[int, ...], str]] = {}
        self.stopped: dict[str, str] = {}
        self.finished_windows: dict[str, FilingReply] = {}
        self.refusal: str | None = None
        # No wait outlasts a demand's renewal interval, so a waiting sender renews its demand in time.
        self.poll = min(poll_seconds, DEMAND_RENEWAL_SECONDS)
        self.shown: str | None = None
        self.refused: int | None = None

    # -- reading and reporting

    def unplanned(self) -> list[Member]:
        """The members that no window holds yet."""
        planned = {path for window in self.series.windows for path in window.paths} | set(self.series.skipped)
        return [member for member in self.series.members if member.path not in planned]

    def unchanged(self, path: str) -> import_selection.Kept | None:
        """The member as the source holds it now, or None when it changed or vanished since the series recorded it."""
        member, fresh = self.members[path], _kept_now(self.source, self.members[path])
        return fresh if fresh is not None and (fresh.size, fresh.mtime_ns) == (member.size, member.mtime_ns) else None

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
        if await self.file_windows():  # Filing moved, so room may have opened: read the budget again before sleeping.
            return
        await self.sleep(self.poll)

    # -- filing

    def _number(self, import_id: str) -> int:
        return 1 + next((n for n, window in enumerate(self.series.windows) if import_id in window.ids()), -1)

    def seed_unfiled(self, reading: Reading) -> None:
        """Windows of this series that the cell holds landed with members left to file: a rerun carries on filing them.

        A window whose filing is done is left out: its members that stay are the person's to review, not more filing.
        """
        mine = {import_id for window in self.series.windows for import_id in window.ids()}
        for record in reading.windows:
            if record.get("import_id") in mine and record.get("state") == import_stage.WINDOW_LANDED \
                    and (record.get("unfiled") or {}).get("count") != 0 and record.get("filing_done") is not True:
                self.unfiled.setdefault(record["import_id"], None)

    async def _file_call(self, import_id: str) -> FilingReply:
        """One `file` call on a landed window, repeated exactly when the cell could not report its outcome, as the cell asks."""
        path, continuation = f"{import_stage.STAGE_DIR}/{import_id}", self.unfiled[import_id]

        async def call() -> FilingReply:
            try:
                reply = await self.cell.file(path, collection=self.collection, continuation=continuation)
            except FilingUnavailable:
                raise
            except Exception as error:  # noqa: BLE001 -- a call that fails on the way stops this window only; it keeps its place
                return FilingReply.refused(_coded(_Where(WINDOWS, import_id), error).code,
                                           "the file call failed before the cell's answer arrived")
            return FilingReply.of(reply)

        reply = await call()
        if reply.state == CALL_ERROR and reply.code in _REPEAT_ONCE:  # nosemgrep: ep-lexical-intent -- `reply` is the cell's parsed envelope; this compares closed protocol states and outcome codes, not text.
            reply = await call()
        waits = 0
        while reply.state == CALL_ERROR and reply.code == _REPEAT_AFTER_WAIT and waits < FILING_STALL_CALLS:  # nosemgrep: ep-lexical-intent -- `reply` is the cell's parsed envelope; this compares closed protocol states and outcome codes, not text.
            await self.sleep(self.poll)
            reply = await call()
            waits += 1
        return reply

    def _say_stop(self, import_id: str, reply: FilingReply) -> None:
        """Say why filing a window stopped, once for each code: the cell's code and reason, and what frees space."""
        code = reply.code or "no code"
        if self.stopped.get(import_id) != code:
            self.progress.say(f"Filing window {self._number(import_id)} stopped ({code})"
                              + (f": {reply.reason}" if reply.reason else ".")
                              + (" Free space on the cell, then run the command again to carry on." if "free_space" in reply.needs else ""))
        self.stopped[import_id] = code

    async def file_windows(self, *, to_the_end: bool = False) -> bool:
        """Call the cell's `file` on each landed window, once while the series waits and until it stops at the end.

        Filing frees room on the cell, so a waiting series files before it sleeps. A stopped window keeps its
        continuation, and the next call carries on from it. Returns whether any window's counts moved.
        """
        moved = False
        for import_id in list(self.unfiled):
            same = 0
            while self.filing and import_id in self.unfiled:
                try:
                    reply = await self._file_call(import_id)
                except FilingUnavailable:
                    self.filing = False
                    self.refusal = ("The cell refused the file call, so it does not file an upload in one call yet. "
                                    "The staged windows wait for your assistant to file them.")
                    self.progress.say(self.refusal)
                    return moved
                if reply.state == CALL_ERROR:  # nosemgrep: ep-lexical-intent -- `reply` is the cell's parsed envelope; this compares closed protocol states and outcome codes, not text.
                    # The cell refused the call, or the call failed on the way: the window keeps its continuation for the
                    # next pass, or for a rerun after this one ends.
                    self._say_stop(import_id, reply)
                    break
                previous = self.marks.get(import_id)
                counted = previous is None or reply.counts != previous[0]
                moved = moved or counted
                same = 0 if counted or reply.position != previous[1] else same + 1
                self.marks[import_id] = (reply.counts, reply.position)
                self.unfiled[import_id] = reply.continuation
                if reply.state == DONE:
                    del self.unfiled[import_id]
                    self.stopped.pop(import_id, None)
                    self.finished_windows[import_id] = reply
                    moved = True
                    skipped = reply.count("skipped_duplicates")
                    duplicates = f"{skipped:,} duplicate{'' if skipped == 1 else 's'} skipped, " if skipped else ""
                    self.progress.say(f"Filed window {self._number(import_id)}: {reply.count('filed'):,} files filed, "
                                      f"{duplicates}{reply.count('failed'):,} failed.")
                elif reply.state == STOPPED:
                    self._say_stop(import_id, reply)
                    if reply.continuation is None:  # Nothing to carry on from: this run leaves the window to the person.
                        del self.unfiled[import_id]
                    break
                else:
                    self.stopped.pop(import_id, None)
                    if not to_the_end:
                        break
                    if same >= FILING_STALL_CALLS:
                        self.stopped[import_id] = "FILING_STALLED"
                        del self.unfiled[import_id]
                        self.progress.say(f"Filing window {self._number(import_id)} made no progress in {same} calls; "
                                          "it stays staged on the cell.")
        return moved

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
        window = SentWindow([member.path for member in taken], f"{WINDOW_DIR}{uuid.uuid4().hex[:8]}", import_id=str(uuid.uuid4()), cost=cost)
        self.series.windows.append(window)
        self.series.write(self.record)
        await self.send(window)

    async def send(self, window: SentWindow) -> None:
        """Send one window; a window that the Job refuses for space is dropped, so the loop plans it again smaller."""
        number, checkpoint = self.series.windows.index(window) + 1, self.record.parent / window.dir
        fresh = {path: self.unchanged(path) for path in window.paths}
        kept, gone = [item for item in fresh.values() if item is not None], [path for path, item in fresh.items() if item is None]
        # Rule (g): a copy that the rerun resumes lands the declared paths, so the window keeps them all.
        copy = bool(gone) and lands_declared(checkpoint)
        row = await self._own_row(window.import_id) if gone and not copy else None
        if row is not None and _uploaded(row):  # Rule (g): only Cloud holds the declared paths, so nothing is left to send.
            message = (f"Cloud import {window.import_id} holds the whole upload of window {number}, with the content from before a "
                       f"change at the source ({len(gone):,} files: {_listed(gone)}), and its state is "
                       f"{safe_code(row['observed_state']) or 'unknown'}. The run continues once the cell receives it or Cloud ends it")
            if _holds_cell(row):  # Cloud refuses to cancel a row that no longer holds the cell.
                message += f". To end it now, {_STOP_ON_CLOUD}; the changed files are then not sent"
            raise self.stop("WINDOW_IMPORTING", message)
        if gone and not copy:  # The window keeps its import ID (rule a); the create decides.
            # The record is the partition: a member that changed is not sent and not planned again, and the person keeps it.
            window.paths = [item.rel for item in kept]
            self.series.skipped.extend(gone)
            self.progress.say(f"Not sent, because the source file changed or vanished since the series began ({len(gone):,}); "
                              "keep the source: " + _listed(gone))
            if not kept:
                if row is not None and _unsent(row):  # Rule (f): settle the attempt before the record forgets its ID.
                    await self._cancel(window.import_id)
                shutil.rmtree(checkpoint, ignore_errors=True)  # The record forgets the window, so no run uses its copy again.
                self.series.windows.remove(window)
                self.series.write(self.record)
                return
            self.series.write(self.record)
        self.progress.say(f"Sending window {number}: {len(window.paths):,} files, "
                          f"{human_bytes(sum(self.members[path].size for path in window.paths))}.")
        renewed, result = False, None
        while True:
            try:
                result = await import_source(
                    self.source, self.config, checkpoint, api=self.api, uploads=self.uploads, poll_seconds=self.poll,
                    progress=self.progress, window=Window(import_selection.Selection(kept=kept), self.series.series_file.encode(), window.import_id),
                    include_hidden=self.series.options["include_hidden"], use_gitignore=self.series.options["use_gitignore"])
                break
            except CloudImportError as error:
                # The Job refuses a window for lack of space with this code: the import ends `failed` with it.
                if error.code == STORAGE_ALLOWANCE_EXCEEDED:
                    self.series.windows.remove(window)
                    self.series.write(self.record)
                    self.refused = window.cost
                    self.progress.say("The Job refused the window for space; planning it again from the current budget.")
                    return
                message, resume = error.message, error.resume
                if error.phase == REGISTER and error.code == "IMPORT_CONFLICT":
                    refused = error.import_id or window.import_id  # The ID that the create declared.
                    row = await self._own_row(refused)
                    if row is not None and not renewed and refused in window.ids() and _unsent(row):
                        row = await self._cancel(refused)  # Rule (f).
                    over = row is not None and await self._over(window, row)
                    if window.state == "landed":  # Rule (c): the cell holds a record for one of the window's IDs.
                        break
                    # Rule (b): one renewal and one retry. A renewed ID has no row, so its conflict reads as rule (d); the flag bounds the loop all the same.
                    if over and not renewed:
                        window.renew()
                        shutil.rmtree(checkpoint, ignore_errors=True)  # Its checkpoint holds the dead ID, and a checkpoint's ID wins over the window's.
                        self.series.write(self.record)
                        renewed = True
                        continue
                    if row is None:  # Rule (d).
                        last = window.attempts[-1] if window.attempts else None
                        try:
                            earlier = await self._own_row(last) if last else None
                        except Exception:  # noqa: BLE001 -- the hint is advice: a failed read leaves it out, and the stop stays.
                            earlier = None
                        if earlier is not None and _holds_cell(earlier):  # An attempt that rule (b) renewed holds the cell only while Cloud discards it.
                            message = (f"another import is open; wait for it to end. The open import is {last}, an earlier "
                                       "attempt of this window that Cloud is still discarding")
                        else:
                            message = "another import is open; cancel it in Home or wait for it to end"
                    else:
                        message = (f"Cloud import {refused} is an earlier attempt of this window with another member set, "
                                   f"and its state is {safe_code(row['observed_state']) or 'unknown'}. The run continues once the cell "
                                   "receives it or Cloud ends it")
                        # The Cloud import protocol fixes `cancelled`: Cloud ends such a row on its own, so another cancel changes nothing.
                        if row["requested_state"] == "cancelled":
                            message += ". Cloud is already ending it after a cancel"
                        elif row["requested_state"] == "discard_requested":  # The protocol's state; Cloud refuses to cancel a row that it discards.
                            message += ". Cloud is discarding it"
                        elif _holds_cell(row):  # Cloud refuses to cancel a row that no longer holds the cell.
                            message += f". To end it now, {_STOP_ON_CLOUD}"
                    resume = True
                elif error.ended:
                    # Rule (b): Cloud reported that this import ended and replays its ID; the rerun uses a new one.
                    window.renew()
                    self.series.write(self.record)
                    resume = True
                elif error.why:
                    # The client gave up its checkpoint, but Cloud's import may be open: the rerun replays the same ID, and Cloud's state decides.
                    message = (f"{error.why}. The checkpoint is removed. Cloud import {error.import_id} stays as it is, and the next run "
                               f"replays it. To stop it, {_STOP_ON_CLOUD}")
                    resume = True
                raise Stopped(error.phase, error.code, f"{message}. {not_sent_notice(self.series.not_landed())}",
                              error.import_id, resume=resume) from None
        if result is not None:  # Else rule (c) landed the window on a record for one of its IDs.
            window.land(result["import_id"])  # The import may resume from a checkpoint that holds its own ID.
        self.series.write(self.record)
        self.refused = None
        await self.settle(window)

    def _row_url(self, import_id: str) -> str:
        return self.config.api_base_url.rstrip("/") + "/api/exomem/cloud/imports/" + import_id

    async def _own_row(self, import_id: str) -> dict | None:
        """Cloud's row for one of the window's import IDs, or None when Cloud cannot show a row for it."""
        try:
            response = await self.api.request("GET", self._row_url(import_id))
            response.raise_for_status()
        except httpx2.HTTPStatusError as error:
            # Only Cloud's own refusal proves that it has no row; a 404 from anything on the way proves nothing.
            if error.response.status_code == 404 and server_code(error.response) == "IMPORT_NOT_FOUND":
                return None
            raise
        return response.json()["import"]

    async def _cancel(self, import_id: str) -> dict | None:
        """Rule (f): cancel the window's own unsent attempt, and return its row once Cloud has ended it."""
        response = await self.api.request("POST", self._row_url(import_id) + "/cancel")
        response.raise_for_status()
        self.progress.say(f"Cancelled Cloud import {import_id}, an earlier attempt of this window that never completed its upload.")
        since = self.clock()
        while (row := await self._own_row(import_id)) is not None and not _ended(row):
            await self.wait(since, f"Cloud is ending import {import_id}, which the CLI cancelled")
        return row

    async def settle(self, window: SentWindow) -> None:
        """A landed window drops its checkpoint, and a landed window of a resend clears its members from the old residual records, once."""
        if window.state == "landed":  # Rule (g), once, whichever way the window landed.
            window.changed = [path for path in window.paths if self.unchanged(path) is None]
            if window.changed:
                self.progress.say(f"Window {self.series.windows.index(window) + 1} landed with the content from before a change at "
                                  f"the source ({len(window.changed):,}); keep the source: " + _listed(window.changed))
        # The client removes the copy only when the window lands through it; a window that lands on the cell's record lands here too.
        shutil.rmtree(self.record.parent / window.dir, ignore_errors=True)
        if self.on_landed is not None and window.state == "landed":
            await self.on_landed(window)
        if self.filing and window.import_id not in self.finished_windows:
            self.unfiled.setdefault(window.import_id, None)
        window.state = "settled"
        self.series.write(self.record)

    # -- the series

    async def resume_active(self) -> None:
        """Resume admitted windows before contacting the cell that their imports keep stopped."""
        for window in list(self.series.windows):
            if window.state != "open":
                continue
            row = await self._own_row(window.import_id)
            # The Cloud import protocol fixes `done`; finished or ended rows still need receipt reconciliation.
            if row is not None and row["observed_state"] != "done" and not _ended(row):
                await self.send(window)

    async def drive(self) -> dict:
        reading = await self.read()
        await self.reconcile(reading)
        if self.filing:
            self.seed_unfiled(reading)
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
        if reading.windows_unknown:
            # The early row read can precede an unknown listing; keep the terminal row's receipt and discard advice.
            for window in list(self.series.windows):
                if window.state == "open":
                    await self.resume(window)
            reading = await self.read()
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
                window.land(received)  # An open window: the import finished, and the stop came before the record said so.
        self.series.write(self.record)
        for window in list(self.series.windows):
            if window.state == "open":
                await self.resume(window)
            if window.state == "open":
                await self.send(window)
            elif window.state == "landed":
                await self.settle(window)

    async def resume(self, window: SentWindow) -> None:
        """Rules (b), (c) and (e) on a rerun: the window's own row decides whether its checkpoint and import ID still hold.

        A record for one of the window's IDs lands the window instead (rule c), and the caller sends nothing.
        """
        checkpoint = self.record.parent / window.dir
        row = await self._own_row(window.import_id)
        if row is None:
            if (checkpoint / CHECKPOINT_RECORD).exists():
                shutil.rmtree(checkpoint)
                self.progress.say(f"Cloud cannot show import {window.import_id} of window {self.series.windows.index(window) + 1}, "
                                  "so its checkpoint is stale; sending the window afresh.")
        elif await self._over(window, row):
            window.renew()
            shutil.rmtree(checkpoint, ignore_errors=True)  # Its checkpoint holds the old ID, and a checkpoint's ID wins.
            self.series.write(self.record)

    async def _over(self, window: SentWindow, row: dict) -> bool:
        """Rule (c): whether the window's attempt with this row is over, so that a new import ID cannot land it twice.

        Only a known receipt listing, read after the row, proves a `done` or ended row over: the cell records a receipt
        before Cloud reports the import done, and the discard of an import that the Job already imported leaves a record.
        A record for one of the window's IDs lands the window instead, and the caller sends nothing. An unknown listing
        proves nothing, and the run stops.
        """
        # The Cloud import protocol fixes `done`: Cloud reports it only once the cell holds the window's receipt.
        done = row["observed_state"] == "done"
        if not done and not _ended(row):
            return False
        reading = await self.read()
        if reading.windows_unknown:
            route = (". If a restore of the cell lost that window, ask your assistant to call transfer_memory with action "
                     f"import_discard, import_id {window.import_id} and confirm=true; the next run then sends the window again")
            raise self.stop("RECEIPTS_UNKNOWN", (
                f"Cloud {'finished' if done else 'ended'} import {window.import_id}, and the cell cannot list its window records "
                f"({reading.windows_unknown}), so the CLI cannot tell whether the cell holds that window. Run the command "
                f"again later{route if done else ''}"))
        receipts = {item.get("import_id") for item in reading.windows}
        received = next((import_id for import_id in window.ids() if import_id in receipts), None)
        if received is not None:
            window.land(received)
            return False
        if done:
            self.progress.say(f"The cell has no receipt for window {self.series.windows.index(window) + 1}, whose import Cloud "
                              "finished; sending it again.")
        return True

    async def finish(self) -> dict:
        await self.file_windows(to_the_end=True)
        final = await self.cell.status()
        keep = Reading.of(final).keep_source
        landed = [window for window in self.series.windows if window.state != "open"]
        before = [path for window in landed for path in window.changed]
        if self.series.skipped or before:
            # Only the source holds the current content of a member that was not sent or that landed from before a change.
            cell_says = keep["message"] if keep and keep.get("keep") and keep.get("message") else "Keep the source folder."
            notes = [f"{len(self.series.skipped):,} files were not sent."] if self.series.skipped else []
            notes += [f"{len(before):,} files were sent with their content from before a change at the source."] if before else []
            keep = {"keep": True, "message": " ".join([cell_says, *notes])}
        waiting = sorted(set(self.unfiled) | set(self.stopped))
        if keep is None or keep.get("keep"):
            # The cell did not take every window to the end of filing when this run filed, so it says what is left.
            lead = "The upload is done." if self.filing and not waiting else "The upload is done and filing continues."
            self.progress.say(lead + " " + ((keep or {}).get("message") or "Keep your source folder until the cell reports that nothing is left to file."))
        else:
            self.progress.say("Every member is filed" + ("" if self.filing else " and discarded") + ", so you no longer need to keep the source folder.")
        result = {"import_id": landed[-1].import_id if landed else None, "windows": len(landed),
                  "import_ids": [window.import_id for window in landed], "keep_source": keep,
                  "not_sent": list(self.series.skipped), "sent_before_change": before}
        if self.filing or self.refusal:
            finished = self.finished_windows.values()
            result["filing"] = {"filed": sum(reply.count("filed") for reply in finished),
                                "skipped_duplicates": sum(reply.count("skipped_duplicates") for reply in finished),
                                "failed": sum(reply.count("failed") for reply in finished),
                                "windows_done": len(self.finished_windows), "windows_waiting": len(waiting)}
        return result


def _holds_cell(row: dict) -> bool:
    """True while the row counts in Substrate's one-open-import-per-cell index; a pending discard keeps a row open."""
    # nosemgrep: ep-word-membership -- The Cloud import protocol fixes the closed states that the index (migration 0061) excludes.
    closed = row["observed_state"] in ("done", "failed", "expired", "discarded")
    return not closed or (row["requested_state"] == "discard_requested" and row["observed_state"] != "discarded")


def _ended(row: dict) -> bool:
    """Rule (c): Cloud ended the attempt short of `done`, and its row no longer holds the cell."""
    return row["observed_state"] != "done" and not _holds_cell(row)


def _unsent(row: dict) -> bool:
    """Rule (f): Cloud holds the attempt, and its upload never completed, so no receipt can come from it."""
    # The Cloud import protocol fixes these states: Cloud keeps `requested` and `key_ready` until the upload completes.
    return row["requested_state"] == "requested" and row["observed_state"] == "key_ready"


def _uploaded(row: dict) -> bool:
    """Rule (g): Cloud holds the attempt's whole upload and has not ended it, so it imports the declared member set."""
    # The Cloud import protocol fixes `uploaded`: Cloud sets it when the upload completes.
    return row["requested_state"] == "uploaded" and not _ended(row)


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
    """Classify by the one rule with the inputs that the cell reports; the sender's own inputs serve only where it reports none."""
    inline = reading.inline_text_limit
    if inline is None:
        text = adoption_run.reads_as_text(kept.rel, kept.size)
    else:  # The suffix rule alone; the cell's inline limit replaces the local size limit.
        text = adoption_run.has_text_suffix(kept.rel) and kept.size <= inline
    largest = reading.bounds.largest_offloadable() if reading.largest_offloadable is None else reading.largest_offloadable
    return Member(kept.rel, kept.size, kept.mtime_ns, media_capacity.classify(kept.size, largest, text=text))


async def import_folder(source: Path, config: CloudImportClientConfig, checkpoint: Path, *, cell: Cell, api, uploads=None,
                        include_hidden: bool = False, use_gitignore: bool = True, progress: Progress | None = None,
                        poll_seconds: float = WAIT_POLL_SECONDS, sleep=asyncio.sleep, clock=time.monotonic,
                        resend_residual: bool = False, file: bool = False, collection: str | None = None) -> dict:
    """Send a folder as windows, or as one import when the cell reports no window budget. See the module docstring.

    `file` also files each landed window through the cell's `file`, while the series waits and at its end; a cell that
    refuses `file` leaves the windows staged, as before. The CLI turns it on by default. It never discards.
    """
    progress, source, checkpoint = progress or Progress(), source.resolve(), checkpoint.resolve()
    where, held = _Where(WINDOWS), []
    try:
        return await _import_folder(source, config, checkpoint, cell=cell, api=api, uploads=uploads, include_hidden=include_hidden,
                                    use_gitignore=use_gitignore, progress=progress, poll_seconds=poll_seconds, sleep=sleep,
                                    clock=clock, resend=resend_residual, held=held, file=file, collection=collection)
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
                         poll_seconds, sleep, clock, resend, held, file, collection) -> dict:
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
    options = {"include_hidden": include_hidden, "use_gitignore": use_gitignore}
    settings = {"cell": cell, "api": api, "uploads": uploads, "limits": limits, "progress": progress,
                "poll_seconds": poll_seconds, "sleep": sleep, "clock": clock, "file": file, "collection": collection}
    if resend:
        return await _resend(source, config, checkpoint, record, series, options, settings, held)
    run = _Run(series, record, source, config, **settings) if series is not None else None
    if run is not None:
        await run.resume_active()
    first = Reading.of(await cell.status())
    if first.older_cell:
        if series is not None:  # A series exists: sending the folder as one import would send every landed member again.
            raise stopped(series, "NO_WINDOW_BUDGET", "the cell reports no window budget now (its media store may be off or rolled back), "
                                                       "so the series cannot go on")
        return await _older_cell(source, config, checkpoint, limits, options, **{k: settings[k] for k in ("api", "uploads", "progress", "poll_seconds")})
    if series is None:
        series = await _begin(source, config, record, first, options, settings)
    if series not in held:
        held.append(series)
    run = run or _Run(series, record, source, config, **settings)
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


async def _resend(source, config, checkpoint, record, series, options, settings, held) -> dict:
    """Send the residual of a finished series as a new series that carries the first series' file unchanged."""
    progress = settings["progress"]
    original = Series.read(checkpoint / SERIES_RECORD)
    if original is None:
        raise CloudImportError(PREFLIGHT, "NO_SERIES", "this folder has no series to resend", resume=False)
    if series is None:
        first = Reading.of(await settings["cell"].status())
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
    await run.resume_active()
    result = await run.drive()
    record.unlink(missing_ok=True)
    return result

