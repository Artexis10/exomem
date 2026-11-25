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

import contextvars
import datetime as dt
import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import import_stage, private_state, state_paths, vault

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
#: The `file` reply could not be read as a reply.
STOP_REPLY_UNREADABLE = "IMPORT_FILING_REPLY_UNREADABLE"
#: The mutation protocol's final answer for an identity: its outcome will never be known, so the next call has a new one.
_OUTCOME_UNKNOWN = "MUTATION_OUTCOME_UNKNOWN"
#: A continuation that `file` no longer knows: the next call names the stage alone and carries on its newest scan.
_STALE_CONTINUATION = "INVALID_CONTINUATION"
#: The cell is not warm yet: a wait, not a try.
_WARMING = "MUTATION_WARMING"

MAX_TRIES_PER_HOUR = 5
FIRST_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
#: A stage that stopped for free space asks again this often; a `file` call or a restart asks sooner.
SPACE_CHECK_SECONDS = 3600.0
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


def _update(vault_root: Path, import_id: str, change: Callable[[dict], None]) -> dict | None:
    """Read the record, let `change` edit it in place and write it durably; None when there is no record."""
    with _RECORD_LOCK:
        record = read_record(vault_root, import_id)
        if record is None:
            return None
        change(record)
        record["as_of"] = _now()
        _write_record(vault_root, record)
        return record


def _fresh_record(import_id: str, principal: dict, surface: dict | None, collection: str | None) -> dict:
    return {
        "version": RECORD_VERSION, "import_id": import_id, "state": WORKING, "principal": principal,
        "surface": surface, "collection": collection, "seq": 0, "call": None, "next_action": None, "continuation": None,
        "progress": None, "stop": None, "person": [], "calls": [], "busy": [], "as_of": _now(),
    }


def _rate(calls: list) -> dict | None:
    """Files handled per minute over the last calls; None until two calls have finished."""
    if len(calls) < 2:
        return None
    (first_at, first_handled), (last_at, last_handled) = calls[0], calls[-1]
    if last_at <= first_at:
        return None
    per_minute = max(0, last_handled - first_handled) / ((last_at - first_at) / 60)
    return {"files_per_minute": round(per_minute, 1), "over_calls": len(calls)}


def read_view(vault_root: Path, import_id: str) -> dict:
    """What `adoption_studio status` answers with `view="filing"`: the stage's record, or `unknown` when it has none.

    One record read. It opens no run, inventory or outcome file, so a client may poll it at any rate. A stage
    with no record is `unknown` and reports no counts, never zero ones. The cell states no time left: a caller
    that derives one from `rate` says "at the measured rate".
    """
    record = read_record(vault_root, import_id)
    if record is None:
        return {"state": UNKNOWN, "stop": None, "person": []}
    view: dict[str, Any] = {
        "state": record["state"], "progress": record.get("progress"), "stop": record.get("stop"),
        "person": record.get("person") or [], "as_of": record.get("as_of"),
    }
    if (rate := _rate(record.get("calls") or [])) is not None:
        view["rate"] = rate
    return view


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

    def register(self, import_id: str, *, principal: dict, surface: dict | None, collection: str | None = None) -> bool:
        """List a stage, or list it again with the principal of the newest caller; False when it ended `discarded`.

        The stored principal is the owner who asked for the filing. A principal that `write_intake.principal_record`
        refuses (a delegated session) is never passed here, so such a caller's stage stays foreground-only.
        """
        with _RECORD_LOCK:
            record = read_record(self.vault_root, import_id)
            if record is not None and record["state"] == DISCARDED:
                return False
            if record is None:
                record = _fresh_record(import_id, principal, surface, collection)
            else:
                record.update(principal=principal, surface=surface, state=WORKING, stop=None, person=[], busy=[])
                record["collection"] = collection or record.get("collection")
            record["as_of"] = _now()
            _write_record(self.vault_root, record)
        with self._condition:
            self._due[import_id] = 0.0
            self._condition.notify_all()
        return True

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
                _update(root, import_id, lambda held: held.update(state=DISCARDED, stop=None, person=[]))
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
        self.register(import_id, principal=principal, surface=None)

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
        principal = write_intake.restore_principal(record["principal"])
        login = write_intake.login_state(principal)
        if login is auth_sessions.OriginLogin.UNREADABLE:
            return FIRST_BACKOFF_SECONDS
        if login is auth_sessions.OriginLogin.NOT_VALID:
            self._park(import_id, STOP_LOGIN_ENDED, "the login that registered this import has ended", person=[])
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
        with write_intake.background_call(principal, descriptor):
            return writer_lease.invoke_command(
                command, *injected,
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
                self._park(import_id, STOP_REPLY_UNREADABLE, "the start call answered with something that is not a reply", person=[])
                return None
            _update(self.vault_root, import_id, lambda held: held.update(call=None, next_action=None, busy=[]))
            return 0.0
        filing = reply.get("filing") if isinstance(reply, dict) else None
        if filing not in (_REPLY_WORKING, _REPLY_STOPPED, _REPLY_DONE):
            self._park(import_id, STOP_REPLY_UNREADABLE, "the file call answered with something that is not a filing reply", person=[])
            return None
        progress = reply.get("progress") if isinstance(reply.get("progress"), dict) else None
        person = reply.get("person") if isinstance(reply.get("person"), list) else []
        continuation = reply.get("continuation") if isinstance(reply.get("continuation"), str) else None
        stop = reply.get("stop") if isinstance(reply.get("stop"), dict) else None

        def change(record: dict) -> None:
            record.update(call=None, progress=progress, person=person, busy=[])
            record["state"] = {_REPLY_WORKING: WORKING, _REPLY_STOPPED: PARKED, _REPLY_DONE: DONE}[filing]
            record["continuation"] = None if filing == _REPLY_DONE else continuation
            record["stop"] = stop if filing == _REPLY_STOPPED else None
            handled = sum((progress or {}).get(key) or 0 for key in ("filed", "skipped_duplicates"))
            record["calls"] = [*record["calls"], [time.time(), handled]][-_RATE_CALLS:]

        _update(self.vault_root, import_id, change)
        if filing == _REPLY_DONE:
            return None
        if filing == _REPLY_WORKING:
            return 0.0
        if continuation is not None:
            return SPACE_CHECK_SECONDS  # a stop with a continuation is the free-space stop: ask again later
        if self._discarded(import_id):
            _update(self.vault_root, import_id, lambda held: held.update(state=DISCARDED, stop=None, person=[]))
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
            _update(self.vault_root, import_id, lambda held: held.update(state=DISCARDED, call=None, stop=None, person=[]))
            return None
        self._park(import_id, code, reason, person=[], clear_call=True)
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
                record.update(state=PARKED, stop={"code": code, "reason": reason}, person=[])

        record = _update(self.vault_root, import_id, change)
        if parked or record is None:
            return None
        tries = len(record["busy"])
        return 0.0 if new_call else min(MAX_BACKOFF_SECONDS, FIRST_BACKOFF_SECONDS * 2 ** max(0, tries - 1))

    def _park(self, import_id: str, code: str, reason: str, *, person: list, clear_call: bool = False) -> None:
        def change(record: dict) -> None:
            record.update(state=PARKED, stop={"code": code, "reason": reason}, person=person)
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


def note_file_call(vault_root: Path, path: str, collection: str | None = None) -> None:
    """Register the stage that a `file` call names, so the cell finishes it after the call returns.

    It registers only where a filer runs (the serving HTTP runtime), and only a caller whose authority may be
    replayed: a delegated authorization session stays foreground, as for a queued save. Never raises: the
    caller's own call goes on whatever happens here.
    """
    filer = filer_for(vault_root)
    if filer is None:
        return
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2 or parts[0] != vault.IMPORT_STAGE_DIRNAME or not vault.is_import_id(parts[1]):
        return
    try:
        from . import write_intake
        from .governance import principal as principal_module

        principal = write_intake.principal_record(principal_module.effective_principal())
        if principal is not None:
            filer.register(parts[1], principal=principal, surface=write_intake.surface_record(), collection=collection)
    except Exception:  # noqa: BLE001 - a registration fault never fails the caller's own file call
        log.warning("import filer could not register %s", path, exc_info=True)
