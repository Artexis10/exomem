"""Manager half of authenticated local ingress (`add-authenticated-local-ingress`).

The public listener and the loopback-only local listener share one
`ServiceIngress`. Both strip every inbound `x-exomem-internal-*` header; only the
local listener stamps its requests, with the manager's per-process proof, and it
refuses Cloudflare transit, browsers, non-literal-loopback hosts and every path
outside its allowlist before anything reaches the worker.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest

from exomem.service_ingress import (
    INGRESS_KEY_ENV,
    INGRESS_PROOF_HEADER,
    INGRESS_STAMP_HEADER,
    LocalListener,
    ServiceIngress,
    local_refusal,
)

KEY = "manager-proof-key"


def _scope(
    method: str = "POST",
    *,
    path: str = "/mcp",
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode(),
        "query_string": b"",
        "headers": headers if headers is not None else [(b"host", b"127.0.0.1:8764")],
    }


async def _call(app, scope: dict, body: bytes = b"") -> list[dict]:  # noqa: ANN001
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

    await app(scope, receive, send)
    return sent


def _recording_client(seen: list[httpx.Request]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://worker")


def _internal(request: httpx.Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower().startswith("x-exomem-internal-")
    }


# ---- R2: strip on both listeners, stamp only on the local one ------------------


def test_public_listener_strips_every_inbound_internal_header() -> None:
    async def scenario() -> None:
        seen: list[httpx.Request] = []
        async with _recording_client(seen) as client:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(client)
            await _call(
                ingress,
                _scope(
                    headers=[
                        (b"host", b"memory.example"),
                        (b"x-exomem-internal-ingress", b"local"),
                        (b"X-Exomem-Internal-Ingress-Proof", KEY.encode()),
                        (b"x-exomem-internal-anything", b"1"),
                        (b"authorization", b"Bearer token"),
                    ]
                ),
            )
            await ingress.aclose()
        assert len(seen) == 1
        assert _internal(seen[0]) == {}
        assert seen[0].headers["authorization"] == "Bearer token"

    asyncio.run(scenario())


def test_local_listener_stamps_with_the_manager_proof_and_drops_forged_values() -> None:
    async def scenario() -> None:
        seen: list[httpx.Request] = []
        async with _recording_client(seen) as client:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(client)
            await _call(
                LocalListener(ingress),
                _scope(
                    headers=[
                        (b"host", b"127.0.0.1:8764"),
                        (b"x-exomem-internal-ingress", b"public"),
                        (b"x-exomem-internal-ingress-proof", b"guessed"),
                    ]
                ),
            )
            await ingress.aclose()
        assert len(seen) == 1
        assert _internal(seen[0]) == {
            INGRESS_STAMP_HEADER.decode(): "local",
            INGRESS_PROOF_HEADER.decode(): KEY,
        }
        assert seen[0].headers.get_list(INGRESS_STAMP_HEADER.decode()) == ["local"]

    asyncio.run(scenario())


def test_a_client_cannot_mark_its_own_scope_local_on_the_public_listener() -> None:
    async def scenario() -> None:
        seen: list[httpx.Request] = []
        async with _recording_client(seen) as client:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(client)
            # Only the local listener adds the scope marker; a header spelled
            # like it is just another stripped internal header.
            await _call(
                ingress,
                _scope(headers=[(b"host", b"x"), (b"x-exomem-internal-local", b"1")]),
            )
            await ingress.aclose()
        assert _internal(seen[0]) == {}

    asyncio.run(scenario())


def test_a_local_listener_needs_an_ingress_that_can_stamp() -> None:
    with pytest.raises(ValueError):
        LocalListener(ServiceIngress())


# ---- R3: fail closed on transit, browsers, hosts and paths ---------------------


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ([(b"host", b"127.0.0.1:8764"), (b"cf-ray", b"8f00000000000000-AMS")], "transit"),
        ([(b"host", b"127.0.0.1:8764"), (b"CF-Connecting-IP", b"203.0.113.9")], "transit"),
        ([(b"host", b"127.0.0.1:8764"), (b"origin", b"https://evil.example")], "origin"),
        ([(b"host", b"127.0.0.1:8764"), (b"origin", b"null")], "origin"),
        ([], "host"),
        ([(b"host", b"localhost:8764")], "host"),
        ([(b"host", b"evil.example")], "host"),
        ([(b"host", b"127.0.0.1.nip.io:8764")], "host"),
        ([(b"host", b"127.0.0.2:8764")], "host"),
        ([(b"host", b"0.0.0.0:8764")], "host"),
        ([(b"host", b" 127.0.0.1")], "host"),
        ([(b"host", b"127.0.0.1"), (b"host", b"evil.example")], "host"),
    ],
)
def test_local_refusal_rejects_transit_browsers_and_non_literal_hosts(headers, reason) -> None:
    assert local_refusal(_scope(headers=headers)) == reason


@pytest.mark.parametrize("host", [b"127.0.0.1", b"127.0.0.1:8764", b"[::1]", b"[::1]:8764"])
@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/api/ask_memory",
        "/api/openapi.json",
        "/upload",
        "/upload/sessions",
        "/upload/sessions/0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        "/health",
        "/health/ready",
    ],
)
def test_local_refusal_admits_the_allowlist_on_literal_loopback(host, path) -> None:
    assert local_refusal(_scope(path=path, headers=[(b"host", host)])) is None


@pytest.mark.parametrize(
    "raw_path",
    [
        b"/control/promote",
        b"/download",
        b"/metrics.json",
        b"/.well-known/oauth-protected-resource/mcp",
        b"/authorize",
        b"/token",
        b"/mcp/",
        b"/mcp/extra",
        b"/api",
        b"/api/",
        b"/healthz",
        b"/uploads",
        b"/upload/held",
        b"/upload/session",
        b"/upload/sessions/",
        b"/upload/sessions/../../control/promote",
        b"/api/../control/promote",
        b"/api/./ask_memory",
        b"/api//ask_memory",
        b"/api/%2e%2e/control/promote",
        b"/api/ask%5Fmemory",
        b"/api\\ask_memory",
        b"/api/ask_memory\xff",
        b"mcp",
        b"",
    ],
)
def test_local_refusal_rejects_paths_outside_the_allowlist(raw_path) -> None:
    decoded = raw_path.decode("latin-1")
    assert local_refusal(_scope(path=decoded, raw_path=raw_path)) == "path"


@pytest.mark.parametrize(
    ("headers", "path", "status"),
    [
        ([(b"host", b"127.0.0.1:8764"), (b"cf-ray", b"x")], "/mcp", 403),
        ([(b"host", b"127.0.0.1:8764"), (b"origin", b"http://127.0.0.1:8764")], "/mcp", 403),
        ([(b"host", b"localhost:8764")], "/mcp", 403),
        ([(b"host", b"127.0.0.1:8764")], "/control/promote", 404),
        ([(b"host", b"127.0.0.1:8764")], "/download", 404),
    ],
)
def test_a_refused_local_request_never_reaches_the_worker(headers, path, status) -> None:
    async def scenario() -> None:
        seen: list[httpx.Request] = []
        async with _recording_client(seen) as client:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(client)
            sent = await _call(LocalListener(ingress), _scope(path=path, headers=headers))
            await ingress.aclose()
        assert seen == []
        assert sent[0]["status"] == status
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert b"www-authenticate" not in b"".join(n for n, _ in sent[0]["headers"])
        assert KEY.encode() not in body

    asyncio.run(scenario())


# ---- managed-service-upgrades: both listeners pause and drain together ---------


def test_a_local_request_queued_during_handoff_is_forwarded_once_with_the_stamp() -> None:
    async def scenario() -> None:
        old: list[httpx.Request] = []
        new: list[httpx.Request] = []
        async with _recording_client(old) as old_client, _recording_client(new) as new_client:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(old_client)
            ingress.pause()
            call = asyncio.create_task(
                _call(LocalListener(ingress), _scope(), body=b'{"jsonrpc":"2.0","id":1}')
            )
            while ingress.stats["queued"] != 1:
                await asyncio.sleep(0.001)
            ingress.resume(new_client)
            await asyncio.wait_for(call, 1)
            await ingress.aclose()
        assert old == []
        assert len(new) == 1
        assert new[0].headers[INGRESS_STAMP_HEADER.decode()] == "local"

    asyncio.run(scenario())


def test_the_drain_waits_for_a_forwarded_local_request() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            await release.wait()
            return httpx.Response(200, content=b"done")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://worker"
        ) as client:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(client)
            call = asyncio.create_task(_call(LocalListener(ingress), _scope()))
            while ingress.stats["active"] != 1:
                await asyncio.sleep(0.001)
            assert await ingress.drain(0.05) is False
            release.set()
            assert await ingress.drain(1) is True
            await call
            await ingress.aclose()

    asyncio.run(scenario())


# ---- R1: the listener is off by default and never takes the public door down ----


@pytest.fixture
def manager():
    if sys.platform != "linux":
        pytest.skip("Managed lifecycle is Linux-only")
    from exomem import service_manager

    return service_manager


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (None, None)),
        ("", (None, None)),
        ("  ", (None, None)),
        ("8764", (8764, None)),
        (" 8764 ", (8764, None)),
        ("0", (None, "malformed")),
        ("65536", (None, "malformed")),
        ("87a4", (None, "malformed")),
        ("٨٧٦٤", (None, "malformed")),
        ("-1", (None, "malformed")),
        ("8765", (None, "same-as-public")),
    ],
)
def test_the_local_port_setting_is_optional_and_never_fatal(manager, value, expected) -> None:
    environ = {} if value is None else {"EXOMEM_LOCAL_PORT": value}
    assert manager.local_port_setting(8765, environ) == expected


def test_a_busy_local_port_is_reported_not_raised_into_the_public_listener(manager) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        busy = holder.getsockname()[1]
        with pytest.raises(OSError):
            manager.bind_local_listener(busy)
    sock = manager.bind_local_listener(0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_every_spawned_worker_receives_the_managers_proof_over_the_file(
    manager, tmp_path, monkeypatch
) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text(f"{INGRESS_KEY_ENV}=operator-value\nEXOMEM_OTHER=1\n")
    runtime = manager.WorkerRuntime(
        tmp_path / "worker.sock",
        host="127.0.0.1",
        port=1,
        environment_file=env_file,
        ingress_key=KEY,
    )
    environment = runtime._spawn_environment()
    assert environment[INGRESS_KEY_ENV] == KEY
    assert environment["EXOMEM_OTHER"] == "1"
    assert INGRESS_KEY_ENV not in os.environ


def test_a_manager_without_a_local_listener_gives_workers_no_proof(
    manager, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(INGRESS_KEY_ENV, "inherited-from-the-unit")
    runtime = manager.WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=1)
    environment = runtime._spawn_environment()
    assert environment is not None
    assert INGRESS_KEY_ENV not in environment

    env_file = tmp_path / "service.env"
    env_file.write_text(f"{INGRESS_KEY_ENV}=operator-value\n")
    runtime = manager.WorkerRuntime(
        tmp_path / "worker.sock", host="127.0.0.1", port=1, environment_file=env_file
    )
    assert INGRESS_KEY_ENV not in runtime._spawn_environment()


def test_without_any_proof_in_play_the_child_still_inherits(manager, tmp_path, monkeypatch):
    monkeypatch.delenv(INGRESS_KEY_ENV, raising=False)
    runtime = manager.WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=1)
    assert runtime._spawn_environment() is None


class _Records:
    def __init__(self, directory: Path) -> None:
        self.directory = directory


class _Runtime:
    def __init__(self) -> None:
        self.pid = 4242
        self.port = 0

    async def stop(self, timeout: float) -> None:
        return None


class _Supervisor:
    def __init__(self, directory: Path, ingress: ServiceIngress) -> None:
        self.records = _Records(directory)
        self.ingress = ingress
        self.runtime = _Runtime()
        self.phase = "starting"
        self.transition_task = None
        self.local_port = None

    async def start(self) -> None:
        self.phase = "ready"


def test_serve_runs_both_listeners_on_one_ingress_and_stops_them_together(
    manager, tmp_path
) -> None:
    async def scenario() -> None:
        seen: list[httpx.Request] = []
        directory = manager.private_directory(tmp_path / "m")
        async with _recording_client(seen) as worker:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(worker)
            supervisor = _Supervisor(directory, ingress)
            public = manager.bind_local_listener(0)
            public_port = public.getsockname()[1]
            public.close()
            local = manager.bind_local_listener(0)
            local_port = local.getsockname()[1]
            serving = asyncio.create_task(
                manager.serve(
                    supervisor, host="127.0.0.1", port=public_port, local_socket=local
                )
            )
            async with httpx.AsyncClient(trust_env=False) as client:
                async with asyncio.timeout(10):
                    while True:
                        try:
                            await client.get(f"http://127.0.0.1:{public_port}/health")
                            break
                        except httpx.HTTPError:
                            await asyncio.sleep(0.02)
                public_reply = await client.post(
                    f"http://127.0.0.1:{public_port}/mcp",
                    headers={"x-exomem-internal-ingress": "local"},
                )
                local_reply = await client.post(f"http://127.0.0.1:{local_port}/mcp")
                refused = await client.post(
                    f"http://127.0.0.1:{local_port}/mcp", headers={"cf-ray": "x"}
                )
            assert public_reply.status_code == 200
            assert local_reply.status_code == 200
            assert refused.status_code == 403
            assert supervisor.local_port == local_port
            stamped = [_internal(request) for request in seen if request.url.path == "/mcp"]
            assert stamped == [
                {},
                {INGRESS_STAMP_HEADER.decode(): "local", INGRESS_PROOF_HEADER.decode(): KEY},
            ]
            # A crashed worker stops the supervisor; both doors close with it.
            supervisor.runtime.pid = 0
            await asyncio.wait_for(serving, 20)
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", local_port)) != 0

    asyncio.run(scenario())
