"""Least-authority environment boundary for init-retry recovery."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url

from .config import DeploymentLock
from .database_bootstrap import DatabaseBootstrapError, validate_runtime_database_url

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_RECOVERY_ENVIRONMENT = {
    "EXOMEM_RECOVERY_DATABASE_URL": "database_url",
    "EXOMEM_RECOVERY_DATABASE_SCHEMA": "database_schema",
    "EXOMEM_RECOVERY_DATABASE_ROLE": "database_role",
    "EXOMEM_RECOVERY_DATABASE_LOCK_TIMEOUT_SECONDS": "database_lock_timeout_seconds",
    "EXOMEM_RECOVERY_ENVELOPE_KEY": "envelope_key",
    "EXOMEM_RECOVERY_PROVIDER_RECOVERY_PUBLIC_KEY": "provider_recovery_public_key",
    "EXOMEM_RECOVERY_DEPLOYMENT_LOCK_JSON": "deployment_lock",
    "EXOMEM_RECOVERY_SOURCE_DEPLOYMENT_LOCK_JSON": "source_deployment_lock",
    "EXOMEM_RECOVERY_RUNTIME_SELECTION": "runtime_selection",
    "EXOMEM_RECOVERY_HCLOUD_TOKEN": "hcloud_token",
    "EXOMEM_RECOVERY_HCLOUD_LOCATION": "hcloud_location",
}


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("recovery deployment lock is invalid")
        value[key] = item
    return value


class RecoverySettings(BaseModel):
    """Only the database and read-only observers required by recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database_url: SecretStr = Field(min_length=1, max_length=4096)
    database_schema: str = Field(min_length=3, max_length=63)
    database_role: str = Field(min_length=3, max_length=63)
    database_lock_timeout_seconds: int = Field(ge=1, le=300)
    envelope_key: SecretStr = Field(min_length=32, max_length=4096)
    provider_recovery_public_key: str = Field(
        min_length=40, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    deployment_lock: DeploymentLock
    source_deployment_lock: DeploymentLock
    runtime_selection: Literal["active", "rollback"]
    hcloud_token: SecretStr = Field(min_length=32, max_length=4096)
    hcloud_location: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            sanitized = validate_runtime_database_url(value.get_secret_value())
        except DatabaseBootstrapError as error:
            raise ValueError("recovery database URL is invalid") from error
        return SecretStr(sanitized.render_as_string(hide_password=False))

    @field_validator("database_schema", "database_role")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value) or value in {"postgres", "public", "neondb_owner"}:
            raise ValueError("recovery database identity is invalid")
        return value

    @model_validator(mode="after")
    def validate_database_identity(self) -> RecoverySettings:
        url = make_url(self.database_url.get_secret_value())
        if url.username != self.database_role or self.database_schema == "public":
            raise ValueError("recovery database identity is invalid")
        return self

    @model_validator(mode="after")
    def validate_selected_runtime(self) -> RecoverySettings:
        self.deployment_lock.selected_runtime(self.runtime_selection)
        self.source_deployment_lock.selected_runtime(self.runtime_selection)
        return self

    @property
    def database_name(self) -> str:
        return str(make_url(self.database_url.get_secret_value()).database)


def load_recovery_settings(
    environment: Mapping[str, str] | None = None,
) -> RecoverySettings:
    """Load the exact recovery-only environment; reject every unrelated secret."""

    source = dict(environment) if environment is not None else dict(os.environ)
    recovery_names = {name for name in source if name.startswith("EXOMEM_RECOVERY_")}
    forbidden_names = {
        name
        for name in source
        if name.startswith("EXOMEM_PROVISIONER_") or name.startswith("EXOMEM_PROVIDER_")
    }
    if recovery_names != set(_RECOVERY_ENVIRONMENT) or forbidden_names:
        raise ValueError("recovery environment is invalid")
    if any(not source[name] for name in _RECOVERY_ENVIRONMENT):
        raise ValueError("recovery environment is invalid")
    try:
        lock_value = json.loads(
            source["EXOMEM_RECOVERY_DEPLOYMENT_LOCK_JSON"],
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        source_lock_value = json.loads(
            source["EXOMEM_RECOVERY_SOURCE_DEPLOYMENT_LOCK_JSON"],
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        if not isinstance(lock_value, dict) or not isinstance(source_lock_value, dict):
            raise ValueError("recovery deployment lock is invalid")
        values: dict[str, object] = {
            field: source[name] for name, field in _RECOVERY_ENVIRONMENT.items()
        }
        values["database_lock_timeout_seconds"] = int(
            source["EXOMEM_RECOVERY_DATABASE_LOCK_TIMEOUT_SECONDS"]
        )
        values["deployment_lock"] = lock_value
        values["source_deployment_lock"] = source_lock_value
        return RecoverySettings.model_validate(values)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("recovery environment is invalid") from error
