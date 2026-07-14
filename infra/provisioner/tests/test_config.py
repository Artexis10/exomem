from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from exomem_provisioner.config import PROVISIONER_PROTOCOL, ProvisionerSettings
from exomem_provisioner.logging import ContentFreeFormatter


def _settings(**overrides: object) -> ProvisionerSettings:
    values: dict[str, object] = {
        "bearer": "b" * 32,
        "envelope_key": "k" * 32,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "database_schema": "exomem_provisioner",
        "database_role": "exomem_provisioner_runtime",
    }
    values.update(overrides)
    return ProvisionerSettings(**values)


def test_settings_require_independent_long_secrets_and_exact_protocol() -> None:
    settings = _settings()

    assert settings.protocol == PROVISIONER_PROTOCOL
    assert settings.bearer.get_secret_value() == "b" * 32
    assert settings.envelope_key.get_secret_value() == "k" * 32

    for field in ("bearer", "envelope_key"):
        with pytest.raises(ValidationError):
            _settings(**{field: "too-short"})
    with pytest.raises(ValidationError):
        _settings(protocol="exomem-cell-provisioner.v2")
    with pytest.raises(ValidationError):
        _settings(bearer="s" * 32, envelope_key="s" * 32)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_schema", "public; drop schema public"),
        ("database_role", "postgres"),
        ("database_role", "role with spaces"),
    ],
)
def test_settings_reject_unsafe_or_shared_database_identifiers(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_content_free_formatter_never_renders_sensitive_values() -> None:
    formatter = ContentFreeFormatter()
    record = logging.LogRecord(
        "provisioner",
        logging.INFO,
        __file__,
        1,
        "operation",
        (),
        None,
    )
    record.event = "operation_submitted"  # type: ignore[attr-defined]
    record.action = "provision"  # type: ignore[attr-defined]
    record.operation_id = "operation-1"  # type: ignore[attr-defined]
    record.authorization = "Bearer secret-sentinel"  # type: ignore[attr-defined]
    record.serviceCredential = "credential-sentinel"  # type: ignore[attr-defined]
    record.note = "private note sentinel"  # type: ignore[attr-defined]

    rendered = formatter.format(record)

    assert "operation_submitted" in rendered
    assert "operation-1" in rendered
    assert "secret-sentinel" not in rendered
    assert "credential-sentinel" not in rendered
    assert "private note sentinel" not in rendered
    assert "authorization" not in rendered


def test_settings_repr_redacts_database_credentials() -> None:
    settings = _settings(
        database_url=(
            "postgresql+asyncpg://provisioner:database-password-sentinel@database.invalid/exomem"
        )
    )

    assert "database-password-sentinel" not in repr(settings)
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
