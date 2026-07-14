"""Strict startup configuration for the hosted provisioner."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROVISIONER_PROTOCOL = "exomem-cell-provisioner.v1"
_DATABASE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_DISALLOWED_ROLES = {"postgres", "public", "neondb_owner"}


class ProvisionerSettings(BaseSettings):
    """Fail-closed configuration loaded only from the provisioner namespace."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_PROVISIONER_",
        extra="forbid",
        case_sensitive=False,
    )

    bearer: SecretStr = Field(min_length=32, max_length=4096)
    envelope_key: SecretStr = Field(min_length=32, max_length=4096)
    database_url: SecretStr = Field(min_length=1, max_length=4096)
    database_schema: str = Field(min_length=3, max_length=63)
    database_role: str = Field(min_length=3, max_length=63)
    trusted_proxy_ips: str = Field(min_length=1, max_length=1024)
    protocol: Literal["exomem-cell-provisioner.v1"] = PROVISIONER_PROTOCOL
    request_max_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    response_max_bytes: int = Field(default=1_048_576, ge=1024, le=1_048_576)
    claim_seconds: int = Field(default=30, ge=5, le=300)
    retry_after_seconds: int = Field(default=2, ge=1, le=300)
    max_failure_attempts: int = Field(default=6, ge=1, le=100)

    @field_validator("database_schema", "database_role")
    @classmethod
    def validate_database_identifier(cls, value: str) -> str:
        if not _DATABASE_IDENTIFIER.fullmatch(value):
            raise ValueError("database identifier must be a bounded lowercase SQL identifier")
        if value == "public":
            raise ValueError("public database schema is not dedicated to the provisioner")
        return value

    @field_validator("database_role")
    @classmethod
    def validate_dedicated_role(cls, value: str) -> str:
        if value in _DISALLOWED_ROLES:
            raise ValueError("database role must be dedicated to the provisioner")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not (raw.startswith("postgresql+asyncpg://") or raw.startswith("sqlite+aiosqlite://")):
            raise ValueError("database URL must use asyncpg or the SQLite test driver")
        return value

    @field_validator("trusted_proxy_ips")
    @classmethod
    def validate_trusted_proxy_ips(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",")]
        if not parts or any(not part for part in parts):
            raise ValueError("trusted proxies must be explicit private or loopback networks")
        for part in parts:
            try:
                network = ipaddress.ip_network(part, strict=False)
            except ValueError as error:
                raise ValueError(
                    "trusted proxies must be explicit private or loopback networks"
                ) from error
            if (
                not (network.is_private or network.is_loopback)
                or network.is_unspecified
                or network.is_multicast
            ):
                raise ValueError("trusted proxies must be private or loopback networks")
        return ",".join(parts)

    @model_validator(mode="after")
    def validate_independent_secrets(self) -> ProvisionerSettings:
        if secrets_equal(self.bearer, self.envelope_key):
            raise ValueError("bearer and envelope key must be independently generated")
        raw_url = self.database_url.get_secret_value()
        if raw_url.startswith("postgresql+asyncpg://"):
            parsed = urlsplit(raw_url)
            if unquote(parsed.username or "") != self.database_role:
                raise ValueError("database URL must authenticate as the dedicated runtime role")
        return self


def secrets_equal(first: SecretStr, second: SecretStr) -> bool:
    return first.get_secret_value() == second.get_secret_value()
