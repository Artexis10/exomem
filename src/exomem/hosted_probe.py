"""Hardened authenticated loopback probe for hosted Exomem cells."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from . import hosted_gateway
from .hosted_security import (
    CredentialMaterial,
    HostedCredentialRejected,
    HostedSecurityError,
    HostedSecurityStateInvalid,
    ProofPersistence,
)

PROBE_PATH = "/private/exomem/v1/ready"
PROBE_RESPONSE_MAX_BYTES = 16 * 1024
PROBE_CONNECT_TIMEOUT_SECONDS = 1.0
PROBE_READ_TIMEOUT_SECONDS = 2.0
PROBE_TOTAL_TIMEOUT_SECONDS = 3.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CREDENTIAL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PROTOCOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_READINESS_FIELDS = frozenset(
    {
        "cell_id",
        "vault_id",
        "exomem_release",
        "hosted_protocol",
        "authenticated_credential_version",
        "security_revision",
        "service_authenticated",
        "mutation_authority",
        "admission_phase",
        "read_admission",
        "write_admission",
        "worker_policy_digest",
    }
)


class HostedProbeError(RuntimeError):
    """Stable redacted probe failure; never includes transport or response data."""

    _MESSAGES = {
        "HOSTED_PROBE_TRANSPORT_INVALID": "hosted probe transport is invalid",
        "HOSTED_PROBE_UNAVAILABLE": "hosted probe is temporarily unavailable",
        "HOSTED_PROBE_TIMEOUT": "hosted probe timed out",
        "HOSTED_PROBE_REDIRECT": "hosted probe rejected a redirect",
        "HOSTED_PROBE_RESPONSE_TOO_LARGE": "hosted probe response is too large",
        "HOSTED_PROBE_MEDIA_INVALID": "hosted probe response media is invalid",
        "HOSTED_PROBE_SCHEMA_INVALID": "hosted probe response schema is invalid",
        "HOSTED_PROBE_AUTH_FAILED": "hosted probe authentication failed",
        "HOSTED_PROBE_CONTRACT_MISMATCH": "hosted probe contract does not match",
        "HOSTED_CREDENTIAL_BUNDLE_INVALID": "hosted credential bundle is invalid",
        "HOSTED_CREDENTIAL_STATE_INVALID": "hosted credential state is invalid",
        "HOSTED_SECURITY_UNAVAILABLE": "hosted security state is unavailable",
    }

    def __init__(self, code: str) -> None:
        self.code = code
        self.message = self._MESSAGES.get(code, "hosted probe failed safely")
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True, slots=True)
class HostedProbeRequest:
    request_id: str
    operation_id: str
    request_digest: str
    cell_id: str
    vault_id: str
    selected_credential_version: str
    expected_release: str
    expected_protocol: str
    expected_worker_policy_digest: str
    expected_revision: int
    port: int

    def __post_init__(self) -> None:
        try:
            request_id = uuid.UUID(self.request_id)
        except (ValueError, AttributeError) as exc:
            raise HostedProbeError("HOSTED_PROBE_TRANSPORT_INVALID") from exc
        valid = (
            str(request_id) == self.request_id
            and request_id.version == 4
            and bool(_OPERATION_ID.fullmatch(self.operation_id))
            and bool(_SHA256.fullmatch(self.request_digest))
            and bool(_OPAQUE_ID.fullmatch(self.cell_id))
            and bool(_OPAQUE_ID.fullmatch(self.vault_id))
            and bool(_CREDENTIAL_VERSION.fullmatch(self.selected_credential_version))
            and isinstance(self.expected_release, str)
            and 1 <= len(self.expected_release.encode("utf-8")) <= 64
            and bool(_PROTOCOL.fullmatch(self.expected_protocol))
            and bool(_SHA256.fullmatch(self.expected_worker_policy_digest))
            and isinstance(self.expected_revision, int)
            and not isinstance(self.expected_revision, bool)
            and self.expected_revision >= 1
            and isinstance(self.port, int)
            and not isinstance(self.port, bool)
            and 1024 <= self.port <= 65535
        )
        if not valid:
            raise HostedProbeError("HOSTED_PROBE_TRANSPORT_INVALID")


@dataclass(frozen=True, slots=True)
class HostedProbeResult:
    cell_id: str
    vault_id: str
    exomem_release: str
    hosted_protocol: str
    authenticated_credential_version: str
    security_revision: int
    service_authenticated: bool
    mutation_authority: bool
    admission_phase: str
    read_admission: bool
    write_admission: bool
    worker_policy_digest: str
    proof_recorded: bool
    proof_valid_until: str | None

    def as_data(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "vault_id": self.vault_id,
            "exomem_release": self.exomem_release,
            "hosted_protocol": self.hosted_protocol,
            "authenticated_credential_version": self.authenticated_credential_version,
            "security_revision": self.security_revision,
            "service_authenticated": self.service_authenticated,
            "mutation_authority": self.mutation_authority,
            "admission_phase": self.admission_phase,
            "read_admission": self.read_admission,
            "write_admission": self.write_admission,
            "worker_policy_digest": self.worker_policy_digest,
            "proof_recorded": self.proof_recorded,
            "proof_valid_until": self.proof_valid_until,
        }


class ProbeSecurityAuthority(Protocol):
    def credential_material(self, version: str) -> CredentialMaterial: ...

    def record_probe_proof(self, **kwargs: Any) -> ProofPersistence: ...


def _duplicates_rejected(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostedProbeError("HOSTED_PROBE_SCHEMA_INVALID")
        result[key] = value
    return result


def _parse_readiness(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicates_rejected,
        )
    except HostedProbeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HostedProbeError("HOSTED_PROBE_SCHEMA_INVALID") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"success", "data"}:
        raise HostedProbeError("HOSTED_PROBE_SCHEMA_INVALID")
    if parsed["success"] is not True or not isinstance(parsed["data"], dict):
        raise HostedProbeError("HOSTED_PROBE_SCHEMA_INVALID")
    data = parsed["data"]
    if set(data) != _READINESS_FIELDS:
        raise HostedProbeError("HOSTED_PROBE_SCHEMA_INVALID")
    string_fields = (
        "cell_id",
        "vault_id",
        "exomem_release",
        "hosted_protocol",
        "authenticated_credential_version",
        "admission_phase",
        "worker_policy_digest",
    )
    if any(not isinstance(data[field], str) for field in string_fields):
        raise HostedProbeError("HOSTED_PROBE_SCHEMA_INVALID")
    if (
        not _OPAQUE_ID.fullmatch(data["cell_id"])
        or not _OPAQUE_ID.fullmatch(data["vault_id"])
        or not _CREDENTIAL_VERSION.fullmatch(data["authenticated_credential_version"])
        or not _SHA256.fullmatch(data["worker_policy_digest"])
        or not isinstance(data["security_revision"], int)
        or isinstance(data["security_revision"], bool)
        or data["security_revision"] < 1
        or any(
            not isinstance(data[field], bool)
            for field in (
                "service_authenticated",
                "mutation_authority",
                "read_admission",
                "write_admission",
            )
        )
    ):
        raise HostedProbeError("HOSTED_PROBE_SCHEMA_INVALID")
    return data


def _readiness_digest(data: dict[str, Any]) -> str:
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_contract(data: dict[str, Any], request: HostedProbeRequest) -> None:
    expected = {
        "cell_id": request.cell_id,
        "vault_id": request.vault_id,
        "exomem_release": request.expected_release,
        "hosted_protocol": request.expected_protocol,
        "authenticated_credential_version": request.selected_credential_version,
        "security_revision": request.expected_revision,
        "service_authenticated": True,
        "mutation_authority": True,
        "admission_phase": "active",
        "read_admission": True,
        "write_admission": True,
        "worker_policy_digest": request.expected_worker_policy_digest,
    }
    if data != expected:
        raise HostedProbeError("HOSTED_PROBE_CONTRACT_MISMATCH")


def _fresh_http_identity(
    *,
    uuid_factory: Callable[[], uuid.UUID],
    random_bytes: Callable[[int], bytes],
) -> tuple[str, str]:
    request_uuid = uuid_factory()
    if not isinstance(request_uuid, uuid.UUID) or request_uuid.version != 4:
        raise HostedProbeError("HOSTED_PROBE_TRANSPORT_INVALID")
    entropy = random_bytes(32)
    if not isinstance(entropy, bytes) or len(entropy) != 32:
        raise HostedProbeError("HOSTED_PROBE_TRANSPORT_INVALID")
    principal = base64.urlsafe_b64encode(hashlib.sha256(entropy).digest()).rstrip(b"=").decode()
    return str(request_uuid), principal


def _probe_security_code(code: str) -> str:
    if code == "HOSTED_CREDENTIAL_WEAK":
        return "HOSTED_CREDENTIAL_BUNDLE_INVALID"
    if code in {
        "HOSTED_CREDENTIAL_REVISION_CONFLICT",
        "HOSTED_CREDENTIAL_TRANSITION_INVALID",
        "HOSTED_OPERATION_CONFLICT",
    }:
        return "HOSTED_CREDENTIAL_STATE_INVALID"
    return code


async def run_hosted_probe(
    request: HostedProbeRequest,
    *,
    authority: ProbeSecurityAuthority,
    transport: httpx.AsyncBaseTransport | None = None,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    random_bytes: Callable[[int], bytes] = os.urandom,
    now: Callable[[], int | float] = time.time,
) -> HostedProbeResult:
    """Perform one fresh, fixed-destination authenticated readiness proof."""

    # Recheck at the call boundary so forged dataclass instances still fail before
    # loading a credential or creating a client.
    if (
        not isinstance(request.port, int)
        or isinstance(request.port, bool)
        or not 1024 <= request.port <= 65535
    ):
        raise HostedProbeError("HOSTED_PROBE_TRANSPORT_INVALID")
    try:
        material = authority.credential_material(request.selected_credential_version)
    except HostedCredentialRejected as exc:
        raise HostedProbeError("HOSTED_PROBE_AUTH_FAILED") from exc
    except HostedSecurityError as exc:
        raise HostedProbeError(_probe_security_code(exc.code)) from exc
    if (
        material.credential_version != request.selected_credential_version
        or material.security_revision != request.expected_revision
    ):
        raise HostedProbeError("HOSTED_CREDENTIAL_STATE_INVALID")

    http_request_id, principal = _fresh_http_identity(
        uuid_factory=uuid_factory,
        random_bytes=random_bytes,
    )
    url = f"http://127.0.0.1:{request.port}{PROBE_PATH}"
    timeout = httpx.Timeout(
        PROBE_TOTAL_TIMEOUT_SECONDS,
        connect=PROBE_CONNECT_TIMEOUT_SECONDS,
        read=PROBE_READ_TIMEOUT_SECONDS,
        write=PROBE_READ_TIMEOUT_SECONDS,
        pool=PROBE_CONNECT_TIMEOUT_SECONDS,
    )
    options: dict[str, Any] = {
        "timeout": timeout,
        "trust_env": False,
        "follow_redirects": False,
    }
    if transport is not None:
        options["transport"] = transport
    headers = {
        "Authorization": f"Bearer {material.secret}",
        hosted_gateway.CELL_HEADER: request.cell_id,
        hosted_gateway.PROTOCOL_HEADER: request.expected_protocol,
        hosted_gateway.REQUEST_HEADER: http_request_id,
        hosted_gateway.PRINCIPAL_HEADER: principal,
        "Accept": "application/json",
    }
    try:
        async with asyncio.timeout(PROBE_TOTAL_TIMEOUT_SECONDS):
            async with client_factory(**options) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if 300 <= response.status_code <= 399:
                        raise HostedProbeError("HOSTED_PROBE_REDIRECT")
                    if response.status_code in {401, 403}:
                        raise HostedProbeError("HOSTED_PROBE_AUTH_FAILED")
                    if response.status_code != 200:
                        raise HostedProbeError("HOSTED_PROBE_UNAVAILABLE")
                    if response.headers.get_list("content-type") != ["application/json"]:
                        raise HostedProbeError("HOSTED_PROBE_MEDIA_INVALID")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > PROBE_RESPONSE_MAX_BYTES:
                            raise HostedProbeError("HOSTED_PROBE_RESPONSE_TOO_LARGE")
                        chunks.append(chunk)
    except HostedProbeError:
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HostedProbeError("HOSTED_PROBE_TIMEOUT") from exc
    except httpx.HTTPError as exc:
        raise HostedProbeError("HOSTED_PROBE_UNAVAILABLE") from exc

    data = _parse_readiness(b"".join(chunks))
    _validate_contract(data, request)
    try:
        persistence = authority.record_probe_proof(
            selected_version=request.selected_credential_version,
            expected_revision=request.expected_revision,
            operation_id=request.operation_id,
            request_digest=request.request_digest,
            request_id=request.request_id,
            release=request.expected_release,
            protocol=request.expected_protocol,
            worker_policy_digest=request.expected_worker_policy_digest,
            readiness_digest=_readiness_digest(data),
            now=int(now()),
        )
    except HostedSecurityStateInvalid as exc:
        raise HostedProbeError("HOSTED_CREDENTIAL_STATE_INVALID") from exc
    except HostedSecurityError as exc:
        raise HostedProbeError(_probe_security_code(exc.code)) from exc
    proof_valid_until = (
        None
        if persistence.valid_until is None
        else datetime.fromtimestamp(persistence.valid_until, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )
    return HostedProbeResult(
        **data,
        proof_recorded=persistence.recorded,
        proof_valid_until=proof_valid_until,
    )


__all__ = [
    "HostedProbeError",
    "HostedProbeRequest",
    "HostedProbeResult",
    "ProbeSecurityAuthority",
    "run_hosted_probe",
]
