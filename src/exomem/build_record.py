"""What an `index_warming` answer waits on, and what that build is doing.

Each check that decides warming names its cause for the request (`note_warming`), and the
abstained packet carries it. A `BuildRecord` holds each single-flight background build's start
and failures, so the cause can say whether its build runs, failed or never started. Without
that, a warm-up that fails on every retry, or hangs, looks the same as one about to finish.
Both live in process memory, like the builds they describe.
"""

from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable, Hashable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

# The diagnostic's states are protocol values for the reader, not vault vocabulary.
READY = "ready"
BUILDING = "building"
FAILED = "failed"
NOT_STARTED = "not_started"

#: What an `index_warming` answer waited on: one protocol value per place that decides warming.
WAITING_ON_RECALL_SEED = "recall_seed"
WAITING_ON_RECALL_REGISTRY = "recall_registry"
WAITING_ON_IDENTITY_INVENTORY = "identity_inventory"
WAITING_ON_ACTIVATION_INDEX = "activation_index"
WAITING_ON_CONTEXT_CATALOGUE = "context_catalogue"
WAITING_ON_CONTEXT_LANE = "context_lane"

#: The cause the request's last warming decision named. Each site that decides warming sets it, so
#: the answer states that site's own reason and never a cause re-derived after the fact.
_WARMING_CAUSE: ContextVar[dict[str, object] | None] = ContextVar("exomem_warming_cause", default=None)

_F = TypeVar("_F", bound=Callable[..., Any])


def note_warming(
    waiting_on: str, build: Mapping[str, object] | None = None, *, site: str | None = None
) -> None:
    """Record what this request's `index_warming` answer waits on; a later decision replaces it.

    `build` is that warm-up's `BuildRecord.state`; `site` is a recall registry refusal site.
    """
    cause: dict[str, object] = {"waiting_on": waiting_on}
    if site is not None:
        cause["site"] = site
    if build is not None:
        cause["build"] = dict(build)
    _WARMING_CAUSE.set(cause)


def warming_cause() -> dict[str, object] | None:
    """The diagnostics part for this request's `index_warming` answer, or None when no site named one."""
    cause = _WARMING_CAUSE.get()
    return None if cause is None else dict(cause)


def warming_scoped(function: _F) -> _F:
    """Start and end each call with no warming cause, so no request reads another's."""

    @functools.wraps(function)
    def scoped(*args: Any, **kwargs: Any) -> Any:
        token = _WARMING_CAUSE.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            _WARMING_CAUSE.reset(token)

    return scoped  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class _Failure:
    #: The exception class only: a message can name a private reserved path.
    error: str
    at: float
    #: Consecutive failures since the last build that succeeded.
    count: int
    job: str | None


class BuildRecord:
    """Start times and consecutive failures of single-flight builds, per key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started: dict[Hashable, tuple[float, str | None]] = {}
        self._failures: dict[Hashable, _Failure] = {}

    def started(self, key: Hashable, *, job: str | None = None) -> None:
        """`job` names the work when several kinds share one key's slot."""
        with self._lock:
            self._started[key] = (time.monotonic(), job)

    def succeeded(self, key: Hashable, *, job: str | None = None) -> None:
        """Clears the last failure only when the same job failed; another job's success proves nothing of it."""
        with self._lock:
            self._started.pop(key, None)
            failure = self._failures.get(key)
            if failure is not None and failure.job == job:
                del self._failures[key]

    def failed(self, key: Hashable, error: BaseException, *, job: str | None = None) -> None:
        with self._lock:
            self._started.pop(key, None)
            prior = self._failures.get(key)
            count = prior.count + 1 if prior is not None and prior.job == job else 1
            self._failures[key] = _Failure(type(error).__name__, time.monotonic(), count, job)

    def ended(self, key: Hashable) -> None:
        """A build stopped without an outcome, such as a shutdown; its last failure stands."""
        with self._lock:
            self._started.pop(key, None)

    def state(self, key: Hashable, *, ready: bool) -> dict[str, object]:
        """`state`, the `seconds` and `job` of a running build, and the `last_failure` since the last success."""
        now = time.monotonic()
        with self._lock:
            started = self._started.get(key)
            failure = self._failures.get(key)
        if ready:
            result: dict[str, object] = {"state": READY}
        elif started is not None:
            result = {"state": BUILDING, "seconds": round(now - started[0], 1)}
            if started[1] is not None:
                result["job"] = started[1]
        else:
            result = {"state": FAILED if failure is not None else NOT_STARTED}
        if failure is not None and not ready:
            result["last_failure"] = {
                "error": failure.error,
                "seconds_ago": round(now - failure.at, 1),
                "failures": failure.count,
                **({"job": failure.job} if failure.job is not None else {}),
            }
        return result
