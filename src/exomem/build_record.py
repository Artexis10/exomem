"""What one process's single-flight background builds are doing, for a warming diagnostic.

A request that answers `index_warming` names the build it waits on. Without that a
warm-up that fails on every retry, or hangs, looks the same as one about to finish.
The record lives in process memory, like the builds it describes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Hashable
from dataclasses import dataclass

# The diagnostic's states are protocol values for the reader, not vault vocabulary.
READY = "ready"
BUILDING = "building"
FAILED = "failed"
NOT_STARTED = "not_started"


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
