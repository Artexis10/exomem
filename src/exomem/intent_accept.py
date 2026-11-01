"""The accept path of the write-intent journal (design D1, D2).

A write that cannot commit inline is recorded in the journal and answered within the accept deadline.
`writer_lease.LeaseManager.invoke` owns the flow; this module holds the pieces that do not need the
manager: the mode switch, which calls qualify, the deadline window, and the accepted envelope.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from . import capabilities, mutation_journal, mutation_lock, request_budget, write_intents
from .cli_ops import OpError

log = logging.getLogger(__name__)

MODE_ENV = "EXOMEM_WRITE_INTENTS"
#: `off` journals nothing. `on` journals a write that cannot commit inline. `always` journals every
#: eligible write, so tests drive the applier through the same path.
MODES = ("off", "on", "always")  # nosemgrep: ep-word-set -- the closed values of the rollout switch, not vocabulary

#: How long an eligible write may wait before it gets an answer (design D1: the owner chose 2.0 s on
#: 2026-11-01). A constant, not a knob.
ACCEPT_DEADLINE_SECONDS = 2.0
#: How soon a client should ask again. The envelope carries no `retry_after_ms`: a success must not
#: invite a resend.
POLL_AFTER_MS = 2000
#: How often a request thread re-reads the row while it waits for the applier, which may be in another process.
POST_ROW_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class ApplierDispatch:
    """What the applier's dispatch of one intent carries into `invoke_command` (design D4).

    The dispatch claims `key` with `digest`, the caller's original input, whatever arguments the leaf
    receives. It carries no accept window, so it is exempt from `intents_queued` on every thread that
    copies this context. `stop` is the applier's stop flag, read before each boundary acquire.
    """

    intent_id: str
    key: str
    digest: str
    window_seconds: float
    request_id: str
    stop: threading.Event


_APPLIER_DISPATCH: ContextVar[ApplierDispatch | None] = ContextVar("exomem_applier_dispatch", default=None)


def current_applier_dispatch() -> ApplierDispatch | None:
    return _APPLIER_DISPATCH.get()


@contextmanager
def applier_dispatch(dispatch: ApplierDispatch) -> Iterator[ApplierDispatch]:
    token = _APPLIER_DISPATCH.set(dispatch)
    try:
        yield dispatch
    finally:
        _APPLIER_DISPATCH.reset(token)


def refuse_if_applier_stopping() -> None:
    """The applier takes no boundary once its stop flag is set; the intent returns to `accepted` (design D4, shutdown)."""
    dispatch = _APPLIER_DISPATCH.get()
    if dispatch is not None and dispatch.stop.is_set():
        raise OpError(
            "MUTATION_BUSY",
            "the write applier is stopping",
            "The accepted write stays queued and the next applier commits it.",
            details={"status": "retryable", "committed": False, "cause": "applier_stopping"},
        )


# An applier in this process signals here when an intent ends, so a request thread inside its accept
# window answers with the committed terminal at once. Another process's applier is found by polling.
_PROGRESS = threading.Condition()
_progress_count = 0


def notify_applier_progress() -> None:
    global _progress_count
    with _PROGRESS:
        _progress_count += 1
        _PROGRESS.notify_all()


def wait_for_applier_progress(timeout: float) -> None:
    """Sleep until an in-process applier ends an intent or `timeout` passes, whichever is first."""
    with _PROGRESS:
        seen = _progress_count
        _PROGRESS.wait_for(lambda: _progress_count != seen, timeout=timeout)


def mode() -> str:
    raw = os.environ.get(MODE_ENV, "off").strip().lower()
    if raw in MODES:
        return raw
    log.warning("%s=%r is not one of %s; the journal stays off", MODE_ENV, raw, "/".join(MODES))
    return "off"


def eligible(command: Any, kwargs: Mapping[str, Any]) -> bool:
    """Whether this call of a write command may become an intent.

    The registry declares the command (`intent_eligible`), the modes of a mixed command and the argument
    that makes a call carry inputs the journal cannot stage yet.
    """
    if command.intent_eligible is not True:
        return False
    if command.intent_modes is not None:
        selector, values = command.intent_modes
        if kwargs.get(selector) not in values:
            return False
    staged = command.intent_staged_argument
    return not (staged is not None and kwargs.get(staged))


def deadline_at(arrival: float | None = None) -> float:
    """The accept deadline on the monotonic clock: the earlier of 2.0 s after arrival and the request budget less its reserve."""
    started = time.monotonic() if arrival is None else arrival
    at = started + ACCEPT_DEADLINE_SECONDS
    budget = request_budget.current()
    if budget is not None:
        at = min(at, budget.deadline - request_budget.DELIVERY_RESERVE_SECONDS)
    return at


@contextmanager
def arrival(command: Any, kwargs: Mapping[str, Any], *, read_only: bool) -> Iterator[None]:
    """Open the accept window at the arrival of an eligible write; a nested call keeps its caller's window."""
    if read_only or mode() == "off" or not eligible(command, kwargs):
        yield
        return
    if current_applier_dispatch() is not None:
        # The applier waits as long as the boundary takes; an accept deadline would journal the intent again.
        yield
        return
    if mutation_lock.current_accept_window() is not None:
        yield
        return
    with mutation_lock.accept_window(mutation_lock.AcceptWindow(deadline_at())):
        yield


def depends_on_queued(
    open_journal: Callable[[], write_intents.WriteIntentJournal | None], command: Any, kwargs: Mapping[str, Any]
) -> bool:
    """Whether this write may depend on an accepted one: same principal audience, or a page it targets (design D2, step 3).

    Validating such a write against the vault now would refuse a link or an edit that only the waiting write makes valid.
    An unreadable journal reports no dependency; the write then runs inline.
    """
    from .governance.principal import effective_principal

    try:
        journal = open_journal()
        queued = [] if journal is None else journal.non_terminal()
    except write_intents.JournalRefusal:
        return False
    audience = effective_principal().audience_id
    pages = target_pages(command, kwargs)
    return any(
        intent.audience == audience or not pages.isdisjoint(intent.planned_paths or ()) for intent in queued
    )


def principal_snapshot(principal: Any) -> dict[str, Any]:
    """Every field of the request principal; none of them holds a bearer, password or session secret."""
    values = dataclasses.asdict(principal)
    values.pop("administrative_ingress", None)
    return values


def target_pages(command: Any, kwargs: Mapping[str, Any]) -> frozenset[str]:
    """The pages a call names through the arguments the registry marks as paths."""
    return frozenset(
        value
        for role in command.path_roles
        if isinstance((value := kwargs.get(role.argument)), str) and value
    )


def intents_queued() -> OpError:
    """The internal refusal of an attempt that would overtake an accepted write. The caller never sees it."""
    return OpError(
        "MUTATION_BUSY",
        "an accepted write is waiting for the vault",
        "Wait for the accepted write to commit.",
        details={"status": "retryable", "committed": False, "cause": "intents_queued"},
    )


def is_precommit_refusal(error: BaseException) -> bool:
    """Whether the error ended an attempt before anything committed, so the write may be journaled instead."""
    if not isinstance(error, OpError):
        return False
    # Two closed protocol error codes that the writer raises before any effect; not a vocabulary.
    if error.code in ("MUTATION_WARMING", "REFERENCE_PREPARATION_UNAVAILABLE"):  # nosemgrep: ep-word-membership
        return True
    return error.code == "MUTATION_BUSY" and error.details.get("committed") is False


def journal_refusal(error: write_intents.JournalRefusal) -> OpError:
    """The one refusal accept makes: `journal_full` or `journal_unavailable`, retryable, nothing written."""
    return OpError(
        "MUTATION_BUSY",
        "the write journal cannot take this write",
        "Retry after retry_after_ms with the same idempotency key if the write carried one.",
        details={
            "status": "retryable",
            "committed": False,
            "cause": error.cause or write_intents.CAUSE_UNAVAILABLE,
            "retry_after_ms": 5000,
        },
    )


def accepted_envelope(
    intent: write_intents.Intent, *, receipt_id: str | None, idempotency_key: str | None
) -> dict[str, Any]:
    """The nonterminal success answer: the write is durable in the journal and not yet committed.

    `request_id` is the intent's dispatch id, so this answer, a resend's answer and the log row of the
    eventual commit name the same request."""
    from .mutation_terminal import marker_fields

    envelope: dict[str, Any] = {
        **marker_fields(),
        "ok": True,
        "state": "accepted",
        "status": "accepted",
        "terminal": False,
        "committed": False,
        "mutated": False,
        "intent_id": intent.intent_id,
        "intent_ref": f"exomem://write/{intent.intent_id}",
        "request_id": intent.request_id,
        "receipt_id": receipt_id,
        "validation": intent.validation,
        "planned_paths": list(intent.planned_paths or []),
        "warnings_count": 0,
        "next_action": (
            f'review_memory(mode="write-intent", ref="exomem://write/{intent.intent_id}", wait_seconds=30)'
        ),
        "poll_after_ms": POLL_AFTER_MS,
    }
    if idempotency_key is not None:
        envelope["idempotency_key"] = idempotency_key
    return envelope


def answer_for(intent: write_intents.Intent, *, receipt_id: str | None, idempotency_key: str | None) -> Any:
    """What a resend under an intent's identity receives: its terminal once it has one, else its state."""
    if intent.terminal is not None and intent.state in write_intents.TERMINAL_STATES:
        return intent.terminal
    if intent.active:
        return accepted_envelope(intent, receipt_id=receipt_id, idempotency_key=idempotency_key)
    # `conflicted` waits for an owner decision. It is not `needs_review`, the draft handshake.
    envelope = accepted_envelope(intent, receipt_id=receipt_id, idempotency_key=idempotency_key)
    envelope.update(state=intent.state, status=intent.state, next_action="Ask the vault owner to decide this write.")
    envelope.pop("poll_after_ms")
    return envelope


# The arguments that bind a write to the state its caller saw (design D3). The command schemas fix these
# names; a later rebase removes exactly these guards.
_BASIS_ARGUMENTS = (  # nosemgrep: ep-word-set -- closed argument names that the command schemas fix
    "expected_hash",
    "draft_token",
    "draft_hash",
    "transition_token",
    "semantic_transition_token",
    "vocabulary_fingerprint",
)
# A call that carries a reviewed draft was previewed already, and its preview arguments do not repeat.
_DRAFT_ARGUMENTS = ("draft_token", "draft_hash", "draft_id")  # nosemgrep: ep-word-set -- closed argument names that the command schemas fix


@dataclass
class Accept:
    """One eligible write's accept state: what it needs to be journaled and what the journal answered."""

    window: mutation_lock.AcceptWindow
    command: Any
    injected: tuple[Any, ...]
    kwargs: dict[str, Any]
    key: str | None
    digest: str
    expires_after: float | None
    open_journal: Callable[[], write_intents.WriteIntentJournal | None]
    #: The lease manager that serves this call; a `validate_only` preview runs under it.
    manager: Any
    intent: write_intents.Intent | None = None
    #: The error this write ends with when it is not accepted: `journal_full`, `journal_unavailable`, or a validation error.
    refusal: BaseException | None = None
    created: bool = False
    waited_ms: float | None = None

    @property
    def settled(self) -> bool:
        return self.intent is not None or self.refusal is not None

    def journal(self, *, deferred: bool, narrow: bool, cause: BaseException | None = None) -> None:
        """Record this write as an intent unless it already is one. Never raises: the result is `intent` or `refusal`."""
        if self.settled:
            return
        self.waited_ms = None if cause is None else mutation_journal.boundary_wait_ms(cause)
        try:
            validation = "deferred" if deferred else self._validate_before_accept(narrow=narrow)
        except Exception as invalid:  # noqa: BLE001 - the command's own validation error answers the caller
            self.refusal = invalid
            return
        from .governance.principal import effective_principal

        surface = capabilities.current_active_surface()
        try:
            journal = self.open_journal()
            if journal is None:
                raise write_intents.JournalUnavailable("this path is not a vault root")
            principal = effective_principal()
            self.intent, self.created = journal.accept(
                audience=principal.audience_id,
                command=self.command.name,
                arguments=self.kwargs,
                surface=getattr(surface, "surface", None) or "library",
                principal=principal_snapshot(principal),
                digest=self.digest,
                retry_key=self.key,
                window_seconds=self.expires_after,
                base_guards={name: self.kwargs[name] for name in _BASIS_ARGUMENTS if name in self.kwargs},
                validation=validation,
                planned_paths=sorted(target_pages(self.command, self.kwargs)),
            )
        except write_intents.JournalRefusal as refused:
            self.refusal = journal_refusal(refused)
        except (TypeError, ValueError) as unstorable:
            # Arguments the journal cannot serialise cannot be replayed later either.
            log.warning("a write's arguments cannot be journaled: %s", type(unstorable).__name__)
            self.refusal = journal_refusal(write_intents.JournalUnavailable("arguments cannot be stored"))

    def _validate_before_accept(self, *, narrow: bool) -> str:
        """`complete` when the command's own validation passed before accept, else `deferred`.

        A narrowed command ran its preflight before its commit seam, so a refusal at the seam means it
        passed. A wide command meets the boundary before its leaf: it runs its `validate_only` preview
        here, outside the boundary, or else its validation waits for the apply (design D2).
        """
        if narrow:
            return "complete"
        names = {param.name for param in self.command.params}
        if "validate_only" not in names or any(name in self.kwargs for name in _DRAFT_ARGUMENTS):
            return "deferred"
        from . import writer_lease

        token = writer_lease._ACTIVE_LEASE_MANAGER.set(self.manager)
        try:
            self.command.leaf(*self.injected, **{**self.kwargs, "validate_only": True})
        except OpError as error:
            if is_precommit_refusal(error):
                return "deferred"
            raise
        finally:
            writer_lease._ACTIVE_LEASE_MANAGER.reset(token)
        return "complete"
