"""Bounded staging and persistence for client-provided remote file handles."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import mimetypes
import os
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urljoin, urlsplit

from .preserve import PreserveError, _sanitize_segment, preserve_stream
from .writer_lease import active_manager, active_mutation_request_id, mark_active_mutation_committed

MAX_FILES = 8
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = MAX_FILE_BYTES
MAX_REDIRECTS = 3
_CHUNK_SIZE = 1024 * 1024
_TIMEOUT_SECONDS = 20.0
_RETRIEVAL_DEADLINE_SECONDS = 20.0
_BATCH_DEADLINE_SECONDS = 60.0
_MAX_FILE_ID_CHARS = 256
_MAX_CONTENT_TYPE_CHARS = 255
_monotonic = time.monotonic
_DNS_WORKERS = threading.BoundedSemaphore(4)
_RETRIEVAL_WORKERS = threading.BoundedSemaphore(4)


class SafeFetchError(Exception):
    """A stable, content-free artifact retrieval failure."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class FetchBudget:
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    total_bytes: int = 0

    def validate_content_length(self, length: int) -> None:
        if length < 0 or length > self.max_file_bytes or self.total_bytes + length > self.max_total_bytes:
            raise SafeFetchError("TOO_LARGE", "download exceeds the size limit")

    def consume(self, size: int) -> None:
        self.validate_content_length(size)
        self.total_bytes += size


@dataclass(frozen=True)
class StagedArtifact:
    file_id: str
    path: Path
    size: int
    sha256: str
    content_type: str | None
    filename: str


def remaining_retrieval_timeout(
    deadline: float, *, clock: Callable[[], float] | None = None
) -> float:
    """Return the remaining absolute retrieval budget or fail content-free."""
    remaining = deadline - (clock or _monotonic)()
    if remaining <= 0:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download retrieval timed out")
    return min(_TIMEOUT_SECONDS, remaining)


def _bounded_retrieval_call(operation: Callable[[], object], *, deadline: float) -> object:
    """Run a blocking HTTP operation within the remaining wall-clock budget."""
    timeout = remaining_retrieval_timeout(deadline)
    if not _RETRIEVAL_WORKERS.acquire(timeout=timeout):
        raise SafeFetchError("SAFE_FETCH_FAILED", "download retrieval timed out")
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def _run() -> None:
        try:
            result.put((True, operation()))
        except Exception as error:  # noqa: BLE001 - preserved for the caller
            result.put((False, error))
        finally:
            _RETRIEVAL_WORKERS.release()

    threading.Thread(target=_run, daemon=True).start()
    try:
        succeeded, value = result.get(timeout=remaining_retrieval_timeout(deadline))
    except Empty as error:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download retrieval timed out") from error
    if not succeeded:
        assert isinstance(value, Exception)
        raise value
    return value


def validate_download_url(value: object):
    """Parse an HTTPS download URL without ever reflecting it into errors."""
    try:
        parsed = urlsplit(str(value))
        _ = parsed.port
    except (TypeError, ValueError) as error:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download URL is not allowed") from error
    if parsed.scheme.lower() != "https":
        raise SafeFetchError("SAFE_FETCH_FAILED", "download URL must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download URL is not allowed")
    try:
        parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download URL is not allowed") from error
    return parsed


def validate_redirect_url(current_url: str, location: str):
    """Resolve and validate one redirect target under the same hostile-input rules."""
    return validate_download_url(urljoin(current_url, location))


def _bounded_resolve(
    resolver: Callable[[], list[str]], *, deadline: float
) -> list[str]:
    """Bound one blocking resolver call without retaining unbounded workers."""
    timeout = remaining_retrieval_timeout(deadline)
    if not _DNS_WORKERS.acquire(timeout=timeout):
        raise SafeFetchError("SAFE_FETCH_FAILED", "download destination could not be resolved")
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def _resolve() -> None:
        try:
            result.put((True, resolver()))
        except Exception as error:  # noqa: BLE001 - normalized below
            result.put((False, error))
        finally:
            _DNS_WORKERS.release()

    threading.Thread(target=_resolve, daemon=True).start()
    try:
        succeeded, value = result.get(timeout=remaining_retrieval_timeout(deadline))
    except Empty as error:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download destination could not be resolved") from error
    if not succeeded:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download destination could not be resolved") from value
    return value  # type: ignore[return-value]


def resolve_public_addresses(
    host: str,
    port: int,
    *,
    resolver: Callable[..., list[str]] | None = None,
    deadline: float | None = None,
) -> tuple[str, ...]:
    """Return DNS answers only when every answer is globally routable."""
    def resolve() -> list[str]:
        if resolver is None:
            return [entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        return resolver(host, port)

    try:
        answers = _bounded_resolve(resolve, deadline=deadline) if deadline is not None else resolve()
    except OSError as error:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download destination could not be resolved") from error
    public: list[str] = []
    try:
        for answer in answers:
            address = ipaddress.ip_address(answer)
            if not address.is_global:
                raise SafeFetchError("SAFE_FETCH_FAILED", "download destination is not public")
            rendered = str(address)
            if rendered not in public:
                public.append(rendered)
    except (TypeError, ValueError) as error:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download destination is not public") from error
    if not public:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download destination is not public")
    return tuple(public)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated address while retaining the URL hostname for TLS."""

    def __init__(
        self, address: str, port: int, *, server_hostname: str, timeout: float
    ) -> None:
        super().__init__(address, port=port, timeout=timeout, context=ssl.create_default_context())
        self._server_hostname = server_hostname

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


def fallback_filename(sha256: str, content_type: str | None) -> str:
    """Generate a deterministic, sanitized filename for unnamed attachments."""
    extension = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip()) or ".bin"
    return f"attachment-{sha256[:16]}.{extension.lstrip('.')}"


def _file_id(file: Mapping[str, object]) -> str:
    value = file.get("file_id")
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_FILE_ID_CHARS:
        raise SafeFetchError("INVALID_FILE", "file_id is required")
    return value.strip()


def _content_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SafeFetchError("INVALID_FILE", "content type is invalid")
    content_type = value.split(";", 1)[0].strip() or None
    if content_type is not None and len(content_type) > _MAX_CONTENT_TYPE_CHARS:
        raise SafeFetchError("INVALID_FILE", "content type is invalid")
    return content_type


def _validate_destination(scope: str, category: str) -> None:
    if not _sanitize_segment(scope):
        raise SafeFetchError("INVALID_PRESERVE", "scope is empty or invalid")
    if not _sanitize_segment(category):
        raise SafeFetchError("INVALID_PRESERVE", "category is empty or invalid")


def stage_artifact(
    file: Mapping[str, object], budget: FetchBudget, *, batch_deadline: float | None = None
) -> StagedArtifact:
    """Download one handle to a private temporary file before vault mutation."""
    file_id = _file_id(file)
    current_url = str(file.get("download_url") or "")
    redirects = 0
    destination: Path | None = None
    deadline = min(_monotonic() + _RETRIEVAL_DEADLINE_SECONDS, batch_deadline or float("inf"))
    try:
        while True:
            remaining_retrieval_timeout(deadline)
            parsed = validate_download_url(current_url)
            host = (parsed.hostname or "").encode("idna").decode("ascii")
            port = parsed.port or 443
            addresses = resolve_public_addresses(host, port, deadline=deadline)
            remaining_retrieval_timeout(deadline)
            target = parsed.path or "/"
            if parsed.query:
                target += f"?{parsed.query}"
            response = None
            selected_connection = None
            last_error: Exception | None = None
            for address in addresses:
                connection = _PinnedHTTPSConnection(
                    address,
                    port,
                    server_hostname=host,
                    timeout=remaining_retrieval_timeout(deadline),
                )
                try:
                    connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
                    host_header = host if port == 443 else f"{host}:{port}"
                    connection.putheader("Host", host_header)
                    connection.putheader("Accept-Encoding", "identity")
                    connection.endheaders()
                    response = _bounded_retrieval_call(connection.getresponse, deadline=deadline)
                    selected_connection = connection
                    remaining_retrieval_timeout(deadline)
                    break
                except SafeFetchError:
                    if response is not None:
                        response.close()
                    connection.close()
                    raise
                except (OSError, ssl.SSLError, http.client.HTTPException) as error:
                    last_error = error
                    connection.close()
            if response is None:
                raise SafeFetchError("SAFE_FETCH_FAILED", "download could not be retrieved") from last_error
            status = response.status
            if status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.close()
                selected_connection.close()
                if not location or redirects >= MAX_REDIRECTS:
                    raise SafeFetchError("SAFE_FETCH_FAILED", "download redirected too many times")
                parsed_next = validate_redirect_url(current_url, location)
                current_url = parsed_next.geturl()
                redirects += 1
                continue
            if not 200 <= status < 300:
                response.close()
                selected_connection.close()
                raise SafeFetchError("SAFE_FETCH_FAILED", "download response was not successful")
            content_length = response.getheader("Content-Length")
            try:
                response_content_type = _content_type(response.getheader("Content-Type"))
            except SafeFetchError:
                response.close()
                selected_connection.close()
                raise
            declared_size: int | None = None
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                    budget.validate_content_length(declared_size)
                except ValueError as error:
                    response.close()
                    selected_connection.close()
                    raise SafeFetchError("SAFE_FETCH_FAILED", "download response is invalid") from error
            fd, raw_path = tempfile.mkstemp(prefix="exomem-artifact-")
            destination = Path(raw_path)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            digest = hashlib.sha256()
            written = 0
            try:
                with os.fdopen(fd, "wb") as output:
                    while True:
                        if selected_connection.sock is not None:
                            selected_connection.sock.settimeout(remaining_retrieval_timeout(deadline))
                        block = _bounded_retrieval_call(
                            partial(response.read, _CHUNK_SIZE), deadline=deadline
                        )
                        remaining_retrieval_timeout(deadline)
                        if not block:
                            break
                        written += len(block)
                        budget.validate_content_length(written)
                        digest.update(block)
                        output.write(block)
            finally:
                response.close()
                selected_connection.close()
            if declared_size is not None and written != declared_size:
                raise SafeFetchError(
                    "SAFE_FETCH_FAILED", "download Content-Length did not match streamed bytes"
                )
            budget.consume(written)
            content_type = response_content_type or _content_type(file.get("mime_type"))
            filename = str(file.get("file_name") or "").strip() or fallback_filename(
                digest.hexdigest(), content_type
            )
            return StagedArtifact(file_id, destination, written, digest.hexdigest(), content_type, filename)
    except SafeFetchError:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    except (OSError, http.client.HTTPException) as error:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise SafeFetchError("SAFE_FETCH_FAILED", "download could not be retrieved") from error


def _failed(file_id: str, error: SafeFetchError | PreserveError) -> dict[str, str]:
    return {"file_id": file_id, "outcome": "failed", "code": error.code, "reason": error.reason}


def preserve_artifacts(
    vault_root: Path,
    *,
    scope: str,
    category: str,
    files: list[Mapping[str, object]],
) -> dict:
    """Stage remote files first, then preserve each append-only artifact under a narrow guard."""
    if not isinstance(files, list) or not files:
        return {"files": [], "summary": {"stored": 0, "failed": 0}}
    try:
        _validate_destination(scope, category)
    except SafeFetchError as error:
        return {
            "files": [
                _failed(str(file.get("file_id") or "") if isinstance(file, Mapping) else "", error)
                for file in files
            ],
            "summary": {"stored": 0, "failed": len(files)},
        }
    if len(files) > MAX_FILES:
        error = SafeFetchError("TOO_MANY_FILES", "too many files in one request")
        return {
            "files": [
                _failed(str(file.get("file_id") or "") if isinstance(file, Mapping) else "", error)
                for file in files
            ],
            "summary": {"stored": 0, "failed": len(files)},
        }
    budget = FetchBudget()
    batch_deadline = _monotonic() + _BATCH_DEADLINE_SECONDS
    staged: dict[int, StagedArtifact] = {}
    outcomes: list[dict | None] = [None] * len(files)
    for index, file in enumerate(files):
        if not isinstance(file, Mapping):
            outcomes[index] = _failed("", SafeFetchError("INVALID_FILE", "file handle is invalid"))
            continue
        try:
            _file_id(file)
            if not isinstance(file.get("download_url"), str) or not file["download_url"].strip():
                raise SafeFetchError("INVALID_FILE", "download_url is required")
            _content_type(file.get("mime_type"))
            staged[index] = stage_artifact(file, budget, batch_deadline=batch_deadline)
        except SafeFetchError as error:
            file_id = str(file.get("file_id") or "") if isinstance(file, Mapping) else ""
            outcomes[index] = _failed(file_id, error)

    manager = active_manager()
    try:
        for index, artifact in staged.items():
            try:
                if (
                    not artifact.file_id
                    or len(artifact.file_id) > _MAX_FILE_ID_CHARS
                    or artifact.content_type is not None
                    and len(artifact.content_type) > _MAX_CONTENT_TYPE_CHARS
                ):
                    raise SafeFetchError("INVALID_FILE", "staged file metadata is invalid")
                with manager.mutation_guard(
                    vault_root,
                    request_id=active_mutation_request_id(),
                    operation="preserve_artifacts_commit",
                    holder_kind="command",
                ):
                    with artifact.path.open("rb") as stream:
                        result = preserve_stream(
                            vault_root,
                            scope=scope,
                            category=category,
                            filename=artifact.filename,
                            stream=stream,
                            content_type=artifact.content_type,
                            max_bytes=MAX_FILE_BYTES,
                        )
                mark_active_mutation_committed()
                payload = result.as_dict()
                warnings = list(payload.get("warnings") or [])
                try:
                    from . import media_processing

                    if media_processing.classify_media(vault_root / payload["path"]) is not None:
                        with manager.mutation_guard(
                            vault_root,
                            request_id=active_mutation_request_id(),
                            operation="preserve_artifacts_media",
                            holder_kind="command",
                        ):
                            media_processing.reconcile_media(vault_root, vault_root / payload["path"], explicit=False)
                except Exception:  # noqa: BLE001 - the original bytes are durable and recoverable
                    warnings.append("media reconciliation failed; evidence remains recoverable")
                outcomes[index] = {
                    "file_id": artifact.file_id,
                    "outcome": "stored",
                    "stored_path": payload.get("stored_path") or payload.get("path"),
                    "size": payload.get("size"),
                    "hash": payload.get("hash"),
                    "hash_algorithm": payload.get("hash_algorithm"),
                    "media_id": payload.get("media_id"),
                    "content_type": payload.get("content_type"),
                    "warnings": warnings,
                }
            except (PreserveError, SafeFetchError) as error:
                outcomes[index] = _failed(artifact.file_id, error)
    finally:
        for artifact in staged.values():
            artifact.path.unlink(missing_ok=True)
    final = [outcome for outcome in outcomes if outcome is not None]
    return {
        "files": final,
        "summary": {
            "stored": sum(item["outcome"] == "stored" for item in final),
            "failed": sum(item["outcome"] == "failed" for item in final),
        },
    }
