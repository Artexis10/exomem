"""The sole public application surface for hosted transfer-v2 capabilities."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import uuid
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, BinaryIO

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import hosted_transfer
from .hosted_runtime import HostedCellConfig, HostedCellLifecycle, HostedLifecycleError
from .vault import VaultPathError, resolve_under_vault

_DOWNLOAD_CHUNK_BYTES = 64 * 1024

# --- Adoption staging intake (Lane D hosted entrypoint) --------------------
# A staged upload rides the SAME verified transfer grant as a normal upload but
# lands as RAW bytes under a vault-relative staging tree OUTSIDE `Knowledge
# Base/`, so the adoption engine treats it as legacy input to scan. The locked
# upload-v1 metadata schema (hosted_transfer._validate_upload_target) has no room
# for new fields, so staging reuses existing SIGNED metadata fields:
#   scope        == _ADOPTION_STAGING_SCOPE -> route to the staging branch
#   category     -> the run identifier (strict slug, validated before path use)
#   filename     -> the raw leaf name (single-file landing)
#   content_type -> a ZIP media type declares an archive to expand cell-side
#   description  -> OPTIONAL relative subdirectory under the run dir (or None)
_ADOPTION_STAGING_SCOPE = "adoption-staging"
_ADOPTION_STAGING_ROOT = "_Staging/adoption"
_ADOPTION_STAGING_ZIP_CONTENT_TYPES = frozenset(
    {"application/zip", "application/x-zip-compressed"}
)
_ADOPTION_STAGING_MAX_ENTRIES = 4096
_ADOPTION_STAGING_MAX_TOTAL_BYTES = hosted_transfer.TRANSFER_UPLOAD_MAX_BYTES
_ADOPTION_STAGING_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ADOPTION_STAGING_COPY_CHUNK_BYTES = 1024 * 1024

_ERROR_CATALOG: dict[str, tuple[int, str, bool, bool]] = {
    "TRANSFER_REQUEST_INVALID": (400, "transfer request is invalid", False, False),
    "TRANSFER_GRANT_REJECTED": (401, "transfer authorization failed", False, True),
    "TRANSFER_ORIGIN_REJECTED": (403, "transfer origin is not allowed", False, False),
    "TRANSFER_ADMISSION_CLOSED": (409, "transfer admission is closed", True, False),
    "TRANSFER_SECURITY_UNAVAILABLE": (
        503,
        "transfer security state is unavailable",
        True,
        False,
    ),
    "TRANSFER_TARGET_UNAVAILABLE": (404, "transfer target is unavailable", False, True),
    "TRANSFER_TOO_LARGE": (413, "transfer exceeded its byte allowance", False, True),
    "TRANSFER_INTEGRITY_FAILED": (
        422,
        "transfer integrity verification failed",
        False,
        True,
    ),
    "TRANSFER_COMMIT_UNAVAILABLE": (
        503,
        "transfer commit is temporarily unavailable",
        True,
        True,
    ),
    "TRANSFER_INTERNAL": (500, "transfer failed safely", False, True),
}
_RFC8187_ATTR_CHAR = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$&+-.^_`|~"
)
_FORBIDDEN_SELECTOR_HEADERS = frozenset(
    {
        "x-tenant-id",
        "x-tenant",
        "x-tenant-scope",
        "x-exomem-tenant-scope",
        "x-cell-id",
        "x-exomem-cell-id",
        "x-exomem-protocol-version",
        "x-exomem-request-id",
        "x-exomem-principal-scope",
        "x-exomem-routing-stopped",
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
        "idempotency-key",
    }
)


class TransferJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8")


def register_public_transfer_routes(
    mcp_app: FastMCP,
    *,
    config: HostedCellConfig,
    lifecycle: HostedCellLifecycle,
    security_authority: hosted_transfer.TransferSecurityAuthority | None,
    mutation_guard_factory: Callable[[Path], AbstractContextManager[None]],
    preserve_stream_func: Callable[..., Any],
    run_in_threadpool_func: Callable[..., Any],
) -> None:
    """Register only exact PUT/GET/OPTIONS capability routes."""

    upload_slot = threading.Lock()
    temp_root = _prepare_temp_root(config.state_root)

    def verified_grant(
        request: Request,
        *,
        operation: str,
        method: str,
    ) -> hosted_transfer.TransferGrantV2:
        if security_authority is None:
            raise hosted_transfer.TransferSecurityUnavailable
        return hosted_transfer.verify_transfer_grant_v2(
            _grant_header(request),
            security_authority=security_authority,
            expected_origin=config.transfer_browser_origin or "",
            expected_operation=operation,
            expected_method=method,
            expected_cell_id=config.cell_id,
            upload_limit_bytes=config.resource_limits.upload_bytes,
            storage_limit_bytes=config.resource_limits.storage_bytes,
            now=int(time.time()),
        )

    @mcp_app.custom_route(hosted_transfer.TRANSFER_UPLOAD_PATH, methods=["OPTIONS"])
    async def upload_options(request: Request) -> Response:
        try:
            headers = _require_preflight(
                request,
                config,
                method="PUT",
                allowed_headers=("Content-Type", hosted_transfer.TRANSFER_GRANT_HEADER),
                optional_headers=("Content-Type",),
            )
        except PublicRequestError as exc:
            return _error_response(
                exc.code,
                request=request,
                config=config,
                cors_authority=False,
            )
        return Response(status_code=204, headers=headers)

    @mcp_app.custom_route(hosted_transfer.TRANSFER_DOWNLOAD_PATH, methods=["OPTIONS"])
    async def download_options(request: Request) -> Response:
        try:
            headers = _require_preflight(
                request,
                config,
                method="GET",
                allowed_headers=(hosted_transfer.TRANSFER_GRANT_HEADER,),
            )
        except PublicRequestError as exc:
            return _error_response(
                exc.code,
                request=request,
                config=config,
                cors_authority=False,
            )
        return Response(status_code=204, headers=headers)

    @mcp_app.custom_route(hosted_transfer.TRANSFER_UPLOAD_PATH, methods=["PUT"])
    async def upload(request: Request) -> TransferJSONResponse:
        admission: AbstractContextManager[None] | None = None
        upload_slot_held = False
        temp_path: Path | None = None
        temp_stream: BinaryIO | None = None
        try:
            _require_route_request(request, config, expected_method="PUT")
            grant = verified_grant(request, operation="upload", method="PUT")
            metadata = grant.upload_metadata
            if metadata is None:
                raise hosted_transfer.TransferGrantRejected
            _validate_upload_framing(request, signed_size=int(metadata["size"]))
            if request.headers.get("content-type") != metadata["content_type"]:
                raise PublicRequestError("TRANSFER_REQUEST_INVALID")
            admitted = lifecycle.admit_public_transfer()
            admitted.__enter__()
            admission = admitted
            if not upload_slot.acquire(blocking=False):
                raise PublicRequestError("TRANSFER_ADMISSION_CLOSED")
            upload_slot_held = True
            assert security_authority is not None
            hosted_transfer.consume_transfer_jti(
                security_authority,
                grant,
                consumed_at=int(time.time()),
            )

            temp_path = temp_root / f"upload-{uuid.uuid4()}.tmp"
            descriptor = os.open(
                temp_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            temp_stream = os.fdopen(descriptor, "w+b")
            digest = hashlib.sha256()
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if (
                    received > int(metadata["size"])
                    or received > grant.max_bytes
                    or received > hosted_transfer.TRANSFER_UPLOAD_MAX_BYTES
                    or received > hosted_transfer.TRANSFER_TEMP_QUOTA_BYTES
                ):
                    raise PublicRequestError("TRANSFER_TOO_LARGE")
                temp_stream.write(chunk)
                digest.update(chunk)
            if received != int(metadata["size"]):
                raise PublicRequestError("TRANSFER_INTEGRITY_FAILED")
            upload_sha256 = digest.hexdigest()
            if not hmac.compare_digest(upload_sha256, str(metadata["sha256"])):
                raise PublicRequestError("TRANSFER_INTEGRITY_FAILED")
            temp_stream.flush()
            os.fsync(temp_stream.fileno())
            temp_stream.seek(0)

            def commit() -> None:
                assert temp_stream is not None
                with mutation_guard_factory(config.vault_root):
                    if metadata["scope"] == _ADOPTION_STAGING_SCOPE:
                        _stage_adoption_upload(
                            vault_root=config.vault_root,
                            temp_root=temp_root,
                            metadata=metadata,
                            stream=temp_stream,
                            max_bytes=grant.max_bytes,
                        )
                    else:
                        preserve_stream_func(
                            config.vault_root,
                            scope=metadata["scope"] or "uploads",
                            category=metadata["category"] or "uncategorized",
                            filename=metadata["filename"],
                            stream=temp_stream,
                            content_type=metadata["content_type"],
                            description=metadata["description"],
                            text=None,
                            max_bytes=grant.max_bytes,
                        )

            try:
                await run_in_threadpool_func(commit)
            except PublicRequestError:
                # Deliberate staging rejections (malicious/oversized archive, an
                # invalid run target) keep their exact public code instead of
                # being masked as a retryable commit fault.
                raise
            except Exception as exc:  # noqa: BLE001 - governed commit stays redacted
                raise PublicRequestError("TRANSFER_COMMIT_UNAVAILABLE") from exc
        except hosted_transfer.TransferSecurityUnavailable:
            return _error_response("TRANSFER_SECURITY_UNAVAILABLE", request=request, config=config)
        except hosted_transfer.TransferGrantRejected:
            return _error_response("TRANSFER_GRANT_REJECTED", request=request, config=config)
        except HostedLifecycleError:
            return _error_response("TRANSFER_ADMISSION_CLOSED", request=request, config=config)
        except PublicRequestError as exc:
            return _error_response(exc.code, request=request, config=config)
        except Exception:  # noqa: BLE001 - public transport/content details are redacted
            return _error_response("TRANSFER_INTERNAL", request=request, config=config)
        finally:
            if temp_stream is not None:
                temp_stream.close()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if upload_slot_held:
                upload_slot.release()
            if admission is not None:
                admission.__exit__(None, None, None)
        return TransferJSONResponse(
            {
                "success": True,
                "data": {
                    "operation": "upload",
                    "bytes": received,
                    "sha256": upload_sha256,
                    "committed": True,
                },
            },
            status_code=201,
            headers=_cors_headers(request, config),
        )

    @mcp_app.custom_route(hosted_transfer.TRANSFER_DOWNLOAD_PATH, methods=["GET"])
    async def download(request: Request) -> Response:
        admission: AbstractContextManager[None] | None = None
        stream: BinaryIO | None = None
        try:
            _require_route_request(request, config, expected_method="GET")
            if _raw_header_values(request, "content-length") or _raw_header_values(
                request, "transfer-encoding"
            ):
                raise PublicRequestError("TRANSFER_REQUEST_INVALID")
            grant = verified_grant(request, operation="download", method="GET")
            admitted = lifecycle.admit_public_transfer()
            admitted.__enter__()
            admission = admitted
            assert security_authority is not None
            hosted_transfer.consume_transfer_jti(
                security_authority,
                grant,
                consumed_at=int(time.time()),
            )
            requested_path = grant.download_path
            if requested_path is None:
                raise hosted_transfer.TransferGrantRejected
            stream, size, filename = await run_in_threadpool_func(
                _open_bounded_vault_file,
                config.vault_root,
                requested_path,
                max_bytes=grant.max_bytes,
            )
        except hosted_transfer.TransferSecurityUnavailable:
            _release(admission)
            return _error_response("TRANSFER_SECURITY_UNAVAILABLE", request=request, config=config)
        except hosted_transfer.TransferGrantRejected:
            _release(admission)
            return _error_response("TRANSFER_GRANT_REJECTED", request=request, config=config)
        except HostedLifecycleError:
            _release(admission)
            return _error_response("TRANSFER_ADMISSION_CLOSED", request=request, config=config)
        except VaultPathError:
            _release(admission)
            return _error_response("TRANSFER_TARGET_UNAVAILABLE", request=request, config=config)
        except PublicRequestError as exc:
            _release(admission)
            return _error_response(exc.code, request=request, config=config)
        except DownloadTooLarge:
            _release(admission)
            return _error_response("TRANSFER_TOO_LARGE", request=request, config=config)
        except Exception:  # noqa: BLE001 - public open details are redacted
            if stream is not None:
                stream.close()
            _release(admission)
            return _error_response("TRANSFER_INTERNAL", request=request, config=config)
        assert admission is not None
        assert stream is not None
        return StreamingResponse(
            _stream_bounded_file(
                stream,
                size,
                admission,
                run_in_threadpool_func=run_in_threadpool_func,
            ),
            media_type="application/octet-stream",
            headers={
                **_cors_headers(request, config, download=True),
                "Content-Disposition": _download_disposition(filename),
                "Content-Length": str(size),
            },
        )


class PublicRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CATALOG else "TRANSFER_INTERNAL"
        super().__init__(self.code)


class DownloadTooLarge(RuntimeError):
    pass


def _prepare_temp_root(state_root: Path) -> Path:
    temp_root = _ensure_temp_root(state_root)
    try:
        if any(temp_root.iterdir()):
            raise OSError("transfer temp root was not cleaned during locked startup")
    except OSError as exc:
        raise RuntimeError("public transfer temp is unavailable") from exc
    return temp_root


def cleanup_hosted_transfer_temp(state_root: Path) -> Path:
    """Remove only recognized v2 upload remnants during locked server startup."""

    temp_root = _ensure_temp_root(state_root)
    try:
        for entry in temp_root.iterdir():
            if (
                entry.is_symlink()
                or not entry.is_file()
                or not re.fullmatch(
                    r"upload-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.tmp",
                    entry.name,
                )
            ):
                raise OSError("transfer temp root contains an unknown entry")
            entry.unlink()
    except OSError as exc:
        raise RuntimeError("public transfer temp is unavailable") from exc
    return temp_root


def _ensure_temp_root(state_root: Path) -> Path:
    temp_root = state_root / "tmp" / "transfers-v2"
    try:
        if temp_root.is_symlink():
            raise OSError("transfer temp root is a symbolic link")
        temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_root.chmod(0o700)
    except OSError as exc:
        raise RuntimeError("public transfer temp is unavailable") from exc
    return temp_root


def _raw_header_values(request: Request, name: str) -> list[str]:
    wanted = name.lower().encode("ascii")
    return [
        value.decode("latin-1")
        for raw_name, value in request.scope.get("headers", ())
        if raw_name.lower() == wanted
    ]


def _cors_headers(
    request: Request,
    config: HostedCellConfig,
    *,
    download: bool = False,
) -> dict[str, str]:
    headers = {"Vary": "Origin", "Cache-Control": "private, no-store"}
    origins = _raw_header_values(request, "origin")
    if (
        len(origins) == 1
        and config.transfer_browser_origin is not None
        and hmac.compare_digest(origins[0], config.transfer_browser_origin)
    ):
        headers["Access-Control-Allow-Origin"] = config.transfer_browser_origin
        if download:
            headers["Access-Control-Expose-Headers"] = (
                "Content-Disposition, Content-Length, Content-Type"
            )
    return headers


def _error_response(
    code: str,
    *,
    request: Request,
    config: HostedCellConfig,
    cors_authority: bool = True,
) -> TransferJSONResponse:
    status, message, retryable, requires_new_grant = _ERROR_CATALOG[code]
    headers = (
        _cors_headers(
            request,
            config,
            download=request.url.path == hosted_transfer.TRANSFER_DOWNLOAD_PATH,
        )
        if cors_authority
        else {"Vary": "Origin", "Cache-Control": "private, no-store"}
    )
    return TransferJSONResponse(
        {
            "success": False,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "requires_new_grant": requires_new_grant,
            },
        },
        status_code=status,
        headers=headers,
    )


def _require_route_request(
    request: Request,
    config: HostedCellConfig,
    *,
    expected_method: str,
) -> None:
    if config.transfer_host is None or config.transfer_browser_origin is None:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    hosts = _raw_header_values(request, "host")
    if len(hosts) != 1 or not hmac.compare_digest(hosts[0], config.transfer_host):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    if request.method != expected_method:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    origins = _raw_header_values(request, "origin")
    if (
        len(origins) != 1
        or not hmac.compare_digest(origins[0], config.transfer_browser_origin)
    ):
        raise PublicRequestError("TRANSFER_ORIGIN_REJECTED")
    if request.url.query:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    if any(_raw_header_values(request, name) for name in ("authorization", "cookie", "range")):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    if any(
        raw_name.decode("latin-1").lower() in _FORBIDDEN_SELECTOR_HEADERS
        or raw_name.decode("latin-1").lower().startswith("x-exomem-internal-")
        for raw_name, _value in request.scope.get("headers", ())
    ):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")


def _require_preflight(
    request: Request,
    config: HostedCellConfig,
    *,
    method: str,
    allowed_headers: tuple[str, ...],
    optional_headers: tuple[str, ...] = (),
) -> dict[str, str]:
    _require_route_request(request, config, expected_method="OPTIONS")
    if _raw_header_values(request, hosted_transfer.TRANSFER_GRANT_HEADER):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    content_lengths = _raw_header_values(request, "content-length")
    if len(content_lengths) > 1 or (content_lengths and content_lengths[0] != "0"):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    if _raw_header_values(request, "transfer-encoding"):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    methods = _raw_header_values(request, "access-control-request-method")
    headers = _raw_header_values(request, "access-control-request-headers")
    if len(methods) != 1 or methods[0] != method or len(headers) != 1:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    presented = tuple(part.strip().lower() for part in headers[0].split(","))
    allowed = frozenset(value.lower() for value in allowed_headers)
    optional = frozenset(value.lower() for value in optional_headers)
    required = allowed - optional
    presented_set = frozenset(presented)
    if (
        not optional.issubset(allowed)
        or len(presented) != len(presented_set)
        or not required.issubset(presented_set)
        or not presented_set.issubset(allowed)
    ):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    return {
        **_cors_headers(request, config),
        "Access-Control-Allow-Methods": method,
        "Access-Control-Allow-Headers": ", ".join(allowed_headers),
        "Access-Control-Max-Age": "300",
    }


def _grant_header(request: Request) -> str:
    grants = _raw_header_values(request, hosted_transfer.TRANSFER_GRANT_HEADER)
    if len(grants) != 1:
        raise hosted_transfer.TransferGrantRejected
    return grants[0]


def _validate_upload_framing(request: Request, *, signed_size: int) -> None:
    if len(_raw_header_values(request, "content-type")) != 1:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    lengths = _raw_header_values(request, "content-length")
    encodings = _raw_header_values(request, "transfer-encoding")
    if len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    if encodings and encodings[0].lower() != "chunked":
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    if lengths:
        value = lengths[0]
        if not re.fullmatch(r"0|[1-9][0-9]*", value) or int(value) != signed_size:
            raise PublicRequestError("TRANSFER_REQUEST_INVALID")


def _open_bounded_vault_file(
    vault_root: Path,
    requested_path: str,
    *,
    max_bytes: int,
) -> tuple[BinaryIO, int, str]:
    _candidate, relative = resolve_under_vault(vault_root, requested_path)
    parts = tuple(relative.split("/"))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    if not parts or not nofollow or not directory or os.open not in os.supports_dir_fd:
        raise RuntimeError("safe open unavailable")
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.open(vault_root, os.O_RDONLY | directory | nofollow | close_on_exec)
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | close_on_exec,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1], os.O_RDONLY | nofollow | close_on_exec, dir_fd=directory_fd
        )
        opened_stat = os.fstat(file_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise VaultPathError(code="NOT_A_FILE", reason="path is not a regular file")
        if opened_stat.st_size > max_bytes:
            raise DownloadTooLarge
        opened = os.fdopen(file_fd, "rb")
        file_fd = None
        return opened, opened_stat.st_size, parts[-1]
    except FileNotFoundError as exc:
        raise VaultPathError(code="NOT_FOUND", reason="path does not exist") from exc
    except NotADirectoryError as exc:
        raise VaultPathError(code="NOT_A_FILE", reason="path is not a file") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EXDEV, errno.EISDIR, errno.ENOTDIR}:
            raise VaultPathError(code="INVALID_PATH", reason="path is invalid") from exc
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


async def _stream_bounded_file(
    stream: BinaryIO,
    size: int,
    admission: AbstractContextManager[None],
    *,
    run_in_threadpool_func: Callable[..., Any],
) -> AsyncIterator[bytes]:
    remaining = size
    try:
        while remaining:
            chunk = await run_in_threadpool_func(
                stream.read, min(_DOWNLOAD_CHUNK_BYTES, remaining)
            )
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()
        admission.__exit__(None, None, None)


def _download_disposition(filename: str) -> str:
    encoded_parts: list[str] = []
    for value in unicodedata.normalize("NFC", filename).encode("utf-8"):
        encoded_parts.append(chr(value) if value in _RFC8187_ATTR_CHAR else f"%{value:02X}")
    header = (
        'attachment; filename="exomem-download"; filename*=UTF-8\'\''
        + "".join(encoded_parts)
    )
    if len(header.encode("ascii")) > 2048:
        raise PublicRequestError("TRANSFER_INTERNAL")
    return header


def _release(admission: AbstractContextManager[None] | None) -> None:
    if admission is not None:
        admission.__exit__(None, None, None)


def _stage_adoption_upload(
    *,
    vault_root: Path,
    temp_root: Path,
    metadata: dict[str, Any],
    stream: BinaryIO,
    max_bytes: int,
) -> None:
    """Land a staged upload as RAW bytes under `_Staging/adoption/<run_id>/`.

    Rides the already-verified transfer grant (auth, tenant confinement, byte
    cap, and admission are enforced by the caller). Never writes under `Knowledge
    Base/`: every target is resolved with `resolve_under_vault` against the
    injected vault root and re-checked to stay under the run directory. A ZIP
    (declared via its media type, not sniffing) is expanded cell-side with
    zip-slip, entry-count, and size guards; a rejected archive leaves nothing
    behind because extraction is fully validated in the hosted temp root before
    anything is moved into place.
    """

    run_id = metadata["category"]
    if not isinstance(run_id, str) or not _ADOPTION_STAGING_RUN_ID.fullmatch(run_id):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    relative_prefix = _staging_relative_prefix(metadata.get("description"))
    run_relative = f"{_ADOPTION_STAGING_ROOT}/{run_id}"
    if metadata["content_type"] in _ADOPTION_STAGING_ZIP_CONTENT_TYPES:
        _stage_adoption_zip(
            vault_root=vault_root,
            temp_root=temp_root,
            run_relative=run_relative,
            relative_prefix=relative_prefix,
            stream=stream,
            max_bytes=max_bytes,
        )
    else:
        _stage_adoption_file(
            vault_root=vault_root,
            run_relative=run_relative,
            relative_prefix=relative_prefix,
            filename=metadata["filename"],
            stream=stream,
            max_bytes=max_bytes,
        )


def _stage_adoption_file(
    *,
    vault_root: Path,
    run_relative: str,
    relative_prefix: str,
    filename: str,
    stream: BinaryIO,
    max_bytes: int,
) -> None:
    member = f"{relative_prefix}/{filename}" if relative_prefix else filename
    destination, _relative = _resolve_staging_target(vault_root, run_relative, member)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.parent / f".staging-{uuid.uuid4()}.part"
    try:
        _copy_stream_to_path(stream, partial, limit=max_bytes)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _stage_adoption_zip(
    *,
    vault_root: Path,
    temp_root: Path,
    run_relative: str,
    relative_prefix: str,
    stream: BinaryIO,
    max_bytes: int,
) -> None:
    extract_root = temp_root / f"staging-{uuid.uuid4()}"
    extract_root.mkdir(mode=0o700)
    try:
        try:
            stream.seek(0)
            archive = zipfile.ZipFile(stream)
        except (OSError, zipfile.BadZipFile) as exc:
            raise PublicRequestError("TRANSFER_REQUEST_INVALID") from exc
        staged: list[str] = []
        seen: set[str] = set()
        total_bytes = 0
        with archive as zip_file:
            for info in zip_file.infolist():
                if info.is_dir():
                    continue
                if _zip_entry_is_symlink(info):
                    raise PublicRequestError("TRANSFER_REQUEST_INVALID")
                member = _validated_zip_member(info.filename, relative_prefix)
                if member in seen:
                    raise PublicRequestError("TRANSFER_REQUEST_INVALID")
                seen.add(member)
                if len(seen) > _ADOPTION_STAGING_MAX_ENTRIES:
                    raise PublicRequestError("TRANSFER_TOO_LARGE")
                if info.file_size > max_bytes:
                    raise PublicRequestError("TRANSFER_TOO_LARGE")
                target = _safe_join(extract_root, member)
                target.parent.mkdir(parents=True, exist_ok=True)
                total_bytes += _extract_zip_member(
                    zip_file, info, target, per_entry_limit=max_bytes
                )
                if total_bytes > _ADOPTION_STAGING_MAX_TOTAL_BYTES:
                    raise PublicRequestError("TRANSFER_TOO_LARGE")
                staged.append(member)
        # The whole archive validated in the hosted temp root: only now is the
        # vault touched, so a rejected archive never leaves a partial extraction.
        for member in staged:
            destination, _relative = _resolve_staging_target(
                vault_root, run_relative, member
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extract_root / member), str(destination))
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)


def _staging_relative_prefix(description: Any) -> str:
    """Validate the optional relative subdirectory carried in `description`."""

    if description is None:
        return ""
    if not isinstance(description, str):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    parts = [part for part in description.replace("\\", "/").split("/") if part]
    if any(not _valid_staging_component(part) for part in parts):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    return "/".join(parts)


def _validated_zip_member(name: Any, relative_prefix: str) -> str:
    if not isinstance(name, str) or not name:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(not _valid_staging_component(part) for part in parts):
        raise PublicRequestError("TRANSFER_REQUEST_INVALID")
    if relative_prefix:
        parts = relative_prefix.split("/") + parts
    return "/".join(parts)


def _valid_staging_component(part: str) -> bool:
    return (
        part not in {".", ".."}
        and "\\" not in part
        and unicodedata.normalize("NFC", part) == part
        and not any(unicodedata.category(character) == "Cc" for character in part)
    )


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _safe_join(base: Path, member: str) -> Path:
    target = base / member
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID") from exc
    return target


def _resolve_staging_target(
    vault_root: Path, run_relative: str, member: str
) -> tuple[Path, str]:
    try:
        run_dir, _run_relative = resolve_under_vault(vault_root, run_relative)
        target, target_relative = resolve_under_vault(
            vault_root, f"{run_relative}/{member}"
        )
    except VaultPathError as exc:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID") from exc
    try:
        target.resolve().relative_to(run_dir.resolve())
    except ValueError as exc:
        raise PublicRequestError("TRANSFER_REQUEST_INVALID") from exc
    return target, target_relative


def _copy_stream_to_path(stream: BinaryIO, dest: Path, *, limit: int) -> int:
    try:
        stream.seek(0)
    except (OSError, ValueError):
        pass
    written = 0
    descriptor = os.open(
        dest,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as out:
        while True:
            chunk = stream.read(_ADOPTION_STAGING_COPY_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise PublicRequestError("TRANSFER_TOO_LARGE")
            out.write(chunk)
    return written


def _extract_zip_member(
    zip_file: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    dest: Path,
    *,
    per_entry_limit: int,
) -> int:
    written = 0
    descriptor = os.open(
        dest,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    with zip_file.open(info) as source, os.fdopen(descriptor, "wb") as out:
        while True:
            chunk = source.read(_ADOPTION_STAGING_COPY_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > per_entry_limit:
                raise PublicRequestError("TRANSFER_TOO_LARGE")
            out.write(chunk)
    return written


__all__ = ["register_public_transfer_routes"]
