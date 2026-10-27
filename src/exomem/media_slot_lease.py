"""Node media-slot lease: caps heavy media work across the cells of one node.

Each cell's brakes read its own cgroup (`media_brakes`), so they cannot see the other
cells. This service holds the one rule they cannot: the anonymous-memory budgets it has
granted add up to at most the node media pool. See `cloud-multimodal-processing` D10.

The service keeps state in memory only. It sees a cell ID, the names of the engines the next
job loads (for the log), the byte count and the job timeout of that job, and timing. The cell
states the bytes and the timeout, because its own settings define them; the service bounds
both against its configuration and holds no copy of the cell's budgets.
A cell that gets no grant waits as a memory-blocked job waits, so a restart costs delay
and never a double grant: after it starts, the service grants nothing for one longest lease.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .media_slot_client import (
    ACQUIRE_PATH,
    POLL_PATH,
    RELEASE_PATH,
    SLOT_PORT,
)

log = logging.getLogger(__name__)

POOL_ENV = "EXOMEM_MEDIA_SLOT_POOL_MIB"
QUEUE_ENV = "EXOMEM_MEDIA_SLOT_QUEUE_SECONDS"
MARGIN_ENV = "EXOMEM_MEDIA_SLOT_MARGIN_SECONDS"
MAX_JOB_ENV = "EXOMEM_MEDIA_SLOT_MAX_JOB_SECONDS"

_MIB = 1024 * 1024
# The shape of a cell ID (cloud_import_job checks the same); the network policy bounds who may send one.
_CELL_ID = re.compile(r"[a-z2-7]{16}")
# A shape check, not a vocabulary: the engine name only reaches the log, so it may not carry a
# newline or a forged log line. Which name means what is for the operator reading it.
_ENGINE = re.compile(r"[a-z0-9_-]{1,64}")
_MAX_LEASE_CHARS = 64
# Transport bounds, not policy.
_REQUEST_BYTES = 1024
_REQUEST_SECONDS = 5.0
_MAX_ENTRIES = 1024


@dataclass(frozen=True)
class Config:
    pool_bytes: int
    #: How long a waiting cell may go without polling, or a grant without being collected.
    queue_seconds: float
    #: The longest job timeout a cell may state. A longer one is refused, not shortened:
    #: a lease shorter than the cell's watchdog would let the child outlive its slot.
    max_job_seconds: float
    #: Added to the job timeout so the watchdog stops a child, and the cell releases, first.
    margin_seconds: float

    @property
    def max_lease_seconds(self) -> float:
        return self.max_job_seconds + self.margin_seconds

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        values = os.environ if env is None else env

        def number(name: str, floor: float = 0) -> float:
            try:
                value = float(values[name])
            except (KeyError, ValueError):
                raise ValueError(f"{name} must be a number above {floor}") from None
            if not floor < value < float("inf"):
                raise ValueError(f"{name} must be a number above {floor}")
            return value

        return cls(
            pool_bytes=int(number(POOL_ENV, floor=1)) * _MIB,
            queue_seconds=number(QUEUE_ENV),
            max_job_seconds=number(MAX_JOB_ENV),
            margin_seconds=number(MARGIN_ENV),
        )


def _positive_number(value: object) -> bool:
    """A JSON number above zero; a flag, NaN and a string are not numbers here."""
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


class Refused(Exception):
    def __init__(self, status: int, code: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


@dataclass(frozen=True)
class Slot:
    lease: str
    granted: bool


@dataclass
class _Entry:
    lease: str
    #: The engines the cell's job loads, as the cell names them; it only reaches the log.
    engine: str
    #: The source address the first request came from; one entry per address.
    source: str
    budget: int
    #: The job timeout the cell stated plus the margin: how long a granted slot lasts.
    lease_seconds: float
    #: The last request or poll of the cell, or the grant that made the cell's turn.
    seen: float
    #: The grant, until a reply tells the cell about it; then that moment, because the cell
    #: starts its child, and so its watchdog, when it learns of the grant.
    granted: float | None = None
    collected: bool = False


class SlotLedger:
    """Strict first-in first-out queue of cells, one entry each, over a fixed pool.

    The methods are synchronous and hold no await point, so the ASGI event loop owns all
    state without a lock. The clock is injected so the rules can be tested without sleeping.
    """

    def __init__(self, config: Config, clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._clock = clock
        #: Insertion order is the queue order; a cell that re-queues goes to the back, which
        #: with the lease length as a turn makes the service round-robin across cells.
        self._entries: dict[str, _Entry] = {}
        # A child started under an earlier service may still run. Its lease began no later
        # than this moment and lasts at most the longest lease, so grant nothing until then.
        self._hold_until = clock() + config.max_lease_seconds

    def acquire(
        self, cell: str, source: str, engine: str, budget: int, job_seconds: float
    ) -> Slot:
        now = self._clock()
        self._settle(now)
        if cell in self._entries:
            raise Refused(409, "SLOT_ENTRY_EXISTS", "this cell already has a queued or held slot")
        if any(entry.source == source for entry in self._entries.values()):
            raise Refused(409, "SLOT_SOURCE_BUSY", "this address already has a queued or held slot")
        if budget > self._config.pool_bytes:
            # No wait can fit it, so say so rather than block the queue behind it.
            raise Refused(
                422,
                "SLOT_ABOVE_POOL",
                f"{engine} asked for {budget // _MIB} MiB; the media pool is "
                f"{self._config.pool_bytes // _MIB} MiB",
            )
        if job_seconds > self._config.max_job_seconds:
            raise Refused(
                422,
                "SLOT_JOB_TOO_LONG",
                f"{engine} stated a {job_seconds:g} s job timeout; the longest allowed is "
                f"{self._config.max_job_seconds:g} s",
            )
        if len(self._entries) >= _MAX_ENTRIES:
            raise Refused(503, "SLOT_LEDGER_FULL", "the media slot queue is full")
        self._entries[cell] = entry = _Entry(
            secrets.token_urlsafe(16),
            engine,
            source,
            budget,
            job_seconds + self._config.margin_seconds,
            now,
        )
        self._settle(now)
        return self._slot(entry, now)

    def poll(self, cell: str, lease: str) -> Slot:
        now = self._clock()
        self._settle(now)
        entry = self._owned(cell, lease)
        entry.seen = now
        self._settle(now)
        return self._slot(entry, now)

    def release(self, cell: str, lease: str) -> bool:
        """Drop the cell's entry if `lease` is the one issued to it; whether it did."""
        now = self._clock()
        self._settle(now)
        try:
            self._owned(cell, lease)
        except Refused:
            return False
        del self._entries[cell]
        log.info("media slot released by %s", cell)
        self._settle(now)
        return True

    def _owned(self, cell: str, lease: str) -> _Entry:
        entry = self._entries.get(cell)
        if entry is None or not secrets.compare_digest(entry.lease, lease):
            raise Refused(404, "SLOT_UNKNOWN", "no such slot for this cell")
        return entry

    def _slot(self, entry: _Entry, now: float) -> Slot:
        if entry.granted is None:
            return Slot(entry.lease, False)
        if not entry.collected:
            entry.granted, entry.collected = now, True
        return Slot(entry.lease, True)

    def _settle(self, now: float) -> None:
        """Drop what expired, then grant down the queue while the head fits the pool."""
        for cell, entry in list(self._entries.items()):
            if entry.granted is not None and now >= entry.granted + entry.lease_seconds:
                reason = "expired"
            elif (entry.granted is None or not entry.collected) and (
                now - entry.seen > self._config.queue_seconds
            ):
                reason = "was not polled"
            else:
                continue
            del self._entries[cell]
            log.info("media slot of %s %s", cell, reason)
        if now < self._hold_until:
            return
        used = sum(entry.budget for entry in self._entries.values() if entry.granted is not None)
        for cell, entry in self._entries.items():
            if entry.granted is not None:
                continue
            if used + entry.budget > self._config.pool_bytes:
                break  # strict order: a head that does not fit blocks the cells behind it
            entry.granted, entry.seen = now, now
            used += entry.budget
            log.info("media slot granted to %s for %s", cell, entry.engine)


class MediaSlotLease:
    """One-process ASGI service over a `SlotLedger`."""

    def __init__(self, config: Config, clock: Callable[[], float] = time.monotonic) -> None:
        self.ledger = SlotLedger(config, clock)

    async def _body(self, scope, receive) -> dict:
        lengths = [v for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
        if len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > _REQUEST_BYTES:
            raise Refused(400, "SLOT_BAD_REQUEST", "the request needs a short body")
        body = bytearray()
        async with asyncio.timeout(_REQUEST_SECONDS):
            while True:
                event = await receive()
                if event["type"] != "http.request":
                    raise Refused(400, "SLOT_BAD_REQUEST", "the request ended early")
                body.extend(event.get("body", b""))
                if len(body) > _REQUEST_BYTES:
                    raise Refused(400, "SLOT_BAD_REQUEST", "the request needs a short body")
                if not event.get("more_body", False):
                    break
        try:
            parsed = json.loads(body)
        except (ValueError, RecursionError):
            parsed = None
        if not isinstance(parsed, dict):
            raise Refused(400, "SLOT_BAD_REQUEST", "the request body is not a JSON object")
        return parsed

    def _handle(self, path: str, source: str, body: Mapping[str, object]) -> dict:
        cell = body.get("cell")
        if not isinstance(cell, str) or _CELL_ID.fullmatch(cell) is None:
            raise Refused(400, "SLOT_BAD_REQUEST", "the cell ID is not valid")
        if path == ACQUIRE_PATH:
            engine, anon_bytes, job_seconds = (
                body.get("engine"),
                body.get("anon_bytes"),
                body.get("timeout_seconds"),
            )
            if not isinstance(engine, str) or _ENGINE.fullmatch(engine) is None:
                raise Refused(400, "SLOT_BAD_REQUEST", "the engine name is not valid")
            if not _positive_number(anon_bytes) or not isinstance(anon_bytes, int):
                raise Refused(400, "SLOT_BAD_REQUEST", "the byte count is not a positive integer")
            if not _positive_number(job_seconds):
                raise Refused(400, "SLOT_BAD_REQUEST", "the job timeout is not a positive number")
            slot = self.ledger.acquire(cell, source, engine, anon_bytes, job_seconds)
        else:
            lease = body.get("lease")
            if not isinstance(lease, str) or not 0 < len(lease) <= _MAX_LEASE_CHARS:
                raise Refused(400, "SLOT_BAD_REQUEST", "the lease is not valid")
            if path == RELEASE_PATH:
                return {"released": self.ledger.release(cell, lease)}
            slot = self.ledger.poll(cell, lease)
        return {"lease": slot.lease, "granted": slot.granted}

    @staticmethod
    async def _reply(send, status: int, payload: dict | None = None) -> None:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = [(b"content-length", str(len(raw)).encode("ascii"))]
        if raw:
            headers.append((b"content-type", b"application/json"))
        async with asyncio.timeout(_REQUEST_SECONDS):
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": raw})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path = scope["path"]
        if path == "/health/ready" and scope["method"] == "GET":
            await self._reply(send, 204)
        elif path in (ACQUIRE_PATH, POLL_PATH, RELEASE_PATH) and scope["method"] == "POST":
            # One entry per address bounds what a compromised cell can queue under made-up IDs.
            # Pod traffic keeps its source address, which the network policy relies on too.
            source = (scope.get("client") or ("",))[0]
            try:
                payload = self._handle(path, source, await self._body(scope, receive))
                await self._reply(send, 200, payload)
            except Refused as refusal:
                log.info("media slot refused (%s): %s", refusal.code, refusal.reason)
                await self._reply(send, refusal.status, {"code": refusal.code, "reason": refusal.reason})
            except TimeoutError:
                await self._reply(send, 408, {"code": "SLOT_TIMEOUT", "reason": "the request was too slow"})
        else:
            await self._reply(send, 404, {"code": "SLOT_NOT_FOUND", "reason": "no such path"})


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = MediaSlotLease(Config.from_env())
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=SLOT_PORT,
        workers=1,
        limit_concurrency=64,
        timeout_keep_alive=5,
        access_log=False,
        # The source address is the pod's own; never take it from a header a cell can write.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
