"""Error-plane capture: every site that converts an `OpError`/exception into a
response envelope (the MCP tool wrapper, the REST facade, the hosted command
routes) logs a failure event and bumps metrics counters BEFORE returning the
envelope, and the envelope stays byte-identical. The MCP tool wrapper cannot
signal `CallTraceMiddleware` via a ContextVar (anyio's threadpool copies
context in, not out), so it uses a bounded, locked dict instead.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from exomem import command_surface, metrics, server
from exomem import server as server_module
from exomem.cli_ops import OpError


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(autouse=True)
def _clear_tool_failures():
    command_surface._TOOL_FAILURES.clear()
    yield
    command_surface._TOOL_FAILURES.clear()


def _bound(monkeypatch, *, read_only: bool = False):
    def leaf(vault):  # noqa: ANN001, ARG001
        raise AssertionError("invoke seam should replace the leaf")

    command = SimpleNamespace(name="mutate", leaf=leaf, read_only=read_only)
    bound = command_surface.bind_vault(leaf, object(), command=command)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "bearer:abc")
    monkeypatch.setattr(
        command_surface, "mcp_request_id", lambda: "11111111-1111-4111-8111-111111111111"
    )
    return bound


def _counters(snap):
    return {(c["name"], tuple(sorted(c["labels"].items()))): c["value"] for c in snap["counters"]}


# --- MCP tool wrapper (command_surface.py) ---------------------------------


def test_successful_call_bumps_success_counter_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = _bound(monkeypatch)
    monkeypatch.setattr("exomem.writer_lease.invoke_command", lambda *a, **k: {"ok": True})

    result = bound()

    assert result == {"ok": True}
    counters = _counters(metrics.snapshot())
    assert counters[("exomem_tool_calls_total", (("outcome", "success"), ("tool", "mutate")))] == 1
    assert ("exomem_tool_calls_total", (("outcome", "failure"), ("tool", "mutate"))) not in counters


def test_op_error_logs_tool_failure_and_bumps_metrics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bound = _bound(monkeypatch)
    public_error = OpError("MUTATION_BUSY", "vault mutation boundary is busy")
    monkeypatch.setattr(
        "exomem.writer_lease.invoke_command",
        lambda *a, **k: (_ for _ in ()).throw(public_error),
    )

    caplog.set_level(logging.WARNING)
    result = bound()

    assert result["success"] is False
    assert result["error"] == public_error.as_public_dict()

    record = next(r for r in caplog.records if getattr(r, "event", None) == "tool_failure")
    assert record.fields["tool"] == "mutate"
    assert record.fields["request_id"] == "11111111-1111-4111-8111-111111111111"
    assert record.fields["code"] == "MUTATION_BUSY"
    assert record.fields["scope"] == "bearer"
    assert isinstance(record.fields["duration_ms"], float)
    assert record.content == {"message": "vault mutation boundary is busy"}

    snap = metrics.snapshot()
    counters = {(c["name"], tuple(sorted(c["labels"].items()))): c["value"] for c in snap["counters"]}
    assert counters[("exomem_tool_calls_total", (("outcome", "failure"), ("tool", "mutate")))] == 1
    assert counters[("exomem_tool_failures_total", (("code", "MUTATION_BUSY"), ("tool", "mutate")))] == 1


def test_op_error_message_truncated_to_300_chars(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bound = _bound(monkeypatch)
    long_message = "x" * 5000
    monkeypatch.setattr(
        "exomem.writer_lease.invoke_command",
        lambda *a, **k: (_ for _ in ()).throw(OpError("OP_ERROR", long_message)),
    )
    caplog.set_level(logging.WARNING)
    bound()
    record = next(r for r in caplog.records if getattr(r, "event", None) == "tool_failure")
    assert len(record.content["message"]) == 300


def test_op_error_records_signal_for_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = _bound(monkeypatch)
    monkeypatch.setattr(
        "exomem.writer_lease.invoke_command",
        lambda *a, **k: (_ for _ in ()).throw(OpError("MUTATION_BUSY", "busy")),
    )
    bound()
    failure = command_surface.pop_tool_failure("11111111-1111-4111-8111-111111111111")
    assert failure is not None
    assert failure["code"] == "MUTATION_BUSY"
    # Unconditional pop: a second pop for the same request id finds nothing.
    assert command_surface.pop_tool_failure("11111111-1111-4111-8111-111111111111") is None


def test_unexpected_exception_does_not_log_tool_failure_or_record_signal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bound = _bound(monkeypatch)
    monkeypatch.setattr(
        "exomem.writer_lease.invoke_command",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unexpected boom")),
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError, match="unexpected boom"):
        bound()
    assert not any(getattr(r, "event", None) == "tool_failure" for r in caplog.records)
    assert command_surface.pop_tool_failure("11111111-1111-4111-8111-111111111111") is None


def _record_failure_like_the_wrapper(request_id: str, code: str) -> None:
    """Record exactly as `_log_tool_failure` does: keyed by the per-call token
    the middleware minted, with the request id only as a fallback."""
    command_surface._record_tool_failure(
        command_surface._MCP_CALL_TOKEN.get() or request_id, code
    )


def test_calls_sharing_request_id_attribute_failures_to_the_right_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client-supplied `x-exomem-request-id` is UUIDv4-shape-validated, not
    unique — concurrent tool calls can legitimately share one request id. The
    failure signal is keyed by the per-call token the middleware mints, so
    each call pops exactly its own failure: both are logged as tool_failure,
    each with ITS OWN code against ITS OWN tool name — never swapped, never
    clobbered, regardless of interleaving."""
    shared_request_id = "44444444-4444-4444-8444-444444444444"
    middleware = server_module.CallTraceMiddleware()

    import exomem.command_surface as cs_module

    original = cs_module.mcp_request_id
    cs_module.mcp_request_id = lambda: shared_request_id
    try:
        def failing_call(code: str):  # noqa: ANN202
            async def call_next(_context):  # noqa: ANN001
                _record_failure_like_the_wrapper(shared_request_id, code)
                return {"success": False, "error": {}}

            return call_next

        context_a = SimpleNamespace(message={"params": {"name": "remember", "arguments": {}}})
        context_b = SimpleNamespace(message={"params": {"name": "find", "arguments": {}}})
        with caplog.at_level(logging.INFO, logger="exomem.calls"):
            asyncio.run(middleware.on_call_tool(context_a, failing_call("MUTATION_BUSY")))
            asyncio.run(
                middleware.on_call_tool(context_b, failing_call("WRITER_LEASE_REQUIRED"))
            )
    finally:
        cs_module.mcp_request_id = original

    lines = caplog.text.splitlines()
    failure_lines = [line for line in lines if "event=tool_failure" in line]
    success_lines = [line for line in lines if "event=tool_success" in line]
    assert len(failure_lines) == 2, caplog.text
    assert success_lines == [], caplog.text
    assert "tool=remember" in failure_lines[0]
    assert "code=MUTATION_BUSY" in failure_lines[0]
    assert "tool=find" in failure_lines[1]
    assert "code=WRITER_LEASE_REQUIRED" in failure_lines[1]


def test_success_sharing_request_id_never_pops_a_failing_calls_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mixed interleaving: one call fails, a concurrent call sharing the same
    request id succeeds. Under request-id keying the succeeding call could pop
    the failure marker — logging tool_failure for the healthy tool and
    tool_success for the failed one. Token keying makes that impossible."""
    shared_request_id = "55555555-5555-4555-8555-555555555555"
    middleware = server_module.CallTraceMiddleware()

    import exomem.command_surface as cs_module

    original = cs_module.mcp_request_id
    cs_module.mcp_request_id = lambda: shared_request_id
    try:
        async def failing_call(_context):  # noqa: ANN001
            _record_failure_like_the_wrapper(shared_request_id, "MUTATION_BUSY")
            return {"success": False, "error": {}}

        async def succeeding_call(_context):  # noqa: ANN001
            return {"success": True, "data": {}}

        failing_context = SimpleNamespace(
            message={"params": {"name": "remember", "arguments": {}}}
        )
        succeeding_context = SimpleNamespace(
            message={"params": {"name": "find", "arguments": {}}}
        )
        with caplog.at_level(logging.INFO, logger="exomem.calls"):
            asyncio.run(middleware.on_call_tool(failing_context, failing_call))
            asyncio.run(middleware.on_call_tool(succeeding_context, succeeding_call))
    finally:
        cs_module.mcp_request_id = original

    lines = caplog.text.splitlines()
    failure_lines = [line for line in lines if "event=tool_failure" in line]
    success_lines = [line for line in lines if "event=tool_success" in line]
    assert len(failure_lines) == 1, caplog.text
    assert "tool=remember" in failure_lines[0]
    assert "code=MUTATION_BUSY" in failure_lines[0]
    assert len(success_lines) == 1, caplog.text
    assert "tool=find" in success_lines[0]


def test_tool_failure_ttl_sweep_drops_stale_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    # stale-request is recorded, then 310s (> the 300s TTL) pass before
    # fresh-request is recorded — the sweep inside that second call drops
    # stale-request. fresh-request is then only 10s old and survives.
    clock = iter([0.0, 310.0, 320.0, 320.0])
    monkeypatch.setattr(command_surface.time, "monotonic", lambda: next(clock))
    command_surface._record_tool_failure("stale-request", "MUTATION_BUSY")
    command_surface._record_tool_failure("fresh-request", "MUTATION_BUSY")
    assert command_surface.pop_tool_failure("stale-request") is None
    assert command_surface.pop_tool_failure("fresh-request") is not None


# --- CallTraceMiddleware (server.py) ----------------------------------------


def test_middleware_logs_tool_failure_when_wrapper_recorded_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = server_module.CallTraceMiddleware()
    context = SimpleNamespace(message={"params": {"name": "remember", "arguments": {}}})
    request_id = "22222222-2222-4222-8222-222222222222"

    async def call_next(_context):  # noqa: ANN001
        _record_failure_like_the_wrapper(
            command_surface.mcp_request_id(), "MUTATION_BUSY"
        )
        return {"success": False, "error": {"code": "MUTATION_BUSY"}}

    import exomem.command_surface as cs_module

    original = cs_module.mcp_request_id
    cs_module.mcp_request_id = lambda: request_id
    try:
        with caplog.at_level(logging.INFO, logger="exomem.calls"):
            asyncio.run(middleware.on_call_tool(context, call_next))
    finally:
        cs_module.mcp_request_id = original

    assert "event=tool_failure" in caplog.text
    assert "code=MUTATION_BUSY" in caplog.text
    assert "event=tool_success" not in caplog.text


def test_middleware_logs_tool_success_when_no_failure_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = server_module.CallTraceMiddleware()
    context = SimpleNamespace(message={"params": {"name": "remember", "arguments": {}}})

    async def call_next(_context):  # noqa: ANN001
        return {"success": True, "data": {}}

    with caplog.at_level(logging.INFO, logger="exomem.calls"):
        asyncio.run(middleware.on_call_tool(context, call_next))

    assert "event=tool_success" in caplog.text
    assert "event=tool_failure" not in caplog.text


def test_middleware_preserves_hosted_call_prefix_for_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = server_module.CallTraceMiddleware(hosted=True)
    context = SimpleNamespace(message={"params": {"name": "remember", "arguments": {}}})
    request_id = "33333333-3333-4333-8333-333333333333"

    async def call_next(_context):  # noqa: ANN001
        _record_failure_like_the_wrapper(
            command_surface.mcp_request_id(), "MUTATION_BUSY"
        )
        return {"success": False, "error": {"code": "MUTATION_BUSY"}}

    import exomem.command_surface as cs_module

    original = cs_module.mcp_request_id
    cs_module.mcp_request_id = lambda: request_id
    try:
        with caplog.at_level(logging.INFO, logger="exomem.calls"):
            asyncio.run(middleware.on_call_tool(context, call_next))
    finally:
        cs_module.mcp_request_id = original

    assert "event=hosted_call kind=tool_failure" in caplog.text


# --- REST facade (server_rest.py) ------------------------------------------


def _rest_client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_REST_API_KEY", "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def test_rest_op_error_logs_rest_failure_and_bumps_metrics(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _rest_client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")

    caplog.set_level(logging.WARNING)
    response = client.post(
        "/api/remember",
        json={"note_type": "research-note", "title": "no project", "content": "x"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["success"] is False

    record = next(r for r in caplog.records if getattr(r, "event", None) == "rest_failure")
    assert record.fields["tool"] == "remember"
    assert record.fields["code"] == payload["error"]["code"]

    snap = metrics.snapshot()
    counters = {(c["name"], tuple(sorted(c["labels"].items()))): c["value"] for c in snap["counters"]}
    assert counters[
        ("exomem_tool_failures_total", (("code", payload["error"]["code"]), ("tool", "remember")))
    ] == 1


def test_rest_success_bumps_success_counter_exactly_once(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _rest_client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")

    response = client.post(
        "/api/ask_memory",
        json={"query": "metabolism", "mode": "keyword", "detail": "full"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 200, response.text
    counters = _counters(metrics.snapshot())
    assert counters[
        ("exomem_tool_calls_total", (("outcome", "success"), ("tool", "ask_memory")))
    ] == 1
    assert (
        "exomem_tool_calls_total",
        (("outcome", "failure"), ("tool", "ask_memory")),
    ) not in counters


# --- Hosted command routes (server_hosted.py) -------------------------------


def test_hosted_error_response_bumps_failure_metrics_with_expected_labels() -> None:
    from exomem import server_hosted

    config = SimpleNamespace(cell_id="cell-metrics-test")
    started = 0.0

    server_hosted._error_response(
        "MUTATION_BUSY",
        config=config,
        operation="remember",
        started=started,
    )

    counters = _counters(metrics.snapshot())
    assert counters[
        ("exomem_tool_calls_total", (("outcome", "failure"), ("tool", "remember")))
    ] == 1
    assert counters[
        ("exomem_tool_failures_total", (("code", "MUTATION_BUSY"), ("tool", "remember")))
    ] == 1
    duration_hist = next(
        h
        for h in metrics.snapshot()["histograms"]
        if h["name"] == "exomem_tool_duration_ms" and h["labels"] == {"tool": "remember"}
    )
    assert duration_hist["count"] == 1


def test_hosted_success_response_bumps_success_metrics_with_expected_labels() -> None:
    from exomem import server_hosted

    config = SimpleNamespace(cell_id="cell-metrics-test")

    server_hosted._success_response(
        {"ok": True}, config=config, operation="ready", request_id="req-1", started=0.0
    )

    counters = _counters(metrics.snapshot())
    assert counters[
        ("exomem_tool_calls_total", (("outcome", "success"), ("tool", "ready")))
    ] == 1


def test_hosted_metrics_bump_swallows_errors_and_logs_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from exomem import server_hosted

    def boom(*_a, **_k):
        raise RuntimeError("metrics boom")

    monkeypatch.setattr(metrics, "inc_counter", boom)
    config = SimpleNamespace(cell_id="cell-metrics-test")

    caplog.set_level(logging.DEBUG)
    # Must not raise even though the metrics call is broken.
    server_hosted._error_response(
        "MUTATION_BUSY", config=config, operation="remember", started=0.0
    )

    assert any(
        getattr(r, "event", None) == "observability_internal_error" for r in caplog.records
    )
