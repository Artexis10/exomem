"""The applier of the write-intent journal (design D4): one per vault, FIFO, through the ordinary command path.

An accepted intent commits exactly once, in acceptance order, as the caller who sent it. The applier
rebuilds that caller's principal from the row's bearer-free snapshot, revalidates the login, and
dispatches the stored arguments through `writer_lease.invoke_command`, so admission, the leaf, the
receipts and the postfilter are the synchronous path's own. The applier-dispatch context variable
(`intent_accept.ApplierDispatch`) makes that dispatch claim the key accept recorded, with the original
digest, and take no accept deadline.

One OS lock per vault (`WriteIntentJournal.applier_lock_path`) admits one applier across processes.
The service is the preferred applier: it holds the lock while it runs, and holds a shared lock on a
sidecar that a CLI applier reads to stand aside. A CLI applier releases the lock after each intent.

Crash recovery resolves a row a dead applier left `applying` before the queue is read (design D4, crash
table). A dispatch that does not commit ends by the closed table in `intent_outcomes`. A cause that belongs to one intent
ends it `stalled` once it outlasts the per-intent bound (design D4, D5).
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

from . import (
    capabilities,
    intent_accept,
    intent_observability,
    intent_outcomes,
    mutation_journal,
    mutation_lock,
    request_budget,
    state_paths,
    write_intents,
)
from .cli_ops import error_dict

log = logging.getLogger(__name__)

KIND_SERVICE = "service"
KIND_CLI = "cli"

#: How often an idle service applier looks for rows that another process journaled.
IDLE_POLL_SECONDS = 0.5
#: How often an applier that does not hold the lock tries for it.
LOCK_POLL_SECONDS = 0.25
#: A CLI applier that finds the lock taken waits this long for its holder to end or hand over before it leaves.
CLI_LOCK_GRACE_SECONDS = 2.0
#: Pause after an attempt that committed nothing, so a held boundary is retried and not spun on.
RETRY_PAUSE_SECONDS = 0.1
#: A service stop waits this long for the applier thread; only a leaf that outlasts it leaves a row `applying`.
STOP_JOIN_SECONDS = 15.0

# Causes this module writes to `conflict` and `wait_cause`: the applier's own protocol, not vocabulary.
CAUSE_REVOKED = intent_outcomes.CAUSE_REVOKED
CAUSE_EXPIRED = intent_outcomes.CAUSE_EXPIRED
CAUSE_APPLY_FAILED = intent_outcomes.CAUSE_APPLY_FAILED
CAUSE_STOPPING = "applier_stopping"
CAUSE_OUTCOME_UNKNOWN = intent_outcomes.CAUSE_OUTCOME_UNKNOWN
CAUSE_WRITER_LEASE = intent_outcomes.CAUSE_WRITER_LEASE
CAUSE_TARGET_MOVED = "target_moved"
#: A replica that is not the writer re-asks the coordinator this often, not on the boundary's retry pause.
LEASE_WAIT_SECONDS = 1.0
# Receipt states of a key whose claim never reached its leaf; every other state is evidence the claim machinery resolves.
_NO_EVIDENCE_STATES = ("reserved", "pending")  # nosemgrep: ep-word-set -- the idempotency store's closed row states


# -- the per-vault lock and its preferred-applier sidecar --------------------------------------------


class _ApplierLock:
    """The cross-process applier lock of one vault."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None
        self._preferred: BinaryIO | None = None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle = mutation_lock._open_lock_handle(self.path)
        if mutation_lock._try_os_lock(handle):
            self._handle = handle
            return True
        handle.close()
        return False

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            mutation_lock._release_os_lock(handle)
        except OSError:
            pass
        finally:
            handle.close()

    def _sidecar(self) -> Path:
        return self.path.with_name(f"{self.path.name}.preferred")

    def prefer(self) -> None:
        """Hold a shared lock on the sidecar for as long as this (service) applier lives, so a CLI applier yields.

        The kernel drops it when the process exits. Windows has no shared advisory lock here, so a CLI
        applier there does not yield; it still never runs beside the service, because of the main lock.
        """
        if os.name == "nt" or self._preferred is not None:
            return
        import fcntl

        handle = mutation_lock._open_lock_handle(self._sidecar())
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return
        self._preferred = handle

    def unprefer(self) -> None:
        handle, self._preferred = self._preferred, None
        if handle is not None:
            try:
                mutation_lock._release_os_lock(handle)
            except OSError:
                pass
            finally:
                handle.close()

    def preferred_applier_present(self) -> bool:
        """Whether a service in another process holds the sidecar. An exclusive try fails only for a holder."""
        if os.name == "nt" or self._preferred is not None or not self._sidecar().exists():
            return False
        import fcntl

        try:
            descriptor = os.open(self._sidecar(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            os.close(descriptor)
        return False


def applier_alive(journal: write_intents.WriteIntentJournal) -> bool:
    """Whether some process holds this vault's applier lock now. A lock this probe can take has no holder."""
    lock = _ApplierLock(journal.applier_lock_path)
    if lock.try_acquire():
        lock.release()
        return False
    return True


# -- the applier -------------------------------------------------------------------------------------


class Applier:
    """Applies one vault's accepted intents, oldest first, on its own thread."""

    def __init__(
        self,
        manager: Any,
        vault_root: Path,
        journal: write_intents.WriteIntentJournal,
        *,
        kind: str,
        on_end: Any = None,
        bound_seconds: float | None = None,
    ):
        self.manager = manager
        # Read at construction, so a test or an operator setting the module constant sets it for the next applier.
        self.bound_seconds = intent_accept.PER_INTENT_BOUND_SECONDS if bound_seconds is None else bound_seconds
        self.vault_root = vault_root
        self.journal = journal
        self.kind = kind
        self._lock = _ApplierLock(journal.applier_lock_path)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_end = on_end

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        # A fresh context: the thread must not inherit the request that happened to start it.
        context = contextvars.Context()
        self._thread = threading.Thread(
            target=context.run, args=(self._run,), name=f"write-intent-applier-{self.kind}", daemon=True
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = STOP_JOIN_SECONDS) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # -- loop --------------------------------------------------------------------------------------

    def _run(self) -> None:
        try:
            if self.kind == KIND_SERVICE:
                self._lock.prefer()
                self._serve()
            else:
                self._drain_as_cli()
        except Exception:  # noqa: BLE001 - an applier that dies must say so; the next accept starts another
            log.exception("write-intent applier stopped on an error")
        finally:
            self._lock.release()
            self._lock.unprefer()
            if self._on_end is not None:
                self._on_end(self)

    def _serve(self) -> None:
        """The service's loop: hold the lock for the lifetime, apply as rows arrive, wait on a wake or the poll."""
        while not self._stop.is_set():
            if not self._lock.try_acquire():
                self._stop.wait(LOCK_POLL_SECONDS)
                continue
            self._apply_next_or_idle(idle_seconds=IDLE_POLL_SECONDS)

    def _drain_as_cli(self) -> None:
        """A CLI applier's loop: take the lock when free, release it after each intent, stand aside for a service."""
        grace_until = time.monotonic() + CLI_LOCK_GRACE_SECONDS
        while not self._stop.is_set():
            if self._lock.preferred_applier_present():
                return
            if not self._lock.try_acquire():
                if time.monotonic() >= grace_until:
                    return
                self._stop.wait(LOCK_POLL_SECONDS)
                continue
            try:
                applied = self._apply_next_or_idle(idle_seconds=0.0)
            finally:
                # Between intents, so a service that started takes over and other processes interleave.
                self._lock.release()
            if not applied:
                if self._on_end is not None and self._on_end(self, only_if_idle=True):
                    return
            grace_until = time.monotonic() + CLI_LOCK_GRACE_SECONDS

    def _apply_next_or_idle(self, *, idle_seconds: float) -> bool:
        try:
            # This process holds the applier lock, so an `applying` row belongs to an applier that is gone.
            for orphan in self.journal.applying():
                if self._stop.is_set():
                    return False
                self._recover(orphan)
            intent = self.journal.next_accepted()
        except write_intents.JournalRefusal:
            log.warning("write-intent journal unreadable; the applier retries", exc_info=True)
            self._stop.wait(max(idle_seconds, LOCK_POLL_SECONDS))
            return False
        if intent is None:
            if idle_seconds:
                self._wake.wait(idle_seconds)
                self._wake.clear()
            return False
        self._apply(intent)
        return True

    # -- crash recovery ----------------------------------------------------------------------------

    def _recover(self, intent: write_intents.Intent) -> None:
        """Resolve one `applying` row without running its leaf again (design D4, crash table).

        K in the idempotency store is the commit authority. A key with a claim past its reservation goes
        back through dispatch, which replays a persisted terminal, resumes one from the commit receipt, or
        raises `MUTATION_OUTCOME_UNKNOWN` for a dead claim without evidence. A key with no claim, or one
        that never reached its leaf, may have lost its receipt, so the row is `outcome_unknown` and never re-run.
        """
        try:
            state = self.manager.idempotency.claim_state(intent.retry_key)
            if state is None or state in _NO_EVIDENCE_STATES:
                self._settle(intent, ("conflicted", self._outcome_unknown(intent)))
            else:
                self._settle(intent, self._attempt(intent))
        except write_intents.JournalRefusal:
            log.warning("write-intent journal could not record a recovery step", exc_info=True)
            self._stop.wait(RETRY_PAUSE_SECONDS)

    def _outcome_unknown(self, intent: write_intents.Intent) -> dict[str, Any]:
        """The conflict record of a write that may or may not have committed, with the evidence an owner decides on."""
        now = intent_accept.planned_hashes(self.vault_root, intent.planned_paths)
        at_accept = intent.target_hashes or {}
        return {
            "cause": CAUSE_OUTCOME_UNKNOWN,
            "evidence": {
                "targets": {
                    path: {"at_accept": at_accept.get(path), "now": now.get(path)}
                    for path in intent.planned_paths or ()
                },
                "logged_outcomes": mutation_journal.outcomes_for_request(intent.request_id),
            },
        }

    # -- one intent --------------------------------------------------------------------------------

    def _apply(self, intent: write_intents.Intent) -> None:
        journal = self.journal
        try:
            if not journal.begin_apply(intent.intent_id):
                return
            self._settle(intent, self._attempt(intent))
        except write_intents.JournalRefusal:
            # The journal could not record the move. The row keeps its state; the next pass reads it again.
            log.warning("write-intent journal could not record an apply step", exc_info=True)
            self._stop.wait(RETRY_PAUSE_SECONDS)

    def _attempt(self, intent: write_intents.Intent) -> tuple[str, Any]:
        """One dispatch. Returns `(outcome, detail)`: committed, wait, conflicted or rejected."""
        from . import auth_sessions, upload_tokens, writer_lease
        from . import schema as schema_module
        from .governance import principal as principal_module

        command = _command(intent.command, intent.surface)
        if command is None:
            return "conflicted", {"cause": CAUSE_APPLY_FAILED, "error": {"code": "UNKNOWN_COMMAND"}}
        try:
            principal = upload_tokens._principal_from_values(copy.deepcopy(intent.principal))
        except (TypeError, ValueError, AttributeError):
            return "conflicted", {"cause": CAUSE_APPLY_FAILED, "error": {"code": "PRINCIPAL_UNREADABLE"}}
        # The authority the caller held at accept is not the authority it holds now (design D4, D5). A key
        # with a claim past its reservation never runs the leaf again, so it needs no authority; the write
        # that holds that claim committed under the authority it had.
        state = self.manager.idempotency.claim_state(intent.retry_key)
        if state is None or state in _NO_EVIDENCE_STATES:
            session = principal.verified_authorization_session
            if session is not None and time.time() > session.expires_at:
                return "conflicted", {"cause": CAUSE_EXPIRED}
            if not asyncio.run(auth_sessions.origin_session_active(principal)):
                return "conflicted", {"cause": CAUSE_REVOKED}

        injected = (
            (self.vault_root, schema_module.load_source_schema(self.vault_root))
            if command.needs_schema
            else (self.vault_root,)
        )
        # The descriptor admits exactly this command; the intent's recorded surface keeps surface-bound checks the same.
        descriptor = capabilities.ActiveSurfaceDescriptor(intent.surface, "full", True, (command.name,))
        dispatch = intent_accept.ApplierDispatch(
            intent_id=intent.intent_id,
            key=intent.retry_key,
            digest=intent.digest,
            window_seconds=intent.window_seconds,
            request_id=intent.request_id,
            stop=self._stop,
            manager=self.manager,
        )
        budget = request_budget.RequestBudget(seconds=dispatch.budget_seconds)
        # The leaf receives the rewritten arguments when a rewrite exists; the claim keeps the original digest.
        arguments = dict(intent.leaf_arguments if intent.leaf_arguments is not None else intent.arguments)
        budget_token = request_budget.set_current(budget)
        try:
            with (
                principal_module.request_scope(principal),
                capabilities.active_surface(descriptor),
                principal_module.invocation_scope(),
                intent_accept.applier_dispatch(dispatch),
            ):
                result = writer_lease.invoke_command(
                    command, *injected, mutation_request_id=intent.request_id, **arguments
                )
        except Exception as error:  # noqa: BLE001 - every dispatch failure is classified, never lost
            return self._classify_error(intent, error)
        finally:
            request_budget.reset_current(budget_token)
        return self._classify_result(result)

    def _classify_result(self, result: Any) -> tuple[str, Any]:
        if isinstance(result, dict) and result.get("state") == "committed":
            return "committed", _portable_value(result)
        # A write that did not commit is held as conflicted, never reported as committed.
        row = intent_outcomes.classify_result(result)
        return row.kind, {"cause": row.cause, "result": _portable_value(result)}

    def _classify_error(self, intent: write_intents.Intent, error: Exception) -> tuple[str, Any]:
        """Map a dispatch that raised to its row of the outcome table (design D5)."""
        public = error_dict(error)
        if intent_outcomes.error_code(error) == intent_outcomes.NOT_FOUND:
            moved = self._follow_moved_target(intent)
            if moved is not None:
                return moved
        row = intent_outcomes.classify_error(error, validation=intent.validation)
        if row.kind == intent_outcomes.WAIT:
            return "wait", row
        if row.kind == intent_outcomes.COMMITTED:
            # A write that committed is never retried; its recorded failure or uncertainty is its terminal.
            log.warning("an applied intent committed without a persisted terminal: %s", public.get("code"), exc_info=error)
            terminal = {"ok": False, "state": "committed", "committed": True, "error": public}
            return "committed", {**terminal, "uncertain": True} if row.uncertain else terminal
        if row.kind == intent_outcomes.REJECTED:
            return "rejected", {"error": public}
        if row.cause == CAUSE_OUTCOME_UNKNOWN:
            # The claim on K died without evidence. The write may have landed, so it is never re-run (design D4).
            return "conflicted", self._outcome_unknown(intent)
        return "conflicted", {"cause": row.cause, "error": public}

    def _follow_moved_target(self, intent: write_intents.Intent) -> tuple[str, Any] | None:
        """Handle a missing target through the stable reference recorded at accept (design D5).

        Returns `("wait", ...)` after it rewrote the leaf's target to the one current path, a `target_missing`
        conflict when the reference no longer resolves to one path, and `None` when no recorded target is missing,
        so the error is not about the target.
        """
        from . import memory_refs
        from .find import RetrievalIndexWarming

        references = (intent.base_guards or {}).get(intent_accept.TARGET_REFERENCES) or {}
        present = intent_accept.planned_hashes(self.vault_root, list(references))
        missing = {path: ref for path, ref in references.items() if path not in present}
        if not missing:
            return None
        moved: dict[str, str] = {}
        for path, reference in missing.items():
            memory_id = memory_refs.parse_memory_ref(reference)
            try:
                found = memory_refs.paths_for_ids_read_only(self.vault_root, [memory_id]).get(memory_id, ())
            except RetrievalIndexWarming:
                return "wait", intent_outcomes.Outcome(intent_outcomes.WAIT, "MUTATION_WARMING", counts=True)
            except (memory_refs.ReferenceError, OSError):
                found = ()
            if len(found) != 1:
                return "conflicted", {
                    "cause": intent_outcomes.CAUSE_TARGET_MISSING,
                    "evidence": {"targets": sorted(missing)},
                }
            moved[path] = found[0]
        command = _command(intent.command, intent.surface)
        arguments = dict(intent.leaf_arguments if intent.leaf_arguments is not None else intent.arguments)
        rewritten = False
        for role in command.path_roles if command is not None else ():
            if arguments.get(role.argument) in moved:
                arguments[role.argument] = moved[arguments[role.argument]]
                rewritten = True
        if not rewritten:
            return None
        self.journal.set_leaf_arguments(intent.intent_id, arguments)
        return "wait", intent_outcomes.Outcome(intent_outcomes.WAIT, CAUSE_TARGET_MOVED)

    def _settle(self, intent: write_intents.Intent, attempt: tuple[str, Any]) -> None:
        outcome, detail = attempt
        journal = self.journal
        if outcome == "wait":
            if detail.counts and self._stalled(intent, detail.cause):
                self._stall(intent, detail.cause)
                return
            journal.requeue(intent.intent_id, str(detail.cause))
            intent_observability.waited(str(detail.cause))
            if detail.cause != CAUSE_STOPPING:
                self._stop.wait(LEASE_WAIT_SECONDS if detail.cause == CAUSE_WRITER_LEASE else RETRY_PAUSE_SECONDS)
            return
        if outcome == "committed":
            journal.finish(intent.intent_id, "committed", terminal=detail)
        elif outcome == "rejected":
            release_vocabulary_binding(self.vault_root, intent)
            journal.finish(intent.intent_id, "rejected", terminal={"ok": False, "state": "rejected", **detail})
        else:
            journal.finish(intent.intent_id, "conflicted", conflict=detail)
        intent_accept.notify_applier_progress()

    def _stalled(self, intent: write_intents.Intent, cause: str | None) -> bool:
        """Whether this intent's own wait cause has lasted the per-intent bound.

        `wait_since` restarts when the cause changes, and a lost writer lease or a held boundary is a different
        cause, so those waits never add to it: only an unbroken wait on one cause of this intent counts.
        """
        return (
            intent.wait_cause == cause
            and intent.wait_since is not None
            and self.journal.clock() - intent.wait_since >= self.bound_seconds
        )

    def _stall(self, intent: write_intents.Intent, cause: str | None) -> None:
        conflict: dict[str, Any] = {
            "cause": intent_outcomes.CAUSE_STALLED,
            "stalled_on": cause,
            "since": intent.wait_since,
            "bound_seconds": self.bound_seconds,
        }
        if cause == intent_outcomes.CAUSE_ACKNOWLEDGEMENT_PENDING:
            # The write may have committed behind the store's pending acknowledgement: the owner needs the same evidence.
            conflict["evidence"] = self._outcome_unknown(intent)["evidence"]
        self.journal.finish(intent.intent_id, "conflicted", conflict=conflict)
        intent_accept.notify_applier_progress()


def release_vocabulary_binding(vault_root: Path, intent: write_intents.Intent) -> bool:
    """Free the vocabulary application that this intent's dispatch bound, for an intent that ends uncommitted (design D5).

    Never raises: a binding that cannot be released stays `applying`, which an owner sees and repairs.
    """
    arguments = intent.arguments
    if not isinstance(arguments, dict) or arguments.get("vocabulary_ref") is None:
        return False
    from . import vocabulary_application

    try:
        return vocabulary_application.release(
            vault_root,
            kwargs=arguments,
            idempotency_key=intent.retry_key,
            command_digest=intent.digest,
            principal=intent.audience,
        )
    except (OSError, ValueError):
        log.warning("a vocabulary application binding could not be released", exc_info=True)
        return False


def discard(journal: write_intents.WriteIntentJournal, vault_root: Path, intent_id: str, reason: str) -> bool:
    """End a `conflicted` intent as `discarded`, releasing its vocabulary binding. Task 5.3's `discard-write` calls this."""
    intent = journal.get(intent_id)
    if intent is None or intent.state != "conflicted":
        return False
    release_vocabulary_binding(vault_root, intent)
    journal.finish(intent_id, "discarded", terminal={"ok": False, "state": "discarded", "reason": reason})
    return True


def _command(name: str, surface: str) -> Any | None:
    """The registry command as the surface that accepted it exports it; a library caller's command may be on no surface, so the full registry serves last."""
    from .commands import COMMANDS, PRODUCT_COMMANDS, product_commands_for

    for commands in (product_commands_for(surface), PRODUCT_COMMANDS, COMMANDS):
        found = next((command for command in commands if command.name == name), None)
        if found is not None:
            return found
    return None


def _portable_value(value: Any) -> Any:
    """The terminal as the journal stores it: JSON, with anything else as its text."""
    return json.loads(json.dumps(value, default=str))


# -- the process's appliers --------------------------------------------------------------------------

_APPLIERS: dict[str, Applier] = {}
_REGISTRY_LOCK = threading.Lock()


def _forget(applier: Applier, *, only_if_idle: bool = False) -> bool:
    """Drop an ending applier from the registry. With `only_if_idle`, do so only while no intent waits.

    The recheck runs under the registry lock that `ensure_running` takes after its row commits, so an
    intent accepted a moment later either shows here or starts a new applier.
    """
    with _REGISTRY_LOCK:
        if only_if_idle:
            try:
                if applier.journal.next_accepted() is not None:
                    return False
            except write_intents.JournalRefusal:
                pass
        for identity, held in list(_APPLIERS.items()):
            if held is applier:
                del _APPLIERS[identity]
        return True


def ensure_running(manager: Any, vault_root: Path | str, *, kind: str = KIND_CLI) -> Applier | None:
    """Start this process's applier for a vault unless one runs, and wake it.

    With the journal off nothing new is accepted, but accepted writes still drain: the applier starts for an
    existing journal that holds active intents, and for no other. It creates no file and no thread otherwise.

    A CLI applier does not start while a service holds the preference, so a service-less process
    never takes work the service would run with its own environment and code version.
    """
    off = intent_accept.mode() == "off"
    if off and not (state_paths.vault_state_dir(Path(vault_root)) / write_intents.JOURNAL_FILENAME).exists():
        return None
    try:
        journal = manager._intent_journal(vault_root)
        if off and (journal is None or not journal.has_active()):
            return None
    except write_intents.JournalRefusal:
        return None
    if journal is None:
        return None
    identity = str(journal.path)
    with _REGISTRY_LOCK:
        held = _APPLIERS.get(identity)
        if held is not None and held.alive:
            held.wake()
            return held
        applier = Applier(manager, Path(vault_root), journal, kind=kind, on_end=_forget)
        _APPLIERS[identity] = applier
    applier.start()
    return applier


def stop_all(timeout: float = STOP_JOIN_SECONDS) -> None:
    """Stop every applier of this process, within a bound. An apply that has not reached the boundary returns to `accepted`."""
    with _REGISTRY_LOCK:
        appliers = list(_APPLIERS.values())
    for applier in appliers:
        applier._stop.set()
        applier._wake.set()
    for applier in appliers:
        applier.stop(timeout)
    with _REGISTRY_LOCK:
        for applier in appliers:
            for identity, held in list(_APPLIERS.items()):
                if held is applier:
                    del _APPLIERS[identity]


__all__ = ["Applier", "ensure_running", "stop_all", "KIND_CLI", "KIND_SERVICE"]
