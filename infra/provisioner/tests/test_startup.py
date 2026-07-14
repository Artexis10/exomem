from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from exomem_provisioner.logging import ContentFreeFormatter
from exomem_provisioner.main import _create_app, create_app_from_env, run_api


def _set_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_PROVISIONER_BEARER", "b" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_ENVELOPE_KEY", "k" * 32)
    monkeypatch.setenv(
        "EXOMEM_PROVISIONER_DATABASE_URL",
        "postgresql+asyncpg://exomem_provisioner_runtime:secret@database.invalid/exomem",
    )
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_SCHEMA", "exomem_provisioner")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ROLE", "exomem_provisioner_runtime")
    monkeypatch.setenv("EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS", "127.0.0.1")


def test_startup_loads_strict_environment_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_production_environment(monkeypatch)

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
    _set_production_environment(monkeypatch)
    monkeypatch.setenv(
        "EXOMEM_PROVISIONER_TRUSTED_PROXY_IPS",
        "127.0.0.1,10.42.7.9/16",
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
    assert captured["forwarded_allow_ips"] == "127.0.0.1/32,10.42.0.0/16"
    assert captured["log_config"] is None


@pytest.mark.parametrize("entrypoint", [create_app_from_env, run_api])
def test_production_entrypoints_reject_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: object,
) -> None:
    _set_production_environment(monkeypatch)
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        "exomem_provisioner.main.uvicorn.run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not start with SQLite"),
    )

    with pytest.raises(RuntimeError, match="PostgreSQL is required"):
        entrypoint()  # type: ignore[operator]


def test_directly_injected_settings_keep_sqlite_available_for_tests() -> None:
    from exomem_provisioner.config import ProvisionerSettings

    settings = ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url="sqlite+aiosqlite:///:memory:",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )

    app = _create_app(settings)

    assert app.state.database is not None
