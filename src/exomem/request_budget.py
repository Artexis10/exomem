"""The request-scoped deadline an MCP tool call carries, and its reserves.

Deliberately a leaf, for the same reason `call_spans` is one: the stages that
have to consult the budget live in `find`, `context_pack` and `writer_lease`,
and a budget that could only be imported by `command_surface` would be a bound
nothing on the hot path could read.

What this exists to prevent is work that completes after the caller has gone.
The serving host's ledger carries `ask_memory` rows running minutes against a
client that waits sixty seconds: the work finished, the conversation had
already marked the tool dead, and nothing in the row said which stage spent the
time. What it costs when it fires wrongly is a narrower result for a call that
would have finished a few seconds late — and that result says so, in a `budget`
block naming exactly what was left out, so the caller can re-ask narrower. The
payer is the caller who asked for a heavy option, which is the right payer.

The numbers below are PROVISIONAL. They were chosen from the ledger's stage
spans, and the `recall.*` spans this change adds are the instrument for tuning
them; the first live week's rows are the follow-up, not another guess.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Mapping
from contextvars import ContextVar

_log = logging.getLogger(__name__)

#: The origin's own bound, below both the ChatGPT connector's 60-second tool
#: timeout and the direct tunnel's 100-second cap. A configurable edge should
#: also time out before the client; the origin must leave room for delivery
#: before whichever outer timeout expires first.
MCP_REQUEST_BUDGET_SECONDS = 50.0
#: Kept back from the deadline for serializing and delivering a terminal, so a
#: committed write is reported rather than raced.
DELIVERY_RESERVE_SECONDS = 5.0
#: Warm cross-encoder over the rerank candidate prefix.
RERANK_RESERVE_SECONDS = 8.0
#: The reranker singleton is reaped after 15 idle minutes and reloaded
#: synchronously inside the next request, so a cold rerank is a model load plus
#: the scoring — a different order of cost, and the reserve has to say so.
RERANK_COLD_RESERVE_SECONDS = 25.0
#: Deep context pack assembly.
PACK_RESERVE_SECONDS = 10.0
#: Graph neighbourhood enrichment inside the pack.
GRAPH_ENRICH_RESERVE_SECONDS = 5.0

#: Operators may lengthen or shorten the origin budget. Clients may not: the
#: client that most needs the bound is the one that cannot be trusted to set
#: it, and a per-call knob would move the tool-surface fingerprint for a
#: control almost nobody should touch.
BUDGET_ENV_VAR = "EXOMEM_MCP_REQUEST_BUDGET_SECONDS"

#: Reconcile-class commands are exempt: their terminal IS the derived state and
#: `wait_for_graph_sync` joins unbounded by design. Same predicate the writer
#: lease already applies at `joins_unbounded_graph`, kept here so the two
#: cannot drift into disagreeing about what "reconcile-class" means.
RECONCILE_CLASS_TOOLS = frozenset({"reconcile"})
RECONCILE_MODE_TOOLS = frozenset({"maintain_memory"})

_BUDGET: ContextVar[RequestBudget | None] = ContextVar(
    "exomem_request_budget", default=None
)

_RESOLUTION_LOCK = threading.Lock()
#: `(raw env value, resolved seconds)` for the last value resolved. The cache
#: is what makes the malformed-value warning ONE warning rather than one per
#: call: a typo in a deployed variable must be visible, not a log flood.
_RESOLVED: tuple[str | None, float] | None = None


def reset_resolution_cache() -> None:
    """Drop the memoized override. For tests that change the environment."""
    global _RESOLVED
    with _RESOLUTION_LOCK:
        _RESOLVED = None


def budget_seconds(env: Mapping[str, str] | None = None) -> float:
    """The configured origin budget, falling back to the default on nonsense."""
    global _RESOLVED
    source = os.environ if env is None else env
    raw = source.get(BUDGET_ENV_VAR)
    with _RESOLUTION_LOCK:
        cached = _RESOLVED
        if cached is not None and cached[0] == raw:
            return cached[1]
        resolved = MCP_REQUEST_BUDGET_SECONDS
        if raw is not None:
            try:
                candidate = float(str(raw).strip())
            except (TypeError, ValueError):
                candidate = float("nan")
            if math.isfinite(candidate) and candidate > 0.0:
                resolved = candidate
            else:
                _log.warning(
                    "%s=%r is not a positive number of seconds; "
                    "falling back to the %.1fs default",
                    BUDGET_ENV_VAR,
                    raw,
                    MCP_REQUEST_BUDGET_SECONDS,
                )
        _RESOLVED = (raw, resolved)
        return resolved


def is_reconcile_class(tool: str | None, arguments: Mapping[str, object] | None) -> bool:
    """Whether this dispatch is a reconcile-class maintenance call."""
    name = str(tool or "")
    if name in RECONCILE_CLASS_TOOLS:
        return True
    if name in RECONCILE_MODE_TOOLS and isinstance(arguments, Mapping):
        return str(arguments.get("mode") or "") == "reconcile"
    return False


class RequestBudget:
    """What is left of one MCP call's wall clock, and what it already cost.

    Mutable and shared by reference on purpose. A stage that skips itself runs
    deep inside FastMCP's threadpool, where setting a ContextVar would not
    propagate back to the middleware that writes the ledger row — but mutating
    the object the copied context already points at does. That is the same
    bridge the failure breadcrumb and the phase timers use, for the same
    reason, and it is why `note_skipped` mutates rather than rebinding.
    """

    __slots__ = ("seconds", "deadline", "_skipped", "_truncated", "_lock")

    def __init__(self, *, seconds: float, entry: float | None = None) -> None:
        self.seconds = float(seconds)
        started = time.monotonic() if entry is None else float(entry)
        self.deadline = started + self.seconds
        self._skipped: list[str] = []
        self._truncated: list[str] = []
        self._lock = threading.Lock()

    def remaining(self) -> float:
        """Seconds left, clamped at zero: a passed deadline is not negative time."""
        return max(0.0, self.deadline - time.monotonic())

    def can_afford(self, reserve: float) -> bool:
        """Whether a stage costing at most `reserve` seconds still fits."""
        return self.remaining() >= float(reserve)

    def note_skipped(self, stage: str) -> None:
        """Record a stage that never started because it could not be afforded."""
        name = str(stage)
        with self._lock:
            if name not in self._skipped:
                self._skipped.append(name)

    def note_truncated(self, stage: str) -> None:
        """Record a stage that started, hit the deadline, and stopped early."""
        name = str(stage)
        with self._lock:
            if name not in self._truncated:
                self._truncated.append(name)

    @property
    def skipped(self) -> list[str]:
        with self._lock:
            return list(self._skipped)

    @property
    def truncated(self) -> list[str]:
        with self._lock:
            return list(self._truncated)

    def yielded(self) -> bool:
        """Whether the budget actually cost the caller anything."""
        with self._lock:
            return bool(self._skipped or self._truncated)

    def as_response_block(self) -> dict[str, object] | None:
        """The advisory `budget` block, or None when nothing was left out.

        Omitted when nothing was skipped so the default response shape is
        byte-identical to the unbudgeted one: a block that always appeared
        would be a schema change for every client, to say nothing happened.
        """
        if not self.yielded():
            return None
        return {
            "applied": True,
            "seconds": self.seconds,
            "remaining_ms_at_return": int(self.remaining() * 1000.0),
            "skipped": self.skipped,
            "truncated": self.truncated,
        }

    def as_ledger_block(self) -> dict[str, object]:
        """The row's `budget`: names and milliseconds, never query content.

        Unconditional, unlike the response block. The asymmetry is deliberate:
        the response block is a message to a client and must not appear to
        report a problem where there was none, while the row is an operator's
        instrument for tuning the PROVISIONAL reserves — and "50 seconds, 400
        left, nothing skipped" is exactly the reading that says a reserve is
        about to start costing calls.
        """
        return {
            "seconds": self.seconds,
            "remaining_ms": int(self.remaining() * 1000.0),
            "skipped": self.skipped,
        }


def current() -> RequestBudget | None:
    """The in-flight call's budget, or None off the MCP dispatch path."""
    return _BUDGET.get()


def set_current(budget: RequestBudget | None):
    """Bind a budget for the current context; returns the reset token."""
    return _BUDGET.set(budget)


def reset_current(token) -> None:
    _BUDGET.reset(token)


def can_afford(reserve: float) -> bool:
    """True when there is no budget at all, or the reserve fits inside it."""
    budget = current()
    return True if budget is None else budget.can_afford(reserve)
