"""`/metrics.json` beside `/health/ready`, and the additive `observability`
block on the readiness payload."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from exomem import metrics, server


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_REST_API_KEY", "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD",
        "EXOMEM_DISABLE_METRICS",
    ):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


def test_metrics_json_serves_counters_and_histograms(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    metrics.inc_counter("exomem_tool_calls_total", {"tool": "ask_memory", "outcome": "success"})
    client = _client(vault, monkeypatch)

    response = client.get("/metrics.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert "counters" in payload
    assert "histograms" in payload
    assert any(
        c["name"] == "exomem_tool_calls_total" and c["labels"] == {"tool": "ask_memory", "outcome": "success"}
        for c in payload["counters"]
    )


def test_metrics_json_disabled_via_env(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch, EXOMEM_DISABLE_METRICS="1")
    response = client.get("/metrics.json")
    assert response.status_code == 404
    assert response.json()["error"] == "METRICS_DISABLED"


def test_health_ready_includes_observability_block(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(vault, monkeypatch)
    response = client.get("/health/ready")
    payload = response.json()
    assert "observability" in payload
    obs = payload["observability"]
    assert set(obs) == {"log_dir_writable", "metrics_snapshot_age_seconds", "journal_ok"}
    assert obs["log_dir_writable"] is True


def test_disabled_metrics_env_stops_counters_from_accumulating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_METRICS", "1")
    metrics.inc_counter("exomem_tool_calls_total", {"tool": "x", "outcome": "success"})
    metrics.observe_duration_ms("exomem_tool_duration_ms", 5, {"tool": "x"})
    snap = metrics.snapshot()
    assert snap["counters"] == []
    assert snap["histograms"] == []
