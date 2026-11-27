"""The import filer: one thread of the serving runtime that files each registered import stage to the end.

`adoption_studio` `file` files a whole stage when its caller repeats the call with each `continuation`. A
Cloud import stops the cell while it stages, so nobody is present when it ends. The filer is that caller: it
repeats `file` through the dispatcher, as the owner who asked, until the stage is `done` or stops.

A stage is registered by its series file (`"filing": "cell"`, found by the scan at start) or by any `file` call
that names it. Each registered stage has one private record, `<state>/import-filing/<import_id>.json`, which
holds both the filer's own state and what the filing view answers.

Exactly once: before each call the record stores the call's number and continuation, and the mutation
identity is `import-file:<import_id>:<number>`. After a crash the filer repeats that exact call under that
identity, and the idempotency store and `file`'s own reconciliation make it land once. The filer's calls and a
foreground `file` call take turns at the vault's mutation boundary; the record lock below is held only to read
and write a record, never across a call.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import import_stage, mutation_lock, private_state, state_paths, vault

log = logging.getLogger(__name__)

RECORDS = "import-filing"
RECORD_VERSION = 1
# The closed states of a filing record and of the filing view; `unknown` is a stage with no record.
WORKING, PARKED, DONE, DISCARDED = "working", "parked", "done", "discarded"  # nosemgrep: ep-word-set -- the closed states of the filing record.
UNKNOWN = "unknown"
_STATES = (WORKING, PARKED, DONE, DISCARDED)  # nosemgrep: ep-word-set -- the closed states of the filing record.
# The closed `filing` values of a `file` reply.
_REPLY_WORKING, _REPLY_STOPPED, _REPLY_DONE = "working", "stopped", "done"  # nosemgrep: ep-word-set -- the closed `filing` values of a `file` reply.
#: A vault with no Knowledge Base yet: `file` refuses it, and the way on is one `start` that creates it.
_KB_NOT_INITIALIZED = "KB_NOT_INITIALIZED"
#: The login behind the stage's registration ended; only a new `file` call from a valid login resumes it.
STOP_LOGIN_ENDED = "IMPORT_FILING_LOGIN_ENDED"
#: A `file` call with `wait` false came from a login that the cell cannot replay (a delegated authorization session).
STOP_NOT_REPLAYABLE = "IMPORT_FILING_LOGIN_NOT_REPLAYABLE"
#: The `file` reply could not be read as a reply.
STOP_REPLY_UNREADABLE = "IMPORT_FILING_REPLY_UNREADABLE"
#: The stage's `working` replies showed no movement for `adoption_run.FILING_STALL_CALLS` calls in a row; the filer waits and tries again.
STOP_STALLED = "IMPORT_FILING_STALLED"
#: What the filing view says of a `working` stage that an agent files, while the cell stands aside from it.
AGENT_FILING_NOTE = "An assistant is filing this import now. If it stops, Evermatter carries on by itself."
#: The mutation protocol's final answer for an identity: its outcome will never be known, so the next call has a new one.
_OUTCOME_UNKNOWN = "MUTATION_OUTCOME_UNKNOWN"
#: A continuation that `file` no longer knows: the next call names the stage alone and carries on its newest scan.
_STALE_CONTINUATION = "INVALID_CONTINUATION"
#: The cell is not warm yet: a wait, not a try.
_WARMING = "MUTATION_WARMING"

#: The cell refuses a write while it serves read-only: a wait that ends when the cell leaves that mode.
_READ_ONLY = "CLOUD_CELL_READ_ONLY"

#: What the filing view says a parked stage waits for: free space, a login, the cell's read-only mode to end, or support.
NEED_FREE_SPACE = "free_space"
NEED_LOGIN = "login"
NEED_READ_ONLY = "read_only"
NEED_SUPPORT = "support"
#: The need that a `file` reply's `person` entry names for a stop on free space (`adoption_run._filing_reply`).
_PERSON_FREE_SPACE = "free_space"
#: The most left-out members that a `done` record lists; the counts by reason cover them all.
LEFT_OUT_ITEMS = 100

MAX_TRIES_PER_HOUR = 5
FIRST_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
#: A stage that stopped for free space asks again this often; a `file` call or a restart asks sooner.
SPACE_CHECK_SECONDS = 3600.0
#: A stage that holds transcript pages for recordings that another window files asks again this often.
HOLD_CHECK_SECONDS = 120.0
#: A stage that waits on a read-only cell asks again this often; a `file` call or a restart asks sooner.
READ_ONLY_CHECK_SECONDS = 60.0
#: A stage whose calls move nothing waits this long, doubling at each stall to the cap, and then calls once more.
STALL_FIRST_BACKOFF_SECONDS = 60.0
STALL_MAX_BACKOFF_SECONDS = 900.0
#: After an agent's `file` call on a stage returns, the filer leaves the stage alone for this long. A call ends inside
#: one limit of a hold, and the agent reads the reply and calls again inside a second one; a stage that no agent has
#: touched for longer has nobody driving it, which is the stage the filer is for.
AGENT_QUIET_SECONDS = 2 * mutation_lock.DEFAULT_LONG_HOLDER_SECONDS
#: What a fault that no call explains waits before the filer looks at the stage again.
_FAULT_BACKOFF_SECONDS = 60.0
_IDLE_WAIT_SECONDS = 3600.0
_RATE_CALLS = 10
_REQUEST_NAMESPACE = uuid.UUID("0c6e8b1a-4d2f-4a57-9d83-5b7f2e91c4a6")
_RECORD_LOCK = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- the filing record


def _record_path(vault_root: Path, import_id: str) -> Path:
    return state_paths.vault_state_dir(vault_root) / RECORDS / f"{import_id}.json"


def read_record(vault_root: Path, import_id: str) -> dict | None:
    """The stage's filing record, or None when it has none or cannot be read: one small file, whatever the stage holds."""
    try:
        document = json.loads(_record_path(vault_root, import_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(document, dict) and document.get("import_id") == import_id and document.get("state") in _STATES
    return document if ok else None


def _write_record(vault_root: Path, record: dict) -> None:
    store = private_state.store(vault_root, RECORDS)
    private_state.write_record(store, record["import_id"], record, temp_suffix=".tmp", durable=True)
    private_state.sync_directory(store)


def _update(vault_root: Path, import_id: str, change: Callable[[dict], bool | None]) -> dict | None:
    """Read the record, let `change` edit it in place and write it durably; None when there is no record.

    A `change` that returns False leaves the record as it is: nothing is written and `as_of` stays.
    """
    with _RECORD_LOCK:
        record = read_record(vault_root, import_id)
        if record is None:
            return None
        if change(record) is False:
            return record
        record["as_of"] = _now()
        _write_record(vault_root, record)
        return record


def _fresh_record(
    import_id: str, principal: dict, surface: dict | None, collection: str | None, *, total: int | None, baseline: bool,
) -> dict:
    # A baseline sample is a registration with no work behind it: the first call's files then count toward `rate`.
    return {
        "version": RECORD_VERSION, "import_id": import_id, "state": WORKING, "principal": principal,
        "surface": surface, "collection": collection, "seq": 0, "call": None, "next_action": None, "continuation": None,
        "total": total, "progress": None, "left_out": None, "stop": None, "needs": [], "person": [],
        "mark": None, "unmoved": 0, "stalls": 0, "calls": [[time.time(), 0]] if baseline else [], "busy": [], "as_of": _now(),
    }


def _handled(counts: dict[str, int]) -> int:
    """The files a reply's counts say were handled: filed, or skipped because the bytes were already stored."""
    return counts["filed"] + counts["skipped_duplicates"]


def _add_rate_sample(record: dict, counts: dict[str, int]) -> None:
    """Add the sample that a finished call's counts give `rate`, keeping the newest `_RATE_CALLS`."""
    record["calls"] = [*record["calls"], [time.time(), _handled(counts)]][-_RATE_CALLS:]


def _rate(samples: list) -> dict | None:
    """Files handled per minute over the newest samples; None until a window of two covers filed work.

    A sample is the files handled at a time: one when a stage is registered with nothing filed, and one after each
    finished call. A window that covers no filed work has no rate, so a stage never shows 0.0 for work it did.
    """
    if len(samples) < 2:
        return None
    (first_at, first_handled), (last_at, last_handled) = samples[0], samples[-1]
    if last_at <= first_at or last_handled <= first_handled:
        return None
    per_minute = (last_handled - first_handled) / ((last_at - first_at) / 60)
    return {"files_per_minute": round(per_minute, 1), "over_calls": len(samples)}


def _progress(record: dict) -> dict | None:
    """The contract's counts: what the last call reported, the manifest's total and what is neither filed nor left out.

    On `done` each failed member is also one of the left-out members, so `remaining` takes it away once; before
    `done` nothing is left out yet, and a failed member is taken away as failed.
    """
    counts = record.get("progress")
    if not isinstance(counts, dict):
        return None
    total = record.get("total")
    left_out = (record.get("left_out") or {}).get("count", 0)
    # Before `done` an item that the entry's rule left out (`set_aside`) is already settled, as a failed one is: it will never file.
    settled = left_out if record["state"] == DONE else counts["failed"] + (record.get("set_aside") or 0)
    remaining = None if total is None else max(0, total - counts["filed"] - counts["skipped_duplicates"] - settled)
    return {
        "total": total, "filed": counts["filed"], "skipped_duplicates": counts["skipped_duplicates"],
        "failed": counts["failed"], "left_out": left_out, "remaining": remaining,
    }


def read_view(vault_root: Path, import_id: str) -> dict:
    """What `adoption_studio status` answers with `view="filing"`: the stage's record, or `unknown` when it has none.

    One record read and one look at the filer's memory. It opens no run, inventory or outcome file, so a client may
    poll it at any rate. A stage with no record is `unknown` and reports no counts, never zero ones. The cell states
    no time left: a caller that derives one from `rate` says "at the measured rate".
    """
    record = read_record(vault_root, import_id)
    if record is None:
        return {"state": UNKNOWN, "progress": None, "stop": None, "needs": [], "person": []}
    view: dict[str, Any] = {
        "state": record["state"], "as_of": record.get("as_of"), "progress": _progress(record),
        "stop": record.get("stop"), "needs": record.get("needs") or [], "person": record.get("person") or [],
    }
    if record["state"] == DONE and record.get("left_out") is not None:
        view["left_out"] = {key: record["left_out"][key] for key in ("by_reason", "items", "truncated")}
    if (rate := _rate(record.get("calls") or [])) is not None:
        view["rate"] = rate
    # The filer holds this in memory, and it ends by itself with the quiet window: a record would claim it for good.
    if record["state"] == WORKING and (filer := filer_for(vault_root)) is not None and filer.agent_active(import_id):
        view["note"] = AGENT_FILING_NOTE
    return view


def _counts(progress: object) -> dict[str, int] | None:
    """The three counts of a `file` reply's `progress` that the view keeps; None when the reply has not all three."""
    if not isinstance(progress, dict):
        return None
    counts = {key: progress.get(key) for key in ("filed", "skipped_duplicates", "failed")}
    return counts if all(isinstance(value, int) and not isinstance(value, bool) for value in counts.values()) else None


def _stage_total(vault_root: Path, import_id: str) -> int | None:
    """The members that the stage's receipt or record lists, but the series file, and what filing has derived from app exports; None when it lists none.

    A derived file, an item that could not be read or was left out, and an export read in part each count as one member,
    so the total grows while an export is being read. The export file is a member that stays unfiled until it is read, so
    `remaining` is not zero before then.
    """
    from . import export_expansion

    stage = import_stage.read(vault_root, import_id, derived=False)  # the counts come from the expansion state, not its rows
    if stage is None:
        return None
    reading = export_expansion.read(vault_root, import_id)
    return (
        sum(1 for path in stage.members if path != import_stage.SERIES_FILE) + reading.files
        + reading.failed + reading.skipped + reading.partial)


def _set_aside(vault_root: Path, import_id: str) -> int:
    """The items of the stage's app exports that the registry entry's own rule left out, from the expansion state."""
    from . import export_expansion

    return export_expansion.read(vault_root, import_id).skipped


def _left_out(vault_root: Path, import_id: str) -> dict:
    """The members that adoption did not file, once, for a stage that just finished: counts by reason and a bounded list.

    A `done` stage is never filed again unless a caller files it again, so this is read once and stored; the
    view then answers without opening a run. Raises when the runs cannot be read, so the stage is not `done` yet.
    """
    from . import adoption_run

    members = sorted(adoption_run.unfiled_members(vault_root, import_id), key=lambda member: member["path"])
    by_reason: dict[str, int] = {}
    for member in members:
        by_reason[member["code"]] = by_reason.get(member["code"], 0) + 1
    return {
        "count": len(members), "by_reason": dict(sorted(by_reason.items())),
        "items": [{"path": member["path"], "code": member["code"]} for member in members[:LEFT_OUT_ITEMS]],
        "truncated": len(members) > LEFT_OUT_ITEMS,
    }


def _held(stop: dict | None) -> bool:
    """Whether a stop is a hold on transcript pages that wait for their recordings, which no person can lift."""
    from . import kept_speech

    return isinstance(stop, dict) and stop.get("code") in kept_speech.HOLD_CODES


def _stopped_needs(vault_root: Path, reply: dict, person: list) -> list[dict]:
    """What a stop asks of the person: nothing for a hold, free space (with the bytes the run records) or, for any other stop, support."""
    if _held(reply.get("stop")):
        return []  # another window's filing, or the index, lifts it
    if not any(isinstance(entry, dict) and entry.get("need") == _PERSON_FREE_SPACE for entry in person):
        return [{"need": NEED_SUPPORT}]
    from . import adoption_run

    try:
        stop = adoption_run.AdoptionRunStore(vault_root).load(reply.get("run_id")).get("apply_stop") or {}
        needed = stop["chunk_bytes"] + stop["reserve_bytes"]
    except (adoption_run.AdoptionRunError, KeyError, TypeError):
        needed = None
    return [{"need": NEED_FREE_SPACE, "bytes": needed if isinstance(needed, int) else None}]


# --------------------------------------------------------------------------- the filer


class Filer:
    """The one thread that files one vault's registered import stages, a call at a time."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Monotonic time at which each listed stage is due; `inf` while a call on it is in flight.
        self._due: dict[str, float] = {}
        #: By stage: the agent `file` calls in flight, and the monotonic time before which the filer leaves it alone.
        self._agent_calls: dict[str, int] = {}
        self._agent_quiet_until: dict[str, float] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def alive(self) -> bool:
        """Whether the thread still runs, a stopped filer's call in flight included."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        thread = threading.Thread(
            target=contextvars.Context().run, args=(self._loop,), name="exomem-import-filer", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float) -> None:
        self._stop.set()
        self.wake()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def register(
        self, import_id: str, *, principal: dict, surface: dict | None, collection: str | None = None,
        fresh_stage: bool = False,
    ) -> bool:
        """List a stage, or list it again with the principal of the newest caller; False when it ended `discarded`.

        The stored principal is the owner who asked for the filing. A principal that `write_intake.principal_record`
        refuses (a delegated session) is never passed here, so such a caller's stage stays foreground-only.
        `fresh_stage` says nothing has filed the stage yet, so the record starts with a baseline for `rate`.
        """
        with _RECORD_LOCK:
            record = read_record(self.vault_root, import_id)
            if record is not None and record["state"] == DISCARDED:
                return False
            if record is None:
                total = _stage_total(self.vault_root, import_id)
                record = _fresh_record(import_id, principal, surface, collection, total=total, baseline=fresh_stage)
            else:
                record.update(
                    principal=principal, surface=surface, state=WORKING, stop=None, needs=[], person=[], busy=[],
                    left_out=None)
                record["collection"] = collection or record.get("collection")
                if record.get("total") is None:
                    record["total"] = _stage_total(self.vault_root, import_id)
            record["as_of"] = _now()
            _write_record(self.vault_root, record)
        with self._condition:
            self._due[import_id] = 0.0
            self._condition.notify_all()
        return True

    # ----- an agent that files the stage

    def agent_began(self, import_id: str) -> None:
        with self._condition:
            self._agent_calls[import_id] = self._agent_calls.get(import_id, 0) + 1

    def agent_ended(self, import_id: str, *, working: bool, counts: dict[str, int] | None = None) -> None:
        """An agent's `file` call on the stage returned. A stage still working stays the agent's for the quiet window.

        `counts` are the reply's: they go into the record before the window starts, so the view's counts and `as_of`
        move with the agent's work and are no staler than a filer call's. A reply that is not `working` (the stage is
        done, or stopped) leaves the agent nothing to call again, so the filer is woken to record that state at once
        instead of after the window.
        """
        if counts is not None:
            self._record_agent_counts(import_id, counts)
        with self._condition:
            left = self._agent_calls.get(import_id, 1) - 1
            if left > 0:
                self._agent_calls[import_id] = left
            else:
                self._agent_calls.pop(import_id, None)
            if working:
                self._agent_quiet_until[import_id] = time.monotonic() + AGENT_QUIET_SECONDS
            else:
                self._agent_quiet_until.pop(import_id, None)
                if self._due.get(import_id, math.inf) != math.inf:
                    self._due[import_id] = 0.0
                    self._condition.notify_all()

    def _record_agent_counts(self, import_id: str, counts: dict[str, int]) -> None:
        """Store what an agent's call filed, as a filer call's reply would; never raises, the agent's call is done."""
        def change(record: dict) -> bool | None:
            if record["state"] in (DONE, DISCARDED):
                return False
            earlier = _counts(record.get("progress"))
            if earlier is not None and _handled(counts) < _handled(earlier):
                # A repeated agent call returns its stored reply, which is behind the record; if the handled count
                # genuinely fell, the view would keep the higher counts while the agent files.
                return False
            record["progress"] = counts
            _add_rate_sample(record, counts)
            return None
        try:
            _update(self.vault_root, import_id, change)
        except Exception:  # noqa: BLE001 - a store fault must not fail the agent's own file call
            log.warning("import filer could not record the counts of an agent call on %s", import_id, exc_info=True)

    def agent_left(self, import_id: str) -> None:
        """The agent hands the stage to the cell (`file` with `wait` false): the quiet window ends, so the filer may call at once."""
        with self._condition:
            self._agent_quiet_until.pop(import_id, None)

    def _agent_wait(self, import_id: str) -> float:
        """Seconds that the filer leaves the stage alone: the quiet window while an agent files it, 0.0 otherwise."""
        with self._condition:
            if self._agent_calls.get(import_id):
                return AGENT_QUIET_SECONDS
            left = self._agent_quiet_until.get(import_id, 0.0) - time.monotonic()
            if left <= 0:
                self._agent_quiet_until.pop(import_id, None)
                return 0.0
            return left

    def agent_active(self, import_id: str) -> bool:
        """Whether an agent files the stage now: a call is in flight, or the quiet window after one has not ended."""
        return self._agent_wait(import_id) > 0.0

    # ----- the loop

    def _loop(self) -> None:
        try:
            self.scan()
        except Exception:  # noqa: BLE001 - the filer outlives a store fault; the next start scans again
            log.warning("import filer could not scan its stages at start", exc_info=True)
        while not self._stop.is_set():
            picked = self._pick()
            if picked is None:
                continue
            try:
                delay = self.run_once(picked)
            except Exception:  # noqa: BLE001 - a fault in one stage must not stop the others
                log.warning("import filer call on %s failed", picked, exc_info=True)
                delay = _FAULT_BACKOFF_SECONDS
            with self._condition:
                if self._due.get(picked) == math.inf:  # a registration during the call keeps its own due time
                    if delay is None:
                        del self._due[picked]
                    else:
                        self._due[picked] = time.monotonic() + delay

    def _pick(self) -> str | None:
        """The stage that is due now, or None after waiting for the next one."""
        with self._condition:
            if self._stop.is_set():
                return None
            now = time.monotonic()
            due = [(at, import_id) for import_id, at in self._due.items() if at != math.inf]
            if due and (soonest := min(due))[0] <= now:
                self._due[soonest[1]] = math.inf
                return soonest[1]
            self._condition.wait(min((at - now for at, _ in due), default=_IDLE_WAIT_SECONDS))
            return None

    def scan(self) -> None:
        """List the stages to carry on: unfinished records, and the stages whose series file asks the cell to file.

        It reads records, receipts and one small series file each, never a stage's files. A record whose stage
        was discarded ends `discarded`; one that is not finished carries on from its saved call.
        """
        root = self.vault_root
        recorded: set[str] = set()
        store = state_paths.vault_state_dir(root) / RECORDS
        for import_id in import_stage.stems(store):
            recorded.add(import_id)
            record = read_record(root, import_id)
            if record is None or record["state"] in (DONE, DISCARDED):
                continue
            if self._discarded(import_id):
                _update(root, import_id, lambda held: held.update(state=DISCARDED, stop=None, needs=[], person=[]))
                continue
            with self._condition:
                self._due[import_id] = 0.0
        for import_id in import_stage.window_ids(root):
            if import_id not in recorded:
                self._register_from_series(import_id)

    def _register_from_series(self, import_id: str) -> None:
        series = import_stage.read_series(self.vault_root, import_id)
        if series is None or series.filing != import_stage.FILING_CELL:
            return
        window = import_stage.window_record(self.vault_root, import_id)
        receipt = import_stage.read_receipt(self.vault_root, import_id)
        if window is None or window.kind != import_stage.WINDOW_LANDED or window.stage is None or not window.stage.members:
            return
        from . import write_intake
        from .governance import principal as principal_module

        cell_id = (receipt or {}).get("request", {}).get("cell_id")
        # The import ran for one Cloud cell; its owner authority holds only in the process that serves that cell.
        principal = write_intake.principal_record(
            principal_module.cloud_owner_principal(cell_id, surface="cloud-import")) if isinstance(cell_id, str) else None
        if principal is None:
            log.info("import filer did not register %s: no Cloud owner for its cell", import_id)
            return
        self.register(import_id, principal=principal, surface=None, fresh_stage=True)

    def _discarded(self, import_id: str) -> bool:
        window = import_stage.window_record(self.vault_root, import_id)
        return window is not None and window.kind == import_stage.WINDOW_DISCARDED

    # ----- one call

    def run_once(self, import_id: str) -> float | None:
        """Make one `file` call on a registered stage and record its outcome.

        Returns the seconds until the stage is due again, or None when it leaves the list: it is `done` or
        `discarded`, or it is parked until a `file` call or a restart wakes it. A call that a crash left open
        is repeated exactly: the same number, continuation and identity.
        """
        from . import auth_sessions, write_intake

        record = read_record(self.vault_root, import_id)
        if record is None or record["state"] in (DONE, DISCARDED):
            return None
        if (aside := self._agent_wait(import_id)) > 0.0:
            # An agent that files the stage takes the boundary at each of its calls, so a call of the filer's would end
            # after one turn for it, and pay the fixed cost of a call again. Not a stall, and no write: the stage
            # stays `working`, and the view reads the window from memory (`read_view`).
            return aside
        principal = write_intake.restore_principal(record["principal"])
        login = write_intake.login_state(principal)
        if login is auth_sessions.OriginLogin.UNREADABLE:
            return FIRST_BACKOFF_SECONDS
        if login is auth_sessions.OriginLogin.NOT_VALID:
            self._park(
                import_id, STOP_LOGIN_ENDED, "the login that registered this import has ended", person=[],
                needs=[{"need": NEED_LOGIN}])
            return None
        call = self._begin_call(import_id)
        try:
            reply = self._invoke(record, call, principal)
        except Exception as error:  # noqa: BLE001 - every outcome of a call ends in a recorded state
            return self._after_error(import_id, error, call)
        return self._after_reply(import_id, reply, call)

    def _begin_call(self, import_id: str) -> dict:
        """Store the call before it runs: a number, the continuation and the collection it carries, and the attempt."""
        def change(record: dict) -> None:
            if record["call"] is None:
                record["seq"] += 1
                record["call"] = {
                    "seq": record["seq"], "action": record.get("next_action") or "file",
                    "continuation": record["continuation"], "collection": record.get("collection"), "attempt": 0,
                }
            record["call"]["attempt"] += 1
        record = _update(self.vault_root, import_id, change)
        assert record is not None
        return dict(record["call"])

    def _invoke(self, record: dict, call: dict, principal: Any) -> Any:
        from . import schema as schema_module
        from . import write_intake, writer_lease

        command = write_intake.command_named("adoption_studio")
        import_id = record["import_id"]
        action = call.get("action", "file")
        arguments: dict[str, Any] = {"action": action, "path": f"{vault.IMPORT_STAGE_DIRNAME}/{import_id}"}
        if action == "start":
            arguments["initialize_kb"] = True
        if call["continuation"]:
            arguments["continuation"] = call["continuation"]
        if call["collection"]:
            arguments["collection"] = call["collection"]
        injected: tuple[Any, ...] = (
            (self.vault_root, schema_module.load_source_schema(self.vault_root)) if command.needs_schema
            else (self.vault_root,)
        )
        descriptor = write_intake.restore_surface(record.get("surface"), command.name)
        # Background: an import nobody waits for. Its wait for the boundary is no writer's, so an agent's own `file`
        # call or the owner's write does not end early to let it in (`writer_lease.as_background_work`).
        with writer_lease.as_background_work():
            return write_intake.invoke_in_background(
                principal, descriptor, command, *injected,
                idempotency_key=f"import-file:{import_id}:{call['seq']}",
                idempotency_principal_scope=principal.audience_id,
                mutation_request_id=str(uuid.uuid5(_REQUEST_NAMESPACE, f"{import_id}:{call['seq']}:{call['attempt']}")),
                **arguments,
            )

    def _after_reply(self, import_id: str, reply: Any, call: dict) -> float | None:
        """Record a `file` reply: `done` leaves the list, `working` goes again at once, and a stop parks the stage.

        The reply of the `start` that created a missing Knowledge Base is no filing reply: `file` goes next.
        """
        if call.get("action", "file") == "start":
            if not isinstance(reply, dict):
                self._park(
                    import_id, STOP_REPLY_UNREADABLE, "the start call answered with something that is not a reply",
                    person=[], needs=[{"need": NEED_SUPPORT}])
                return None
            _update(self.vault_root, import_id, lambda held: held.update(call=None, next_action=None, busy=[]))
            return 0.0
        filing = reply.get("filing") if isinstance(reply, dict) else None
        if filing not in (_REPLY_WORKING, _REPLY_STOPPED, _REPLY_DONE):
            self._park(
                import_id, STOP_REPLY_UNREADABLE, "the file call answered with something that is not a filing reply",
                person=[], needs=[{"need": NEED_SUPPORT}])
            return None
        counts = _counts(reply.get("progress"))
        person = reply.get("person") if isinstance(reply.get("person"), list) else []
        continuation = reply.get("continuation") if isinstance(reply.get("continuation"), str) else None
        stop = reply.get("stop") if isinstance(reply.get("stop"), dict) else None
        # Both are read before the record changes: a fault here leaves the call stored, and its repeat reads them again.
        left_out = _left_out(self.vault_root, import_id) if filing == _REPLY_DONE else None
        needs = _stopped_needs(self.vault_root, reply, person) if filing == _REPLY_STOPPED else []
        total = _stage_total(self.vault_root, import_id)  # filing derives members from an app export, so the total grows
        set_aside = _set_aside(self.vault_root, import_id)
        from . import adoption_run

        # The mark of a `working` reply, as the Cloud CLI reads it: the same counts and position twice is no movement.
        mark = adoption_run.filing_mark(reply.get("progress")) if filing == _REPLY_WORKING else None
        stored_mark = None if mark is None else [list(mark[0]), mark[1]]

        def change(record: dict) -> None:
            record.update(call=None, progress=counts, left_out=left_out, needs=needs, person=person, busy=[])
            repeated = stored_mark is not None and record.get("mark") == stored_mark
            record["unmoved"] = record.get("unmoved", 0) + 1 if repeated else 0
            if not repeated:
                record["stalls"] = 0  # a call that moved, or any other reply, ends the run of stalls
            record["mark"] = stored_mark
            if total is not None:
                record["total"] = total
                record["set_aside"] = set_aside
            record["state"] = {_REPLY_WORKING: WORKING, _REPLY_STOPPED: PARKED, _REPLY_DONE: DONE}[filing]
            record["continuation"] = None if filing == _REPLY_DONE else continuation
            record["stop"] = stop if filing == _REPLY_STOPPED else None
            if counts is not None:
                _add_rate_sample(record, counts)

        record = _update(self.vault_root, import_id, change)
        if filing == _REPLY_DONE:
            return None
        if filing == _REPLY_WORKING:
            if record is not None and record["unmoved"] >= adoption_run.FILING_STALL_CALLS:
                # Calling again at once would repeat this for ever, and keep the boundary busy while it does.
                return self._stall(import_id, record["unmoved"])
            from .writer_lease import get_manager

            # The call released the boundary, and the next one asks for it at once. A writer that polls for it, above
            # all one in another process, would lose that race for good, so the filer waits while one still waits.
            get_manager().yield_to_waiting_writes(self.vault_root)
            return 0.0
        if continuation is not None:
            # A stop with a continuation is a hold or the free-space stop: ask again later.
            return HOLD_CHECK_SECONDS if _held(stop) else SPACE_CHECK_SECONDS
        if self._discarded(import_id):
            _update(self.vault_root, import_id, lambda held: held.update(state=DISCARDED, stop=None, needs=[], person=[]))
        return None

    def _after_error(self, import_id: str, error: Exception, call: dict) -> float | None:
        """Record a refusal or a fault: retry the same call, start a new one, or park the stage with its code."""
        from . import cli_ops, write_intake

        refusal = cli_ops.public_refusal(error)
        code = str(refusal.get("code")) if refusal else "INTERNAL"
        reason = str(refusal.get("message") or code) if refusal else type(error).__name__
        if refusal is None or code in write_intake.RETRY_REFUSALS:
            return self._back_off(import_id, code, reason)
        if code == _KB_NOT_INITIALIZED and call.get("action", "file") == "file":
            # No one is present to create the Knowledge Base, so the filer does what `file`'s own refusal tells an agent.
            _update(self.vault_root, import_id, lambda held: held.update(call=None, next_action="start"))
            return 0.0
        if code in (_OUTCOME_UNKNOWN, _STALE_CONTINUATION):
            # The identity is spent, or the continuation is stale: a path alone carries on the newest scan.
            return self._back_off(import_id, code, reason, new_call=True)
        if self._discarded(import_id):
            _update(
                self.vault_root, import_id,
                lambda held: held.update(state=DISCARDED, call=None, stop=None, needs=[], person=[]))
            return None
        if code == _READ_ONLY:
            # The refusal comes before any work, so the stored call stays and its repeat has the same identity.
            self._park(import_id, code, reason, person=[], needs=[{"need": NEED_READ_ONLY}])
            return READ_ONLY_CHECK_SECONDS
        self._park(import_id, code, reason, person=[], needs=[{"need": NEED_SUPPORT}], clear_call=True)
        return None

    def _back_off(self, import_id: str, code: str, reason: str, *, new_call: bool = False) -> float | None:
        """Wait for a busy cell with a growing delay; after five tries in an hour, park the stage until it is woken.

        A warming cell is a wait and not a try. The stored call stays, so the retry is the same identity, unless
        `new_call` drops it: the next call then starts with no continuation, at once and still counted as a try.
        """
        now = time.time()
        parked = False

        def change(record: dict) -> None:
            nonlocal parked
            tries = [at for at in record["busy"] if at > now - 3600]
            if code != _WARMING:
                tries.append(now)
            record["busy"] = tries
            if new_call:
                record.update(call=None, continuation=None)
            if len(tries) >= MAX_TRIES_PER_HOUR:
                parked = True
                record.update(
                    state=PARKED, stop={"code": code, "reason": reason}, needs=[{"need": NEED_SUPPORT}], person=[])

        record = _update(self.vault_root, import_id, change)
        if parked or record is None:
            return None
        tries = len(record["busy"])
        return 0.0 if new_call else min(MAX_BACKOFF_SECONDS, FIRST_BACKOFF_SECONDS * 2 ** max(0, tries - 1))

    def _stall(self, import_id: str, calls: int) -> float:
        """Show why a stage whose calls move nothing waits, and give the seconds until its next call.

        The wait doubles at each stall in a row, from `STALL_FIRST_BACKOFF_SECONDS` to `STALL_MAX_BACKOFF_SECONDS`, so a
        stage that stays stuck costs the boundary one call each quarter hour. The stage stays on the list and asks
        for no support: a stall is not a fault the person can mend, and a step that is only slow moves on later.
        """
        waits: list[float] = []

        def change(record: dict) -> None:
            stalls = record.get("stalls", 0)
            waits.append(min(STALL_MAX_BACKOFF_SECONDS, STALL_FIRST_BACKOFF_SECONDS * 2 ** min(stalls, 16)))
            record.update(
                state=PARKED, needs=[], person=[], stalls=stalls + 1,
                stop={"code": STOP_STALLED, "reason": f"filing made no progress in {calls} calls; it will try again"})

        _update(self.vault_root, import_id, change)
        return waits[0] if waits else STALL_FIRST_BACKOFF_SECONDS

    def _park(
        self, import_id: str, code: str, reason: str, *, person: list, needs: list[dict], clear_call: bool = False,
    ) -> None:
        def change(record: dict) -> None:
            record.update(state=PARKED, stop={"code": code, "reason": reason}, needs=needs, person=person)
            record.update(mark=None, unmoved=0)  # a stage woken from a park gets its full count of calls again
            if clear_call:
                record["call"] = None
        _update(self.vault_root, import_id, change)


# --------------------------------------------------------------------------- this process's filers

_FILERS: dict[Path, Filer] = {}
_FILERS_LOCK = threading.Lock()


def filer_for(vault_root: Any) -> Filer | None:
    """This process's running filer for the vault, or None: only the serving HTTP runtime starts one."""
    try:
        key = Path(vault_root).resolve()
    except (TypeError, OSError):
        return None
    with _FILERS_LOCK:
        filer = _FILERS.get(key)
    return filer if filer is not None and filer.running else None


def start(vault_root: Path) -> Filer:
    """Start this process's filer for one vault. The serving HTTP runtime calls it once."""
    key = Path(vault_root).resolve()
    with _FILERS_LOCK:
        existing = _FILERS.get(key)
        if existing is not None and existing.running:
            return existing
        filer = Filer(key)
        _FILERS[key] = filer
    filer.start()
    return filer


def stop(vault_root: Path, *, timeout: float = 10.0) -> None:
    key = Path(vault_root).resolve()
    with _FILERS_LOCK:
        filer = _FILERS.pop(key, None)
    if filer is not None:
        filer.stop(timeout=timeout)


def _import_id_of(path: str) -> str | None:
    """The import ID of a staged folder path, `_Imports/<id>`, or None for any other path."""
    parts = [part for part in path.strip("/").split("/") if part]
    ok = len(parts) == 2 and parts[0] == vault.IMPORT_STAGE_DIRNAME and vault.is_import_id(parts[1])
    return parts[1] if ok else None


class AgentFileCall:
    """An agent's `file` call as it runs: its reply says whether the stage is still the agent's to file."""

    def __init__(self) -> None:
        self.working = True  # a call that returns no reply (it raised) leaves the stage with the agent for the window
        self.counts: dict[str, int] | None = None

    def returned(self, reply: Any) -> None:
        filing = reply.get("filing") if isinstance(reply, dict) else None
        self.working = filing not in (_REPLY_DONE, _REPLY_STOPPED)
        self.counts = _counts(reply.get("progress")) if isinstance(reply, dict) else None


@contextlib.contextmanager
def agent_filing(vault_root: Path, path: str, collection: str | None = None) -> Iterator[AgentFileCall]:
    """Wrap an agent's `file` call on a staged folder: the cell finishes the stage after it, and stands aside while it runs.

    It registers the stage with the filer, where one runs (the serving HTTP runtime), and only for a caller whose
    authority may be replayed: a delegated authorization session stays foreground, as for a queued save. The
    filer then leaves the stage alone during the call and for `AGENT_QUIET_SECONDS` after it (`Filer.agent_ended`).
    The filer's own `file` call comes through the same command and is no agent: listing the stage again from it
    would reset the stage's due time and its back-off. Never raises: the caller's own call goes on whatever
    happens here.
    """
    from . import writer_lease

    call = AgentFileCall()
    filer = filer_for(vault_root)
    import_id = _import_id_of(path)
    if filer is None or import_id is None or writer_lease.in_background_work():
        yield call
        return
    filer.agent_began(import_id)  # before the listing wakes the filer, so that it finds the agent there
    try:
        _register_caller(filer, import_id, path, collection)
        yield call
    finally:
        filer.agent_ended(import_id, working=call.working, counts=call.counts)


def _register_caller(filer: Filer, import_id: str, path: str, collection: str | None) -> None:
    try:
        from . import write_intake
        from .governance import principal as principal_module

        principal = write_intake.principal_record(principal_module.effective_principal())
        if principal is not None:
            filer.register(import_id, principal=principal, surface=write_intake.surface_record(), collection=collection)
    except Exception:  # noqa: BLE001 - a registration fault never fails the caller's own file call
        log.warning("import filer could not register %s", path, exc_info=True)


def wake(vault_root: Path, path: str, collection: str | None = None) -> None:
    """Register the stage under the caller's login: `file` with `wait` false. The caller then reads the filing view.

    It makes no `file` call: the filer's thread does, as the caller, after the reply. A stage that ended `done`
    or `discarded` stays as it is, and a stage whose login ended goes on under this caller's. Raises
    `AdoptionRunError` when the call cannot register the stage, so the caller never reads a view as a promise.
    """
    from . import write_intake
    from .adoption_run import AdoptionRunError
    from .governance import principal as principal_module

    import_id = _import_id_of(path)
    if import_id is None:
        raise AdoptionRunError(
            "IMPORT_REQUEST_INVALID", f"wait false takes the staged folder, {vault.IMPORT_STAGE_DIRNAME}/<id>")
    filer = filer_for(vault_root)
    if filer is None:
        raise AdoptionRunError(
            "IMPORT_REQUEST_INVALID",
            "this server does not file in the background; call file again with its continuation until filing is not working")
    record = read_record(vault_root, import_id)
    if record is not None and record["state"] in (DONE, DISCARDED):
        return
    if not (vault_root / vault.IMPORT_STAGE_DIRNAME / import_id).is_dir():
        raise AdoptionRunError("IMPORT_REQUEST_INVALID", f"no staged folder {vault.IMPORT_STAGE_DIRNAME}/{import_id}")
    principal = write_intake.principal_record(principal_module.effective_principal())
    if principal is None:
        raise AdoptionRunError(
            STOP_NOT_REPLAYABLE,
            "this login cannot be replayed by the cell, so it cannot file in the background; "
            "call file again with its continuation until filing is not working")
    filer.agent_left(import_id)  # the agent hands the stage over: the window that its earlier calls left must not delay the cell
    filer.register(
        import_id, principal=principal, surface=write_intake.surface_record(), collection=collection, fresh_stage=True)
