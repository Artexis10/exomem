"""Bounded staging and persistence for client-provided remote file handles."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import mimetypes
import os
import re
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
from urllib.parse import urljoin, urlsplit

from .preserve import (
    _ADOPTION_RECEIPT_FIELD,
    _ADOPTION_RECEIPT_FIELDS,
    _ADOPTION_RECEIPT_VERSION,
    PreserveError,
    _sanitize_segment,
    preserve_stream,
)
from .vault import FrontmatterError, kb_root, parse_frontmatter, walk_vault_md
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
_MAX_ADOPTION_KEY_CHARS = 512
_MAX_ADOPTION_TRIGGER_CHARS = 64
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


@dataclass(frozen=True)
class AdoptionEnvelope:
    """Validated semantic selection supplied by the active agent."""

    key_digest: str
    trigger: str
    selected_file_id: str

    def seed(self, *, lane: str, destination: str) -> dict[str, object]:
        return {
            "key_digest": self.key_digest,
            "trigger": self.trigger,
            "selected_file_id": self.selected_file_id,
            "lane": lane,
            "destination": destination,
        }


def remaining_retrieval_timeout(
    deadline: float, *, clock: Callable[[], float] | None = None
) -> float:
    """Return the remaining absolute retrieval budget or fail content-free."""
    remaining = deadline - (clock or _monotonic)()
    if remaining <= 0:
        raise SafeFetchError("SAFE_FETCH_FAILED", "download retrieval timed out")
    return min(_TIMEOUT_SECONDS, remaining)


def _cancel_connection(connection: http.client.HTTPConnection) -> None:
    """Interrupt a timed-out HTTP response before its buffered reader is closed."""
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try:
            sock.close()
        except (AttributeError, OSError):
            pass
    try:
        connection.close()
    except (OSError, http.client.HTTPException):
        pass


def _bounded_retrieval_call(
    operation: Callable[[], object], *, deadline: float, cancel: Callable[[], None] | None = None
) -> object:
    """Run a blocking HTTP operation within the remaining wall-clock budget."""
    def timed_out() -> SafeFetchError:
        if cancel is not None:
            try:
                cancel()
            except (AttributeError, OSError, http.client.HTTPException):
                pass
        return SafeFetchError("SAFE_FETCH_FAILED", "download retrieval timed out")

    try:
        timeout = remaining_retrieval_timeout(deadline)
    except SafeFetchError as error:
        raise timed_out() from error
    if not _RETRIEVAL_WORKERS.acquire(timeout=timeout):
        raise timed_out()
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
        timeout = remaining_retrieval_timeout(deadline)
        succeeded, value = result.get(timeout=timeout)
    except SafeFetchError as error:
        raise timed_out() from error
    except Empty as error:
        raise timed_out() from error
    if not succeeded:
        assert isinstance(value, Exception)
        if isinstance(value, TimeoutError):
            raise timed_out() from value
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
                    response = _bounded_retrieval_call(
                        connection.getresponse,
                        deadline=deadline,
                        cancel=partial(_cancel_connection, connection),
                    )
                    selected_connection = connection
                    remaining_retrieval_timeout(deadline)
                    break
                except SafeFetchError:
                    _cancel_connection(connection)
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
                try:
                    with os.fdopen(fd, "wb") as output:
                        while True:
                            if selected_connection.sock is not None:
                                selected_connection.sock.settimeout(remaining_retrieval_timeout(deadline))
                            block = _bounded_retrieval_call(
                                partial(response.read, _CHUNK_SIZE),
                                deadline=deadline,
                                cancel=partial(_cancel_connection, selected_connection),
                            )
                            remaining_retrieval_timeout(deadline)
                            if not block:
                                break
                            written += len(block)
                            budget.validate_content_length(written)
                            digest.update(block)
                            output.write(block)
                except SafeFetchError:
                    _cancel_connection(selected_connection)
                    raise
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
    reason = "artifact already exists" if error.code == "ARTIFACT_EXISTS" else error.reason[:300]
    return {"file_id": file_id, "outcome": "failed", "code": error.code, "reason": reason}


def _bounded_file_id(file: object) -> str:
    if not isinstance(file, Mapping):
        return ""
    value = file.get("file_id")
    return value[:_MAX_FILE_ID_CHARS] if isinstance(value, str) else ""


def _validate_adoption(value: object) -> AdoptionEnvelope:
    if not isinstance(value, Mapping) or set(value) != {"key", "trigger", "selected_file_id"}:
        raise SafeFetchError(
            "INVALID_ADOPTION",
            "adoption must contain exactly key, trigger, and selected_file_id",
        )
    key = value.get("key")
    trigger = value.get("trigger")
    selected_file_id = value.get("selected_file_id")
    if (
        not isinstance(key, str)
        or not key.strip()
        or len(key.strip()) > _MAX_ADOPTION_KEY_CHARS
        or any(ord(char) < 32 for char in key)
    ):
        raise SafeFetchError("INVALID_ADOPTION", "adoption key is invalid")
    if (
        not isinstance(trigger, str)
        or not trigger.strip()
        or len(trigger.strip()) > _MAX_ADOPTION_TRIGGER_CHARS
        or any(ord(char) < 32 for char in trigger)
    ):
        raise SafeFetchError("INVALID_ADOPTION", "adoption trigger is invalid")
    try:
        selected = _file_id({"file_id": selected_file_id})
    except SafeFetchError as error:
        raise SafeFetchError("INVALID_ADOPTION", "selected_file_id is invalid") from error
    return AdoptionEnvelope(
        key_digest=hashlib.sha256(key.strip().encode("utf-8")).hexdigest(),
        trigger=trigger.strip(),
        selected_file_id=selected,
    )


def _selected_index(files: list[Mapping[str, object]], envelope: AdoptionEnvelope) -> int:
    matches: list[int] = []
    for index, file in enumerate(files):
        if not isinstance(file, Mapping):
            continue
        try:
            file_id = _file_id(file)
        except SafeFetchError:
            continue
        if file_id == envelope.selected_file_id:
            matches.append(index)
    if len(matches) != 1:
        raise SafeFetchError(
            "INVALID_ADOPTION",
            "selected_file_id must match exactly one supplied handle",
        )
    return matches[0]


def _adoption_error_result(
    files: list[Mapping[str, object]], error: SafeFetchError | PreserveError
) -> dict:
    outcomes = [_failed(_bounded_file_id(file), error) for file in files]
    return {"files": outcomes, "summary": {"stored": 0, "failed": len(outcomes)}}


def _adoption_outcomes(
    files: list[Mapping[str, object]], selected_index: int
) -> list[dict[str, object] | None]:
    return [
        None
        if index == selected_index
        else {"file_id": _bounded_file_id(file), "outcome": "unselected"}
        for index, file in enumerate(files)
    ]


def _finish_adoption(outcomes: list[dict[str, object] | None]) -> dict:
    final = [outcome for outcome in outcomes if outcome is not None]
    return {
        "files": final,
        "summary": {
            "stored": sum(item.get("outcome") == "stored" for item in final),
            "replayed": sum(item.get("outcome") == "replayed" for item in final),
            "failed": sum(item.get("outcome") == "failed" for item in final),
            "unselected": sum(item.get("outcome") == "unselected" for item in final),
        },
    }


def _destination(vault_root: Path, *parts: str) -> str:
    return kb_root(vault_root).joinpath(*parts).relative_to(vault_root).as_posix()


def _source_destination(
    vault_root: Path, source_fields: Mapping[str, object]
) -> str:
    from . import source_taxonomy

    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    try:
        raw_kind = source_fields.get("source_type") or source_taxonomy.FALLBACK_KIND
        raw_domain = source_fields.get("domain")
        if not isinstance(raw_kind, str) or (
            raw_domain is not None and not isinstance(raw_domain, str)
        ):
            raise source_taxonomy.TaxonomyError("source classification is invalid")
        kind = taxonomy.resolve_kind(raw_kind)
        domain = taxonomy.resolve_domain(raw_domain) if raw_domain is not None else None
    except source_taxonomy.TaxonomyError as error:
        raise SafeFetchError("INVALID_SOURCE", str(error)) from error
    return _destination(vault_root, *source_taxonomy.source_segments(kind, domain))


def _receipt_error(code: str, reason: str) -> SafeFetchError:
    return SafeFetchError(code, reason)


def _validate_receipt(
    value: object,
    *,
    page_path: Path,
    vault_root: Path,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or not set(_ADOPTION_RECEIPT_FIELDS).issubset(value):
        raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt is incomplete")
    receipt = {field: value[field] for field in _ADOPTION_RECEIPT_FIELDS}
    if (
        type(receipt["version"]) is not int
        or receipt["version"] != _ADOPTION_RECEIPT_VERSION
        or receipt["committed"] is not True
    ):
        raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt is invalid")
    for field in (
        "key_digest",
        "trigger",
        "selected_file_id",
        "lane",
        "destination",
        "stored_path",
        "page_path",
        "hash_algorithm",
        "hash",
        "media_id",
    ):
        if not isinstance(receipt[field], str) or not str(receipt[field]).strip():
            raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt is invalid")
    if receipt["content_type"] is not None and not isinstance(receipt["content_type"], str):
        raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt is invalid")
    if (
        type(receipt["size"]) is not int
        or receipt["size"] < 0
        or re.fullmatch(r"[0-9a-f]{64}", str(receipt["key_digest"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(receipt["hash"])) is None
        or receipt["hash_algorithm"] != "sha256"
        or receipt["media_id"] != f"sha256:{receipt['hash']}"
    ):
        raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt is invalid")
    actual_page = page_path.relative_to(vault_root).as_posix()
    if receipt["page_path"] != actual_page:
        raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt page is inconsistent")
    for field in ("stored_path", "page_path", "destination"):
        logical = PurePosixPath(str(receipt[field]))
        if logical.is_absolute() or ".." in logical.parts:
            raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt path is invalid")
    stored = PurePosixPath(str(receipt["stored_path"]))
    page = PurePosixPath(str(receipt["page_path"]))
    destination = PurePosixPath(str(receipt["destination"]))
    if (
        stored.parts[: len(destination.parts)] != destination.parts
        or page.parts[: len(destination.parts)] != destination.parts
    ):
        raise _receipt_error("ADOPTION_KEY_REUSED", "stored adoption receipt destination is inconsistent")
    return receipt


def _find_adoption_receipt(
    vault_root: Path, key_digest: str
) -> dict[str, object] | None:
    """Locate portable receipt truth from canonical vault-owned pages."""
    matches: list[dict[str, object]] = []
    needle = f"key_digest: {key_digest}"
    for page in walk_vault_md(vault_root):
        try:
            source = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f"{_ADOPTION_RECEIPT_FIELD}:" not in source or needle not in source:
            continue
        try:
            frontmatter, _body, marker = parse_frontmatter(source, strict=True)
        except FrontmatterError as error:
            raise _receipt_error(
                "ADOPTION_KEY_REUSED", "stored adoption receipt cannot be parsed"
            ) from error
        if marker is None:
            continue
        value = frontmatter.get(_ADOPTION_RECEIPT_FIELD)
        if not isinstance(value, Mapping) or value.get("key_digest") != key_digest:
            continue
        matches.append(_validate_receipt(value, page_path=page, vault_root=vault_root))
    if len(matches) > 1:
        raise _receipt_error("ADOPTION_KEY_REUSED", "adoption key has multiple stored receipts")
    return matches[0] if matches else None


def _existing_receipt(
    vault_root: Path,
    envelope: AdoptionEnvelope,
    *,
    lane: str,
    destination: str,
) -> dict[str, object] | None:
    receipt = _find_adoption_receipt(vault_root, envelope.key_digest)
    if receipt is None:
        return None
    expected = {
        "trigger": envelope.trigger,
        "selected_file_id": envelope.selected_file_id,
        "lane": lane,
        "destination": destination,
    }
    if any(receipt[field] != value for field, value in expected.items()):
        raise _receipt_error("ADOPTION_KEY_REUSED", "adoption key was reused for another identity")
    return receipt


def _effective_content_type(artifact: StagedArtifact, *, lane: str) -> str | None:
    if artifact.content_type is not None or lane == "source":
        return artifact.content_type
    return mimetypes.guess_type(artifact.filename)[0]


def _validate_staged_adoption(
    artifact: StagedArtifact, envelope: AdoptionEnvelope
) -> None:
    if (
        artifact.file_id != envelope.selected_file_id
        or not artifact.filename.strip()
        or type(artifact.size) is not int
        or artifact.size < 0
        or re.fullmatch(r"[0-9a-f]{64}", artifact.sha256) is None
        or artifact.content_type is not None
        and (not artifact.content_type or len(artifact.content_type) > _MAX_CONTENT_TYPE_CHARS)
    ):
        raise SafeFetchError("INVALID_FILE", "staged file metadata is invalid")


def _assert_replay_identity(
    receipt: Mapping[str, object], artifact: StagedArtifact, *, lane: str
) -> None:
    identity = {
        "hash_algorithm": "sha256",
        "hash": artifact.sha256,
        "size": artifact.size,
        "content_type": _effective_content_type(artifact, lane=lane),
        "media_id": f"sha256:{artifact.sha256}",
    }
    if any(receipt[field] != value for field, value in identity.items()):
        raise _receipt_error("ADOPTION_KEY_REUSED", "adoption key was reused for different bytes")


def _replayed_outcome(receipt: Mapping[str, object]) -> dict[str, object]:
    return {
        "file_id": receipt["selected_file_id"],
        "outcome": "replayed",
        "stored_path": receipt["stored_path"],
        "path": receipt["stored_path"],
        "page": receipt["page_path"],
        "size": receipt["size"],
        "hash": receipt["hash"],
        "hash_algorithm": receipt["hash_algorithm"],
        "media_id": receipt["media_id"],
        "content_type": receipt["content_type"],
        "warnings": [],
        "adoption": dict(receipt),
    }


def _stored_outcome(
    artifact: StagedArtifact,
    payload: Mapping[str, object],
    *,
    source: bool,
) -> dict[str, object]:
    receipt = payload.get("adoption")
    if not isinstance(receipt, Mapping):
        raise RuntimeError("canonical artifact writer omitted its adoption receipt")
    outcome: dict[str, object] = {
        "file_id": artifact.file_id,
        "outcome": "stored",
        "stored_path": receipt["stored_path"],
        "size": receipt["size"],
        "hash": receipt["hash"],
        "hash_algorithm": receipt["hash_algorithm"],
        "media_id": receipt["media_id"],
        "content_type": receipt["content_type"],
        "warnings": list(payload.get("warnings") or []),
        "adoption": dict(receipt),
    }
    if source:
        outcome.update(
            {
                "path": receipt["stored_path"],
                "page": receipt["page_path"],
                "ref": payload.get("ref"),
            }
        )
    return outcome


def _adoption_inputs(
    files: list[Mapping[str, object]], adoption: object
) -> tuple[AdoptionEnvelope, int] | dict:
    if not isinstance(files, list) or not files:
        error = SafeFetchError("INVALID_ADOPTION", "adoption requires supplied file handles")
        return _adoption_error_result(files if isinstance(files, list) else [], error)
    if len(files) > MAX_FILES:
        error = SafeFetchError("TOO_MANY_FILES", "too many files in one request")
        return _adoption_error_result(files, error)
    try:
        envelope = _validate_adoption(adoption)
        selected_index = _selected_index(files, envelope)
    except SafeFetchError as error:
        return _adoption_error_result(files, error)
    return envelope, selected_index


def _capture_source_adoption(
    vault_root: Path,
    *,
    source_schema: object,
    title: str,
    files: list[Mapping[str, object]],
    adoption: object,
    source_fields: Mapping[str, object],
) -> dict:
    from . import add as add_module

    prepared = _adoption_inputs(files, adoption)
    if isinstance(prepared, dict):
        return prepared
    envelope, selected_index = prepared
    outcomes = _adoption_outcomes(files, selected_index)
    try:
        destination = _source_destination(vault_root, source_fields)
    except SafeFetchError as error:
        outcomes[selected_index] = _failed(envelope.selected_file_id, error)
        return _finish_adoption(outcomes)
    selected = files[selected_index]
    try:
        receipt = _existing_receipt(
            vault_root, envelope, lane="source", destination=destination
        )
    except SafeFetchError as error:
        outcomes[selected_index] = _failed(envelope.selected_file_id, error)
        return _finish_adoption(outcomes)

    artifact: StagedArtifact | None = None
    try:
        try:
            if not isinstance(selected.get("download_url"), str) or not selected[
                "download_url"
            ].strip():
                raise SafeFetchError("INVALID_FILE", "download_url is required")
            _content_type(selected.get("mime_type"))
            artifact = stage_artifact(
                selected,
                FetchBudget(),
                batch_deadline=_monotonic() + _BATCH_DEADLINE_SECONDS,
            )
            _validate_staged_adoption(artifact, envelope)
        except SafeFetchError as fetch_error:
            error = fetch_error
            if receipt is not None:
                error = SafeFetchError(
                    "ADOPTION_REPLAY_UNVERIFIABLE",
                    "previous adoption bytes could not be reverified",
                )
            outcomes[selected_index] = _failed(envelope.selected_file_id, error)
            return _finish_adoption(outcomes)

        if receipt is not None:
            try:
                _assert_replay_identity(receipt, artifact, lane="source")
            except SafeFetchError as error:
                outcomes[selected_index] = _failed(artifact.file_id, error)
                return _finish_adoption(outcomes)
            outcomes[selected_index] = _replayed_outcome(receipt)
            return _finish_adoption(outcomes)

        manager = active_manager()
        try:
            with manager.mutation_guard(
                vault_root,
                request_id=active_mutation_request_id(),
                operation="capture_source_artifacts_commit",
                holder_kind="command",
            ):
                raced = _existing_receipt(
                    vault_root, envelope, lane="source", destination=destination
                )
                if raced is not None:
                    _assert_replay_identity(raced, artifact, lane="source")
                    payload = None
                else:
                    result = add_module.add(
                        vault_root,
                        source_schema,
                        content="",
                        title=title,
                        artifact=add_module.SourceArtifact(
                            staged_path=artifact.path,
                            filename=artifact.filename,
                            content_type=artifact.content_type,
                        ),
                        adoption_seed=envelope.seed(
                            lane="source", destination=destination
                        ),
                        **source_fields,
                    )
                    payload = result.as_dict()
            if raced is not None:
                outcomes[selected_index] = _replayed_outcome(raced)
            else:
                mark_active_mutation_committed()
                outcomes[selected_index] = _stored_outcome(
                    artifact, payload, source=True
                )
        except (SafeFetchError, PreserveError) as error:
            outcomes[selected_index] = _failed(artifact.file_id, error)
        except add_module.AddError as error:
            outcomes[selected_index] = _failed(
                artifact.file_id, SafeFetchError(error.code, error.reason)
            )
        return _finish_adoption(outcomes)
    finally:
        if artifact is not None:
            try:
                artifact.path.unlink(missing_ok=True)
            except OSError:
                pass


def _preserve_evidence_adoption(
    vault_root: Path,
    *,
    scope: str,
    category: str,
    files: list[Mapping[str, object]],
    adoption: object,
) -> dict:
    try:
        _validate_destination(scope, category)
    except SafeFetchError as error:
        return _adoption_error_result(files, error)
    prepared = _adoption_inputs(files, adoption)
    if isinstance(prepared, dict):
        return prepared
    envelope, selected_index = prepared
    outcomes = _adoption_outcomes(files, selected_index)
    destination = _destination(
        vault_root, "Evidence", _sanitize_segment(scope), _sanitize_segment(category)
    )
    selected = files[selected_index]
    try:
        receipt = _existing_receipt(
            vault_root, envelope, lane="evidence", destination=destination
        )
    except SafeFetchError as error:
        outcomes[selected_index] = _failed(envelope.selected_file_id, error)
        return _finish_adoption(outcomes)

    artifact: StagedArtifact | None = None
    try:
        try:
            if not isinstance(selected.get("download_url"), str) or not selected[
                "download_url"
            ].strip():
                raise SafeFetchError("INVALID_FILE", "download_url is required")
            _content_type(selected.get("mime_type"))
            artifact = stage_artifact(
                selected,
                FetchBudget(),
                batch_deadline=_monotonic() + _BATCH_DEADLINE_SECONDS,
            )
            _validate_staged_adoption(artifact, envelope)
        except SafeFetchError as fetch_error:
            error = fetch_error
            if receipt is not None:
                error = SafeFetchError(
                    "ADOPTION_REPLAY_UNVERIFIABLE",
                    "previous adoption bytes could not be reverified",
                )
            outcomes[selected_index] = _failed(envelope.selected_file_id, error)
            return _finish_adoption(outcomes)

        if receipt is not None:
            try:
                _assert_replay_identity(receipt, artifact, lane="evidence")
            except SafeFetchError as error:
                outcomes[selected_index] = _failed(artifact.file_id, error)
                return _finish_adoption(outcomes)
            outcomes[selected_index] = _replayed_outcome(receipt)
            return _finish_adoption(outcomes)

        manager = active_manager()
        try:
            with manager.mutation_guard(
                vault_root,
                request_id=active_mutation_request_id(),
                operation="preserve_artifacts_commit",
                holder_kind="command",
            ):
                raced = _existing_receipt(
                    vault_root, envelope, lane="evidence", destination=destination
                )
                if raced is not None:
                    _assert_replay_identity(raced, artifact, lane="evidence")
                    payload = None
                else:
                    with artifact.path.open("rb") as stream:
                        result = preserve_stream(
                            vault_root,
                            scope=scope,
                            category=category,
                            filename=artifact.filename,
                            stream=stream,
                            content_type=artifact.content_type,
                            max_bytes=MAX_FILE_BYTES,
                            adoption_seed=envelope.seed(
                                lane="evidence", destination=destination
                            ),
                        )
                    payload = result.as_dict()
            if raced is not None:
                outcomes[selected_index] = _replayed_outcome(raced)
                return _finish_adoption(outcomes)

            mark_active_mutation_committed()
            warnings = list(payload.get("warnings") or [])
            receipt_payload = payload.get("adoption")
            if not isinstance(receipt_payload, Mapping):
                raise RuntimeError("canonical artifact writer omitted its adoption receipt")
            try:
                from . import media_processing

                if media_processing.classify_media(
                    vault_root / str(receipt_payload["stored_path"])
                ) is not None:
                    with manager.mutation_guard(
                        vault_root,
                        request_id=active_mutation_request_id(),
                        operation="preserve_artifacts_media",
                        holder_kind="command",
                    ):
                        media_processing.reconcile_media(
                            vault_root,
                            vault_root / str(receipt_payload["stored_path"]),
                            explicit=False,
                        )
            except Exception:  # noqa: BLE001 - committed custody remains authoritative
                warnings.append("media reconciliation failed; evidence remains recoverable")
            payload = dict(payload)
            payload["warnings"] = warnings
            outcomes[selected_index] = _stored_outcome(
                artifact, payload, source=False
            )
        except (PreserveError, SafeFetchError) as error:
            outcomes[selected_index] = _failed(artifact.file_id, error)
        return _finish_adoption(outcomes)
    finally:
        if artifact is not None:
            try:
                artifact.path.unlink(missing_ok=True)
            except OSError:
                pass


def capture_source_artifacts(
    vault_root: Path,
    *,
    source_schema: object,
    title: str,
    files: list[Mapping[str, object]],
    adoption: Mapping[str, object] | None = None,
    **source_fields: object,
) -> dict:
    """Stage remote files first, then capture each as a Source under a guard.

    The Evidence twin of this function is `preserve_artifacts`. They share
    staging deliberately — one implementation of hostile-URL handling, redirect
    and byte bounds, and per-file outcomes — and differ only in where the bytes
    are committed and what describes them. The lane is the command's, never the
    transport's: nothing here reads a MIME type or an extension to decide it.
    """
    from . import add as add_module

    if adoption is not None:
        return _capture_source_adoption(
            vault_root,
            source_schema=source_schema,
            title=title,
            files=files,
            adoption=adoption,
            source_fields=source_fields,
        )

    if not isinstance(files, list) or not files:
        return {"files": [], "summary": {"stored": 0, "failed": 0}}
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
                # One title per artifact, because each becomes its own source
                # page. A batch would otherwise land several pages under one
                # name and rely on uniquify to tell them apart.
                item_title = title if len(staged) == 1 else f"{title} — {artifact.filename}"
                with manager.mutation_guard(
                    vault_root,
                    request_id=active_mutation_request_id(),
                    operation="capture_source_artifacts_commit",
                    holder_kind="command",
                ):
                    result = add_module.add(
                        vault_root,
                        source_schema,
                        content="",
                        title=item_title,
                        artifact=add_module.SourceArtifact(
                            staged_path=artifact.path,
                            filename=artifact.filename,
                            content_type=artifact.content_type,
                        ),
                        **source_fields,
                    )
                mark_active_mutation_committed()
                payload = result.as_dict()
                # `stored_path` and `media_id` are required by the bounded
                # artifact-receipt projection a compact terminal applies; a row
                # missing either is replaced wholesale with
                # INVALID_ARTIFACT_RECEIPT. That projection also drops any key
                # outside its allowlist, so `page` and `ref` survive only in a
                # full terminal — which costs nothing, because citation resolves
                # from the artifact path and the page is `<stored_path>.md`.
                stored_path = payload.get("artifact_path")
                outcomes[index] = {
                    "file_id": artifact.file_id,
                    "outcome": "stored",
                    "stored_path": stored_path,
                    "path": stored_path,
                    "page": payload["path"],
                    "ref": payload["ref"],
                    "size": payload.get("size"),
                    "hash": payload.get("hash"),
                    "hash_algorithm": payload.get("hash_algorithm"),
                    "media_id": None,
                    "content_type": artifact.content_type,
                    "warnings": list(payload.get("warnings") or []),
                }
            except (SafeFetchError, PreserveError) as error:
                outcomes[index] = _failed(artifact.file_id, error)
            except add_module.AddError as error:
                outcomes[index] = _failed(
                    artifact.file_id, SafeFetchError(error.code, error.reason)
                )
    finally:
        for artifact in staged.values():
            try:
                artifact.path.unlink(missing_ok=True)
            except OSError:
                pass

    resolved = [outcome for outcome in outcomes if outcome is not None]
    stored = sum(1 for outcome in resolved if outcome.get("outcome") == "stored")
    return {
        "files": resolved,
        "summary": {"stored": stored, "failed": len(resolved) - stored},
    }


def preserve_artifacts(
    vault_root: Path,
    *,
    scope: str,
    category: str,
    files: list[Mapping[str, object]],
    adoption: Mapping[str, object] | None = None,
) -> dict:
    """Stage remote files first, then preserve each append-only artifact under a narrow guard."""
    if adoption is not None:
        return _preserve_evidence_adoption(
            vault_root,
            scope=scope,
            category=category,
            files=files,
            adoption=adoption,
        )
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
