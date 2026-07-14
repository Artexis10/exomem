"""Exact public transfer-v2 capability primitives for hosted cells.

This module deliberately owns no replay database and no credential state.  The
security lane injects the narrow :class:`TransferSecurityAuthority` boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import SplitResult, urlsplit

TRANSFER_GRANT_VERSION = 2
TRANSFER_AUDIENCE = "exomem-hosted-transfer"
TRANSFER_GRANT_HEADER = "X-Exomem-Transfer-Grant"
TRANSFER_GRANT_MAX_ASCII_BYTES = 8192
TRANSFER_MAX_TTL_SECONDS = 15 * 60
TRANSFER_CLOCK_SKEW_SECONDS = 30
TRANSFER_UPLOAD_MAX_BYTES = 90 * 1024 * 1024
TRANSFER_TEMP_QUOTA_BYTES = 96 * 1024 * 1024
TRANSFER_RUNTIME_TEMP_QUOTA_BYTES = 16 * 1024 * 1024
TRANSFER_V1_UPLOAD_MAX_BYTES = 4 * 1024 * 1024
TRANSFER_UPLOAD_PATH = "/public/exomem/v2/transfers/upload"
TRANSFER_DOWNLOAD_PATH = "/public/exomem/v2/transfers/download"

_CLAIM_FIELDS = frozenset(
    {
        "v",
        "aud",
        "kid",
        "origin",
        "op",
        "method",
        "cell",
        "principal",
        "iat",
        "nbf",
        "exp",
        "jti",
        "limits",
        "target",
    }
)
_KID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CELL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+"
    r"(?:;[!#$%&'*+.^_`|~0-9A-Za-z=-]+)*$"
)


class TransferContractError(RuntimeError):
    """Base class for content-free public transfer failures."""


class TransferGrantRejected(TransferContractError):
    """A grant is malformed, invalid, expired, finalized, or replayed."""


class TransferSecurityUnavailable(TransferContractError):
    """Durable credential/JTI state cannot currently be proven."""


@runtime_checkable
class TransferSecurityAuthority(Protocol):
    """The only credential/replay surface used by public transfers."""

    def verify_transfer_signature(
        self,
        kid: str,
        ascii_payload: bytes,
        signature: bytes,
    ) -> bool: ...

    def consume_transfer_jti(
        self,
        *,
        cell_id: str,
        schema_version: int,
        kid: str,
        jti: str,
        expires_at: int,
        consumed_at: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TransferGrantV2:
    credential_version: str
    origin: str
    operation: str
    method: str
    cell_id: str
    principal_scope: str
    issued_at: int
    not_before: int
    expires_at: int
    jti: str
    max_bytes: int
    target: dict[str, Any]

    @property
    def upload_metadata(self) -> dict[str, Any] | None:
        if self.operation != "upload":
            return None
        metadata = self.target.get("metadata")
        return dict(metadata) if isinstance(metadata, dict) else None

    @property
    def download_path(self) -> str | None:
        if self.operation != "download":
            return None
        path = self.target.get("path")
        return path if isinstance(path, str) else None


def canonical_json(value: Any) -> bytes:
    """Return the exact UTF-8 canonical JSON used by transfer grants."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load_transfer_contract(path: str | Path) -> dict[str, Any]:
    """Load the normative artifact without accepting duplicate JSON keys."""

    raw = Path(path).read_bytes()
    return _strict_json_object(raw)


def canonical_https_origin(value: str) -> str:
    """Validate and return one already-canonical HTTPS Origin value."""

    if not isinstance(value, str) or not value or not value.isascii() or len(value) > 255:
        raise ValueError("origin is not canonical")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin is not canonical") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin is not canonical")
    host = parsed.hostname
    if host != host.lower() or host.endswith(".") or not _valid_dns_name(host):
        raise ValueError("origin is not canonical")
    if port is not None and (port < 1 or port > 65535 or port == 443):
        raise ValueError("origin is not canonical")
    canonical_netloc = host if port is None else f"{host}:{port}"
    canonical = SplitResult("https", canonical_netloc, "", "", "").geturl()
    if canonical != value:
        raise ValueError("origin is not canonical")
    return canonical


def canonical_transfer_host(value: str) -> str:
    """Validate a canonical lower-case DNS host with an optional nondefault port."""

    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError("transfer host is not canonical")
    if any(character in value for character in "/?#@"):
        raise ValueError("transfer host is not canonical")
    host, separator, port_text = value.partition(":")
    if host != host.lower() or host.endswith(".") or not _valid_dns_name(host):
        raise ValueError("transfer host is not canonical")
    if separator:
        if not port_text or not port_text.isdecimal() or port_text.startswith("0"):
            raise ValueError("transfer host is not canonical")
        port = int(port_text)
        if not 1 <= port <= 65535 or port == 443:
            raise ValueError("transfer host is not canonical")
    return value


def private_v1_compatibility_enabled(
    *,
    deadline: str | None,
    signed_release_build_time: str | None,
    now: int,
) -> bool:
    """Evaluate the immutable, default-off private-v1 rollout window."""

    try:
        deadline_seconds = _canonical_utc_seconds(deadline)
        build_seconds = _canonical_utc_seconds(signed_release_build_time)
    except ValueError:
        return False
    return (
        build_seconds <= deadline_seconds <= build_seconds + 7 * 24 * 60 * 60
        and now < deadline_seconds
    )


def mint_transfer_grant_v2(
    *,
    signing_credential: str,
    kid: str,
    origin: str,
    operation: str,
    cell_id: str,
    principal_scope: str,
    jti: str,
    max_bytes: int,
    target: dict[str, Any],
    issued_at: int,
    not_before: int,
    expires_at: int,
) -> str:
    """Mint the exact v2 encoding for control-plane fixtures and issuers."""

    if len(signing_credential.encode("utf-8")) < 32:
        raise ValueError("signing credential is invalid")
    method = {"upload": "PUT", "download": "GET"}.get(operation)
    if method is None:
        raise ValueError("operation is invalid")
    claims = {
        "v": TRANSFER_GRANT_VERSION,
        "aud": TRANSFER_AUDIENCE,
        "kid": kid,
        "origin": origin,
        "op": operation,
        "method": method,
        "cell": cell_id,
        "principal": principal_scope,
        "iat": issued_at,
        "nbf": not_before,
        "exp": expires_at,
        "jti": jti,
        "limits": {"max_bytes": max_bytes},
        "target": target,
    }
    # Validate everything except verifier-relative time before signing.
    _claims_to_grant(
        claims,
        expected_origin=origin,
        expected_operation=operation,
        expected_method=method,
        expected_cell_id=cell_id,
        upload_limit_bytes=TRANSFER_UPLOAD_MAX_BYTES,
        storage_limit_bytes=max(max_bytes, 1),
        now=issued_at,
    )
    payload = _b64encode(canonical_json(claims))
    signature = hmac.new(
        signing_credential.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    token = f"{payload}.{_b64encode(signature)}"
    if len(token.encode("ascii")) > TRANSFER_GRANT_MAX_ASCII_BYTES:
        raise ValueError("grant exceeds its header bound")
    return token


def verify_transfer_grant_v2(
    token: str,
    *,
    security_authority: TransferSecurityAuthority,
    expected_origin: str,
    expected_operation: str,
    expected_method: str,
    expected_cell_id: str,
    upload_limit_bytes: int,
    storage_limit_bytes: int,
    now: int,
) -> TransferGrantV2:
    """Verify the exact canonical grant without consuming its JTI."""

    canonical_https_origin(expected_origin)
    try:
        encoded = token.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise TransferGrantRejected from exc
    if not encoded or len(encoded) > TRANSFER_GRANT_MAX_ASCII_BYTES:
        raise TransferGrantRejected
    if encoded.count(b".") != 1:
        raise TransferGrantRejected
    payload, encoded_signature = encoded.split(b".", 1)
    raw_claims = _b64decode(payload)
    signature = _b64decode(encoded_signature)
    if len(signature) != hashlib.sha256().digest_size:
        raise TransferGrantRejected
    claims = _strict_json_object(raw_claims)
    if canonical_json(claims) != raw_claims:
        raise TransferGrantRejected
    kid = claims.get("kid")
    if not isinstance(kid, str) or not _KID.fullmatch(kid):
        raise TransferGrantRejected
    try:
        accepted = security_authority.verify_transfer_signature(kid, payload, signature)
    except Exception as exc:  # noqa: BLE001 - authority failures are mapped content-free
        if _security_exception_is_unavailable(exc):
            raise TransferSecurityUnavailable from exc
        raise TransferGrantRejected from exc
    if not accepted:
        raise TransferGrantRejected
    return _claims_to_grant(
        claims,
        expected_origin=expected_origin,
        expected_operation=expected_operation,
        expected_method=expected_method,
        expected_cell_id=expected_cell_id,
        upload_limit_bytes=upload_limit_bytes,
        storage_limit_bytes=storage_limit_bytes,
        now=now,
    )


def consume_transfer_jti(
    security_authority: TransferSecurityAuthority,
    grant: TransferGrantV2,
    *,
    consumed_at: int,
) -> None:
    """Consume once through the injected durable security authority."""

    try:
        security_authority.consume_transfer_jti(
            cell_id=grant.cell_id,
            schema_version=TRANSFER_GRANT_VERSION,
            kid=grant.credential_version,
            jti=grant.jti,
            expires_at=grant.expires_at,
            consumed_at=consumed_at,
        )
    except Exception as exc:  # noqa: BLE001 - map foreign authority types by stable code
        if _security_exception_is_unavailable(exc):
            raise TransferSecurityUnavailable from exc
        raise TransferGrantRejected from exc


def _claims_to_grant(
    claims: dict[str, Any],
    *,
    expected_origin: str,
    expected_operation: str,
    expected_method: str,
    expected_cell_id: str,
    upload_limit_bytes: int,
    storage_limit_bytes: int,
    now: int,
) -> TransferGrantV2:
    if set(claims) != _CLAIM_FIELDS:
        raise TransferGrantRejected
    if claims.get("v") != TRANSFER_GRANT_VERSION or claims.get("aud") != TRANSFER_AUDIENCE:
        raise TransferGrantRejected
    kid = claims.get("kid")
    origin = claims.get("origin")
    operation = claims.get("op")
    method = claims.get("method")
    cell_id = claims.get("cell")
    principal = claims.get("principal")
    jti = claims.get("jti")
    if (
        not isinstance(kid, str)
        or not _KID.fullmatch(kid)
        or not isinstance(origin, str)
        or not isinstance(operation, str)
        or not isinstance(method, str)
        or not isinstance(cell_id, str)
        or not _CELL.fullmatch(cell_id)
        or not isinstance(principal, str)
        or not isinstance(jti, str)
    ):
        raise TransferGrantRejected
    try:
        canonical_https_origin(origin)
        _validate_principal(principal)
        _validate_uuid4(jti)
    except ValueError as exc:
        raise TransferGrantRejected from exc
    if (
        not hmac.compare_digest(origin, expected_origin)
        or not hmac.compare_digest(operation, expected_operation)
        or not hmac.compare_digest(method, expected_method)
        or not hmac.compare_digest(cell_id, expected_cell_id)
        or (operation, method) not in {("upload", "PUT"), ("download", "GET")}
    ):
        raise TransferGrantRejected

    times = (claims.get("iat"), claims.get("nbf"), claims.get("exp"))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in times):
        raise TransferGrantRejected
    issued_at, not_before, expires_at = times
    assert isinstance(issued_at, int)
    assert isinstance(not_before, int)
    assert isinstance(expires_at, int)
    if (
        not issued_at <= not_before < expires_at
        or expires_at - issued_at > TRANSFER_MAX_TTL_SECONDS
        or issued_at > now + TRANSFER_CLOCK_SKEW_SECONDS
        or not_before > now + TRANSFER_CLOCK_SKEW_SECONDS
        or now >= expires_at
    ):
        raise TransferGrantRejected

    limits = claims.get("limits")
    if not isinstance(limits, dict) or set(limits) != {"max_bytes"}:
        raise TransferGrantRejected
    max_bytes = limits.get("max_bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise TransferGrantRejected
    bound = upload_limit_bytes if operation == "upload" else storage_limit_bytes
    if operation == "upload":
        bound = min(bound, TRANSFER_UPLOAD_MAX_BYTES)
    if max_bytes > bound:
        raise TransferGrantRejected

    target = claims.get("target")
    if not isinstance(target, dict):
        raise TransferGrantRejected
    if operation == "upload":
        _validate_upload_target(target, max_bytes=max_bytes)
    else:
        _validate_download_target(target)
    return TransferGrantV2(
        credential_version=kid,
        origin=origin,
        operation=operation,
        method=method,
        cell_id=cell_id,
        principal_scope=principal,
        issued_at=issued_at,
        not_before=not_before,
        expires_at=expires_at,
        jti=jti,
        max_bytes=max_bytes,
        target=target,
    )


def _validate_upload_target(target: dict[str, Any], *, max_bytes: int) -> None:
    if set(target) != {"kind", "metadata", "metadata_sha256"}:
        raise TransferGrantRejected
    if target.get("kind") != "upload-v1":
        raise TransferGrantRejected
    metadata = target.get("metadata")
    metadata_sha256 = target.get("metadata_sha256")
    if not isinstance(metadata, dict) or not isinstance(metadata_sha256, str):
        raise TransferGrantRejected
    fields = {
        "category",
        "content_type",
        "description",
        "filename",
        "scope",
        "sha256",
        "size",
    }
    if set(metadata) != fields or not _SHA256.fullmatch(metadata_sha256):
        raise TransferGrantRejected
    expected_metadata_sha256 = hashlib.sha256(canonical_json(metadata)).hexdigest()
    if not hmac.compare_digest(metadata_sha256, expected_metadata_sha256):
        raise TransferGrantRejected
    filename = metadata.get("filename")
    if not isinstance(filename, str) or not _valid_filename(filename):
        raise TransferGrantRejected
    content_type = metadata.get("content_type")
    if (
        not isinstance(content_type, str)
        or not 1 <= len(content_type.encode("ascii", "ignore")) <= 255
        or not content_type.isascii()
        or not _MEDIA_TYPE.fullmatch(content_type)
    ):
        raise TransferGrantRejected
    for field, maximum in (("scope", 512), ("category", 512), ("description", 2048)):
        value = metadata.get(field)
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or not value
            or unicodedata.normalize("NFC", value) != value
            or len(value.encode("utf-8")) > maximum
        ):
            raise TransferGrantRejected
    digest = metadata.get("sha256")
    size = metadata.get("size")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise TransferGrantRejected
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > TRANSFER_UPLOAD_MAX_BYTES
        or size > max_bytes
    ):
        raise TransferGrantRejected


def _validate_download_target(target: dict[str, Any]) -> None:
    if set(target) != {"kind", "path"} or target.get("kind") != "download-v1":
        raise TransferGrantRejected
    path = target.get("path")
    if not isinstance(path, str) or not _valid_download_path(path):
        raise TransferGrantRejected


def _valid_filename(value: str) -> bool:
    encoded = value.encode("utf-8")
    return (
        1 <= len(encoded) <= 512
        and unicodedata.normalize("NFC", value) == value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


def _valid_download_path(value: str) -> bool:
    encoded = value.encode("utf-8")
    if (
        not 1 <= len(encoded) <= 4096
        or unicodedata.normalize("NFC", value) != value
        or value.startswith("/")
        or "\\" in value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        return False
    parts = value.split("/")
    return bool(
        parts
        and all(part and part not in {".", ".."} for part in parts)
        and len(parts[-1].encode("utf-8")) <= 512
    )


def _validate_principal(value: str) -> None:
    if not _PRINCIPAL.fullmatch(value):
        raise ValueError("principal is invalid")
    decoded = _b64decode(value.encode("ascii"))
    if len(decoded) != hashlib.sha256().digest_size:
        raise ValueError("principal is invalid")


def _validate_uuid4(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("JTI is invalid") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise ValueError("JTI is invalid")


def _valid_dns_name(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 253
        and all(_DNS_LABEL.fullmatch(label) for label in value.split("."))
    )


def _canonical_utc_seconds(value: str | None) -> int:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        value,
    ):
        raise ValueError("timestamp is not canonical UTC RFC3339")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("timestamp is not canonical UTC RFC3339") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("timestamp is not canonical UTC RFC3339")
    return int(parsed.timestamp())


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: bytes) -> bytes:
    if not value or not re.fullmatch(rb"[A-Za-z0-9_-]+", value):
        raise TransferGrantRejected
    try:
        decoded = base64.b64decode(
            value + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise TransferGrantRejected from exc
    if _b64encode(decoded).encode("ascii") != value:
        raise TransferGrantRejected
    return decoded


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TransferGrantRejected
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_hook)
    except TransferGrantRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransferGrantRejected from exc
    if not isinstance(value, dict):
        raise TransferGrantRejected
    return value


def _security_exception_is_unavailable(exc: Exception) -> bool:
    code = getattr(exc, "code", "")
    return code in {"HOSTED_SECURITY_UNAVAILABLE", "HOSTED_JTI_CAPACITY"}


__all__ = [
    "TRANSFER_AUDIENCE",
    "TRANSFER_DOWNLOAD_PATH",
    "TRANSFER_GRANT_HEADER",
    "TRANSFER_GRANT_MAX_ASCII_BYTES",
    "TRANSFER_GRANT_VERSION",
    "TRANSFER_RUNTIME_TEMP_QUOTA_BYTES",
    "TRANSFER_TEMP_QUOTA_BYTES",
    "TRANSFER_UPLOAD_MAX_BYTES",
    "TRANSFER_UPLOAD_PATH",
    "TRANSFER_V1_UPLOAD_MAX_BYTES",
    "TransferContractError",
    "TransferGrantRejected",
    "TransferGrantV2",
    "TransferSecurityAuthority",
    "TransferSecurityUnavailable",
    "canonical_https_origin",
    "canonical_json",
    "canonical_transfer_host",
    "consume_transfer_jti",
    "load_transfer_contract",
    "mint_transfer_grant_v2",
    "private_v1_compatibility_enabled",
    "verify_transfer_grant_v2",
]
