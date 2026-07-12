"""Private authenticated HTTP adapter for one hosted Exomem cell."""

from __future__ import annotations

import errno
import hmac
import json
import logging
import os
import stat
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import cli_ops, hosted_runtime
from . import commands as commands_module
from . import hosted_gateway as gateway
from .hosted_runtime import (
    HostedCellConfig,
    HostedCellLifecycle,
    HostedLifecycleError,
)
from .vault import VaultPathError, resolve_under_vault

_call_log = logging.getLogger("exomem.calls")
_MAX_COMMAND_BODY_BYTES = 1024 * 1024
_MAX_QUIESCE_SECONDS = 30.0
_MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_MAX_UPLOAD_FIELDS = 8
_MAX_UPLOAD_METADATA_BYTES = 32 * 1024
_MAX_UPLOAD_SHORT_FIELD_BYTES = 512
_DOWNLOAD_CHUNK_BYTES = 64 * 1024

_RESERVED_FIELDS = frozenset(
    {
        "tenant",
        "tenant_id",
        "tenant_scope",
        "account",
        "account_id",
        "cell",
        "cell_id",
        "cell_endpoint",
        "vault",
        "vault_path",
        "vault_root",
        "principal",
        "principal_scope",
        "request_id",
        "protocol",
        "protocol_version",
        "service_credential",
        "internal_endpoint",
        "endpoint",
        "private_address",
        "public_subject",
        "storage_root",
        "subject",
        "idempotency_scope",
        "retry_scope",
    }
)
_FORBIDDEN_HEADERS = frozenset(
    {
        "x-tenant-id",
        "x-tenant",
        "x-tenant-scope",
        "x-exomem-tenant-scope",
        "x-cell-id",
        "x-vault-path",
        "x-vault-root",
        "x-principal-scope",
        "x-request-id",
        "x-protocol-version",
        "x-idempotency-scope",
        "x-retry-scope",
        "x-internal-endpoint",
        "x-exomem-service-credential",
        "x-exomem-private-address",
    }
)
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        gateway.CELL_HEADER.lower(),
        gateway.PROTOCOL_HEADER.lower(),
        gateway.REQUEST_HEADER.lower(),
        gateway.PRINCIPAL_HEADER.lower(),
        "idempotency-key",
        "content-length",
        "content-type",
        gateway.TRANSFER_GRANT_HEADER.lower(),
        gateway.ROUTING_STOPPED_HEADER.lower(),
    }
)


class HostedJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        ).encode("utf-8")


def _message_for(code: str) -> str:
    if code == "HOSTED_UNAUTHORIZED":
        return "private authentication failed"
    if code == "HOSTED_CELL_CONTEXT_MISMATCH":
        return "trusted cell context is invalid"
    if code == "HOSTED_PROTOCOL_MISMATCH":
        return "gateway and cell protocol versions are incompatible"
    if code == "HOSTED_SELECTOR_REJECTED":
        return "request contains forbidden routing metadata"
    if code in {"HOSTED_TRANSFER_GRANT_INVALID", "HOSTED_TRANSFER_GRANT_EXPIRED"}:
        return "transfer authorization failed"
    if code.endswith("_NOT_FOUND") or code == "NOT_FOUND":
        return "requested resource was not found"
    if code.startswith("IDEMPOTENCY_"):
        return "request retry identity conflicts with an existing operation"
    if code.startswith("HOSTED_"):
        return "hosted cell cannot perform this operation"
    if code in {"UNKNOWN_PARAM", "MISSING_ARGUMENT", "BAD_INT", "BAD_BOOL", "BAD_JSON"}:
        return "request arguments do not match the command contract"
    return "hosted command failed"


def _status_for(code: str) -> int:
    if code == "HOSTED_UNAUTHORIZED" or code.startswith("HOSTED_TRANSFER_GRANT_"):
        return 401
    if code == "HOSTED_CELL_CONTEXT_MISMATCH":
        return 403
    if code == "HOSTED_PROTOCOL_MISMATCH":
        return 409
    if code == "HOSTED_TRANSFER_INTERCEPT_REQUIRED":
        return 409
    if code == "HOSTED_IMPORT_INTERCEPT_REQUIRED":
        return 409
    if code in {
        "HOSTED_MUTATION_NOT_ADMITTED",
        "HOSTED_READ_NOT_ADMITTED",
        "HOSTED_MUTATION_AUTHORITY_UNAVAILABLE",
        "HOSTED_TRANSFER_UNAVAILABLE",
        "HOSTED_TRANSFER_IN_FLIGHT",
        "HOSTED_DELETION_SEALED",
        "HOSTED_QUIESCE_TIMEOUT",
        "HOSTED_BACKGROUND_STOP_FAILED",
        "HOSTED_BACKGROUND_START_FAILED",
    }:
        return 503
    if code in {"TOO_LARGE", "HOSTED_TRANSFER_LIMIT_INVALID"}:
        return 413
    if code == "INTERNAL":
        return 500
    return cli_ops.http_status_for(code)


def _trace(
    *,
    config: HostedCellConfig,
    operation: str,
    request_id: str | None,
    outcome: str,
    code: str,
    started: float,
) -> None:
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    _call_log.info(
        "event=hosted_call cell=%s operation=%s request_id=%s outcome=%s code=%s duration_ms=%s",
        config.cell_id,
        operation,
        request_id or "untrusted",
        outcome,
        code,
        duration_ms,
    )


def _error_response(
    code: str,
    *,
    config: HostedCellConfig,
    operation: str,
    started: float,
    request_id: str | None = None,
    status: int | None = None,
) -> HostedJSONResponse:
    _trace(
        config=config,
        operation=operation,
        request_id=request_id,
        outcome="error",
        code=code,
        started=started,
    )
    return HostedJSONResponse(
        cli_ops.envelope(
            False,
            error={
                "code": code,
                "message": _message_for(code),
                "remediation": None,
            },
        ),
        status_code=_status_for(code) if status is None else status,
    )


def _success_response(
    data: Any,
    *,
    config: HostedCellConfig,
    operation: str,
    request_id: str,
    started: float,
    status: int = 200,
) -> HostedJSONResponse:
    _trace(
        config=config,
        operation=operation,
        request_id=request_id,
        outcome="success",
        code="OK",
        started=started,
    )
    return HostedJSONResponse(cli_ops.envelope(True, data=data), status_code=status)


def _normalized_field(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _request_has_selector(request: Request) -> bool:
    for header in request.headers:
        lowered = header.lower()
        if lowered in _FORBIDDEN_HEADERS or lowered.startswith("x-exomem-internal-"):
            return True
    if any(_normalized_field(key) in _RESERVED_FIELDS for key in request.query_params):
        return True
    return any(_normalized_field(key) in _RESERVED_FIELDS for key in request.cookies)


def _value_has_selector(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _normalized_field(str(key)) in _RESERVED_FIELDS:
                return True
            if _value_has_selector(nested):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_value_has_selector(item) for item in value)
    return False


def _has_duplicate_sensitive_headers(request: Request) -> bool:
    counts: dict[str, int] = {}
    for raw_name, _raw_value in request.scope.get("headers", ()):
        try:
            name = raw_name.decode("latin-1").lower()
        except AttributeError:
            name = str(raw_name).lower()
        if name in _SENSITIVE_HEADERS or name in _FORBIDDEN_HEADERS:
            counts[name] = counts.get(name, 0) + 1
            if counts[name] > 1:
                return True
    return False


def _bearer_credential(request: Request) -> str | None:
    scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


def _trusted_context(request: Request, config: HostedCellConfig) -> gateway.TrustedGatewayContext:
    if _has_duplicate_sensitive_headers(request):
        raise gateway.HostedGatewayError(
            "HOSTED_CONTEXT_INVALID", "trusted headers must occur exactly once"
        )
    if not config.matches_service_credential(_bearer_credential(request)):
        raise gateway.HostedGatewayError(
            "HOSTED_UNAUTHORIZED", "private service authentication failed"
        )
    if _request_has_selector(request):
        raise gateway.HostedGatewayError(
            "HOSTED_SELECTOR_REJECTED", "request contains forbidden selector metadata"
        )
    presented_cell = request.headers.get(gateway.CELL_HEADER, "").strip()
    if not presented_cell or not hmac.compare_digest(presented_cell, config.cell_id):
        raise gateway.HostedGatewayError(
            "HOSTED_CELL_CONTEXT_MISMATCH", "trusted cell context is invalid"
        )
    protocol = request.headers.get(gateway.PROTOCOL_HEADER, "").strip()
    if not protocol or not hmac.compare_digest(protocol, config.protocol_version):
        raise gateway.HostedGatewayError(
            "HOSTED_PROTOCOL_MISMATCH", "gateway and cell protocol are incompatible"
        )
    request_id = gateway.validate_request_id(request.headers.get(gateway.REQUEST_HEADER, ""))
    principal = gateway.validate_principal_scope(request.headers.get(gateway.PRINCIPAL_HEADER, ""))
    idempotency_key = request.headers.get("idempotency-key", "").strip() or None
    if idempotency_key is not None:
        idempotency_key = gateway.validate_opaque_scope(idempotency_key, field="idempotency key")
    return gateway.TrustedGatewayContext(
        cell_id=config.cell_id,
        protocol_version=config.protocol_version,
        request_id=request_id,
        principal_scope=principal,
        idempotency_key=idempotency_key,
    )


async def _json_body(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise ValueError
            if parsed_content_length > _MAX_COMMAND_BODY_BYTES:
                raise gateway.HostedGatewayError("TOO_LARGE", "request body is too large")
        except ValueError as exc:
            raise gateway.HostedGatewayError(
                "INVALID_BODY", "request content length is invalid"
            ) from exc
    try:
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > _MAX_COMMAND_BODY_BYTES:
                raise gateway.HostedGatewayError("TOO_LARGE", "request body is too large")
            chunks.append(chunk)
        raw = b"".join(chunks)
    except gateway.HostedGatewayError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert transport failures to a stable code
        raise gateway.HostedGatewayError("INVALID_BODY", "request body is invalid") from exc
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise gateway.HostedGatewayError("INVALID_BODY", "request body is invalid") from exc
    if not isinstance(value, dict):
        raise gateway.HostedGatewayError("INVALID_BODY", "request body must be an object")
    if _value_has_selector(value):
        raise gateway.HostedGatewayError(
            "HOSTED_SELECTOR_REJECTED", "request contains forbidden selector metadata"
        )
    return value


def _bounded_upload_request(request: Request, *, max_body_bytes: int) -> Request:
    """Wrap ASGI receive so chunked multipart bodies cannot exceed the grant."""

    received = 0

    async def bounded_receive() -> dict[str, Any]:
        nonlocal received
        message = await request.receive()
        if message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > max_body_bytes:
                # Starlette closes parser-owned temporary files for this exception.
                raise MultiPartException("multipart body exceeded its transfer grant")
        return message

    return Request(request.scope, receive=bounded_receive)


def _upload_content_length(request: Request, *, max_body_bytes: int) -> None:
    presented = request.headers.get("content-length", "").strip()
    if not presented:
        return
    try:
        length = int(presented)
    except ValueError as exc:
        raise gateway.HostedGatewayError(
            "INVALID_UPLOAD", "upload content length is invalid"
        ) from exc
    if length < 0:
        raise gateway.HostedGatewayError("INVALID_UPLOAD", "upload content length is invalid")
    if length > max_body_bytes:
        raise gateway.HostedGatewayError("TOO_LARGE", "upload is too large")


def _validate_upload_form(form: Any, *, max_bytes: int) -> UploadFile:
    allowed_fields = {"file", "scope", "category", "description", "text", "filename"}
    if set(form.keys()) - allowed_fields:
        raise gateway.HostedGatewayError("INVALID_UPLOAD", "upload contains unknown fields")
    if _value_has_selector(form):
        raise gateway.HostedGatewayError(
            "HOSTED_SELECTOR_REJECTED", "upload contains selector metadata"
        )
    files = form.getlist("file")
    if len(files) != 1 or not isinstance(files[0], UploadFile):
        raise gateway.HostedGatewayError(
            "INVALID_UPLOAD", "exactly one multipart file field is required"
        )
    for field in allowed_fields - {"file"}:
        if len(form.getlist(field)) > 1:
            raise gateway.HostedGatewayError("INVALID_UPLOAD", "upload fields must not be repeated")
    upload = files[0]
    if upload.size is not None and (upload.size < 0 or upload.size > max_bytes):
        raise gateway.HostedGatewayError("TOO_LARGE", "upload is too large")
    return upload


def _validate_upload_metadata(form: Any, upload: UploadFile) -> dict[str, str | None]:
    values: dict[str, str] = {}
    for field in ("scope", "category", "description", "text", "filename"):
        raw = form.get(field)
        if isinstance(raw, UploadFile):
            raise gateway.HostedGatewayError(
                "INVALID_UPLOAD", "upload metadata fields must be text"
            )
        values[field] = str(raw or "").strip()

    fallback_filename = str(upload.filename or "").strip()
    filename = values["filename"] or fallback_filename
    for field in ("scope", "category"):
        if len(values[field].encode("utf-8")) > _MAX_UPLOAD_SHORT_FIELD_BYTES:
            raise gateway.HostedGatewayError("INVALID_UPLOAD", "upload metadata is too large")
    if len(filename.encode("utf-8")) > _MAX_UPLOAD_SHORT_FIELD_BYTES:
        raise gateway.HostedGatewayError("INVALID_UPLOAD", "upload metadata is too large")
    metadata_bytes = sum(len(values[field].encode("utf-8")) for field in ("description", "text"))
    if metadata_bytes > _MAX_UPLOAD_METADATA_BYTES:
        raise gateway.HostedGatewayError("INVALID_UPLOAD", "upload metadata is too large")
    return {
        "scope": values["scope"],
        "category": values["category"],
        "description": values["description"] or None,
        "text": values["text"] or None,
        "filename": filename,
    }


def _measure_and_rewind_upload(upload: UploadFile) -> int:
    stream = upload.file
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    return size


def _open_bounded_vault_file(
    vault_root: Path,
    requested_path: str,
    *,
    max_bytes: int,
) -> tuple[BinaryIO, int, str]:
    """Open one regular vault file without following any path-component symlink."""

    raw = str(requested_path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise VaultPathError(code="INVALID_PATH", reason="path is invalid")
    _candidate, relative = resolve_under_vault(vault_root, raw)
    parts = tuple(relative.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise VaultPathError(code="INVALID_PATH", reason="path is invalid")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory or os.open not in os.supports_dir_fd:
        raise gateway.HostedGatewayError(
            "HOSTED_TRANSFER_UNAVAILABLE",
            "safe hosted download opening is unavailable",
        )

    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.open(
            vault_root,
            os.O_RDONLY | directory | nofollow | close_on_exec,
        )
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | close_on_exec,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | nofollow | close_on_exec,
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise VaultPathError(code="NOT_A_FILE", reason="path is not a regular file")
        if file_stat.st_size > max_bytes:
            raise gateway.HostedGatewayError(
                "HOSTED_TRANSFER_LIMIT_INVALID", "download exceeds grant limit"
            )
        opened = os.fdopen(file_fd, "rb")
        file_fd = None
        return opened, file_stat.st_size, parts[-1]
    except FileNotFoundError as exc:
        raise VaultPathError(code="NOT_FOUND", reason="path does not exist") from exc
    except NotADirectoryError as exc:
        raise VaultPathError(code="NOT_A_FILE", reason="path is not a file") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EXDEV}:
            raise VaultPathError(code="INVALID_PATH", reason="path is invalid") from exc
        if exc.errno in {errno.EISDIR, errno.ENOTDIR}:
            raise VaultPathError(code="NOT_A_FILE", reason="path is not a file") from exc
        raise gateway.HostedGatewayError(
            "HOSTED_TRANSFER_UNAVAILABLE", "safe hosted download opening failed"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


async def _stream_bounded_file(
    stream: BinaryIO,
    size: int,
    admission: AbstractContextManager[None],
) -> AsyncIterator[bytes]:
    remaining = size
    try:
        while remaining:
            chunk = await run_in_threadpool(stream.read, min(_DOWNLOAD_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()
        admission.__exit__(None, None, None)


def _default_mutation_guard(vault_root: Path) -> AbstractContextManager[None]:
    return hosted_runtime.hosted_mutation_guard(vault_root)


def _default_preserve_stream(*args: Any, **kwargs: Any) -> Any:
    from . import preserve

    return preserve.preserve_stream(*args, **kwargs)


def register_hosted_routes(
    mcp_app: FastMCP,
    *,
    config: HostedCellConfig,
    lifecycle: HostedCellLifecycle,
    source_schema: Any,
    expose_tier2: bool = True,
    invoke_command_func: Callable[..., Any] | None = None,
    mutation_guard_factory: Callable[[Path], AbstractContextManager[None]] | None = None,
    preserve_stream_func: Callable[..., Any] | None = None,
) -> None:
    """Register the private v1 contract. Every custom route authenticates itself."""

    commands = commands_module.product_commands_for("rest", expose_tier2=expose_tier2)
    by_name = {command.name: command for command in commands}
    contract = gateway.build_gateway_contract(
        protocol_version=config.protocol_version,
        expose_tier2=expose_tier2,
    )
    invoke = invoke_command_func
    if invoke is None:
        from .writer_lease import invoke_command

        invoke = invoke_command
    guard_factory = mutation_guard_factory or _default_mutation_guard
    preserve_stream = preserve_stream_func or _default_preserve_stream

    @mcp_app.custom_route("/private/exomem/v1/contract", methods=["GET"])
    async def _contract(request: Request) -> Response:
        started = time.perf_counter()
        try:
            context = _trusted_context(request, config)
        except gateway.HostedGatewayError as exc:
            return _error_response(exc.code, config=config, operation="contract", started=started)
        _trace(
            config=config,
            operation="contract",
            request_id=context.request_id,
            outcome="success",
            code="OK",
            started=started,
        )
        return Response(
            gateway.canonical_contract_json(contract),
            media_type="application/json",
        )

    @mcp_app.custom_route("/private/exomem/v1/live", methods=["GET"])
    async def _live(request: Request) -> HostedJSONResponse:
        started = time.perf_counter()
        try:
            context = _trusted_context(request, config)
        except gateway.HostedGatewayError as exc:
            return _error_response(exc.code, config=config, operation="live", started=started)
        return _success_response(
            lifecycle.liveness().as_dict(),
            config=config,
            operation="live",
            request_id=context.request_id,
            started=started,
        )

    @mcp_app.custom_route("/private/exomem/v1/ready", methods=["GET"])
    async def _ready(request: Request) -> HostedJSONResponse:
        started = time.perf_counter()
        try:
            context = _trusted_context(request, config)
        except gateway.HostedGatewayError as exc:
            return _error_response(exc.code, config=config, operation="ready", started=started)
        return _success_response(
            lifecycle.readiness().as_dict(),
            config=config,
            operation="ready",
            request_id=context.request_id,
            started=started,
        )

    @mcp_app.custom_route("/private/exomem/v1/command/{command_name}", methods=["POST"])
    async def _command(request: Request) -> HostedJSONResponse:
        started = time.perf_counter()
        operation = "command"
        context: gateway.TrustedGatewayContext | None = None
        try:
            context = _trusted_context(request, config)
            body = await _json_body(request)
            command_name = str(request.path_params.get("command_name", ""))
            command = by_name.get(command_name)
            if command is None:
                raise cli_ops.OpError("COMMAND_NOT_FOUND", "command is not exposed")
            if command.leaf is commands_module.op_transfer_artifact:
                raise gateway.HostedGatewayError(
                    "HOSTED_TRANSFER_INTERCEPT_REQUIRED",
                    "hosted transfers must use the gateway transfer flow",
                )
            if command.leaf is commands_module.op_adopt_vault:
                raise gateway.HostedGatewayError(
                    "HOSTED_IMPORT_INTERCEPT_REQUIRED",
                    "hosted imports must use the gateway lifecycle flow",
                )
            operation = command.name
            kwargs = cli_ops.coerce(
                command.params,
                body,
                guarded_fields=command.guarded_fields,
                tool=command.name,
            )
            injected = (
                (config.vault_root, source_schema) if command.needs_schema else (config.vault_root,)
            )

            def invoke_admitted() -> Any:
                if command.read_only:
                    lifecycle.require_read_admission()
                    return invoke(
                        command,
                        *injected,
                        idempotency_key=gateway.scoped_idempotency_key(context),
                        implicit_idempotency_scope=gateway.implicit_retry_scope(context),
                        **kwargs,
                    )
                with lifecycle.admit_mutation():
                    return invoke(
                        command,
                        *injected,
                        idempotency_key=gateway.scoped_idempotency_key(context),
                        implicit_idempotency_scope=gateway.implicit_retry_scope(context),
                        **kwargs,
                    )

            result = await run_in_threadpool(invoke_admitted)
        except gateway.HostedGatewayError as exc:
            return _error_response(
                exc.code,
                config=config,
                operation=operation,
                request_id=context.request_id if context else None,
                started=started,
            )
        except HostedLifecycleError as exc:
            return _error_response(
                exc.code,
                config=config,
                operation=operation,
                request_id=context.request_id if context else None,
                started=started,
            )
        except Exception as exc:  # noqa: BLE001 - private boundary redacts exception text
            error = cli_ops.error_dict(exc)
            return _error_response(
                error["code"],
                config=config,
                operation=operation,
                request_id=context.request_id if context else None,
                started=started,
            )
        assert context is not None
        return _success_response(
            result,
            config=config,
            operation=operation,
            request_id=context.request_id,
            started=started,
        )

    async def lifecycle_context(
        request: Request, operation: str
    ) -> tuple[gateway.TrustedGatewayContext | None, HostedJSONResponse | None, float]:
        started = time.perf_counter()
        try:
            return _trusted_context(request, config), None, started
        except gateway.HostedGatewayError as exc:
            return (
                None,
                _error_response(exc.code, config=config, operation=operation, started=started),
                started,
            )

    @mcp_app.custom_route("/private/exomem/v1/lifecycle/quiesce", methods=["POST"])
    async def _quiesce(request: Request) -> HostedJSONResponse:
        context, error, started = await lifecycle_context(request, "quiesce")
        if error is not None:
            return error
        assert context is not None
        try:
            body = await _json_body(request)
            if set(body) - {"timeout_seconds"}:
                raise gateway.HostedGatewayError("INVALID_BODY", "quiesce body has unknown fields")
            timeout = body.get("timeout_seconds", 5)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 0 <= float(timeout) <= _MAX_QUIESCE_SECONDS
            ):
                raise gateway.HostedGatewayError("INVALID_BODY", "quiesce timeout is invalid")
            result = await run_in_threadpool(lifecycle.quiesce, timeout=float(timeout))
        except (gateway.HostedGatewayError, HostedLifecycleError) as exc:
            return _error_response(
                exc.code,
                config=config,
                operation="quiesce",
                request_id=context.request_id,
                started=started,
            )
        return _success_response(
            result.as_dict(),
            config=config,
            operation="quiesce",
            request_id=context.request_id,
            started=started,
        )

    @mcp_app.custom_route("/private/exomem/v1/lifecycle/resume", methods=["POST"])
    async def _resume(request: Request) -> HostedJSONResponse:
        context, error, started = await lifecycle_context(request, "resume")
        if error is not None:
            return error
        assert context is not None
        try:
            body = await _json_body(request)
            if body:
                raise gateway.HostedGatewayError("INVALID_BODY", "resume body must be empty")
            result = await run_in_threadpool(lifecycle.resume)
        except (gateway.HostedGatewayError, HostedLifecycleError) as exc:
            return _error_response(
                exc.code,
                config=config,
                operation="resume",
                request_id=context.request_id,
                started=started,
            )
        return _success_response(
            result.as_dict(),
            config=config,
            operation="resume",
            request_id=context.request_id,
            started=started,
        )

    @mcp_app.custom_route("/private/exomem/v1/lifecycle/seal", methods=["POST"])
    async def _seal(request: Request) -> HostedJSONResponse:
        context, error, started = await lifecycle_context(request, "seal")
        if error is not None:
            return error
        assert context is not None
        try:
            body = await _json_body(request)
            if body:
                raise gateway.HostedGatewayError("INVALID_BODY", "seal body must be empty")
            if request.headers.get(gateway.ROUTING_STOPPED_HEADER, "").strip().lower() != "true":
                raise gateway.HostedGatewayError(
                    "HOSTED_ROUTING_NOT_STOPPED", "public routing must be stopped"
                )
            result = await run_in_threadpool(lifecycle.seal_for_deletion)
        except (gateway.HostedGatewayError, HostedLifecycleError) as exc:
            return _error_response(
                exc.code,
                config=config,
                operation="seal",
                request_id=context.request_id,
                started=started,
            )
        return _success_response(
            result.as_dict(),
            config=config,
            operation="seal",
            request_id=context.request_id,
            started=started,
        )

    @mcp_app.custom_route("/private/exomem/v1/upload", methods=["POST"])
    async def _upload(request: Request) -> HostedJSONResponse:
        started = time.perf_counter()
        context: gateway.TrustedGatewayContext | None = None
        try:
            context = _trusted_context(request, config)
            grant = gateway.verify_transfer_grant(
                request.headers.get(gateway.TRANSFER_GRANT_HEADER, ""),
                config,
                expected_operation="upload",
                expected_tenant_scope=None,
                expected_principal_scope=context.principal_scope,
            )
            max_body_bytes = grant.max_bytes + _MAX_MULTIPART_OVERHEAD_BYTES
            _upload_content_length(request, max_body_bytes=max_body_bytes)
            bounded_request = _bounded_upload_request(request, max_body_bytes=max_body_bytes)
            try:
                async with bounded_request.form(
                    max_files=1,
                    max_fields=_MAX_UPLOAD_FIELDS,
                    max_part_size=_MAX_UPLOAD_METADATA_BYTES,
                ) as form:
                    upload = _validate_upload_form(form, max_bytes=grant.max_bytes)
                    metadata = _validate_upload_metadata(form, upload)
                    measured_size = await run_in_threadpool(_measure_and_rewind_upload, upload)
                    if measured_size > grant.max_bytes:
                        raise gateway.HostedGatewayError("TOO_LARGE", "upload is too large")

                    def commit_upload() -> Any:
                        with lifecycle.admit_mutation():
                            with guard_factory(config.vault_root):
                                return preserve_stream(
                                    config.vault_root,
                                    scope=metadata["scope"],
                                    category=metadata["category"],
                                    filename=metadata["filename"],
                                    stream=upload.file,
                                    content_type=upload.content_type,
                                    description=metadata["description"],
                                    text=metadata["text"],
                                    max_bytes=grant.max_bytes,
                                )

                    result = await run_in_threadpool(commit_upload)
            except MultiPartException as exc:
                raise gateway.HostedGatewayError("TOO_LARGE", "upload is too large") from exc
        except gateway.HostedGatewayError as exc:
            return _error_response(
                exc.code,
                config=config,
                operation="upload",
                request_id=context.request_id if context else None,
                started=started,
            )
        except HostedLifecycleError as exc:
            return _error_response(
                exc.code,
                config=config,
                operation="upload",
                request_id=context.request_id if context else None,
                started=started,
            )
        except Exception as exc:  # noqa: BLE001 - redact preserve and transport details
            code = getattr(exc, "code", "INTERNAL")
            return _error_response(
                code,
                config=config,
                operation="upload",
                request_id=context.request_id if context else None,
                started=started,
            )
        assert context is not None
        return _success_response(
            result.as_dict(),
            config=config,
            operation="upload",
            request_id=context.request_id,
            started=started,
            status=201,
        )

    @mcp_app.custom_route("/private/exomem/v1/download", methods=["GET"])
    async def _download(request: Request) -> Response:
        started = time.perf_counter()
        context: gateway.TrustedGatewayContext | None = None
        transfer_admission: AbstractContextManager[None] | None = None
        try:
            context = _trusted_context(request, config)
            grant = gateway.verify_transfer_grant(
                request.headers.get(gateway.TRANSFER_GRANT_HEADER, ""),
                config,
                expected_operation="download",
                expected_tenant_scope=None,
                expected_principal_scope=context.principal_scope,
            )
            admitted_transfer = lifecycle.admit_transfer()
            admitted_transfer.__enter__()
            transfer_admission = admitted_transfer
            requested_paths = request.query_params.getlist("path")
            if len(requested_paths) != 1 or not requested_paths[0].strip():
                raise gateway.HostedGatewayError("INVALID_PATH", "download path is required")
            stream, size, filename = await run_in_threadpool(
                _open_bounded_vault_file,
                config.vault_root,
                requested_paths[0],
                max_bytes=grant.max_bytes,
            )
        except VaultPathError as exc:
            if transfer_admission is not None:
                transfer_admission.__exit__(None, None, None)
            return _error_response(
                exc.code,
                config=config,
                operation="download",
                request_id=context.request_id if context else None,
                started=started,
            )
        except (gateway.HostedGatewayError, HostedLifecycleError) as exc:
            if transfer_admission is not None:
                transfer_admission.__exit__(None, None, None)
            return _error_response(
                exc.code,
                config=config,
                operation="download",
                request_id=context.request_id if context else None,
                started=started,
            )
        except Exception:  # noqa: BLE001 - private boundary redacts path/open details
            if transfer_admission is not None:
                transfer_admission.__exit__(None, None, None)
            return _error_response(
                "INTERNAL",
                config=config,
                operation="download",
                request_id=context.request_id if context else None,
                started=started,
            )
        assert context is not None
        assert transfer_admission is not None
        _trace(
            config=config,
            operation="download",
            request_id=context.request_id,
            outcome="success",
            code="OK",
            started=started,
        )
        safe_filename = quote(filename, safe="")
        return StreamingResponse(
            _stream_bounded_file(stream, size, transfer_admission),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=utf-8''{safe_filename}",
                "Content-Length": str(size),
            },
        )


__all__ = ["HostedJSONResponse", "register_hosted_routes"]
