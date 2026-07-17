"""Pure private gateway contract and transfer authority for hosted cells."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import __version__
from . import commands as commands_module
from .hosted_runtime import (
    HOSTED_PROTOCOL_VERSION,
    SUPPORTED_HOSTED_PROTOCOL_VERSIONS,
    HostedCellConfig,
)

CONTRACT_SCHEMA_VERSION = 1
TRANSFER_GRANT_VERSION = 1
TRANSFER_AUDIENCE = "exomem-hosted-transfer"
TRANSFER_MAX_TTL_SECONDS = 15 * 60
TRANSFER_CLOCK_SKEW_SECONDS = 30

CELL_HEADER = "X-Exomem-Cell-Id"
PROTOCOL_HEADER = "X-Exomem-Protocol-Version"
REQUEST_HEADER = "X-Exomem-Request-Id"
PRINCIPAL_HEADER = "X-Exomem-Principal-Scope"
TRANSFER_GRANT_HEADER = "X-Exomem-Transfer-Grant"
ROUTING_STOPPED_HEADER = "X-Exomem-Routing-Stopped"

_OPAQUE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PRINCIPAL_SCOPE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_OPERATIONS = frozenset({"upload", "download"})
_GRANT_FIELDS = frozenset(
    {"v", "aud", "op", "tenant", "cell", "principal", "iat", "exp", "jti", "limits"}
)


class HostedGatewayError(RuntimeError):
    """Stable private-contract error that never embeds a credential or tenant value."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class TrustedGatewayContext:
    cell_id: str
    protocol_version: str
    request_id: str
    principal_scope: str
    idempotency_key: str | None = None
    authenticated_credential_version: str | None = None
    security_revision: int | None = None


@dataclass(frozen=True, slots=True)
class TransferGrant:
    operation: str
    tenant_scope: str
    cell_id: str
    principal_scope: str
    issued_at: int
    expires_at: int
    jti: str
    max_bytes: int


def canonical_json(value: Any) -> bytes:
    """Render the one canonical JSON encoding used for digests and HMAC payloads."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_contract_json(contract: dict[str, Any]) -> bytes:
    return canonical_json(contract)


def _command_contract(command: commands_module.Command) -> dict[str, Any]:
    return {
        "name": command.name,
        "params": [
            {
                "name": param.name,
                "type": param.type,
                "required": param.required,
                "description": param.help,
            }
            for param in command.params
        ],
        "read_only": command.read_only,
        "mode": "read" if command.read_only else "write",
        "tier": command.tier,
        "capability": "core" if command.tier == 1 else "tier-2",
        "product_surface": command.product_surface,
        "actions": list(command.product_actions),
        "first_run_safe": command.first_run_safe,
        "routes": list(command.routes),
        "guarded_fields": list(command.guarded_fields),
    }


def build_gateway_contract(
    *,
    protocol_version: str = HOSTED_PROTOCOL_VERSION,
    expose_tier2: bool = True,
) -> dict[str, Any]:
    """Build the deterministic private contract directly from the REST registry."""

    if protocol_version not in SUPPORTED_HOSTED_PROTOCOL_VERSIONS:
        raise HostedGatewayError(
            "HOSTED_PROTOCOL_UNSUPPORTED",
            "hosted protocol version is not supported by this release",
        )

    registry = commands_module.product_commands_for("rest", expose_tier2=expose_tier2)
    base: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "protocol_version": protocol_version,
        "exomem_release": __version__,
        "compatibility": {
            "policy": "additive",
            "optional_response_fields_may_be_added": True,
            "breaking_changes_require_coordinated_rollout": True,
            "breaking_change_classes": [
                "command-removal",
                "parameter-removal-or-change",
                "envelope-change",
                "stable-error-removal-or-change",
            ],
        },
        "trusted_headers": {
            "cell": CELL_HEADER,
            "protocol": PROTOCOL_HEADER,
            "request": REQUEST_HEADER,
            "principal": PRINCIPAL_HEADER,
            "idempotency": "Idempotency-Key",
        },
        "envelopes": {
            "success": {
                "required": ["success", "data"],
                "shape": {"success": True, "data": "command-result"},
            },
            "error": {
                "required": ["success", "error"],
                "error_required": ["code", "message", "remediation"],
                "shape": {
                    "success": False,
                    "error": {
                        "code": "stable-code",
                        "message": "content-free-message",
                        "remediation": None,
                    },
                },
            },
        },
        "transfer_grant": {
            "version": TRANSFER_GRANT_VERSION,
            "audience": TRANSFER_AUDIENCE,
            "operations": sorted(_OPERATIONS),
            "max_ttl_seconds": TRANSFER_MAX_TTL_SECONDS,
        },
        "commands": [_command_contract(command) for command in registry],
    }
    return {
        **base,
        "digest": {
            "algorithm": "sha256",
            "value": hashlib.sha256(canonical_json(base)).hexdigest(),
        },
    }


def validate_opaque_scope(value: str, *, field: str) -> str:
    clean = str(value or "").strip()
    if not _OPAQUE_SCOPE.fullmatch(clean):
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID", f"trusted {field} must be an opaque identifier"
        )
    return clean


def validate_request_id(value: str) -> str:
    """Require the canonical UUIDv4 shape emitted by ``crypto.randomUUID``."""

    clean = str(value or "").strip()
    try:
        parsed = uuid.UUID(clean)
    except (AttributeError, ValueError) as exc:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted request identity must be a canonical UUIDv4",
        ) from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != clean:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted request identity must be a canonical UUIDv4",
        )
    return clean


def validate_principal_scope(value: str) -> str:
    """Require an unpadded base64url-encoded 256-bit principal scope."""

    clean = str(value or "").strip()
    if not _PRINCIPAL_SCOPE.fullmatch(clean):
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted principal scope must be an opaque 256-bit digest",
        )
    try:
        decoded = base64.b64decode(clean + "=", altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted principal scope must be an opaque 256-bit digest",
        ) from exc
    if len(decoded) != hashlib.sha256().digest_size or _b64encode(decoded) != clean:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted principal scope must be an opaque 256-bit digest",
        )
    return clean


def scoped_idempotency_key(context: TrustedGatewayContext) -> str | None:
    """Hash public retry identity with immutable cell and principal context."""

    if not context.idempotency_key:
        return None
    payload = "\0".join(
        (
            context.cell_id,
            context.principal_scope,
            context.idempotency_key,
        )
    )
    return f"hosted:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def implicit_retry_scope(context: TrustedGatewayContext) -> str:
    payload = "\0".join(
        (
            context.cell_id,
            context.principal_scope,
        )
    )
    return f"hosted-principal:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    if _b64encode(decoded) != value:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    return decoded


def _resource_bound(config: HostedCellConfig, operation: str) -> int:
    if operation == "upload":
        return config.resource_limits.upload_bytes
    return config.resource_limits.storage_bytes


def _validate_grant_limits(config: HostedCellConfig, operation: str, max_bytes: int) -> int:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or max_bytes > _resource_bound(config, operation)
    ):
        raise HostedGatewayError(
            "HOSTED_TRANSFER_LIMIT_INVALID",
            "transfer grant exceeds the configured resource bound",
        )
    return max_bytes


def mint_transfer_grant(
    config: HostedCellConfig,
    *,
    tenant_scope: str,
    principal_scope: str,
    operation: str,
    jti: str,
    max_bytes: int,
    now: int | float | None = None,
    ttl_seconds: int = 5 * 60,
) -> str:
    """Mint an alpha HMAC grant using the existing unique cell credential."""

    service_credential = config.service_credential
    if service_credential is None:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_UNAVAILABLE",
            "legacy private transfer is unavailable for dynamic-credential cells",
        )

    if operation not in _OPERATIONS:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_OPERATION_INVALID", "transfer operation is invalid"
        )
    tenant = validate_opaque_scope(tenant_scope, field="tenant scope")
    principal = validate_principal_scope(principal_scope)
    grant_id = validate_opaque_scope(jti, field="grant identity")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 0 < ttl_seconds <= TRANSFER_MAX_TTL_SECONDS
    ):
        raise HostedGatewayError(
            "HOSTED_TRANSFER_TTL_INVALID", "transfer grant lifetime is invalid"
        )
    limit = _validate_grant_limits(config, operation, max_bytes)
    issued_at = int(time.time() if now is None else now)
    claims = {
        "v": TRANSFER_GRANT_VERSION,
        "aud": TRANSFER_AUDIENCE,
        "op": operation,
        "tenant": tenant,
        "cell": config.cell_id,
        "principal": principal,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": grant_id,
        "limits": {"max_bytes": limit},
    }
    payload = _b64encode(canonical_json(claims))
    signature = hmac.new(
        service_credential.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload}.{_b64encode(signature)}"


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise HostedGatewayError(
                    "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
                )
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_hook)
    except HostedGatewayError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    return value


def verify_transfer_grant(
    token: str,
    config: HostedCellConfig,
    *,
    expected_operation: str,
    expected_tenant_scope: str | None,
    expected_principal_scope: str,
    now: int | float | None = None,
) -> TransferGrant:
    """Verify signature, strict claims, bindings, lifetime, and resource bounds."""

    service_credential = config.service_credential
    if service_credential is None:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_UNAVAILABLE",
            "legacy private transfer is unavailable for dynamic-credential cells",
        )

    try:
        payload, signature = str(token or "").split(".")
    except ValueError as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    presented_signature = _b64decode(signature)
    expected_signature = hmac.new(
        service_credential.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if len(presented_signature) != len(expected_signature) or not hmac.compare_digest(
        presented_signature, expected_signature
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    raw_claims = _b64decode(payload)
    claims = _strict_json_object(raw_claims)
    if canonical_json(claims) != raw_claims:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    if set(claims) != _GRANT_FIELDS or claims.get("v") != TRANSFER_GRANT_VERSION:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    limits = claims.get("limits")
    if not isinstance(limits, dict) or set(limits) != {"max_bytes"}:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in (issued_at, expires_at)
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    assert isinstance(issued_at, int) and isinstance(expires_at, int)
    operation = claims.get("op")
    tenant = claims.get("tenant")
    cell = claims.get("cell")
    principal = claims.get("principal")
    audience = claims.get("aud")
    grant_id = claims.get("jti")
    if not all(
        isinstance(value, str) for value in (operation, tenant, cell, principal, audience, grant_id)
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    assert isinstance(operation, str)
    assert isinstance(tenant, str)
    assert isinstance(cell, str)
    assert isinstance(principal, str)
    assert isinstance(audience, str)
    assert isinstance(grant_id, str)
    try:
        validate_opaque_scope(tenant, field="tenant scope")
        validate_principal_scope(principal)
        validate_opaque_scope(grant_id, field="grant identity")
    except HostedGatewayError as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    if (
        audience != TRANSFER_AUDIENCE
        or operation not in _OPERATIONS
        or expected_operation not in _OPERATIONS
        or not hmac.compare_digest(operation, expected_operation)
        or not hmac.compare_digest(cell, config.cell_id)
        or (
            expected_tenant_scope is not None
            and not hmac.compare_digest(tenant, expected_tenant_scope)
        )
        or not hmac.compare_digest(principal, expected_principal_scope)
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    current = int(time.time() if now is None else now)
    if issued_at > current + TRANSFER_CLOCK_SKEW_SECONDS:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is not yet valid")
    if expires_at <= current:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_EXPIRED", "transfer grant has expired")
    if expires_at <= issued_at or expires_at - issued_at > TRANSFER_MAX_TTL_SECONDS:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant lifetime is invalid"
        )
    max_bytes = _validate_grant_limits(config, operation, limits.get("max_bytes"))
    return TransferGrant(
        operation=operation,
        tenant_scope=tenant,
        cell_id=cell,
        principal_scope=principal,
        issued_at=issued_at,
        expires_at=expires_at,
        jti=grant_id,
        max_bytes=max_bytes,
    )


__all__ = [
    "CELL_HEADER",
    "CONTRACT_SCHEMA_VERSION",
    "PRINCIPAL_HEADER",
    "PROTOCOL_HEADER",
    "REQUEST_HEADER",
    "ROUTING_STOPPED_HEADER",
    "TRANSFER_AUDIENCE",
    "TRANSFER_GRANT_HEADER",
    "TRANSFER_GRANT_VERSION",
    "HostedGatewayError",
    "TransferGrant",
    "TrustedGatewayContext",
    "build_gateway_contract",
    "canonical_contract_json",
    "canonical_json",
    "implicit_retry_scope",
    "mint_transfer_grant",
    "scoped_idempotency_key",
    "validate_opaque_scope",
    "validate_principal_scope",
    "validate_request_id",
    "verify_transfer_grant",
]
