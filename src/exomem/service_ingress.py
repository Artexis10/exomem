"""Persistent ASGI ingress for a replaceable managed service worker.

The caller owns upstream clients and closes the old one only after finite
requests drain and standalone GET streams detach.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]

_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
_MISSING = object()


@dataclass(frozen=True)
class IngressLimits:
    queue_requests: int = 64
    queue_bytes: int = 64 * 1024 * 1024
    body_bytes: int = 32 * 1024 * 1024
    body_timeout: float = 10.0
    queue_timeout: float = 45.0
    heartbeat_interval: float = 5.0
    stream_requests: int = 64

    def __post_init__(self) -> None:
        for name in ("queue_requests", "queue_bytes", "body_bytes", "stream_requests"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("body_timeout", "queue_timeout", "heartbeat_interval"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not 0 < value < float("inf"):
                raise ValueError(f"{name} must be finite and positive")


@dataclass(eq=False)
class _Stream:
    detach: asyncio.Event = field(default_factory=asyncio.Event)
    detached: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


def _headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    connection_tokens = {
        part.strip().lower()
        for name, value in headers
        if name.lower() == b"connection"
        for part in value.split(b",")
    }
    removed = _HOP_HEADERS | connection_tokens
    return [(name, value) for name, value in headers if name.lower() not in removed]


def _request_id(body: bytes) -> object:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return _MISSING
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or "id" not in value:
        return _MISSING
    request_id = value["id"]
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int, float, type(None))):
        return _MISSING
    return request_id


async def _reply(send: Send, status: int, body: bytes, content_type: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _undispatched(send: Send, status: int, reason: str, body: bytes | None = None) -> None:
    request_id = _request_id(body) if body is not None else _MISSING
    if request_id is not _MISSING:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": f"Request not dispatched: {reason}"},
            },
            separators=(",", ":"),
        ).encode()
        await _reply(send, 200, payload, b"application/json")
    else:
        await _reply(send, status, b"Request not dispatched", b"text/plain; charset=utf-8")


async def _body_stream(receive: Receive, completed: asyncio.Event) -> AsyncIterator[bytes]:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ConnectionError("downstream disconnected during request body")
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        if chunk:
            yield chunk
        if not message.get("more_body", False):
            completed.set()
            return


async def _wait_disconnect(receive: Receive, body_complete: asyncio.Event) -> None:
    await body_complete.wait()
    while (await receive())["type"] != "http.disconnect":
        pass


class ServiceIngress:
    """ASGI proxy with bounded handoff admission and finite-operation ownership."""

    def __init__(self, limits: IngressLimits | None = None) -> None:
        self.limits = limits or IngressLimits()
        self._client: httpx.AsyncClient | None = None
        self._paused = True
        self._closed = False
        self._unavailable = False
        self._ready = asyncio.Event()
        self._active: set[asyncio.Task[None]] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._queued = 0
        self._queued_bytes = 0
        self._get_slots = 0
        self._streams: set[_Stream] = set()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "active": len(self._active),
            "queued": self._queued,
            "queued_bytes": self._queued_bytes,
            "streams": len(self._streams),
        }

    def pause(self) -> None:
        self._paused = True
        self._ready.clear()

    def resume(self, client: httpx.AsyncClient | None = None) -> None:
        if self._closed:
            raise RuntimeError("ingress is closed")
        if client is not None:
            self._client = client
        if self._client is None:
            raise RuntimeError("no worker client installed")
        self._unavailable = False
        self._paused = False
        self._ready.set()

    def unavailable(self) -> None:
        self._client = None
        self.pause()
        self._unavailable = True
        self._ready.set()

    async def drain(self, timeout: float) -> bool:
        if timeout < 0:
            raise ValueError("timeout must be nonnegative")
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def detach_streams(self) -> None:
        streams = tuple(self._streams)
        for stream in streams:
            stream.detach.set()
        if streams:
            await asyncio.gather(*(stream.detached.wait() for stream in streams))

    async def aclose(self) -> None:
        self._closed = True
        self.unavailable()
        streams = tuple(self._streams)
        for stream in streams:
            stream.detach.set()
        await asyncio.gather(
            *self._active, *(s.task for s in streams if s.task), return_exceptions=True
        )

    async def __call__(self, scope: ASGIMessage, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        is_get_stream = scope.get("method") == "GET" and scope.get("path") == "/mcp"
        if is_get_stream:
            if self._get_slots >= self.limits.stream_requests:
                await _undispatched(send, 503, "stream capacity exhausted")
                return
            self._get_slots += 1

        if self._closed or self._unavailable:
            if is_get_stream:
                self._get_slots -= 1
            await _undispatched(send, 503, "worker unavailable")
            return
        if self._paused or self._client is None:
            try:
                client, body = await self._queue(receive, send)
            except BaseException:
                if is_get_stream:
                    self._get_slots -= 1
                raise
            if client is None:
                if is_get_stream:
                    self._get_slots -= 1
                return
        else:
            client, body = self._client, None

        task = asyncio.create_task(self._forward(scope, receive, send, client, body, is_get_stream))
        self._active.add(task)
        self._idle.clear()
        task.add_done_callback(lambda done: self._finished(done, is_get_stream))
        await asyncio.shield(task)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    def _finished(self, task: asyncio.Task[None], is_get_stream: bool) -> None:
        self._active.discard(task)
        if is_get_stream:
            self._get_slots -= 1
        if not self._active:
            self._idle.set()

    async def _queue(self, receive: Receive, send: Send) -> tuple[httpx.AsyncClient | None, bytes]:
        if self._queued >= self.limits.queue_requests:
            await _undispatched(send, 503, "queue full")
            return None, b""
        self._queued += 1  # Reserve before the first body read.
        loop = asyncio.get_running_loop()
        started = loop.time()
        body = bytearray()
        try:
            body_deadline = min(
                started + self.limits.body_timeout, started + self.limits.queue_timeout
            )
            while True:
                remaining = body_deadline - loop.time()
                if remaining <= 0:
                    await _undispatched(send, 408, "body read timed out")
                    return None, b""
                try:
                    # Keep cancellation on this task: Python 3.11 wait_for can
                    # swallow it when the child finishes in the same turn.
                    async with asyncio.timeout(remaining):
                        message = await receive()
                except TimeoutError:
                    await _undispatched(send, 408, "body read timed out")
                    return None, b""
                if message["type"] == "http.disconnect":
                    return None, b""
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                if (
                    len(body) + len(chunk) > self.limits.body_bytes
                    or self._queued_bytes + len(chunk) > self.limits.queue_bytes
                ):
                    await _undispatched(send, 413, "queued body limit exceeded")
                    return None, b""
                body.extend(chunk)
                self._queued_bytes += len(chunk)
                if not message.get("more_body", False):
                    break

            remaining = started + self.limits.queue_timeout - loop.time()
            if remaining <= 0:
                await _undispatched(send, 503, "queue wait timed out", bytes(body))
                return None, b""
            try:
                async with asyncio.timeout(remaining):
                    await self._ready.wait()
            except TimeoutError:
                await _undispatched(send, 503, "queue wait timed out", bytes(body))
                return None, b""
            if self._closed or self._unavailable or self._client is None or self._paused:
                await _undispatched(send, 503, "worker unavailable", bytes(body))
                return None, b""
            return self._client, bytes(body)
        finally:
            self._queued -= 1
            self._queued_bytes -= len(body)

    async def _forward(
        self,
        scope: ASGIMessage,
        receive: Receive,
        send: Send,
        client: httpx.AsyncClient,
        body: bytes | None,
        is_get_stream: bool,
    ) -> None:
        raw_path = scope.get("raw_path") or scope["path"].encode("utf-8")
        query = scope.get("query_string") or b""
        target = raw_path + (b"?" + query if query else b"")
        url = client.base_url.copy_with(raw_path=target)
        headers = _headers(scope.get("headers") or [])
        body_complete = asyncio.Event()
        if body is not None:
            body_complete.set()
        request = httpx.Request(
            scope["method"],
            url,
            headers=headers,
            content=body if body is not None else _body_stream(receive, body_complete),
        )
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except Exception:  # noqa: BLE001 - transport failure has ambiguous dispatch status
            try:
                await _reply(send, 503, b"Worker unavailable", b"text/plain; charset=utf-8")
            except Exception:  # noqa: BLE001 - downstream is gone
                pass
            return

        stream: _Stream | None = None
        if is_get_stream and response.status_code == 200 and _is_sse(response):
            stream = _Stream(task=asyncio.current_task())
            self._streams.add(stream)
            self._active.discard(asyncio.current_task())
            if not self._active:
                self._idle.set()

        deliver = True
        try:
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": _headers(list(response.headers.raw)),
                    }
                )
            except Exception:  # noqa: BLE001 - keep consuming a forwarded finite response
                deliver = False
            if stream is not None:
                if deliver:
                    await self._relay_stream(
                        stream, response, scope, headers, receive, body_complete, send
                    )
            else:
                chunks = (
                    _consumed_body(response)
                    if response.is_stream_consumed
                    else response.aiter_raw()
                )
                async for chunk in chunks:
                    if deliver:
                        try:
                            await send(
                                {"type": "http.response.body", "body": chunk, "more_body": True}
                            )
                        except Exception:  # noqa: BLE001 - keep draining after disconnect
                            deliver = False
            if deliver:
                try:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except Exception:  # noqa: BLE001 - response is already consumed
                    pass
        finally:
            await response.aclose()
            if stream is not None:
                self._streams.discard(stream)
                stream.detached.set()

    async def _relay_stream(
        self,
        stream: _Stream,
        response: httpx.Response,
        scope: ASGIMessage,
        headers: list[tuple[bytes, bytes]],
        receive: Receive,
        body_complete: asyncio.Event,
        send: Send,
    ) -> None:
        disconnect = asyncio.create_task(_wait_disconnect(receive, body_complete))
        try:
            while True:
                iterator = response.aiter_raw().__aiter__()
                while True:
                    next_chunk = asyncio.create_task(anext(iterator))
                    detach = asyncio.create_task(stream.detach.wait())
                    done, _ = await asyncio.wait(
                        {next_chunk, detach, disconnect}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in (next_chunk, detach):
                        if task not in done:
                            task.cancel()
                    # Gather also retrieves an exception if detach and next_chunk finish together.
                    await asyncio.gather(next_chunk, detach, return_exceptions=True)
                    if disconnect in done:
                        return
                    if detach in done:
                        await response.aclose()
                        stream.detached.set()
                        break
                    try:
                        chunk = next_chunk.result()
                    except StopAsyncIteration:
                        return
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})

                while not self._closed and not self._unavailable:
                    ready = asyncio.create_task(self._ready.wait())
                    done, _ = await asyncio.wait(
                        {ready, disconnect},
                        timeout=self.limits.heartbeat_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if ready not in done:
                        ready.cancel()
                    await asyncio.gather(ready, return_exceptions=True)
                    if disconnect in done:
                        return
                    if ready not in done:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b": keepalive\n\n",
                                "more_body": True,
                            }
                        )
                        continue
                    client = self._client
                    if client is None or self._paused:
                        continue
                    target = (scope.get("raw_path") or scope["path"].encode()) + (
                        b"?" + scope["query_string"] if scope.get("query_string") else b""
                    )
                    request = httpx.Request(
                        "GET", client.base_url.copy_with(raw_path=target), headers=headers
                    )
                    assert stream.task is not None
                    self._active.add(stream.task)
                    self._idle.clear()
                    try:
                        try:
                            candidate = await client.send(
                                request, stream=True, follow_redirects=False
                            )
                        except Exception:  # noqa: BLE001 - new worker may disappear
                            return
                        if candidate.status_code != 200 or not _is_sse(candidate):
                            await candidate.aclose()
                            return
                        response = candidate
                        stream.detach.clear()
                        stream.detached.clear()
                    finally:
                        self._active.discard(stream.task)
                        if not self._active:
                            self._idle.set()
                    break
                else:
                    return
        finally:
            disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)
            await response.aclose()


def _is_sse(response: httpx.Response) -> bool:
    return (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        == "text/event-stream"
    )


async def _consumed_body(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.content:
        yield response.content
