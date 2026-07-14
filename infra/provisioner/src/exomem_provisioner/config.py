"""Strict startup configuration for the hosted provisioner."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROVISIONER_PROTOCOL = "exomem-cell-provisioner.v1"
_DATABASE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_DISALLOWED_ROLES = {"postgres", "public", "neondb_owner"}
_TRUSTED_IPV4_RANGES = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
)
_TRUSTED_IPV6_RANGES = tuple(ipaddress.ip_network(value) for value in ("fc00::/7", "::1/128"))
_RELEASE_MANIFEST_FILENAME = "exomem-hosted-release-v1.json"
_RELEASE_MANIFEST_MAX_BYTES = 1_048_576
_RELEASE_BUILD_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def _is_trusted_proxy_network(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    allowed = _TRUSTED_IPV4_RANGES if network.version == 4 else _TRUSTED_IPV6_RANGES
    return any(network.subnet_of(candidate) for candidate in allowed)


class HostedReleaseCommand(BaseModel):
    """One command row embedded in the immutable hosted release unit."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    readOnly: bool
    mode: Literal["read", "write"]
    tier: int = Field(ge=1, le=2)
    capability: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")

    @model_validator(mode="after")
    def validate_mode(self) -> HostedReleaseCommand:
        if self.readOnly != (self.mode == "read"):
            raise ValueError("release command readOnly and mode differ")
        return self


class HostedReleaseManifest(BaseModel):
    """The sole deploy pin for one reviewed hosted runtime release."""

    model_config = ConfigDict(extra="forbid", strict=True)

    artifact: Literal["exomem-hosted-release"]
    schemaVersion: Literal[1]
    sourceRepository: Literal["https://github.com/Artexis10/exomem"]
    sourceCommit: str = Field(pattern=r"^[0-9a-f]{40}$")
    release: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:/-]+$")
    hostedProtocol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:/-]+$")
    releaseBuildTime: str = Field(min_length=20, max_length=40)
    runtimeImage: str = Field(pattern=r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
    publishedTag: str = Field(min_length=1, max_length=512)
    operatorContractSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gatewayContractSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commandRegistry: list[HostedReleaseCommand]

    @field_validator("releaseBuildTime")
    @classmethod
    def validate_release_build_time(cls, value: str) -> str:
        if not _RELEASE_BUILD_TIME.fullmatch(value):
            raise ValueError("release build time must be canonical RFC3339 UTC")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("release build time must be canonical RFC3339 UTC") from error
        if parsed.tzinfo is None or parsed.astimezone(UTC) != parsed:
            raise ValueError("release build time must be canonical RFC3339 UTC")
        return value

    @model_validator(mode="after")
    def validate_complete_release_unit(self) -> HostedReleaseManifest:
        expected_tag = f"ghcr.io/artexis10/exomem:{self.sourceCommit}-hosted"
        if self.publishedTag != expected_tag:
            raise ValueError("published tag is not bound to the release source commit")
        if len(self.commandRegistry) != 21:
            raise ValueError("hosted release must carry the complete 21-command registry")
        names = [command.name for command in self.commandRegistry]
        if len(names) != len(set(names)):
            raise ValueError("hosted release command registry contains duplicate names")
        return self


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate release manifest field: {key}")
        value[key] = item
    return value


def load_hosted_release_manifest(path: str | Path) -> HostedReleaseManifest:
    """Load a bounded, duplicate-free, exact hosted release manifest."""

    manifest_path = Path(path)
    size = manifest_path.stat().st_size
    if not 1 <= size <= _RELEASE_MANIFEST_MAX_BYTES:
        raise ValueError("hosted release manifest has an invalid size")
    raw = manifest_path.read_bytes()
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("hosted release manifest is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("hosted release manifest must be one JSON object")
    return HostedReleaseManifest.model_validate(value)


class ProvisionerSettings(BaseSettings):
    """Fail-closed configuration loaded only from the provisioner namespace."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_PROVISIONER_",
        extra="forbid",
        case_sensitive=False,
        populate_by_name=True,
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
    provider_recovery_signing_key: SecretStr | None = Field(
        default=None,
        min_length=32,
        max_length=4096,
        validation_alias="EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
    )

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
        normalized: list[str] = []
        seen: set[str] = set()
        for part in parts:
            try:
                network = ipaddress.ip_network(part, strict=False)
            except ValueError as error:
                raise ValueError(
                    "trusted proxies must be explicit private or loopback networks"
                ) from error
            if not _is_trusted_proxy_network(network):
                raise ValueError("trusted proxies must be private or loopback networks")
            canonical = str(network)
            if canonical not in seen:
                normalized.append(canonical)
                seen.add(canonical)
        return ",".join(normalized)

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


class ProviderWorkerSettings(BaseSettings):
    """Routine provider settings with public verification and no HCloud authority."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_PROVISIONER_",
        extra="forbid",
        case_sensitive=False,
        populate_by_name=True,
    )

    release_manifest_path: str = Field(min_length=1, max_length=4096)
    cell_chart_path: str = Field(min_length=1, max_length=4096)
    cell_chart_version: str = Field(min_length=1, max_length=64)
    helm_binary: str = Field(min_length=1, max_length=4096)
    helm_version: str = Field(min_length=1, max_length=64)
    control_hostname: str = Field(min_length=1, max_length=253)
    transfer_hostname: str = Field(min_length=1, max_length=253)
    browser_origin: str = Field(min_length=1, max_length=255)
    location: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")
    internal_origin: str = Field(min_length=1, max_length=2048)
    worker_id: str = Field(min_length=1, max_length=128)
    poll_seconds: float = Field(default=1.0, ge=0.05, le=30)
    provider_recovery_public_key: str = Field(
        min_length=40,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        validation_alias="EXOMEM_PROVIDER_RECOVERY_PUBLIC_KEY",
    )

    @field_validator("release_manifest_path")
    @classmethod
    def validate_release_manifest_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.name != _RELEASE_MANIFEST_FILENAME:
            raise ValueError("release manifest path must be absolute and use the v1 filename")
        return value

    @field_validator("control_hostname", "transfer_hostname")
    @classmethod
    def validate_hostname(cls, value: str) -> str:
        parsed = urlsplit("https://" + value)
        if (
            parsed.hostname != value
            or parsed.port is not None
            or value != value.lower()
            or len(value) > 253
        ):
            raise ValueError("hostnames must be canonical DNS names without ports")
        return value

    @field_validator("browser_origin")
    @classmethod
    def validate_browser_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("browser origin must be one canonical HTTPS origin")
        return value

    @field_validator("internal_origin")
    @classmethod
    def validate_internal_origin(cls, value: str) -> str:
        if "{resource}" not in value or "{namespace}" not in value:
            raise ValueError("internal origin must bind resource and namespace placeholders")
        rendered = value.format(resource="exo-test", namespace="exo-test", cell="cell-test")
        parsed = urlsplit(rendered)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("internal origin must render one HTTP(S) origin")
        return value

    @model_validator(mode="after")
    def reject_independent_release_environment(self) -> ProviderWorkerSettings:
        forbidden = (
            "EXOMEM_PROVISIONER_CELL_IMAGE",
            "EXOMEM_PROVISIONER_CONTRACT_DIGEST",
            "EXOMEM_PROVISIONER_RELEASE_VERSION",
            "EXOMEM_PROVISIONER_PROTOCOL_VERSION",
        )
        if any(os.environ.get(name) is not None for name in forbidden):
            raise ValueError("independent hosted release overrides are forbidden")
        return self


class VolumeWorkerSettings(BaseSettings):
    """Narrow PV/HCloud lane settings; no runtime, Helm, route, or B2 credentials."""

    model_config = SettingsConfigDict(
        env_prefix="EXOMEM_PROVISIONER_",
        extra="forbid",
        case_sensitive=False,
        populate_by_name=True,
    )

    hcloud_token: SecretStr = Field(min_length=32, max_length=4096)
    provider_recovery_signing_key: SecretStr = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
        validation_alias="EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
    )
    volume_encryption_secret_name: str = Field(min_length=1, max_length=63)
    volume_encryption_secret_namespace: str = Field(min_length=1, max_length=63)
    location: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")
    worker_id: str = Field(min_length=1, max_length=128)
    poll_seconds: float = Field(default=1.0, ge=0.05, le=30)


def secrets_equal(first: SecretStr, second: SecretStr) -> bool:
    return first.get_secret_value() == second.get_secret_value()
