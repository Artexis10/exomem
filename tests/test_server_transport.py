from __future__ import annotations

import asyncio

from exomem.server_transport import PrimeMcpSSEMiddleware


async def _receive() -> dict:
    return {"type": "http.disconnect"}


def test_mcp_get_sse_is_primed_immediately() -> None:
    async def scenario() -> list[dict]:
        sent: list[dict] = []

        async def capture(message: dict) -> None:
            sent.append(message)

        async def app(scope, receive, send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"data: later\r\n\r\n"})

        middleware = PrimeMcpSSEMiddleware(app)
        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [(b"accept", b"text/event-stream")],
            },
            _receive,
            capture,
        )
        return sent

    sent = asyncio.run(scenario())

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]
    assert sent[1] == {
        "type": "http.response.body",
        "body": b": stream-ready\r\n\r\n",
        "more_body": True,
    }


def test_non_sse_or_failed_response_is_not_primed() -> None:
    async def run(scope: dict, *, status: int, content_type: bytes) -> list[dict]:
        sent: list[dict] = []

        async def capture(message: dict) -> None:
            sent.append(message)

        async def app(inner_scope, receive, send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", content_type)],
                }
            )
            await send({"type": "http.response.body", "body": b"original"})

        await PrimeMcpSSEMiddleware(app)(scope, _receive, capture)
        return sent

    base = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": [(b"accept", b"text/event-stream")],
    }
    assert len(asyncio.run(run(base, status=401, content_type=b"text/event-stream"))) == 2
    assert len(asyncio.run(run(base, status=200, content_type=b"application/json"))) == 2
    assert len(
        asyncio.run(
            run(
                {**base, "method": "POST"},
                status=200,
                content_type=b"text/event-stream",
            )
        )
    ) == 2
