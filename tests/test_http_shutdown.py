"""A tool call in flight when the HTTP server receives SIGTERM still returns its result.

A Cloud cell stops at every upgrade. Before the fix, sse-starlette ended the call's
event stream at the signal, and the client behind the gateway got a 502.
"""

import json
import signal
import socket
import threading
import time

import anyio
import httpx
import uvicorn
from fastmcp import FastMCP
from sse_starlette.sse import AppStatus

from exomem.server import finish_responses_on_shutdown


def test_a_tool_call_in_flight_at_sigterm_returns_its_result() -> None:
    started = threading.Event()
    app = FastMCP("shutdown-probe")

    @app.tool
    async def slow_tool() -> str:
        started.set()
        # Longer than sse-starlette's 0.5 s shutdown poll, shorter than the 2 s graceful timeout.
        await anyio.sleep(1.2)
        return "finished"

    finish_responses_on_shutdown()
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app.http_app(stateless_http=True),
            lifespan="on",
            timeout_graceful_shutdown=2,
            log_level="warning",
        )
    )
    serving = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    outcome: dict[str, object] = {}

    def call() -> None:
        try:
            response = httpx.post(
                f"http://127.0.0.1:{port}/mcp",
                headers={
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                },
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "slow_tool", "arguments": {}},
                    }
                ),
                timeout=10,
            )
            outcome["status"], outcome["body"] = response.status_code, response.text
        except httpx.HTTPError as error:
            outcome["error"] = repr(error)

    caller = threading.Thread(target=call)
    try:
        serving.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        caller.start()
        assert started.wait(10)
        server.handle_exit(signal.SIGTERM, None)
        caller.join(10)
        serving.join(10)
    finally:
        server.should_exit = True
        serving.join(10)
        listener.close()
        AppStatus.should_exit = False
        AppStatus.enable_automatic_graceful_drain_mode()

    assert outcome.get("status") == 200, outcome
    assert "finished" in str(outcome.get("body")), outcome
