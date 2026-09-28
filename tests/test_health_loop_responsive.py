"""Liveness must stay answerable while the runtime is busy doing blocking work.

The first index build over a large vault keeps several threads busy and holds
the process's locks and disk. `/health` and `/health/ready` are async
handlers; if they do blocking work themselves, that work runs on the event-loop
thread, so every other request (including the kubelet's 2 s liveness probe) waits
behind it. These tests hold the blocking operations open and assert the loop and
`/health` keep answering.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from fastmcp import FastMCP

from exomem import deploy_provenance, server_assets, state_migration
from exomem import runtime_readiness as runtime_readiness_module
from exomem import vault as vault_module

# The kubelet gives a probe 2 s; a healthy answer is milliseconds. 0.5 s leaves
# room for a loaded CI runner while still failing on any blocked loop.
ANSWER_BUDGET_SECONDS = 0.5
# Long enough that a handler which blocks on the loop cannot be mistaken for one
# that merely ran slowly, short enough that the red run does not hang.
HOLD_CAP_SECONDS = 4.0


class _Hold:
    """A blocking operation that stays blocked until released (or capped)."""

    def __init__(self, result=None) -> None:
        self.result = {} if result is None else result
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.entered.set()
        self.release.wait(HOLD_CAP_SECONDS)
        return self.result


async def _measure(app, request_path: str, *, alongside: str | None = None) -> tuple[float, float]:
    """Return (seconds `request_path` took, worst event-loop stall) while it ran.

    With `alongside`, that route is requested first and concurrently, so the
    measured request queues behind whatever the other handler does to the loop.
    """
    worst_gap = 0.0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal worst_gap
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst_gap = max(worst_gap, now - last)
            last = now

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
    ) as client:
        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        other = asyncio.create_task(client.get(alongside)) if alongside else None
        measured = asyncio.create_task(client.get(request_path))
        await asyncio.wait({measured})
        elapsed = time.perf_counter() - started
        response = measured.result()
        stop.set()
        await tick
        if other is not None:
            await asyncio.wait_for(other, HOLD_CAP_SECONDS + 5)
    assert response.status_code == 200, response.text
    return elapsed, worst_gap


def _app() -> object:
    mcp = FastMCP("health-loop-test")
    server_assets.register_health_routes(mcp)
    return mcp.http_app()


def test_health_does_not_block_the_loop_on_provenance_or_state_reads(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    # Every per-request read `/health` used to do, now held open. The refresh
    # interval is zero so each request would re-read them.
    monkeypatch.setattr(server_assets, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.0, raising=False)
    holds = [_Hold(), _Hold(), _Hold()]
    monkeypatch.setattr(deploy_provenance, "provenance", holds[0])
    monkeypatch.setattr(state_migration, "migration_status", holds[1])
    monkeypatch.setattr(vault_module, "resolve_vault", holds[2])

    async def scenario() -> tuple[float, float]:
        try:
            return await _measure(app, "/health")
        finally:
            # Release inside the loop: asyncio.run() joins its executor threads
            # on exit, and a refresh thread still held would stall it.
            for hold in holds:
                hold.release.set()

    elapsed, worst_gap = asyncio.run(scenario())
    assert elapsed < ANSWER_BUDGET_SECONDS, f"/health took {elapsed:.2f}s"
    assert worst_gap < ANSWER_BUDGET_SECONDS, f"loop stalled {worst_gap:.2f}s"


def test_health_keeps_answering_while_readiness_measurement_is_blocked(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    hold = _Hold({"status": "ready"})
    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", hold)

    async def scenario() -> tuple[float, float]:
        try:
            return await _measure(app, "/health", alongside="/health/ready")
        finally:
            hold.release.set()

    elapsed, worst_gap = asyncio.run(scenario())
    assert elapsed < ANSWER_BUDGET_SECONDS, f"/health took {elapsed:.2f}s behind readiness"
    assert worst_gap < ANSWER_BUDGET_SECONDS, f"loop stalled {worst_gap:.2f}s"
