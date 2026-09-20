"""Narrow private control-plane service for Hosted activation acknowledgement."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import Response

from .authorization_membership import (
    HostedAuthorizationBundle,
    advance_hosted_activation_bundle,
    inspect_hosted_authorization_bundle,
)
from .conflict_reason import ConflictReason
from .hosted_activation_ack_protocol import PROTOCOL, ProtocolError, decode_message, encode_message
from .lifecycle import MetadataConflict, OpaqueProviderMetadata
from .repository import ActivationAckCellSnapshot, RepositoryConflict

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,511}\Z")
_CREDENTIAL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MACHINE_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_PROOF_MAC_DOMAIN = b"exomem.hosted-activation-proof/v1\0"
_REQUEST_LIMIT = 8 * 1024
_HEADER_LIMIT = 8 * 1024
_RECEIVE_TIMEOUT_SECONDS = 0.5
_PROCESS_TIMEOUT_SECONDS = 3.5


class ActivationAckCellLookup(Protocol):
    async def lookup_activation_ack_cell(self, cell_id: str) -> ActivationAckCellSnapshot: ...


class ActivationAckFailure(RuntimeError):
    def __init__(self, code: str, status: int, *, retry_after_ms: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after_ms = retry_after_ms


def _failure(code: str, status: int, request_id: str | None, retry_after_ms: int = 0) -> Response:
    body = encode_message(
        {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "code": code,
            "retry_after_ms": retry_after_ms,
        },
        "errorResponse",
    )
    return Response(
        content=body,
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _success(value: Mapping[str, object]) -> Response:
    return Response(
        content=encode_message(value, "httpBundleResponse"),
        status_code=200,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _unpad_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _bundle_response(
    bundle: HostedAuthorizationBundle,
    *,
    metadata: OpaqueProviderMetadata,
    request_id: str,
    outcome: str,
) -> dict[str, object]:
    control = json.loads(bundle.control)
    return {
        "protocol": PROTOCOL,
        "request_id": request_id,
        "outcome": outcome,
        "cell_id": metadata.subject_id,
        "logical_vault_id": metadata.tenant_id,
        "registry_attachment_id": bundle.registry_attachment_id,
        "attachment_epoch": control["attachment_epoch"],
        "bundle_revision": bundle.revision,
        "keyring_sha256": hashlib.sha256(bundle.keyring).hexdigest(),
        "control_b64": _unpad_b64(bundle.control),
        "serving_membership_b64": _unpad_b64(bundle.membership),
    }


class ActivationAckService:
    """Authenticate one cell and publish only its exact committed successor."""

    def __init__(
        self,
        *,
        cell_lookup: ActivationAckCellLookup,
        cell: Any,
        runtime: Any,
        runtime_release: str,
        runtime_protocol_version: str,
        clock: Callable[[], int | float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        challenge_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if not runtime_release or not runtime_protocol_version:
            raise ValueError("activation acknowledgement runtime identity is required")
        self._lookup = cell_lookup
        self._cell = cell
        self._runtime = runtime
        self._runtime_release = runtime_release
        self._runtime_protocol_version = runtime_protocol_version
        self._clock = clock
        self._monotonic = monotonic
        self._challenge_bytes = challenge_bytes

    async def _authenticate(
        self,
        *,
        cell_id: str,
        credential_version: str,
        credential: str,
        deadline: float,
    ) -> tuple[OpaqueProviderMetadata, Any, HostedAuthorizationBundle]:
        self._require_live(deadline=deadline)
        try:
            context = await self._lookup.lookup_activation_ack_cell(cell_id)
            self._require_live(deadline=deadline)
            metadata = context.metadata
            credentials, _annotations = await self._cell.read_credential_bundle_authority(metadata)
            self._require_live(deadline=deadline)
        except (RepositoryConflict, MetadataConflict, KeyError, ValueError):
            raise ActivationAckFailure("AUTHENTICATION_FAILED", 401) from None
        if (
            metadata.subject_id != cell_id
            or _CREDENTIAL_VERSION.fullmatch(credential_version) is None
            or not isinstance(credentials, dict)
            or not 1 <= len(credentials) <= 2
            or credential_version not in credentials
            or not isinstance(credentials[credential_version], str)
            or not secrets.compare_digest(credentials[credential_version], credential)
        ):
            raise ActivationAckFailure("AUTHENTICATION_FAILED", 401)
        try:
            snapshot = await self._cell.read_authorization_session_authority(metadata)
            self._require_live(deadline=deadline)
            if snapshot is None:
                raise MetadataConflict("authorization session bundle is absent")
            current = int(self._clock())
            bundle = inspect_hosted_authorization_bundle(
                snapshot.files,
                expected_cell_id=metadata.subject_id,
                expected_logical_vault_id=metadata.tenant_id,
                expected_replica_id=metadata.resource_name + "-0",
                expected_software_version=self._runtime_release,
                expected_schema_version=4,
                expected_recovery_envelope=snapshot.recovery_envelope,
                now=current,
            )
            if snapshot.revision != bundle.revision:
                raise MetadataConflict("authorization session revision differs")
        except MetadataConflict:
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409) from None
        return metadata, snapshot, bundle

    def _require_live(self, *, deadline: float, proof_expires_at: int | None = None) -> None:
        if self._monotonic() >= deadline:
            raise ActivationAckFailure("ACK_DEADLINE_EXCEEDED", 503, retry_after_ms=250)
        if proof_expires_at is not None and int(self._clock()) >= proof_expires_at:
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)

    async def _effect_guard(
        self,
        *,
        expected_metadata: OpaqueProviderMetadata,
        credential_version: str,
        credential: str,
        deadline: float,
        proof_expires_at: int,
    ) -> None:
        self._require_live(deadline=deadline, proof_expires_at=proof_expires_at)
        try:
            context = await self._lookup.lookup_activation_ack_cell(expected_metadata.subject_id)
            self._require_live(deadline=deadline, proof_expires_at=proof_expires_at)
            credentials, _annotations = await self._cell.read_credential_bundle_authority(
                context.metadata
            )
            self._require_live(deadline=deadline, proof_expires_at=proof_expires_at)
        except (RepositoryConflict, MetadataConflict, KeyError, ValueError):
            raise ActivationAckFailure("AUTHENTICATION_FAILED", 401) from None
        if (
            context.metadata != expected_metadata
            or credential_version not in credentials
            or not secrets.compare_digest(credentials[credential_version], credential)
        ):
            raise ActivationAckFailure("AUTHENTICATION_FAILED", 401)
        self._require_live(deadline=deadline, proof_expires_at=proof_expires_at)

    def _advance(
        self,
        bundle: HostedAuthorizationBundle,
        snapshot: Any,
        metadata: OpaqueProviderMetadata,
        publication: Mapping[str, object],
    ) -> HostedAuthorizationBundle:
        predecessor = publication["predecessor"]
        successor = publication["successor"]
        assert isinstance(predecessor, Mapping) and isinstance(successor, Mapping)
        return advance_hosted_activation_bundle(
            bundle.files,
            expected_cell_id=metadata.subject_id,
            expected_logical_vault_id=metadata.tenant_id,
            expected_replica_id=metadata.resource_name + "-0",
            expected_software_version=self._runtime_release,
            expected_schema_version=4,
            expected_recovery_envelope=snapshot.recovery_envelope,
            expected_registry_attachment_id=bundle.registry_attachment_id,
            activation_store_id=str(successor["activation_store_id"]),
            predecessor_epoch=int(predecessor["activation_epoch"]),
            predecessor_digest=str(predecessor["activation_state_digest"]),
            successor_epoch=int(successor["activation_epoch"]),
            successor_digest=str(successor["activation_state_digest"]),
            now=int(self._clock()),
        )

    @staticmethod
    def _proof_key(bundle: HostedAuthorizationBundle, proof: Mapping[str, object]) -> bytes:
        keyring = json.loads(bundle.keyring)
        signing_key_id = proof["signing_key_id"]
        entries = [
            entry
            for entry in keyring["accepted_keys"]
            if isinstance(entry, dict) and entry.get("key_id") == signing_key_id
        ]
        if len(entries) != 1:
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)
        entry = entries[0]
        issued_at = proof["issued_at"]
        expires_at = proof["expires_at"]
        if (
            not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
            or not entry["not_before"] <= issued_at < expires_at <= entry["not_after"]
        ):
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)
        try:
            return base64.b64decode(
                str(entry["key"]).encode("ascii") + b"=",
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeError, ValueError):
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409) from None

    def _verify_proof(
        self,
        *,
        request: Mapping[str, object],
        proof: Mapping[str, object],
        bundle: HostedAuthorizationBundle,
    ) -> None:
        try:
            parsed = decode_message(encode_message(proof, "proofResponse"), "proofResponse")
        except ProtocolError:
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409) from None
        if any(parsed.get(name) != value for name, value in request.items()):
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)
        current = int(self._clock())
        issued_at = parsed["issued_at"]
        expires_at = parsed["expires_at"]
        if (
            not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
            or issued_at > current + 1
            or not issued_at <= current < expires_at
        ):
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)
        key = self._proof_key(bundle, parsed)
        supplied = str(parsed["mac"])
        signed = {name: value for name, value in parsed.items() if name != "mac"}
        canonical = json.dumps(
            signed,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected = hmac.new(key, _PROOF_MAC_DOMAIN + canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)

    async def current(
        self,
        *,
        cell_id: str,
        request_id: str,
        credential_version: str,
        credential: str,
        budget_seconds: float,
    ) -> dict[str, object]:
        deadline = self._monotonic() + budget_seconds
        metadata, _snapshot, bundle = await self._authenticate(
            cell_id=cell_id,
            credential_version=credential_version,
            credential=credential,
            deadline=deadline,
        )
        self._require_live(deadline=deadline)
        return _bundle_response(bundle, metadata=metadata, request_id=request_id, outcome="current")

    async def acknowledge(
        self,
        *,
        cell_id: str,
        request_id: str,
        credential_version: str,
        credential: str,
        budget_seconds: float,
        publication: Mapping[str, object],
    ) -> dict[str, object]:
        deadline = self._monotonic() + budget_seconds

        def remaining() -> float:
            value = deadline - self._monotonic()
            if value <= 0:
                raise ActivationAckFailure("ACK_DEADLINE_EXCEEDED", 503, retry_after_ms=250)
            return value

        for attempt in range(2):
            metadata, snapshot, bundle = await self._authenticate(
                cell_id=cell_id,
                credential_version=credential_version,
                credential=credential,
                deadline=deadline,
            )
            try:
                successor = self._advance(bundle, snapshot, metadata, publication)
            except MetadataConflict:
                raise ActivationAckFailure("ACTIVATION_CONFLICT", 409) from None
            callback_budget = remaining()
            challenge = self._challenge_bytes(32)
            if not isinstance(challenge, bytes) or len(challenge) != 32:
                raise ActivationAckFailure("ACK_UNAVAILABLE", 503, retry_after_ms=250)
            wall_now = float(self._clock())
            issued_at = int(wall_now)
            proof_request = {
                "protocol": PROTOCOL,
                "challenge_id": challenge.hex(),
                "issued_at": issued_at,
                "expires_at": min(
                    issued_at + 3,
                    bundle.expires_at,
                    int(wall_now + callback_budget),
                ),
                "cell_id": metadata.subject_id,
                "logical_vault_id": metadata.tenant_id,
                "registry_attachment_id": bundle.registry_attachment_id,
                "attachment_epoch": json.loads(bundle.control)["attachment_epoch"],
                "expected_bundle_revision": bundle.revision,
                "publication": dict(publication),
            }
            if proof_request["expires_at"] <= issued_at:
                raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)
            proof_expires_at = int(proof_request["expires_at"])
            try:
                proof = await self._runtime.activation_proof(
                    metadata,
                    credential=credential,
                    protocol_version=self._runtime_protocol_version,
                    request=proof_request,
                    timeout_seconds=min(1.0, callback_budget),
                )
                self._verify_proof(request=proof_request, proof=proof, bundle=bundle)
            except ActivationAckFailure:
                raise
            except Exception:  # noqa: BLE001 - callback details are never public
                raise ActivationAckFailure("ACK_UNAVAILABLE", 503, retry_after_ms=250) from None
            remaining()

            current_metadata, current_snapshot, current_bundle = await self._authenticate(
                cell_id=cell_id,
                credential_version=credential_version,
                credential=credential,
                deadline=deadline,
            )
            self._require_live(deadline=deadline, proof_expires_at=proof_expires_at)
            if current_metadata != metadata or current_snapshot.revision != snapshot.revision:
                if attempt == 0:
                    continue
                raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)
            try:
                successor = self._advance(current_bundle, current_snapshot, metadata, publication)
            except MetadataConflict:
                raise ActivationAckFailure("ACTIVATION_CONFLICT", 409) from None
            if successor.revision == current_bundle.revision:
                return _bundle_response(
                    successor,
                    metadata=metadata,
                    request_id=request_id,
                    outcome="unchanged",
                )
            try:
                remaining()
                await self._cell.write_authorization_session_bundle(
                    metadata,
                    successor.files,
                    recovery_envelope=current_snapshot.recovery_envelope,
                    membership_epoch=successor.epoch,
                    membership_digest=successor.membership_digest,
                    revision=successor.revision,
                    expected_revision=current_bundle.revision,
                    effect_guard=lambda guarded_metadata=metadata, guarded_proof_expires_at=proof_expires_at: (
                        self._effect_guard(
                            expected_metadata=guarded_metadata,
                            credential_version=credential_version,
                            credential=credential,
                            deadline=deadline,
                            proof_expires_at=guarded_proof_expires_at,
                        )
                    ),
                )
            except MetadataConflict as error:
                if (
                    error.reason is ConflictReason.AUTHORIZATION_SESSION_BUNDLE_CHANGED_CONCURRENTLY
                    and attempt == 0
                ):
                    continue
                raise ActivationAckFailure("ACTIVATION_CONFLICT", 409) from None
            remaining()
            return _bundle_response(
                successor,
                metadata=metadata,
                request_id=request_id,
                outcome="advanced",
            )
        raise ActivationAckFailure("ACTIVATION_CONFLICT", 409)


class _Admission:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.active = 0
        self.cells: set[str] = set()
        self.draining = False

    def acquire(self, cell_id: str) -> bool:
        if self.draining or self.active >= self.maximum or cell_id in self.cells:
            return False
        self.active += 1
        self.cells.add(cell_id)
        return True

    def release(self, cell_id: str) -> None:
        self.active -= 1
        self.cells.remove(cell_id)


def create_activation_ack_app(
    service: ActivationAckService,
    *,
    global_limit: int = 8,
) -> FastAPI:
    """Build the private listener application without general admission authority."""

    if global_limit != 8:
        raise ValueError("activation acknowledgement global limit must remain eight")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    admission = _Admission(global_limit)
    operations: set[asyncio.Task[dict[str, object]]] = set()

    async def bounded_body(request: Request) -> bytes:
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > _REQUEST_LIMIT:
                raise ProtocolError
            if chunk:
                chunks.append(chunk)
        return b"".join(chunks)

    def request_id(request: Request) -> str | None:
        value = request.headers.get("x-exomem-request-id")
        return value if isinstance(value, str) and _DIGEST.fullmatch(value) else None

    def authentication(request: Request) -> tuple[str, str, str, str]:
        correlation = request_id(request)
        cell_id = request.headers.get("x-exomem-cell-id", "")
        version = request.headers.get("x-exomem-credential-version", "")
        protocol = request.headers.get("x-exomem-activation-protocol", "")
        if (
            correlation is None
            or _IDENTIFIER.fullmatch(cell_id) is None
            or _CREDENTIAL_VERSION.fullmatch(version) is None
            or protocol != PROTOCOL
        ):
            raise ProtocolError
        authorization = request.headers.get("authorization", "")
        if (
            not authorization.startswith("Bearer ")
            or _MACHINE_CREDENTIAL.fullmatch(authorization[7:]) is None
        ):
            raise ActivationAckFailure("AUTHENTICATION_FAILED", 401)
        return cell_id, version, authorization[7:], correlation

    async def admitted(
        cell_id: str,
        operation: Callable[[asyncio.Future[float]], Awaitable[dict[str, object]]],
        *,
        receive_deadline: float,
    ) -> dict[str, object]:
        if not admission.acquire(cell_id):
            raise ActivationAckFailure("ACK_CAPACITY_EXCEEDED", 429, retry_after_ms=250)
        deadline_ready: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(operation(deadline_ready))
        operations.add(task)

        def release(completed: asyncio.Task[Any]) -> None:
            operations.discard(completed)
            admission.release(cell_id)
            if completed.cancelled():
                return
            completed.exception()

        task.add_done_callback(release)
        receive_wait = max(0.0, receive_deadline - time.monotonic())
        done, _pending = await asyncio.wait(
            {task, deadline_ready},
            timeout=receive_wait,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            return task.result()
        if deadline_ready not in done:
            raise ActivationAckFailure("ACK_DEADLINE_EXCEEDED", 503, retry_after_ms=250)
        processing_deadline = deadline_ready.result()
        done, _pending = await asyncio.wait(
            {task},
            timeout=max(0.0, processing_deadline - time.monotonic()),
        )
        if task not in done:
            raise ActivationAckFailure("ACK_DEADLINE_EXCEEDED", 503, retry_after_ms=250)
        return task.result()

    async def dispatch(request: Request, *, acknowledge: bool) -> Response:
        correlation = request_id(request)
        try:
            header_bytes = sum(len(name) + len(value) + 4 for name, value in request.headers.raw)
            if header_bytes > _HEADER_LIMIT or request.url.query:
                raise ProtocolError
            cell_id, version, credential, correlation = authentication(request)
            started = time.monotonic()
            receive_deadline = started + _RECEIVE_TIMEOUT_SECONDS
            if acknowledge:
                if request.headers.get("content-type", "").lower() != "application/json":
                    raise ProtocolError

                async def acknowledge_operation(
                    deadline_ready: asyncio.Future[float],
                ) -> dict[str, object]:
                    value = decode_message(await bounded_body(request), "httpAckRequest")
                    if time.monotonic() >= receive_deadline:
                        raise ActivationAckFailure("ACK_DEADLINE_EXCEEDED", 503, retry_after_ms=250)
                    if value["request_id"] != correlation:
                        raise ProtocolError
                    deadline = started + min(
                        int(value["budget_ms"]) / 1000,
                        _PROCESS_TIMEOUT_SECONDS,
                    )
                    deadline_ready.set_result(deadline)
                    return await service.acknowledge(
                        cell_id=cell_id,
                        request_id=correlation,
                        credential_version=version,
                        credential=credential,
                        budget_seconds=max(0.0, deadline - time.monotonic()),
                        publication=value["publication"],
                    )

                result = await admitted(
                    cell_id,
                    acknowledge_operation,
                    receive_deadline=receive_deadline,
                )
            else:

                async def current_operation(
                    deadline_ready: asyncio.Future[float],
                ) -> dict[str, object]:
                    if await bounded_body(request):
                        raise ProtocolError
                    if time.monotonic() >= receive_deadline:
                        raise ActivationAckFailure("ACK_DEADLINE_EXCEEDED", 503, retry_after_ms=250)
                    deadline = started + _PROCESS_TIMEOUT_SECONDS
                    deadline_ready.set_result(deadline)
                    return await service.current(
                        cell_id=cell_id,
                        request_id=correlation,
                        credential_version=version,
                        credential=credential,
                        budget_seconds=max(0.0, deadline - time.monotonic()),
                    )

                result = await admitted(
                    cell_id,
                    current_operation,
                    receive_deadline=receive_deadline,
                )
            return _success(result)
        except ProtocolError:
            return _failure("MALFORMED_REQUEST", 400, correlation)
        except ActivationAckFailure as error:
            return _failure(error.code, error.status, correlation, error.retry_after_ms)
        except Exception:  # noqa: BLE001 - private details must never cross this boundary
            return _failure("ACK_UNAVAILABLE", 503, correlation, 250)

    @app.post("/cell-runtime/v1/activation/ack")
    async def acknowledge(request: Request) -> Response:
        return await dispatch(request, acknowledge=True)

    @app.get("/cell-runtime/v1/activation/current")
    async def current(request: Request) -> Response:
        return await dispatch(request, acknowledge=False)

    async def drain() -> None:
        admission.draining = True
        while operations:
            await asyncio.gather(*tuple(operations), return_exceptions=True)

    app.state.activation_ack_drain = drain

    return app
