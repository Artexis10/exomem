"""The node media-slot lease: the pool rule, order, expiry and ownership, over real HTTP.

The service runs behind a real uvicorn listener with a clock the test moves, so expiry
needs no sleeping. A cell is a 16-character ID that asks for the bytes and the job timeout of
its next job; a 1280 MiB pool holds one 1024 MiB job, or two 640 MiB jobs, at once.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
import time

import httpx
import pytest

from exomem import media_slot_lease as lease_module
from exomem.media_slot_client import ACQUIRE_PATH, POLL_PATH, RELEASE_PATH

POOL_MIB = 1280
BIG, SMALL = 1024, 640  # MiB a job reserves: one of the first, or two of the second, fit the pool
JOB_SECONDS = 900.0
LEASE_SECONDS = 960.0  # the 900 s job timeout plus a 60 s margin
QUEUE_SECONDS = 60.0
A, B, C, D = "a" * 16, "b" * 16, "c" * 16, "d" * 16


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _config() -> lease_module.Config:
    return lease_module.Config.from_env(
        {
            lease_module.POOL_ENV: str(POOL_MIB),
            lease_module.QUEUE_ENV: str(QUEUE_SECONDS),
            lease_module.MARGIN_ENV: "60",
            lease_module.MAX_JOB_ENV: str(JOB_SECONDS),
        }
    )


def _one_address_per_cell(app):
    """Stand in for the cluster, where each cell is its own pod with its own source address.

    The test client has one address, so this names the peer after the cell in the request.
    A test of the address rule itself runs the service without it.
    """

    async def named(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body", False):
                break
        try:
            cell = json.loads(body).get("cell", "")
        except (ValueError, AttributeError):
            cell = ""

        async def replay():
            return {"type": "http.request", "body": body, "more_body": False}

        await app({**scope, "client": (f"cell-{cell}", 0)}, replay, send)

    return named


@contextlib.contextmanager
def _listening(app):
    import uvicorn

    bound = socket.socket()
    bound.bind(("127.0.0.1", 0))
    listener = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
    thread = threading.Thread(target=listener.run, kwargs={"sockets": [bound]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not listener.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the listener never started"
        time.sleep(0.01)
    try:
        yield bound.getsockname()
    finally:
        listener.should_exit = True
        thread.join(30)
        bound.close()


class Service:
    """Calls as a cell makes them; the ledger's own clock is `clock`."""

    def __init__(self, http: httpx.Client, clock: Clock) -> None:
        self.http, self.clock = http, clock
        self.leases: dict[str, str] = {}

    def acquire(
        self, cell: str, mib: int, *, seconds: float = JOB_SECONDS, engine: str = "speech"
    ) -> httpx.Response:
        reply = self.http.post(
            ACQUIRE_PATH,
            json={"cell": cell, "engine": engine, "anon_bytes": mib * 1024 * 1024, "timeout_seconds": seconds},
        )
        if reply.status_code == 200:
            self.leases[cell] = reply.json()["lease"]
        return reply

    def granted(self, cell: str) -> bool:
        reply = self.http.post(POLL_PATH, json={"cell": cell, "lease": self.leases[cell]})
        assert reply.status_code == 200, reply.text
        return reply.json()["granted"]

    def release(self, cell: str, lease: str | None = None) -> bool:
        reply = self.http.post(RELEASE_PATH, json={"cell": cell, "lease": lease or self.leases[cell]})
        assert reply.status_code == 200, reply.text
        return reply.json()["released"]

    def wait_polling(self, cell: str, seconds: float) -> bool:
        """Let `seconds` pass with `cell` polling inside the queue window; its last answer."""
        step = QUEUE_SECONDS / 2
        elapsed, answer = 0.0, False
        while elapsed < seconds:
            self.clock.now += min(step, seconds - elapsed)
            elapsed += step
            answer = self.granted(cell)
        return answer

    def advance_past_restart_hold(self) -> None:
        self.clock.now += LEASE_SECONDS


@pytest.fixture
def service():
    clock = Clock()
    app = _one_address_per_cell(lease_module.MediaSlotLease(_config(), clock))
    with _listening(app) as (host, port), httpx.Client(
        base_url=f"http://{host}:{port}", trust_env=False, timeout=30
    ) as http:
        yield Service(http, clock)


@pytest.fixture
def ready(service):
    service.advance_past_restart_hold()
    return service


def test_granted_budgets_never_exceed_the_pool(ready) -> None:
    """The node's memory is what the pool bounds; every grant is checked against it."""
    assert ready.acquire(A, BIG).json()["granted"] is True  # 1024 of 1280
    assert ready.acquire(B, SMALL).json()["granted"] is False  # 1024 + 640 does not fit
    assert ready.acquire(C, SMALL).json()["granted"] is False
    assert ready.release(A) is True
    # Two 640 MiB budgets now fit exactly; a third cell has to wait.
    assert ready.granted(B) is True
    assert ready.granted(C) is True
    assert ready.acquire(D, SMALL).json()["granted"] is False
    assert ready.release(B) is True
    assert ready.granted(D) is True


def test_a_head_that_does_not_fit_blocks_the_cells_behind_it(ready) -> None:
    """Strict first-in first-out: a small request cannot overtake a large one forever."""
    assert ready.acquire(A, SMALL).json()["granted"] is True
    assert ready.acquire(B, BIG).json()["granted"] is False  # 640 + 1024 does not fit
    assert ready.acquire(C, SMALL).json()["granted"] is False  # would fit, but B is first
    assert ready.granted(C) is False
    assert ready.release(A) is True
    assert ready.granted(B) is True
    assert ready.granted(C) is False  # 1024 + 640 does not fit behind B


def test_a_cell_cannot_queue_twice(ready) -> None:
    assert ready.acquire(A, SMALL).status_code == 200
    again = ready.acquire(A, SMALL)
    assert again.status_code == 409 and again.json()["code"] == "SLOT_ENTRY_EXISTS"
    # Still one entry: after one release the cell can ask again.
    assert ready.release(A) is True
    assert ready.acquire(A, SMALL).status_code == 200


def test_an_expired_lease_frees_its_slot(ready) -> None:
    """A cell that dies holding a slot must not hold it forever."""
    assert ready.acquire(A, BIG).json()["granted"] is True
    assert ready.acquire(B, BIG).json()["granted"] is False
    assert ready.wait_polling(B, LEASE_SECONDS - 1) is False
    assert ready.wait_polling(B, 2) is True


def test_a_lease_runs_from_the_cell_learning_of_its_grant_not_from_the_grant(ready) -> None:
    """The cell starts its child, and so its watchdog, when it learns of the grant.

    A lease counted from the grant would end while the child still ran, and the next cell
    would be granted a pool that the child still held.
    """
    assert ready.acquire(A, BIG).json()["granted"] is True
    assert ready.acquire(B, BIG).json()["granted"] is False
    assert ready.acquire(C, BIG).json()["granted"] is False
    assert ready.release(A) is True  # B is granted, and learns of it at its next poll
    ready.clock.now += QUEUE_SECONDS - 1
    assert ready.granted(B) is True  # collected 59 s after the grant
    assert ready.granted(C) is False  # C keeps its place
    assert ready.wait_polling(C, LEASE_SECONDS - 1) is False  # B's child may still run
    assert ready.wait_polling(C, 2) is True


def test_a_grant_that_is_never_collected_frees_its_slot(ready) -> None:
    assert ready.acquire(A, SMALL).json()["granted"] is True
    assert ready.acquire(B, BIG).json()["granted"] is False
    assert ready.acquire(C, BIG).json()["granted"] is False
    assert ready.release(A) is True  # B is granted, but B never polls
    ready.clock.now += QUEUE_SECONDS - 1
    assert ready.granted(C) is False
    ready.clock.now += 2
    assert ready.granted(C) is True
    # The cell that never collected lost its place; its old ticket no longer opens anything.
    assert ready.http.post(POLL_PATH, json={"cell": B, "lease": ready.leases[B]}).status_code == 404


def test_a_cell_cannot_release_or_poll_another_cells_lease(ready) -> None:
    assert ready.acquire(A, BIG).json()["granted"] is True
    assert ready.acquire(C, BIG).json()["granted"] is False
    a_lease = ready.leases[A]

    assert ready.acquire(B, SMALL).json()["granted"] is False
    assert ready.release(B, a_lease) is False  # B names A's lease
    assert ready.release(A, "not-the-lease") is False  # A names the wrong lease
    assert ready.http.post(POLL_PATH, json={"cell": B, "lease": a_lease}).status_code == 404
    assert ready.granted(C) is False  # A still holds the slot
    assert ready.release(A, a_lease) is True


def test_nothing_is_granted_for_one_lease_after_a_restart(service) -> None:
    """A child of the earlier service may still run, so a restarted service waits it out."""
    assert service.acquire(A, SMALL).json()["granted"] is False
    service.clock.now += LEASE_SECONDS - QUEUE_SECONDS
    assert service.acquire(B, SMALL).json()["granted"] is False
    assert service.granted(B) is False
    service.clock.now += QUEUE_SECONDS  # A stopped polling long ago; B keeps its place
    assert service.granted(B) is True


def test_a_cell_cannot_hold_the_pool_longer_than_the_configured_job_timeout(ready, caplog) -> None:
    """The cell states its timeout, so the service caps it; a shorter lease would be unsafe."""
    with caplog.at_level(logging.INFO, logger=lease_module.__name__):
        reply = ready.acquire(A, SMALL, seconds=JOB_SECONDS + 1)
    assert reply.status_code == 422 and reply.json()["code"] == "SLOT_JOB_TOO_LONG"
    assert "SLOT_JOB_TOO_LONG" in caplog.text
    assert ready.acquire(A, SMALL, seconds=JOB_SECONDS).json()["granted"] is True


def test_a_budget_above_the_pool_is_refused_not_queued_and_the_log_says_why(caplog) -> None:
    app = lease_module.MediaSlotLease(
        lease_module.Config.from_env(
            {
                lease_module.POOL_ENV: "512",
                lease_module.QUEUE_ENV: "60",
                lease_module.MARGIN_ENV: "60",
                lease_module.MAX_JOB_ENV: "900",
            }
        ),
        Clock(),
    )
    with _listening(app) as (host, port), httpx.Client(
        base_url=f"http://{host}:{port}", trust_env=False, timeout=30
    ) as http, caplog.at_level(logging.INFO, logger=lease_module.__name__):
        reply = http.post(
            ACQUIRE_PATH,
            json={"cell": A, "engine": "speech", "anon_bytes": BIG << 20, "timeout_seconds": JOB_SECONDS},
        )
    assert reply.status_code == 422 and reply.json()["code"] == "SLOT_ABOVE_POOL"
    assert "SLOT_ABOVE_POOL" in caplog.text


def test_one_address_holds_one_entry_whatever_cell_IDs_it_sends() -> None:
    """A compromised cell cannot fill the queue with made-up cell IDs."""
    clock = Clock()
    with _listening(lease_module.MediaSlotLease(_config(), clock)) as (host, port), httpx.Client(
        base_url=f"http://{host}:{port}", trust_env=False, timeout=30
    ) as http:  # no stand-in here: every request comes from this one address
        cells = Service(http, clock)
        assert cells.acquire(A, SMALL).status_code == 200
        second = cells.acquire(B, SMALL)
        assert second.status_code == 409 and second.json()["code"] == "SLOT_SOURCE_BUSY"
        assert cells.release(A) is True
        assert cells.acquire(B, SMALL).status_code == 200


def test_the_pool_and_queue_window_are_configuration_not_defaults() -> None:
    with pytest.raises(ValueError, match=lease_module.POOL_ENV):
        lease_module.Config.from_env({lease_module.QUEUE_ENV: "60", lease_module.MARGIN_ENV: "60"})
    config = _config()
    assert config.pool_bytes == POOL_MIB * 1024 * 1024 and config.max_lease_seconds == LEASE_SECONDS


GOOD = {"cell": A, "engine": "speech", "anon_bytes": SMALL << 20, "timeout_seconds": JOB_SECONDS}


@pytest.mark.parametrize(
    "body",
    [
        {**GOOD, "cell": "not-a-cell"},
        {**GOOD, "engine": ""},
        {**GOOD, "engine": "speech\nINFO media slot granted to forged"},  # would forge a log line
        {**GOOD, "anon_bytes": 0},
        {**GOOD, "anon_bytes": True},
        {**GOOD, "timeout_seconds": 0},
        {**GOOD, "timeout_seconds": "900"},
        {"cell": A},
        ["list"],
    ],
)
def test_a_malformed_request_is_refused_without_touching_the_queue(ready, body) -> None:
    assert ready.http.post(ACQUIRE_PATH, json=body).status_code == 400
    assert ready.acquire(A, BIG).json()["granted"] is True
