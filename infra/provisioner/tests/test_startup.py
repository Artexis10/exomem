from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from exomem_provisioner.logging import ContentFreeFormatter
from exomem_provisioner.main import create_app_from_env, run_api


def test_startup_loads_strict_environment_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_PROVISIONER_BEARER", "b" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_ENVELOPE_KEY", "k" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_SCHEMA", "exomem_provisioner")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ROLE", "exomem_provisioner_runtime")
    monkeypatch.setenv("EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS", "127.0.0.1")

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        app = create_app_from_env()
        assert any(isinstance(handler.formatter, ContentFreeFormatter) for handler in root.handlers)
    finally:
        root.handlers[:] = original_handlers

    assert app.state.database is not None
    assert app.state.repository is not None
    assert {route.path for route in app.routes} >= {
        "/health/live",
        "/health/ready",
        "/cells/provision",
        "/cells/destroy",
    }


def test_startup_fails_closed_when_required_environment_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "EXOMEM_PROVISIONER_BEARER",
        "EXOMEM_PROVISIONER_ENVELOPE_KEY",
        "EXOMEM_PROVISIONER_DATABASE_URL",
        "EXOMEM_PROVISIONER_DATABASE_SCHEMA",
        "EXOMEM_PROVISIONER_DATABASE_ROLE",
        "EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        create_app_from_env()


def test_uvicorn_trusts_only_configured_proxy_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_PROVISIONER_BEARER", "b" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_ENVELOPE_KEY", "k" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_SCHEMA", "exomem_provisioner")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ROLE", "exomem_provisioner_runtime")
    monkeypatch.setenv(
        "EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS",
        "127.0.0.1,10.42.0.0/16",
    )
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr("exomem_provisioner.main.uvicorn.run", fake_run)
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        run_api()
    finally:
        root.handlers[:] = original_handlers

    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "127.0.0.1,10.42.0.0/16"
    assert captured["log_config"] is None
