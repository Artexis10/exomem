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


async def _measure(
    app, request_path: str, *, alongside: str | None = None, release=None
) -> tuple[float, float]:
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
        if release is not None:
            release()
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
            return await _measure(
                app, "/health", alongside="/health/ready", release=hold.release.set
            )
        finally:
            hold.release.set()

    elapsed, worst_gap = asyncio.run(scenario())
    assert elapsed < ANSWER_BUDGET_SECONDS, f"/health took {elapsed:.2f}s behind readiness"
    assert worst_gap < ANSWER_BUDGET_SECONDS, f"loop stalled {worst_gap:.2f}s"


def _get(app, path: str, *, delay: float = 0.0):
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
        ) as client:
            await asyncio.sleep(delay)
            return await client.get(path)

    return run()


def test_readiness_answers_while_the_default_worker_limiter_is_saturated(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow tool calls fill anyio's default thread limiter; readiness must not queue behind them."""
    import anyio.to_thread

    app = _app()
    monkeypatch.setattr(
        runtime_readiness_module, "runtime_readiness", lambda **_kw: {"status": "ready"}
    )
    hold = threading.Event()

    async def scenario() -> tuple[float, int]:
        limiter = anyio.to_thread.current_default_thread_limiter()
        jobs = [
            asyncio.create_task(anyio.to_thread.run_sync(lambda: hold.wait(HOLD_CAP_SECONDS)))
            for _ in range(int(limiter.total_tokens))
        ]
        try:
            await asyncio.sleep(0.2)
            started = time.perf_counter()
            response = await asyncio.wait_for(_get(app, "/health/ready"), HOLD_CAP_SECONDS + 2)
            return time.perf_counter() - started, response.status_code
        finally:
            hold.set()
            await asyncio.gather(*jobs)

    elapsed, status = asyncio.run(scenario())
    assert status == 200
    assert elapsed < ANSWER_BUDGET_SECONDS, f"/health/ready took {elapsed:.2f}s behind tool calls"


def test_health_fails_once_a_refresh_has_been_wedged_past_the_bound(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung filesystem must still be detectable: a stuck refresh turns /health 503."""
    app = _app()
    monkeypatch.setattr(server_assets, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.0)
    monkeypatch.setattr(server_assets, "HEALTH_REFRESH_WEDGED_SECONDS", 0.3, raising=False)
    # Provenance is read once, at registration; the state read is what refreshes.
    hold = _Hold("complete")
    monkeypatch.setattr(state_migration, "migration_status", hold)

    async def scenario() -> tuple[int, int, int]:
        first = await _get(app, "/health")  # starts the refresh, which blocks
        await asyncio.sleep(0.05)
        early = await _get(app, "/health")  # in flight, but within the bound
        await asyncio.sleep(0.4)
        wedged = await _get(app, "/health")
        hold.release.set()
        await asyncio.sleep(0.2)
        recovered = await _get(app, "/health")
        assert first.status_code == 200 and recovered.status_code == 200
        return early.status_code, wedged.status_code, recovered.status_code

    early, wedged, _ = asyncio.run(scenario())
    assert early == 200
    assert wedged == 503


def test_snapshot_refreshes_after_the_ttl_with_one_refresh_in_flight(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = iter(range(1, 1000))
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def migration_status(_root):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return f"v{next(reads)}"

    monkeypatch.setattr(state_migration, "migration_status", migration_status)
    app = _app()  # registration reads v1
    monkeypatch.setattr(server_assets, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.1)

    async def scenario() -> list[str]:
        seen = []
        for _ in range(40):
            seen.append((await _get(app, "/health")).json()["state"]["migration"])
            await asyncio.sleep(0.02)
        return seen

    seen = asyncio.run(scenario())
    assert seen[0] == "v1"
    assert seen[-1] != "v1", "a later probe must serve the refreshed value"
    assert max_in_flight == 1
