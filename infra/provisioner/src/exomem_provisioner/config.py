"""Strict startup configuration for the hosted provisioner."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROVISIONER_PROTOCOL: Literal["exomem-cell-provisioner.v1"] = "exomem-cell-provisioner.v1"
_DATABASE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_DISALLOWED_ROLES = {"postgres", "public", "neondb_owner"}
_TRUSTED_IPV4_RANGES = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
)
_TRUSTED_IPV6_RANGES = tuple(ipaddress.ip_network(value) for value in ("fc00::/7", "::1/128"))
_RELEASE_MANIFEST_FILENAME = "exomem-hosted-release-v1.json"
_CAPACITY_CONTRACT_FILENAME = "private-alpha-capacity-v1.json"
_RELEASE_MANIFEST_MAX_BYTES = 1_048_576
_RELEASE_BUILD_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_DEPLOYMENT_LOCK_MAX_BYTES = 1_048_576
_SHA256 = r"^[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40}$"
_RUNTIME_IMAGE = r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$"
_PROVISIONER_IMAGE = r"^ghcr\.io/artexis10/exomem-provisioner@sha256:[0-9a-f]{64}$"


def _is_trusted_proxy_network(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    if network.version == 4:
        ipv4_network = cast(ipaddress.IPv4Network, network)
        return any(
            ipv4_network.subnet_of(cast(ipaddress.IPv4Network, candidate))
            for candidate in _TRUSTED_IPV4_RANGES
        )
    ipv6_network = cast(ipaddress.IPv6Network, network)
    return any(
        ipv6_network.subnet_of(cast(ipaddress.IPv6Network, candidate))
        for candidate in _TRUSTED_IPV6_RANGES
    )


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


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
    ).hexdigest()


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


class DeploymentRuntimeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    releaseVersion: str = Field(min_length=1)
    protocolVersion: str = Field(min_length=1)
    agentProfile: str = Field(min_length=1)
    gatewayContractDigest: str = Field(pattern=_SHA256)
    commandFingerprint: str = Field(pattern=_SHA256)
    schemaDigest: str = Field(pattern=_SHA256)


class DeploymentRuntimeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    image: str = Field(pattern=_RUNTIME_IMAGE)
    sourceCommit: str = Field(pattern=_COMMIT)
    candidateSha256: str = Field(pattern=_SHA256)


class DeploymentProvisionerComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    image: str = Field(pattern=_PROVISIONER_IMAGE)
    sourceCommit: str = Field(pattern=_COMMIT)
    candidateSha256: str = Field(pattern=_SHA256)
    wireProtocol: Literal["exomem-cell-provisioner.v2"]


class DeploymentComponents(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    runtime: DeploymentRuntimeComponent
    provisioner: DeploymentProvisionerComponent


class DeploymentSourceClosure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    candidateCommit: str = Field(pattern=_COMMIT)
    compositionCommit: str = Field(pattern=_COMMIT)
    paths: tuple[str, ...] = Field(min_length=1, strict=False)


class DeploymentSourceClosures(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    runtime: DeploymentSourceClosure
    provisioner: DeploymentSourceClosure


class DeploymentLegacyContract(DeploymentRuntimeTarget):
    runtimeImage: str = Field(pattern=_RUNTIME_IMAGE)
    sourceCommit: str = Field(pattern=_COMMIT)


class DeploymentLegacyUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    releaseVersion: str = Field(min_length=1)
    protocolVersion: str = Field(min_length=1)
    runtimeImage: str = Field(pattern=_RUNTIME_IMAGE)
    sourceCommit: str = Field(pattern=_COMMIT)
    contractSha256: str = Field(pattern=_SHA256)
    contract: DeploymentLegacyContract

    @model_validator(mode="after")
    def validate_contract_identity(self) -> DeploymentLegacyUnit:
        if (
            self.contract.releaseVersion != self.releaseVersion
            or self.contract.protocolVersion != self.protocolVersion
            or self.contract.runtimeImage != self.runtimeImage
            or self.contract.sourceCommit != self.sourceCommit
        ):
            raise ValueError("legacy runtime contract does not match catalog identity")
        return self


class DeploymentComposition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    commit: str = Field(pattern=_COMMIT)
    sourceClosure: DeploymentSourceClosures
    forwardContractSha256: str = Field(pattern=_SHA256)
    authoritativeLegacyReleaseSetSha256: str = Field(pattern=_SHA256)
    legacyCatalog: tuple[DeploymentLegacyUnit, ...] = Field(strict=False)
    legacyReleaseSetSha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_unique_legacy_catalog(self) -> DeploymentComposition:
        keys = [(unit.releaseVersion, unit.protocolVersion) for unit in self.legacyCatalog]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("legacy runtime catalog is not canonical")
        for unit in self.legacyCatalog:
            if _canonical_json_sha256(unit.contract.model_dump(mode="json")) != unit.contractSha256:
                raise ValueError("legacy runtime contract hash is invalid")
        release_set = [
            {"releaseVersion": release, "protocolVersion": protocol} for release, protocol in keys
        ]
        if _canonical_json_sha256(release_set) != self.legacyReleaseSetSha256:
            raise ValueError("legacy runtime release set hash is invalid")
        return self


class DeploymentRollback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    provisionerImage: str = Field(pattern=_PROVISIONER_IMAGE)
    provisionerSourceCommit: str = Field(pattern=_COMMIT)
    v1CorpusSha256: str = Field(pattern=_SHA256)
    legacyManifestSha256: str = Field(pattern=_SHA256)
    substrateV1ConsumerCommit: str = Field(pattern=_COMMIT)


class DeploymentRecordsRollbackRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    image: str = Field(pattern=_RUNTIME_IMAGE)
    sourceCommit: str = Field(pattern=_COMMIT)
    candidateSha256: str = Field(pattern=_SHA256)
    recordsReaderVersion: Literal[2]
    readerStatusProof: DeploymentRecordsReaderStatusProof
    runtimeTarget: DeploymentRuntimeTarget


class DeploymentRecordsReaderStatusProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    profile: Literal["hosted-alpha-agent-v1"]
    recordsReaderVersion: Literal[2]
    lifecycleActionsEnabled: Literal[False]
    issuedAt: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    expiresAt: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    signerWorkflow: Literal["Artexis10/exomem/.github/workflows/release-please.yml"]
    signerWorkflowDigest: str = Field(pattern=_COMMIT)


class DeploymentRecordsCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    minimum_records_reader_version: Literal[2]
    activeProfile: Literal["hosted-alpha-agent-v2"]
    activeLifecycleActionsEnabled: Literal[True]
    rollbackProfile: Literal["hosted-alpha-agent-v1"]
    rollbackLifecycleActionsEnabled: Literal[False]
    rollbackRuntime: DeploymentRecordsRollbackRuntime


class SelectedDeploymentRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    image: str = Field(pattern=_RUNTIME_IMAGE)
    runtimeTarget: DeploymentRuntimeTarget
    recordsReaderVersion: Literal[2] | None = None
    lifecycleActionsEnabled: bool = False
    compatibilityDigest: str | None = Field(default=None, pattern=_SHA256)
    migrationMode: Literal["none", "binding-v1-to-v2", "state-root-v1"] = "none"


class DeploymentRuntimeUpgrade(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    compatibilityDigest: str = Field(pattern=_SHA256)
    migrationMode: Literal["none", "binding-v1-to-v2", "state-root-v1"]
    substrateConsumerCommit: str = Field(pattern=_COMMIT)
    substrateTrustSha256: str = Field(pattern=_SHA256)


class DeploymentLock(BaseModel):
    """One selected, independently composed deployment-lock member."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    artifact: Literal["exomem-hosted-deployment-lock"]
    schemaVersion: Literal[2, 3]
    admissionMode: Literal["expand", "contract"]
    components: DeploymentComponents
    runtimeTarget: DeploymentRuntimeTarget
    runtimeUpgrade: DeploymentRuntimeUpgrade | None = None
    composition: DeploymentComposition
    rollback: DeploymentRollback
    recordsCompatibility: DeploymentRecordsCompatibility | None = None

    @model_validator(mode="after")
    def validate_records_compatibility(self) -> DeploymentLock:
        if self.schemaVersion == 2 and self.recordsCompatibility is not None:
            raise ValueError("deployment lock v2 cannot carry Records compatibility")
        if self.schemaVersion == 3:
            if self.recordsCompatibility is None:
                raise ValueError("deployment lock v3 requires Records compatibility")
            if self.runtimeTarget.agentProfile != self.recordsCompatibility.activeProfile:
                raise ValueError("active Records profile does not match the runtime target")
            rollback = self.recordsCompatibility.rollbackRuntime
            if (
                rollback.runtimeTarget.agentProfile != self.recordsCompatibility.rollbackProfile
                or rollback.readerStatusProof.profile != self.recordsCompatibility.rollbackProfile
            ):
                raise ValueError("rollback Records profile does not match the runtime target")
        return self

    @property
    def admission_mode(self) -> Literal["expand", "contract"]:
        return self.admissionMode

    @property
    def runtime_target(self) -> DeploymentRuntimeTarget:
        return self.runtimeTarget

    def selected_runtime(
        self, selection: Literal["active", "rollback"] | None
    ) -> SelectedDeploymentRuntime:
        if self.schemaVersion == 2:
            if selection == "rollback":
                raise ValueError("deployment lock v2 does not support rollback runtime selection")
            if selection not in {None, "active"}:
                raise ValueError("deployment runtime selection is invalid")
            return SelectedDeploymentRuntime(
                image=self.components.runtime.image,
                runtimeTarget=self.runtimeTarget,
                compatibilityDigest=(
                    self.runtimeUpgrade.compatibilityDigest if self.runtimeUpgrade else None
                ),
                migrationMode=(
                    self.runtimeUpgrade.migrationMode if self.runtimeUpgrade else "none"
                ),
            )
        if selection not in {"active", "rollback"}:
            raise ValueError("deployment lock v3 requires an explicit runtime selection")
        assert self.recordsCompatibility is not None
        if selection == "active":
            return SelectedDeploymentRuntime(
                image=self.components.runtime.image,
                runtimeTarget=self.runtimeTarget,
                recordsReaderVersion=self.recordsCompatibility.minimum_records_reader_version,
                lifecycleActionsEnabled=self.recordsCompatibility.activeLifecycleActionsEnabled,
                compatibilityDigest=(
                    self.runtimeUpgrade.compatibilityDigest if self.runtimeUpgrade else None
                ),
                migrationMode=(
                    self.runtimeUpgrade.migrationMode if self.runtimeUpgrade else "none"
                ),
            )
        rollback = self.recordsCompatibility.rollbackRuntime
        return SelectedDeploymentRuntime(
            image=rollback.image,
            runtimeTarget=rollback.runtimeTarget,
            recordsReaderVersion=rollback.recordsReaderVersion,
            lifecycleActionsEnabled=self.recordsCompatibility.rollbackLifecycleActionsEnabled,
        )

    @property
    def records_compatibility(self) -> DeploymentRecordsCompatibility | None:
        return self.recordsCompatibility

    @property
    def legacy_catalog(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (unit.releaseVersion, unit.protocolVersion) for unit in self.composition.legacyCatalog
        )

    @property
    def authoritative_legacy_release_set_sha256(self) -> str:
        return self.composition.authoritativeLegacyReleaseSetSha256

    def matches_runtime_request(
        self,
        request: dict[str, object],
        *,
        wire_protocol: str,
        selection: Literal["active", "rollback"] | None = None,
    ) -> bool:
        from .wire_protocol import WIRE_PROTOCOL_V2, runtime_identity

        try:
            target = runtime_identity(request)
        except (KeyError, ValueError):
            return False
        if wire_protocol == WIRE_PROTOCOL_V2:
            try:
                selected = self.selected_runtime(selection)
            except ValueError:
                return False
            expected = selected.runtimeTarget.model_dump(mode="json")
            if selected.compatibilityDigest is not None:
                expected["compatibilityDigest"] = selected.compatibilityDigest
            return target == expected
        return (target["releaseVersion"], target["protocolVersion"]) in self.legacy_catalog


def load_deployment_lock(path: str | Path) -> DeploymentLock:
    """Load one strict selected deployment-lock member, never a pair."""

    lock_path = Path(path)
    try:
        size = lock_path.stat().st_size
        raw = lock_path.read_bytes()
    except OSError as error:
        raise ValueError("deployment lock is unavailable") from error
    if not 1 <= size <= _DEPLOYMENT_LOCK_MAX_BYTES:
        raise ValueError("deployment lock has an invalid size")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("deployment lock is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("deployment lock must be one JSON object")
    try:
        return DeploymentLock.model_validate(value)
    except ValueError as error:
        raise ValueError("deployment lock is invalid") from error


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
    deployment_lock_path: str | None = Field(default=None, min_length=1, max_length=4096)
    runtime_selection: Literal["active", "rollback"] | None = None
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

    @field_validator("deployment_lock_path")
    @classmethod
    def validate_deployment_lock_path(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError("deployment lock path must be absolute")
        return value

    @property
    def deployment_lock(self) -> DeploymentLock | None:
        if self.deployment_lock_path is None:
            return None
        lock = load_deployment_lock(self.deployment_lock_path)
        lock.selected_runtime(self.runtime_selection)
        return lock

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

    deployment_lock_path: str = Field(min_length=1, max_length=4096)
    runtime_selection: Literal["active", "rollback"] | None = None
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
    capacity_receipt_public_key: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    capacity_contract_path: str = Field(min_length=1, max_length=4096)
    capacity_receipt_namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    capacity_receipt_config_map: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    hcloud_server_id: int = Field(gt=0)

    @field_validator("deployment_lock_path")
    @classmethod
    def validate_deployment_lock_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("deployment lock path must be absolute")
        return value

    @property
    def deployment_lock(self) -> DeploymentLock:
        lock = load_deployment_lock(self.deployment_lock_path)
        lock.selected_runtime(self.runtime_selection)
        return lock

    @field_validator("capacity_contract_path")
    @classmethod
    def validate_capacity_contract_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.name != _CAPACITY_CONTRACT_FILENAME:
            raise ValueError("capacity contract path must be absolute and use the v1 filename")
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
        validation_alias="EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY",
    )
    volume_encryption_secret_name: str = Field(min_length=1, max_length=63)
    volume_encryption_secret_namespace: str = Field(min_length=1, max_length=63)
    location: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")
    worker_id: str = Field(min_length=1, max_length=128)
    poll_seconds: float = Field(default=1.0, ge=0.05, le=30)
    capacity_receipt_public_key: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )
    capacity_contract_path: str = Field(min_length=1, max_length=4096)
    capacity_receipt_namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    capacity_receipt_config_map: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    hcloud_server_id: int = Field(gt=0)

    @field_validator("provider_recovery_signing_key")
    @classmethod
    def validate_provider_recovery_signing_key(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", value.get_secret_value()) is None:
            raise ValueError("provider recovery signing key is invalid")
        return value

    @field_validator("capacity_contract_path")
    @classmethod
    def validate_capacity_contract_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.name != _CAPACITY_CONTRACT_FILENAME:
            raise ValueError("capacity contract path must be absolute and use the v1 filename")
        return value


def secrets_equal(first: SecretStr, second: SecretStr) -> bool:
    return first.get_secret_value() == second.get_secret_value()
