"""FastAPI application surface for the hosted provisioner."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .config import PROVISIONER_PROTOCOL, ProvisionerSettings
from .models import OperationState
from .provider_identity import (
    ProviderRecoveryIdentityCodec,
    cell_provider_recovery_envelopes,
    cell_resource_name,
    provider_operation_resource_name,
)
from .repository import IdempotencyConflict, OperationRepository, StaleFence
from .schemas import FINAL_MODELS, REQUEST_MODELS, PendingResponse, request_plaintext

ReadinessProbe = Callable[[], Awaitable[bool]]
Clock = Callable[[], datetime]
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")


def _failure(code: str, status: int, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "retryable": retryable})


def _validated_final(
    action: str,
    result: dict[str, Any],
    request_data: dict[str, object],
) -> Response:
    model = FINAL_MODELS[action]
    if model is None:
        if result:
            return _failure("PROVISIONER_RESPONSE_INVALID", 500)
        return Response(status_code=204)
    try:
        value = model.model_validate(result)
    except ValidationError:
        return _failure("PROVISIONER_RESPONSE_INVALID", 500)
    if (
        action == "rotate-credential"
        and request_data.get("phase") == "finalize"
        and result.get("previousCredentialRejected") is not True
    ):
        return _failure("PROVISIONER_RESPONSE_INVALID", 500)
    if action == "health" and any(
        result.get(response_name) != request_data.get(request_name)
        for response_name, request_name in (
            ("cellId", "cellId"),
            ("protocolVersion", "protocolVersion"),
            ("releaseVersion", "releaseVersion"),
            ("workerPolicy", "workerPolicy"),
        )
    ):
        return _failure("PROVISIONER_RESPONSE_INVALID", 500)
    return JSONResponse(status_code=200, content=value.model_dump(mode="json"))


def _new_export_expiry_is_valid(value: object, *, now: datetime) -> bool:
    if not isinstance(value, str):
        return False
    expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    checked_at = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    ttl = expires_at.astimezone(UTC) - checked_at.astimezone(UTC)
    return timedelta(0) < ttl <= timedelta(days=30)


def create_app(
    *,
    settings: ProvisionerSettings,
    readiness_probe: ReadinessProbe,
    repository: OperationRepository | None = None,
    provider_identity_codec: ProviderRecoveryIdentityCodec | None = None,
    clock: Clock = lambda: datetime.now(UTC),
) -> FastAPI:
    """Build an application with no implicit environment or provider access."""

    app = FastAPI(
        title="exomem-provisioner",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_failure(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return _failure("PROVISIONER_REJECTED", 422)

    @app.exception_handler(Exception)
    async def internal_failure(_request: Request, _error: Exception) -> JSONResponse:
        return _failure("PROVISIONER_UNAVAILABLE", 500, retryable=True)

    @app.middleware("http")
    async def enforce_contract(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ):
        if not request.url.path.startswith("/cells/"):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {settings.bearer.get_secret_value()}"
        if not secrets.compare_digest(authorization, expected):
            return _failure("PROVISIONER_REJECTED", 401)
        if request.url.scheme != "https":
            return _failure("PROVISIONER_REJECTED", 400)
        if request.headers.get("x-exomem-provisioner-protocol") != settings.protocol:
            return _failure("PROVISIONER_REJECTED", 400)
        if request.headers.get("content-type", "").lower() != "application/json":
            return _failure("PROVISIONER_REJECTED", 415)
        idempotency_key = request.headers.get("idempotency-key", "")
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            return _failure("PROVISIONER_REJECTED", 400)
        declared = request.headers.get("content-length")
        try:
            if declared is not None and int(declared) > settings.request_max_bytes:
                return _failure("PROVISIONER_REJECTED", 413)
        except ValueError:
            return _failure("PROVISIONER_REJECTED", 400)
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > settings.request_max_bytes:
                return _failure("PROVISIONER_REJECTED", 413)
            if chunk:
                chunks.append(chunk)
        body = b"".join(chunks)
        request._body = body  # Starlette replays this bounded generation to the endpoint.
        response = await call_next(request)
        response_length = response.headers.get("content-length")
        if response_length is not None and int(response_length) > settings.response_max_bytes:
            return _failure("PROVISIONER_RESPONSE_INVALID", 500)
        return response

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"protocol": PROVISIONER_PROTOCOL, "status": "live"}

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        try:
            ready = await readiness_probe()
        except Exception:  # noqa: BLE001 - readiness fails closed without exception detail
            ready = False
        if not ready:
            return _failure("PROVISIONER_UNAVAILABLE", 503, retryable=True)
        return JSONResponse(
            status_code=200,
            content={"protocol": settings.protocol, "status": "ready"},
        )

    if repository is None:
        return app

    def endpoint_for(action: str) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                raw = json.loads((await request.body()).decode("utf-8"))
                model = REQUEST_MODELS[action].model_validate(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
                return _failure("PROVISIONER_REJECTED", 422)
            try:
                request_data = request_plaintext(model)
                if provider_identity_codec is not None and "cellId" in request_data:
                    cell_id = str(request_data["cellId"])
                    operation_id = str(request_data["operationId"])
                    request_data["_providerRecoveryEnvelopes"] = cell_provider_recovery_envelopes(
                        provider_identity_codec,
                        tenant_id=str(request_data["tenantId"]),
                        cell_id=cell_id,
                        operation_id=operation_id,
                        fence_generation=int(request_data["fenceGeneration"]),
                        resource_name=cell_resource_name(cell_id),
                        operation_resource_name=provider_operation_resource_name(operation_id),
                    )
                if action == "export" and not _new_export_expiry_is_valid(
                    request_data.get("expiresAt"),
                    now=clock(),
                ):
                    existing = await repository.get(
                        action,
                        request.headers["idempotency-key"],
                    )
                    if existing is None:
                        return _failure("PROVISIONER_REJECTED", 422)
                operation = await repository.submit(
                    action,
                    request.headers["idempotency-key"],
                    request_data,
                    retry_after_seconds=settings.retry_after_seconds,
                )
            except (IdempotencyConflict, StaleFence):
                return _failure("CONTROL_PLANE_STATE_CONFLICT", 409)
            if operation.state is OperationState.ERROR:
                return _failure("PROVISIONER_REJECTED", 409)
            if operation.state is OperationState.FINAL:
                result = await repository.load_result(operation.id)
                if result is None:
                    return _failure("PROVISIONER_RESPONSE_INVALID", 500)
                return _validated_final(action, result, request_data)
            pending = PendingResponse(
                operationId=operation.external_operation_id,
                checkpoint=operation.caller_checkpoint,
                retryAfterSeconds=operation.retry_after_seconds,
            )
            return JSONResponse(
                status_code=202,
                headers={"Retry-After": str(operation.retry_after_seconds)},
                content=pending.model_dump(mode="json"),
            )

        endpoint.__name__ = f"post_{action.replace('-', '_')}"
        return endpoint

    for action in REQUEST_MODELS:
        app.add_api_route(
            f"/cells/{action}",
            endpoint_for(action),
            methods=["POST"],
            response_model=None,
        )
    return app
