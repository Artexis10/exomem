"""`bootstrap` carries a `latency` block only while the calling client's recalls breach
their ceiling, computed from the in-process watch and never from a file."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import command_surface, commands, latency_watch

T0 = 1_800_000_000.0
AFTER_GRACE = T0 + latency_watch.STARTUP_GRACE_SECONDS + 1.0


@pytest.fixture
def watch():
    watch = latency_watch.reset(started_at=T0, clock=lambda: AFTER_GRACE)
    yield watch
    latency_watch.reset()


def _identity(monkeypatch: pytest.MonkeyPatch, client: str | None) -> None:
    monkeypatch.setattr(
        command_surface, "mcp_caller_identity",
        lambda: {"client_name": client, "client_version": None, "transport": None, "session_id": None},
    )


def _breach(watch, client: str) -> None:
    for _ in range(25):
        watch.observe(tool="ask_memory", client=client, deep=False, total_ms=3000,
                      spans=[{"name": "embeddings.matrix_load", "ms": 2500}])


@pytest.mark.parametrize("profile", ["compact", "full", "diagnostics", "session"])
def test_a_healthy_service_leaves_every_profile_unchanged(
    vault: Path, monkeypatch: pytest.MonkeyPatch, watch, profile: str
) -> None:
    _identity(monkeypatch, "openai-mcp/1.0.0")
    assert "latency" not in commands.op_bootstrap(vault, profile=profile)


def test_a_breaching_client_is_told_what_is_slow(
    vault: Path, monkeypatch: pytest.MonkeyPatch, watch
) -> None:
    _identity(monkeypatch, "openai-mcp/1.0.0")
    _breach(watch, "openai-mcp/1.0.0")
    out = commands.op_bootstrap(vault)
    [row] = out["latency"]
    assert row["tool"] == "ask_memory" and row["deep"] is False
    assert row["samples"] == 25 and row["p90_ms"] == 3000 and row["ceiling_ms"] == 1000
    assert row["dominant_spans"][0]["name"] == "embeddings.matrix_load"
    assert set(row) == {"tool", "deep", "samples", "p50_ms", "p90_ms", "ceiling_ms", "dominant_spans"}


def test_another_clients_breach_is_not_this_clients(
    vault: Path, monkeypatch: pytest.MonkeyPatch, watch
) -> None:
    _identity(monkeypatch, "claude-code")
    _breach(watch, "openai-mcp/1.0.0")
    assert "latency" not in commands.op_bootstrap(vault)


def test_the_block_survives_the_session_projection(
    vault: Path, monkeypatch: pytest.MonkeyPatch, watch
) -> None:
    _identity(monkeypatch, "openai-mcp/1.0.0")
    _breach(watch, "openai-mcp/1.0.0")
    out = commands.op_bootstrap(vault, profile="session")
    assert out.get("profile") in ("session", "compact")
    assert "latency" in out
