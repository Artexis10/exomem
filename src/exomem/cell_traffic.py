"""A Cloud cell's hourly traffic counts (automate-cloud-releases D4).

The cell counts each request to its MCP path per UTC hour, with the seconds it
ran in that hour, and keeps them in one small file in its state root, under its
custody home and outside the vault. The platform operator reads them from the health-only port to pick
the cell's quiet upgrade hour. The counts are advisory: no failure here stops
the cell, and an unreadable file starts a new ring.

The counts are content-free: a number of requests and a number of seconds per
hour, never a path, a tool name or a body.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

FILE_NAME = "cell-traffic.json"
# 28 days of hourly buckets. The ring size lives only here; the history answer
# reports it as `retention_hours`, and cellctl clamps its lookback to it.
RETENTION_HOURS = 28 * 24
# A crash loses at most this much of the counts.
FLUSH_INTERVAL_SECONDS = 60.0
HOUR_SECONDS = 3600
FORMAT_VERSION = 1


class CellTraffic:
    """The ring of hourly buckets, the requests in flight and the newest request's time."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._inflight = 0
        self._last_request_at: float | None = None
        now = clock()
        self._first_hour, self._requests, self._seconds = self._load(int(now // HOUR_SECONDS))
        # Seconds accrue from here into the hour buckets.
        self._run_mark = now
        self._last_flush = monotonic()

    def _load(self, current_hour: int) -> tuple[int, list[int], list[int]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            first = data["first_hour"]
            requests = data["requests"]
            seconds = data["observed_seconds"]
            if (
                data.get("version") != FORMAT_VERSION
                or type(first) is not int
                or not isinstance(requests, list)
                or not isinstance(seconds, list)
                or len(requests) != len(seconds)
                or not all(type(value) is int and value >= 0 for value in (*requests, *seconds))
                or any(value > HOUR_SECONDS for value in seconds)
                or first + len(requests) > current_hour + 1
            ):
                raise ValueError("counts file shape")
        except FileNotFoundError:
            return current_hour, [0], [0]
        # RecursionError: deep nesting in a damaged file must not stop the start.
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            log.warning("event=cell_traffic_reset the hourly counts file was unreadable; new counts start")
            return current_hour, [0], [0]
        return first, list(requests), list(seconds)

    def _advance(self, hour: int) -> None:
        """Extend the ring to `hour`, with not-observed buckets for any gap, and drop
        buckets older than the retention."""

        last = self._first_hour + len(self._requests) - 1
        if hour > last:
            gap = hour - last
            self._requests.extend([0] * gap)
            self._seconds.extend([0] * gap)
        excess = len(self._requests) - RETENTION_HOURS
        if excess > 0:
            del self._requests[:excess]
            del self._seconds[:excess]
            self._first_hour += excess

    def _accrue(self, now: float) -> None:
        """Add the seconds since the run mark to the hours they fell in."""

        start = self._run_mark
        while start < now:
            hour = int(start // HOUR_SECONDS)
            end = min(now, (hour + 1) * HOUR_SECONDS)
            self._advance(hour)
            index = hour - self._first_hour
            if index >= 0:
                # Whole seconds crossed, not a rounded segment: requests less than a
                # second apart still add up, and hour boundaries are whole seconds.
                crossed = int(end) - int(start)
                self._seconds[index] = min(HOUR_SECONDS, self._seconds[index] + crossed)
            start = end
        self._run_mark = max(self._run_mark, now)
        self._advance(int(now // HOUR_SECONDS))

    def request_started(self) -> None:
        with self._lock:
            now = self._clock()
            self._accrue(now)
            self._requests[int(now // HOUR_SECONDS) - self._first_hour] += 1
            self._inflight += 1
            self._last_request_at = self._monotonic()

    def request_finished(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self._last_request_at = self._monotonic()

    def readiness_fields(self) -> dict[str, object]:
        """The two fields the readiness `traffic` block gains; never the history."""

        with self._lock:
            last = self._last_request_at
            return {
                "inflight": self._inflight,
                "last_request_age_seconds": (
                    round(max(0.0, self._monotonic() - last), 3) if last is not None else None
                ),
            }

    def history(self) -> dict[str, object]:
        """The `/health/traffic-history` answer: the retention, the first bucket's
        UTC hour and two integer arrays, one bucket per hour up to the current one."""

        with self._lock:
            self._accrue(self._clock())
            return {
                "retention_hours": RETENTION_HOURS,
                "first_hour": datetime.fromtimestamp(self._first_hour * HOUR_SECONDS, UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "requests": list(self._requests),
                "observed_seconds": list(self._seconds),
            }

    def flush_due(self) -> bool:
        return self._monotonic() - self._last_flush >= FLUSH_INTERVAL_SECONDS

    def flush(self) -> None:
        """Replace the counts file atomically. A failure is logged and changes nothing else."""

        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                self._accrue(self._clock())
                payload = {
                    "version": FORMAT_VERSION,
                    "first_hour": self._first_hour,
                    "requests": list(self._requests),
                    "observed_seconds": list(self._seconds),
                }
                self._last_flush = self._monotonic()
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            from .governance.transaction import fsync_directory

            fsync_directory(self.path.parent)
        except OSError:
            log.warning("event=cell_traffic_flush_failed the hourly counts were not saved")
        finally:
            self._flush_lock.release()


class CountMcpRequests:
    """ASGI middleware that counts each HTTP request to the MCP path. The kubelet's
    and cellctl's probes reach other paths, so they never count."""

    def __init__(self, app, *, traffic: CellTraffic, path: str) -> None:
        self.app = app
        self.traffic = traffic
        self.path = path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path", "").rstrip("/") != self.path:
            await self.app(scope, receive, send)
            return
        try:
            self.traffic.request_started()
        except Exception:  # noqa: BLE001 - the counts are advisory and never refuse a request
            log.debug("cell traffic count failed", exc_info=True)
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.traffic.request_finished()
            schedule_flush(self.traffic)


def schedule_flush(traffic: CellTraffic) -> None:
    """Save the counts off the event loop when a minute has passed since the last save:
    the write ends with an fsync. Readiness probes call it too, so an idle cell still
    saves its observed seconds."""

    if traffic.flush_due():
        asyncio.get_running_loop().run_in_executor(None, traffic.flush)


def shutdown_lifespan(inner, traffic: CellTraffic):
    """Save the counts and flush the state root after every inner lifespan closes.

    Each vault writer supplies its own durability barrier before acknowledging a write.
    """

    @contextlib.asynccontextmanager
    async def _lifespan(server):
        try:
            async with inner(server) as state:
                yield state
        finally:
            traffic.flush()
            # A synced SQLite WAL replays on the next open. Only this state root's files: `os.sync`
            # would wait on every filesystem the host mounts.
            from . import cloud_import

            try:
                cloud_import.flush_to_disk(traffic.path.parent)
            except OSError:
                log.warning("event=cell_state_flush_failed the state root was not fully flushed")

    return _lifespan
