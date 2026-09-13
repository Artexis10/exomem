"""ASGI contract for the managed service's persistent HTTP ingress."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from exomem.service_ingress import IngressLimits, ServiceIngress


def _scope(
    method: str = "POST",
    *,
    target: str = "/mcp?x=%2F&x=2",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    path, _, query = target.partition("?")
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers or [(b"host", b"service.example")],
    }


async def _call(
    ingress: ServiceIngress,
    *,
    scope: dict | None = None,
    body: bytes = b"",
) -> list[dict]:
    sent: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        sent.append(message)

    await ingress(scope or _scope(), receive, send)
    return sent


def _body(sent: list[dict]) -> bytes:
    return b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://worker")


async def _until(predicate) -> None:  # noqa: ANN001
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 1)


class _HeldStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self.first = first
        self.closed = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):  # noqa: ANN201
        yield self.first
        await self.release.wait()

    async def aclose(self) -> None:
        self.closed.set()


def test_forward_preserves_target_bytes_and_end_to_end_headers() -> None:
    async def scenario() -> None:
        seen: list[tuple[str, bytes, httpx.Headers]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), await request.aread(), request.headers))
            return httpx.Response(
                201,
                headers={
                    "content-type": "application/json",
                    "connection": "close",
                    "x-result": "ok",
                },
                content=b'{"ok":true}',
            )

        async with _client(handler) as client:
            ingress = ServiceIngress()
            ingress.resume(client)
            sent = await _call(
                ingress,
                scope=_scope(
                    headers=[
                        (b"host", b"service.example"),
                        (b"authorization", b"Bearer secret"),
                        (b"x-request", b"yes"),
                        (b"connection", b"x-remove"),
                        (b"x-remove", b"no"),
                    ]
                ),
                body=b'{"id":7}',
            )
            assert seen[0][0].endswith("/mcp?x=%2F&x=2")
            assert seen[0][1] == b'{"id":7}'
            assert seen[0][2]["authorization"] == "Bearer secret"
            assert seen[0][2]["x-request"] == "yes"
            assert "connection" not in seen[0][2]
            assert "x-remove" not in seen[0][2]
            assert sent[0]["status"] == 201
            assert (b"x-result", b"ok") in sent[0]["headers"]
            assert not any(name == b"connection" for name, _ in sent[0]["headers"])
            assert _body(sent) == b'{"ok":true}'
            await ingress.aclose()

    asyncio.run(scenario())


def test_paused_request_forwards_once_to_new_generation() -> None:
    async def scenario() -> None:
        old_calls: list[bytes] = []
        new_calls: list[bytes] = []

        async def old(request: httpx.Request) -> httpx.Response:
            old_calls.append(await request.aread())
            return httpx.Response(200, content=b"old")

        async def new(request: httpx.Request) -> httpx.Response:
            new_calls.append(await request.aread())
            return httpx.Response(200, content=b"new")

        async with _client(old) as old_client, _client(new) as new_client:
            ingress = ServiceIngress()
            ingress.resume(old_client)
            ingress.pause()
            call = asyncio.create_task(_call(ingress, body=b'{"jsonrpc":"2.0","id":1}'))
            await _until(lambda: ingress.stats["queued_bytes"] == 24)
            assert ingress.stats["queued"] == 1
            assert ingress.stats["queued_bytes"] == 24
            assert old_calls == []
            ingress.resume(new_client)
            sent = await asyncio.wait_for(call, 1)
            assert _body(sent) == b"new"
            assert old_calls == []
            assert new_calls == [b'{"jsonrpc":"2.0","id":1}']
            assert ingress.stats["queued"] == 0
            await ingress.aclose()

    asyncio.run(scenario())


def test_queue_count_exhaustion_never_reads_or_forwards_second_request() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        async with _client(handler) as client:
            ingress = ServiceIngress(IngressLimits(queue_requests=1))
            ingress.resume(client)
            ingress.pause()
            first = asyncio.create_task(_call(ingress, body=b'{"jsonrpc":"2.0","id":1}'))
            await _until(lambda: ingress.stats["queued"] == 1)
            reads = 0

            async def receive() -> dict:
                nonlocal reads
                reads += 1
                return {"type": "http.request", "body": b"{}", "more_body": False}

            sent: list[dict] = []

            async def send(message: dict) -> None:
                sent.append(message)

            await ingress(_scope(), receive, send)
            assert reads == 0
            assert sent[0]["status"] == 503
            assert calls == 0
            ingress.resume()
            await first
            assert calls == 1
            await ingress.aclose()

    asyncio.run(scenario())


def test_queue_expiry_returns_jsonrpc_error_with_original_id() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        async with _client(handler) as client:
            ingress = ServiceIngress(IngressLimits(queue_timeout=0.02))
            ingress.resume(client)
            ingress.pause()
            sent = await _call(
                ingress, body=b'{"jsonrpc":"2.0","id":"call-7","method":"tools/call"}'
            )
            assert sent[0]["status"] in {200, 503}
            error = json.loads(_body(sent))
            assert error["id"] == "call-7"
            assert error["error"]["code"] == -32000
            assert "not dispatched" in error["error"]["message"].lower()
            assert calls == 0
            assert ingress.stats["queued"] == 0
            await ingress.aclose()

    asyncio.run(scenario())


def test_queue_body_and_aggregate_limits_never_dispatch_oversize_request() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        async with _client(handler) as client:
            ingress = ServiceIngress(IngressLimits(queue_requests=2, queue_bytes=8, body_bytes=7))
            ingress.resume(client)
            ingress.pause()
            sent = await _call(ingress, body=b"12345678")
            assert sent[0]["status"] == 413
            assert calls == 0
            assert ingress.stats["queued"] == 0
            assert ingress.stats["queued_bytes"] == 0
            await ingress.aclose()

    asyncio.run(scenario())


def test_forwarded_request_stays_active_after_downstream_cancellation() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return httpx.Response(200, content=b"done")

        async with _client(handler) as client:
            ingress = ServiceIngress()
            ingress.resume(client)
            call = asyncio.create_task(_call(ingress, body=b"mutation"))
            await asyncio.wait_for(entered.wait(), 1)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert ingress.stats["active"] == 1
            assert await ingress.drain(0.01) is False
            release.set()
            assert await ingress.drain(1) is True
            assert calls == 1
            await ingress.aclose()

    asyncio.run(scenario())


def test_normal_forwarding_accepts_body_larger_than_queue_limit() -> None:
    async def scenario() -> None:
        sizes: list[int] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            sizes.append(len(await request.aread()))
            return httpx.Response(204)

        async with _client(handler) as client:
            ingress = ServiceIngress()
            ingress.resume(client)
            sent = await _call(ingress, body=b"x" * (33 * 1024 * 1024))
            assert sent[0]["status"] == 204
            assert sizes == [33 * 1024 * 1024]
            await ingress.aclose()

    asyncio.run(scenario())


def test_aggregate_queued_bytes_reject_second_request_without_dispatch() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(204)

        async with _client(handler) as client:
            ingress = ServiceIngress(IngressLimits(queue_requests=2, queue_bytes=10, body_bytes=10))
            ingress.resume(client)
            ingress.pause()
            first = asyncio.create_task(_call(ingress, body=b"123456"))
            await _until(lambda: ingress.stats["queued_bytes"] == 6)
            sent = await _call(ingress, body=b"12345")
            assert sent[0]["status"] == 413
            assert ingress.stats["queued_bytes"] == 6
            assert calls == 0
            ingress.resume()
            await first
            assert calls == 1
            await ingress.aclose()

    asyncio.run(scenario())


def test_queue_deadline_includes_body_intake() -> None:
    async def scenario() -> None:
        async with _client(lambda request: httpx.Response(204)) as client:
            ingress = ServiceIngress(IngressLimits(queue_timeout=0.15, body_timeout=1))
            ingress.resume(client)
            ingress.pause()
            sent: list[dict] = []

            async def receive() -> dict:
                await asyncio.sleep(0.1)
                return {
                    "type": "http.request",
                    "body": b'{"jsonrpc":"2.0","id":1}',
                    "more_body": False,
                }

            async def send(message: dict) -> None:
                sent.append(message)

            started = asyncio.get_running_loop().time()
            await ingress(_scope(), receive, send)
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 0.22
            assert json.loads(_body(sent))["id"] == 1
            await ingress.aclose()

    asyncio.run(scenario())


def test_post_sse_remains_finite_until_full_response_is_consumed() -> None:
    async def scenario() -> None:
        stream = _HeldStream(b"data: first\n\n")

        async def handler(request: httpx.Request) -> httpx.Response:
            await request.aread()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        async with _client(handler) as client:
            ingress = ServiceIngress()
            ingress.resume(client)
            call = asyncio.create_task(_call(ingress, body=b"post"))
            await _until(lambda: ingress.stats["active"] == 1)
            ingress.pause()
            assert await ingress.drain(0.01) is False
            assert ingress.stats["streams"] == 0
            stream.release.set()
            await call
            assert await ingress.drain(1) is True
            await ingress.aclose()

    asyncio.run(scenario())


def test_standalone_get_sse_detaches_heartbeats_and_reattaches_with_auth() -> None:
    async def scenario() -> None:
        old_stream = _HeldStream(b"data: old\n\n")
        new_stream = _HeldStream(b"data: new\n\n")
        old_auth: list[str] = []
        new_auth: list[str] = []

        async def old(request: httpx.Request) -> httpx.Response:
            old_auth.append(request.headers["authorization"])
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=old_stream
            )

        async def new(request: httpx.Request) -> httpx.Response:
            new_auth.append(request.headers["authorization"])
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=new_stream
            )

        async with _client(old) as old_client, _client(new) as new_client:
            ingress = ServiceIngress(IngressLimits(heartbeat_interval=0.01))
            ingress.resume(old_client)
            sent: list[dict] = []
            delivered = False

            async def receive() -> dict:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def send(message: dict) -> None:
                sent.append(message)

            call = asyncio.create_task(
                ingress(
                    _scope(
                        "GET",
                        headers=[
                            (b"host", b"service.example"),
                            (b"authorization", b"Bearer secret"),
                        ],
                    ),
                    receive,
                    send,
                )
            )
            await _until(lambda: b"data: old\n\n" in _body(sent))
            assert await ingress.drain(0.01) is True
            ingress.pause()
            await asyncio.wait_for(ingress.detach_streams(), 1)
            assert old_stream.closed.is_set()
            await _until(lambda: b": keepalive\n\n" in _body(sent))
            ingress.resume(new_client)
            await _until(lambda: b"data: new\n\n" in _body(sent))
            assert old_auth == ["Bearer secret"]
            assert new_auth == ["Bearer secret"]
            assert ingress.stats["streams"] == 1
            await ingress.aclose()
            await asyncio.wait_for(call, 1)

    asyncio.run(scenario())


def test_sse_stream_slots_bound_concurrent_gets_before_forwarding() -> None:
    async def scenario() -> None:
        stream = _HeldStream(b"data: one\n\n")
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        async with _client(handler) as client:
            ingress = ServiceIngress(IngressLimits(stream_requests=1))
            ingress.resume(client)
            first = asyncio.create_task(_call(ingress, scope=_scope("GET")))
            await _until(lambda: ingress.stats["streams"] == 1)
            second = await _call(ingress, scope=_scope("GET"))
            assert second[0]["status"] == 503
            assert calls == 1
            await ingress.aclose()
            await first

    asyncio.run(scenario())


def test_cancelled_queued_get_releases_stream_slot() -> None:
    async def scenario() -> None:
        async with _client(lambda request: httpx.Response(204)) as client:
            ingress = ServiceIngress(IngressLimits(stream_requests=1))
            ingress.resume(client)
            ingress.pause()
            first = asyncio.create_task(_call(ingress, scope=_scope("GET")))
            await _until(lambda: ingress.stats["queued"] == 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert ingress.stats["queued"] == 0
            second = asyncio.create_task(_call(ingress, scope=_scope("GET")))
            await _until(lambda: ingress.stats["queued"] == 1)
            ingress.resume()
            assert (await second)[0]["status"] == 204
            await ingress.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("phase", ["body", "ready"])
def test_queue_cancellation_wins_when_wait_completes(method: str, phase: str) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        sent: list[dict] = []
        waiting = asyncio.Event()

        class ObservedReady(asyncio.Event):
            async def wait(self) -> bool:
                waiting.set()
                return await super().wait()

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(204)

        async def receive() -> dict:
            if phase == "body":
                # Cancel the outer request in the same event-loop turn as
                # body completion, before its completed wait resumes.
                asyncio.get_running_loop().call_soon(first.cancel)
            return {"type": "http.request", "body": b"payload", "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        async with _client(handler) as client:
            ingress = ServiceIngress(IngressLimits(stream_requests=1, queue_timeout=0.1))
            ingress._ready = ObservedReady()
            ingress.resume(client)
            ingress.pause()
            first = asyncio.create_task(ingress(_scope(method), receive, send))
            if phase == "ready":
                await waiting.wait()
                ingress.resume()
                first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert calls == []
            assert sent == []
            assert ingress.stats["queued"] == ingress.stats["queued_bytes"] == 0
            ingress.resume()
            assert (await _call(ingress, scope=_scope(method)))[0]["status"] == 204
            assert calls == [method]
            await ingress.aclose()

    asyncio.run(scenario())


def test_upstream_redirect_is_not_followed_even_if_client_default_is_enabled() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(307, headers={"location": "/other"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://worker",
            follow_redirects=True,
        ) as client:
            ingress = ServiceIngress()
            ingress.resume(client)
            sent = await _call(ingress)
            assert sent[0]["status"] == 307
            assert calls == ["/mcp"]
            await ingress.aclose()

    asyncio.run(scenario())


def test_unavailable_refuses_new_request_before_body_intake() -> None:
    async def scenario() -> None:
        ingress = ServiceIngress()
        ingress.unavailable()
        reads = 0
        sent: list[dict] = []

        async def receive() -> dict:
            nonlocal reads
            reads += 1
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        await ingress(_scope(), receive, send)
        assert sent[0]["status"] == 503
        assert reads == 0
        assert ingress.stats["queued"] == 0
        await ingress.aclose()

    asyncio.run(scenario())


def test_get_stream_closes_if_new_worker_denies_original_credential() -> None:
    async def scenario() -> None:
        old_stream = _HeldStream(b"data: old\n\n")
        reattached_auth: list[str] = []

        async def old(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=old_stream
            )

        async def new(request: httpx.Request) -> httpx.Response:
            reattached_auth.append(request.headers["authorization"])
            return httpx.Response(401, content=b"denied")

        async with _client(old) as old_client, _client(new) as new_client:
            ingress = ServiceIngress(IngressLimits(heartbeat_interval=0.01))
            ingress.resume(old_client)
            sent: list[dict] = []
            body_delivered = False

            async def receive() -> dict:
                nonlocal body_delivered
                if not body_delivered:
                    body_delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def send(message: dict) -> None:
                sent.append(message)

            call = asyncio.create_task(
                ingress(
                    _scope(
                        "GET",
                        headers=[
                            (b"host", b"service.example"),
                            (b"authorization", b"Bearer expired"),
                        ],
                    ),
                    receive,
                    send,
                )
            )
            await _until(lambda: ingress.stats["streams"] == 1)
            ingress.pause()
            await ingress.detach_streams()
            ingress.resume(new_client)
            await asyncio.wait_for(call, 1)
            assert reattached_auth == ["Bearer expired"]
            assert ingress.stats["streams"] == 0
            assert b"denied" not in _body(sent)
            await ingress.aclose()

    asyncio.run(scenario())


def test_detached_get_stream_closes_when_worker_becomes_unavailable() -> None:
    async def scenario() -> None:
        stream = _HeldStream(b"data: old\n\n")

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        async with _client(handler) as client:
            ingress = ServiceIngress()
            ingress.resume(client)
            call = asyncio.create_task(_call(ingress, scope=_scope("GET")))
            await _until(lambda: ingress.stats["streams"] == 1)
            ingress.pause()
            await ingress.detach_streams()
            ingress.unavailable()
            await asyncio.wait_for(call, 1)
            assert ingress.stats["streams"] == 0
            await ingress.aclose()

    asyncio.run(scenario())


def test_silent_downstream_disconnect_closes_get_stream_and_releases_slot() -> None:
    async def scenario() -> None:
        stream = _HeldStream(b"data: first\n\n")
        disconnect = asyncio.Event()
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
            return httpx.Response(204)

        async with _client(handler) as client:
            ingress = ServiceIngress(IngressLimits(stream_requests=1))
            ingress.resume(client)
            sent: list[dict] = []
            body_delivered = False

            async def receive() -> dict:
                nonlocal body_delivered
                if not body_delivered:
                    body_delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict) -> None:
                sent.append(message)  # Uvicorn can silently accept sends after disconnect.

            call = asyncio.create_task(ingress(_scope("GET"), receive, send))
            await _until(lambda: b"data: first\n\n" in _body(sent))
            disconnect.set()
            await asyncio.wait_for(call, 1)
            assert stream.closed.is_set()
            assert ingress.stats["streams"] == 0
            second = await _call(ingress, scope=_scope("GET"))
            assert second[0]["status"] == 204
            assert calls == 2
            await ingress.aclose()

    asyncio.run(scenario())


def test_reattached_upstream_closes_if_downstream_send_raises() -> None:
    async def scenario() -> None:
        old_stream = _HeldStream(b"data: old\n\n")
        new_stream = _HeldStream(b"data: new\n\n")

        async def old(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=old_stream
            )

        async def new(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=new_stream
            )

        async with _client(old) as old_client, _client(new) as new_client:
            ingress = ServiceIngress()
            ingress.resume(old_client)
            sent: list[dict] = []
            body_delivered = False

            async def receive() -> dict:
                nonlocal body_delivered
                if not body_delivered:
                    body_delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def send(message: dict) -> None:
                if message.get("body") == b"data: new\n\n":
                    raise ConnectionError("downstream closed")
                sent.append(message)

            call = asyncio.create_task(ingress(_scope("GET"), receive, send))
            await _until(lambda: b"data: old\n\n" in _body(sent))
            ingress.pause()
            await ingress.detach_streams()
            ingress.resume(new_client)
            with pytest.raises(ConnectionError, match="downstream closed"):
                await asyncio.wait_for(call, 1)
            assert new_stream.closed.is_set()
            assert ingress.stats["streams"] == 0
            await ingress.aclose()

    asyncio.run(scenario())


def test_second_upgrade_waits_for_prior_get_reattachment_handshake() -> None:
    async def scenario() -> None:
        old_stream = _HeldStream(b"data: old\n\n")
        new_stream = _HeldStream(b"data: new\n\n")
        third_stream = _HeldStream(b"data: third\n\n")
        entered = asyncio.Event()
        allow_new = asyncio.Event()

        async def old(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=old_stream
            )

        async def new(request: httpx.Request) -> httpx.Response:
            entered.set()
            await allow_new.wait()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=new_stream
            )

        async def third(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=third_stream
            )

        async with (
            _client(old) as old_client,
            _client(new) as new_client,
            _client(third) as third_client,
        ):
            ingress = ServiceIngress()
            ingress.resume(old_client)
            sent: list[dict] = []
            body_delivered = False

            async def receive() -> dict:
                nonlocal body_delivered
                if not body_delivered:
                    body_delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def send(message: dict) -> None:
                sent.append(message)

            call = asyncio.create_task(ingress(_scope("GET"), receive, send))
            await _until(lambda: b"data: old\n\n" in _body(sent))
            ingress.pause()
            await ingress.detach_streams()
            ingress.resume(new_client)
            await asyncio.wait_for(entered.wait(), 1)
            ingress.pause()
            assert await ingress.drain(0.02) is False
            allow_new.set()
            await _until(lambda: b"data: new\n\n" in _body(sent))
            assert await ingress.drain(1) is True
            await ingress.detach_streams()
            ingress.resume(third_client)
            await _until(lambda: b"data: third\n\n" in _body(sent))
            assert ingress.stats["streams"] == 1
            await ingress.aclose()
            await call

    asyncio.run(scenario())
