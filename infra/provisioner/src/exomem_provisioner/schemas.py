"""Exact request and response schemas mirrored from the Substrate client."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from .credentials import validate_machine_credential

OpaqueId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:/-]+$",
        strip_whitespace=False,
    ),
]
OpaqueReference = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        strip_whitespace=False,
    ),
]
ShortLabel = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:/-]+$",
        strip_whitespace=False,
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FailureCode = Literal[
    "CONTROL_PLANE_STATE_CONFLICT",
    "EXPORT_REQUEST_EXPIRED",
    "PROVISIONER_REJECTED",
    "PROVISIONER_RESPONSE_INVALID",
    "PROVISIONER_UNAVAILABLE",
]


def _validate_secret_value(value: SecretStr) -> SecretStr:
    validate_machine_credential(value.get_secret_value())
    return value


SecretValue = Annotated[
    SecretStr,
    Field(min_length=43, max_length=43),
    AfterValidator(_validate_secret_value),
]
_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_CANONICAL_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FailureResponse(StrictSchema):
    code: FailureCode
    retryable: bool


class WorkerPolicy(StrictSchema):
    workerCount: int = Field(ge=0, le=64)
    semantic: bool
    media: bool


class RuntimeTarget(StrictSchema):
    releaseVersion: ShortLabel
    protocolVersion: ShortLabel
    agentProfile: ShortLabel
    gatewayContractDigest: Sha256
    commandFingerprint: Sha256
    schemaDigest: Sha256


class ContextRequest(StrictSchema):
    operationId: OpaqueId
    checkpoint: OpaqueId
    fenceGeneration: int = Field(ge=1, le=9_007_199_254_740_991)
    tenantId: OpaqueId


class CellRequest(ContextRequest):
    cellId: OpaqueId
    protocolVersion: ShortLabel
    releaseVersion: ShortLabel
    serviceCredential: SecretValue
    workerPolicy: WorkerPolicy


class ProvisionRequest(CellRequest):
    provisionMode: Literal["serve", "restore-candidate"]


class TargetRequest(CellRequest):
    providerRef: OpaqueReference


class ExportRequest(TargetRequest):
    expiresAt: str = Field(min_length=20, max_length=40)

    @field_validator("expiresAt")
    @classmethod
    def validate_expiry(cls, value: str) -> str:
        if not _CANONICAL_RFC3339_UTC.fullmatch(value):
            raise ValueError("export expiry must be canonical RFC3339 UTC")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("export expiry must be canonical RFC3339 UTC") from error
        return value


class RotateCredentialRequest(TargetRequest):
    phase: Literal["stage", "finalize"]
    credentialVersion: int = Field(ge=1, le=2_147_483_647)
    nextCredential: SecretValue


class RestoreRequest(TargetRequest):
    restoreRef: SecretStr = Field(min_length=1, max_length=256)
    sourceCellId: OpaqueReference
    archiveSha256: Sha256
    manifestSha256: Sha256
    archiveSize: int = Field(gt=0, le=9_007_199_254_740_991)

    @field_validator("restoreRef")
    @classmethod
    def validate_restore_ref(cls, value: SecretStr) -> SecretStr:
        if not _OPAQUE_REFERENCE.fullmatch(value.get_secret_value()):
            raise ValueError("invalid opaque restore reference")
        return value


class ReleaseExportRequest(TargetRequest):
    releaseRef: SecretStr = Field(min_length=1, max_length=256)

    @field_validator("releaseRef")
    @classmethod
    def validate_release_ref(cls, value: SecretStr) -> SecretStr:
        if not _OPAQUE_REFERENCE.fullmatch(value.get_secret_value()):
            raise ValueError("invalid opaque release reference")
        return value


class ExportReferenceRequest(ContextRequest):
    exportRef: SecretStr = Field(min_length=1, max_length=256)

    @field_validator("exportRef")
    @classmethod
    def validate_export_ref(cls, value: SecretStr) -> SecretStr:
        if not _OPAQUE_REFERENCE.fullmatch(value.get_secret_value()):
            raise ValueError("invalid opaque export reference")
        return value


class DestroyRequest(ContextRequest):
    pass


class V2CellRequest(ContextRequest):
    cellId: OpaqueId
    serviceCredential: SecretValue
    workerPolicy: WorkerPolicy
    runtimeTarget: RuntimeTarget


class V2ProvisionRequest(V2CellRequest):
    provisionMode: Literal["serve", "restore-candidate"]


class V2TargetRequest(V2CellRequest):
    providerRef: OpaqueReference


class V2HealthRequest(V2TargetRequest):
    pass


class V2QuiesceRequest(V2TargetRequest):
    pass


class V2ResumeRequest(V2TargetRequest):
    pass


class V2StopRequest(V2TargetRequest):
    pass


class V2ExportRequest(V2TargetRequest):
    expiresAt: str = Field(min_length=20, max_length=40)

    @field_validator("expiresAt")
    @classmethod
    def validate_expiry(cls, value: str) -> str:
        return ExportRequest.validate_expiry(value)


class V2RotateCredentialRequest(V2TargetRequest):
    phase: Literal["stage", "finalize"]
    credentialVersion: int = Field(ge=1, le=2_147_483_647)
    nextCredential: SecretValue


class V2RestoreRequest(V2TargetRequest):
    restoreRef: SecretStr = Field(min_length=1, max_length=256)
    sourceCellId: OpaqueReference
    archiveSha256: Sha256
    manifestSha256: Sha256
    archiveSize: int = Field(gt=0, le=9_007_199_254_740_991)

    @field_validator("restoreRef")
    @classmethod
    def validate_restore_ref(cls, value: SecretStr) -> SecretStr:
        return RestoreRequest.validate_restore_ref(value)


class V2ReleaseExportRequest(V2TargetRequest):
    releaseRef: SecretStr = Field(min_length=1, max_length=256)

    @field_validator("releaseRef")
    @classmethod
    def validate_release_ref(cls, value: SecretStr) -> SecretStr:
        return ReleaseExportRequest.validate_release_ref(value)


class V2SealRequest(V2TargetRequest):
    pass


class V2DiscardRequest(V2TargetRequest):
    pass


class V2ExportDeleteRequest(ExportReferenceRequest):
    pass


class V2ExportDownloadRequest(ExportReferenceRequest):
    pass


class V2DestroyRequest(DestroyRequest):
    pass


class PendingResponse(StrictSchema):
    status: Literal["pending"] = "pending"
    operationId: OpaqueId
    checkpoint: OpaqueId
    retryAfterSeconds: int = Field(ge=1, le=300)


class ProvisionResponse(StrictSchema):
    providerRef: OpaqueReference
    privateEndpoint: str = Field(min_length=1, max_length=2048)

    @field_validator("privateEndpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("private endpoint must be an HTTPS URL without user information")
        return value


class HealthResponse(StrictSchema):
    live: bool
    ready: bool
    cellId: OpaqueId
    protocolVersion: ShortLabel
    releaseVersion: ShortLabel
    serviceAuthenticated: bool
    mutationAuthority: bool
    readAdmission: bool
    writeAdmission: bool
    workerPolicy: WorkerPolicy
    code: ShortLabel


class RotationResponse(StrictSchema):
    previousCredentialRejected: bool


class ExportResponse(StrictSchema):
    exportRef: OpaqueReference
    releaseRef: OpaqueReference
    archiveSha256: Sha256
    manifestSha256: Sha256
    archiveSize: int = Field(gt=0, le=9_007_199_254_740_991)
    encryptionScheme: Literal["envelope-aes-256-gcm"]
    integrityVerified: Literal[True]


class ExportDeletionResponse(StrictSchema):
    objectDestroyed: Literal[True]


class ExportDownloadResponse(StrictSchema):
    url: str = Field(min_length=1, max_length=4096)
    expiresAt: str = Field(min_length=20, max_length=40)

    @model_validator(mode="after")
    def validate_download(self) -> ExportDownloadResponse:
        parsed = urlsplit(self.url)
        try:
            expires_at = datetime.fromisoformat(self.expiresAt.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("download expiry must be RFC3339") from exc
        if expires_at.tzinfo is None:
            raise ValueError("download expiry must include timezone")
        ttl = expires_at.astimezone(UTC) - datetime.now(UTC)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or ttl <= timedelta(0)
            or ttl > timedelta(minutes=15)
        ):
            raise ValueError("download result is invalid")
        return self


class DestructionResponse(StrictSchema):
    computeDestroyed: Literal[True]
    storageDestroyed: Literal[True]
    keysDestroyed: Literal[True]


class TenantDestructionResponse(DestructionResponse):
    tenantResourcesDestroyed: Literal[True]


REQUEST_MODELS: dict[str, type[StrictSchema]] = {
    "provision": ProvisionRequest,
    "health": TargetRequest,
    "rotate-credential": RotateCredentialRequest,
    "quiesce": TargetRequest,
    "resume": TargetRequest,
    "stop": TargetRequest,
    "export": ExportRequest,
    "export-release": ReleaseExportRequest,
    "export-delete": ExportReferenceRequest,
    "restore": RestoreRequest,
    "export-download": ExportReferenceRequest,
    "seal": TargetRequest,
    "discard": TargetRequest,
    "destroy": DestroyRequest,
}

FINAL_MODELS: dict[str, type[StrictSchema] | None] = {
    "provision": ProvisionResponse,
    "health": HealthResponse,
    "rotate-credential": RotationResponse,
    "quiesce": None,
    "resume": None,
    "stop": None,
    "export": ExportResponse,
    "export-release": None,
    "export-delete": ExportDeletionResponse,
    "restore": None,
    "export-download": ExportDownloadResponse,
    "seal": None,
    "discard": DestructionResponse,
    "destroy": TenantDestructionResponse,
}

V1_REQUEST_MODELS = REQUEST_MODELS
V1_FINAL_MODELS = FINAL_MODELS

V2_REQUEST_MODELS: dict[str, type[StrictSchema]] = {
    "provision": V2ProvisionRequest,
    "health": V2HealthRequest,
    "rotate-credential": V2RotateCredentialRequest,
    "quiesce": V2QuiesceRequest,
    "resume": V2ResumeRequest,
    "stop": V2StopRequest,
    "export": V2ExportRequest,
    "export-release": V2ReleaseExportRequest,
    "export-delete": V2ExportDeleteRequest,
    "restore": V2RestoreRequest,
    "export-download": V2ExportDownloadRequest,
    "seal": V2SealRequest,
    "discard": V2DiscardRequest,
    "destroy": V2DestroyRequest,
}


class V2HealthResponse(StrictSchema):
    live: bool
    ready: bool
    cellId: OpaqueId
    runtimeIdentity: RuntimeTarget
    serviceAuthenticated: bool
    mutationAuthority: bool
    readAdmission: bool
    writeAdmission: bool
    workerPolicy: WorkerPolicy
    code: ShortLabel


V2_FINAL_MODELS: dict[str, type[StrictSchema] | None] = {
    **FINAL_MODELS,
    "health": V2HealthResponse,
}


def request_plaintext(model: StrictSchema) -> dict[str, object]:
    def reveal(value: object) -> object:
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, BaseModel):
            return {name: reveal(item) for name, item in value.__dict__.items()}
        if isinstance(value, dict):
            return {str(name): reveal(item) for name, item in value.items()}
        if isinstance(value, list):
            return [reveal(item) for item in value]
        return value

    return {name: reveal(value) for name, value in model.__dict__.items()}
