"""Pure-ASGI access log: one structured `event=http_request` record per HTTP
request, request-id correlation injected both into the request scope (so
downstream MCP code resolves the same id) and the response headers.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from exomem import access_log, metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


async def _receive() -> dict:
    return {"type": "http.disconnect"}


def _scope(*, method: str = "GET", path: str = "/health/ready", headers: dict[str, str] | None = None, client=("10.0.0.5", 5555)) -> dict:
    encoded = [
        (name.lower().encode("ascii"), value.encode("ascii")) for name, value in (headers or {}).items()
    ]
    return {"type": "http", "method": method, "path": path, "headers": encoded, "client": client}


def _run(scope: dict, *, downstream_status: int = 200):
    seen_scopes: list[dict] = []

    async def inner_app(inner_scope, receive, send) -> None:  # noqa: ANN001
        seen_scopes.append(inner_scope)
        await send({"type": "http.response.start", "status": downstream_status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    sent: list[dict] = []

    async def capture(message: dict) -> None:
        sent.append(message)

    middleware = access_log.AccessLogMiddleware(inner_app)

    async def scenario() -> None:
        await middleware(scope, _receive, capture)

    asyncio.run(scenario())
    return seen_scopes, sent


def _response_headers(sent: list[dict]) -> dict[bytes, bytes]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"])


def test_mints_and_injects_request_id_when_absent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="exomem.access")
    scope = _scope()
    seen_scopes, sent = _run(scope)

    downstream_headers = dict(seen_scopes[0]["headers"])
    minted = downstream_headers[b"x-exomem-request-id"].decode("latin-1")
    assert minted  # a value was minted

    response_headers = _response_headers(sent)
    assert response_headers[b"x-exomem-request-id"].decode("latin-1") == minted

    record = next(r for r in caplog.records if getattr(r, "event", None) == "http_request")
    assert record.fields["request_id"] == minted


def test_preserves_valid_client_supplied_request_id() -> None:
    supplied = "11111111-1111-4111-8111-111111111111"
    scope = _scope(headers={"x-exomem-request-id": supplied})
    seen_scopes, sent = _run(scope)

    downstream_headers = dict(seen_scopes[0]["headers"])
    assert downstream_headers[b"x-exomem-request-id"].decode("latin-1") == supplied
    assert _response_headers(sent)[b"x-exomem-request-id"].decode("latin-1") == supplied


def test_invalid_client_supplied_request_id_is_replaced() -> None:
    scope = _scope(headers={"x-exomem-request-id": "attacker supplied log content"})
    seen_scopes, sent = _run(scope)
    downstream_headers = dict(seen_scopes[0]["headers"])
    minted = downstream_headers[b"x-exomem-request-id"].decode("latin-1")
    assert minted != "attacker supplied log content"
    import uuid

    uuid.UUID(minted)  # does not raise


def test_logs_method_path_status_and_duration(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="exomem.access")
    scope = _scope(method="POST", path="/api/remember")
    _run(scope, downstream_status=400)

    record = next(r for r in caplog.records if getattr(r, "event", None) == "http_request")
    assert record.fields["method"] == "POST"
    # The raw path is client-controlled text, so it is content-classified:
    # kept locally, dropped by the hosted privacy boundary.
    assert record.content["path"] == "/api/remember"
    assert "path" not in record.fields
    assert record.fields["status"] == 400
    assert isinstance(record.fields["duration_ms"], float)


def test_session_id_and_cf_ray_included_when_present(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="exomem.access")
    scope = _scope(headers={"mcp-session-id": "sess-abc", "cf-ray": "ray-123"})
    _run(scope)

    record = next(r for r in caplog.records if getattr(r, "event", None) == "http_request")
    assert record.fields["session_id"] == "sess-abc"
    assert record.fields["cf_ray"] == "ray-123"


def test_client_ip_lands_in_content_not_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="exomem.access")
    scope = _scope(client=("203.0.113.7", 12345))
    _run(scope)

    record = next(r for r in caplog.records if getattr(r, "event", None) == "http_request")
    assert "client_ip" not in record.fields
    assert record.content["client_ip"] == "203.0.113.7"
    # `path` is also content-classified (client-controlled text).
    assert set(record.content) == {"client_ip", "path"}


def test_non_http_scope_passes_through_untouched() -> None:
    async def inner_app(scope, receive, send) -> None:  # noqa: ANN001
        pass

    middleware = access_log.AccessLogMiddleware(inner_app)

    async def scenario() -> None:
        await middleware({"type": "lifespan"}, _receive, lambda message: asyncio.sleep(0))

    asyncio.run(scenario())  # must not raise


def test_disabled_via_env_skips_logging_and_header_injection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_ACCESS_LOG", "1")
    caplog.set_level(logging.INFO, logger="exomem.access")
    scope = _scope()
    seen_scopes, _sent = _run(scope)

    assert not any(getattr(r, "event", None) == "http_request" for r in caplog.records)
    downstream_headers = dict(seen_scopes[0]["headers"])
    assert b"x-exomem-request-id" not in downstream_headers


def test_bumps_http_requests_total_by_status() -> None:
    _run(_scope(), downstream_status=200)
    _run(_scope(), downstream_status=200)
    _run(_scope(), downstream_status=500)

    snap = metrics.snapshot()
    counters = {(c["name"], tuple(sorted(c["labels"].items()))): c["value"] for c in snap["counters"]}
    assert counters[("exomem_http_requests_total", (("status", "200"),))] == 2
    assert counters[("exomem_http_requests_total", (("status", "500"),))] == 1
