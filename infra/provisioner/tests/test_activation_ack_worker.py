from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from exomem_provisioner.production import (
    build_activation_ack_server,
    run_worker_with_activation_ack,
)


def test_activation_ack_server_uses_fixed_private_tls_listener() -> None:
    server = build_activation_ack_server(
        FastAPI(),
        certificate_path="/run/exomem/activation-ack/tls.crt",
        key_path="/run/exomem/activation-ack/tls.key",
    )

    assert server.config.host == "0.0.0.0"
    assert server.config.port == 8443
    assert server.config.ssl_certfile == "/run/exomem/activation-ack/tls.crt"
    assert server.config.ssl_keyfile == "/run/exomem/activation-ack/tls.key"
    assert server.config.access_log is False
    assert server.config.limit_concurrency == 9
    assert server.config.backlog == 8
    assert server.config.timeout_keep_alive == 1
    assert server.config.h11_max_incomplete_event_size == 8 * 1024


async def test_listener_exit_cancels_worker_and_waits_for_shutdown() -> None:
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()

    async def worker() -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()

    async def listener() -> None:
        await worker_started.wait()

    await run_worker_with_activation_ack(worker(), listener())

    assert worker_stopped.is_set()


async def test_outer_cancellation_stops_worker_and_listener_before_returning() -> None:
    started = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]

    async def blocked(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped[index].set()

    task = asyncio.create_task(run_worker_with_activation_ack(blocked(0), blocked(1)))
    await asyncio.gather(*(event.wait() for event in started))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert all(event.is_set() for event in stopped)
