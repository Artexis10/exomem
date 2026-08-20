"""Origin-side detection for probes continuing without successful MCP traffic."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from fastmcp import FastMCP
from starlette.testclient import TestClient

from exomem import doctor as doctor_module
from exomem import runtime_readiness, server, server_assets


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _monitor(clock: Clock):
    return runtime_readiness.SilentTrafficMonitor(
        clock=clock,
        window_seconds=10.0,
        minimum_health_probes=3,
    )


def _raise_signal(monitor, clock: Clock) -> dict:
    monitor.record_health_probe()
    clock.advance(5.0)
    monitor.record_health_probe()
    clock.advance(5.0)
    return monitor.record_health_probe()


def test_probes_without_tool_traffic_raise_the_signal() -> None:
    clock = Clock()
    monitor = _monitor(clock)

    snapshot = _raise_signal(monitor, clock)

    assert snapshot["suspected_silent_outage"] is True
    assert snapshot["health_probe_count"] == 3
    assert snapshot["successful_tool_call_count"] == 0
    assert snapshot["health_probes_since_last_tool_call"] == 3
    assert monitor.last_health_probe_at == 10.0
    assert monitor.last_successful_tool_call_at is None


def test_genuinely_idle_server_does_not_raise_the_signal() -> None:
    clock = Clock()
    monitor = _monitor(clock)

    clock.advance(100.0)
    snapshot = monitor.snapshot()

    assert snapshot["suspected_silent_outage"] is False
    assert snapshot["health_probe_count"] == 0
    assert snapshot["successful_tool_call_count"] == 0


def test_successful_tool_traffic_clears_signal_and_logs_once(
    caplog, monkeypatch
) -> None:
    clock = Clock()
    monitor = _monitor(clock)
    _raise_signal(monitor, clock)
    monkeypatch.setattr(server, "_record_ledger_row", lambda **_kwargs: None)
    middleware = server.CallTraceMiddleware(traffic_monitor=monitor)
    context = SimpleNamespace(
        message={"params": {"name": "ask_memory", "arguments": {}}}
    )

    async def call_next(_context):
        return {"ok": True}

    caplog.set_level(logging.INFO, logger="exomem.runtime_readiness")
    asyncio.run(middleware.on_call_tool(context, call_next))
    asyncio.run(middleware.on_call_tool(context, call_next))

    snapshot = monitor.snapshot()
    assert snapshot["suspected_silent_outage"] is False
    assert snapshot["successful_tool_call_count"] == 2
    assert caplog.text.count("event=silent_traffic_outage_cleared") == 1


def test_signal_is_reported_but_readiness_stays_ready() -> None:
    clock = Clock()
    traffic = _raise_signal(_monitor(clock), clock)

    snapshot = runtime_readiness.build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="a" * 64,
        traffic=traffic,
    )

    assert snapshot["status"] == "ready"
    assert snapshot["reasons"] == []
    assert snapshot["traffic"]["suspected_silent_outage"] is True

    check = doctor_module._check_silent_traffic_payload(snapshot)
    rendered = doctor_module.render_human(
        doctor_module.DoctorReport(profile="remote", checks=[check])
    )
    assert check.status == "warn"
    assert "edge" in rendered.lower()
    assert "tunnel" in rendered.lower()
    assert "route" in rendered.lower()


def test_warning_does_not_repeat_per_probe(caplog) -> None:
    clock = Clock()
    monitor = _monitor(clock)
    caplog.set_level(logging.WARNING, logger="exomem.runtime_readiness")

    _raise_signal(monitor, clock)
    for _ in range(20):
        clock.advance(1.0)
        monitor.record_health_probe()

    assert caplog.text.count("event=silent_traffic_outage_suspected") == 1


def test_health_routes_record_probes_with_the_injected_monitor(monkeypatch) -> None:
    clock = Clock()
    monitor = _monitor(clock)
    mcp = FastMCP("traffic-test")

    def fake_readiness(*, mcp_tool_surface_sha256, traffic):
        assert mcp_tool_surface_sha256 is not None
        return {"status": "ready", "reasons": [], "traffic": traffic}

    monkeypatch.setattr(runtime_readiness, "runtime_readiness", fake_readiness)
    server_assets.register_asset_routes(mcp, traffic_monitor=monitor)
    client = TestClient(mcp.http_app())

    assert client.get("/health").status_code == 200
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["traffic"]["health_probe_count"] == 2


def test_doctor_probe_renders_signal_up_and_down(monkeypatch) -> None:
    healthy = {
        "status": "ready",
        "reasons": [],
        "traffic": {
            "suspected_silent_outage": False,
            "health_probe_count": 8,
            "successful_tool_call_count": 2,
            "health_probes_since_last_tool_call": 1,
            "probe_window_seconds": 0.0,
            "window_seconds": 86_400.0,
            "minimum_health_probes": 288,
        },
    }
    suspicious = {
        **healthy,
        "traffic": {
            **healthy["traffic"],
            "suspected_silent_outage": True,
            "health_probe_count": 19_000,
            "health_probes_since_last_tool_call": 19_000,
            "probe_window_seconds": 90_000.0,
        },
    }
    responses = iter(((200, healthy), (200, suspicious)))
    monkeypatch.setattr(doctor_module, "_probe_get", lambda _url: next(responses))

    down = doctor_module._check_probe_traffic()
    up = doctor_module._check_probe_traffic()

    assert down.status == "pass"
    assert up.status == "warn"
    assert "edge" in (up.remediation or "").lower()
    assert "tunnel" in (up.remediation or "").lower()
    assert "route" in (up.remediation or "").lower()
