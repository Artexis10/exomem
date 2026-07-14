from __future__ import annotations

import pytest
from pydantic import ValidationError

from exomem_provisioner.main import create_app_from_env


def test_startup_loads_strict_environment_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_PROVISIONER_BEARER", "b" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_ENVELOPE_KEY", "k" * 32)
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_SCHEMA", "exomem_provisioner")
    monkeypatch.setenv("EXOMEM_PROVISIONER_DATABASE_ROLE", "exomem_provisioner_runtime")

    app = create_app_from_env()

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
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        create_app_from_env()
