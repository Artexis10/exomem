"""Confined Cloud artifact staging through the gateway-authorized broker."""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import tempfile
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import jwt

from . import client_artifacts as artifacts

FETCH_PATH = "/internal/artifacts/fetch/v1"
BROKER_PORT = 8767
GRANT_AUDIENCE = "exomem-artifact-broker"
MAX_GRANT_CHARS = 8192
MAX_FILENAME_BYTES = 1024
_MIME_TOKEN = r"[A-Za-z0-9!#$&^_.+\-]+"
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _failure() -> artifacts.SafeFetchError:
    return artifacts.SafeFetchError("SAFE_FETCH_FAILED", "artifact broker transport is unavailable")


def descriptor_digest(file: Mapping[str, object]) -> str:
    """Bind the canonical file ID and otherwise exact descriptor values."""
    file_id = artifacts._file_id(file)
    url = file.get("download_url")
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise _failure()
    mime, filename = file.get("mime_type"), file.get("file_name")
    if mime is not None and (not isinstance(mime, str) or len(mime) > 255):
        raise _failure()
    if filename is not None and (
        not isinstance(filename, str) or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES
    ):
        raise _failure()
    try:
        raw = json.dumps(
            [file_id, url, mime, filename], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (UnicodeError, TypeError, ValueError) as error:
        raise _failure() from error
    return hashlib.sha256(raw).hexdigest()


def transport_content_type(value: object) -> str:
    """Validate MIME metadata before it becomes an HTTP response header."""
    mime = artifacts._content_type(value) or "application/octet-stream"
    if re.fullmatch(f"{_MIME_TOKEN}/{_MIME_TOKEN}", mime) is None:
        raise _failure()
    return mime


def encode_filename(value: str) -> str:
    from .preserve import _sanitize_filename

    filename = _sanitize_filename(value)
    try:
        encoded = filename.encode("utf-8")
    except UnicodeError as error:
        raise _failure() from error
    if (
        not encoded
        or len(encoded) > MAX_FILENAME_BYTES
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
    ):
        raise _failure()
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_filename(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4 * ((MAX_FILENAME_BYTES + 2) // 3):
        raise _failure()
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        filename = raw.decode("utf-8")
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise _failure() from error
    if encode_filename(filename) != value.rstrip("="):
        raise _failure()
    return filename


def broker_endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname or "")
        port = parsed.port
    except (ValueError, TypeError) as error:
        raise _failure() from error
    if (
        parsed.scheme != "http"
        or port != BROKER_PORT
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not any(address in network for network in _PRIVATE_NETWORKS)
    ):
        raise _failure()
    return str(address), port


class _PrivateConnection(http.client.HTTPConnection):
    """Connect to the literal broker address without involving cell DNS."""

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect((self.host, self.port))
        except BaseException:
            self.sock.close()
            raise


def _request_grant(file: Mapping[str, object], *, cell: str, operation: str, index: int) -> str:
    from fastmcp.server import dependencies

    token = dependencies.get_access_token()
    if (
        token is None
        or token.claims.get("iss") != "exomem-cloud-cell"
        or token.claims.get("sub") != cell
        or not cell
    ):
        raise _failure()
    grant = dependencies.get_http_headers().get("x-exomem-artifact-grant")
    if not isinstance(grant, str) or not grant or len(grant) > MAX_GRANT_CHARS:
        raise _failure()
    try:
        # The broker verifies the signature. Here the authenticated private
        # request must match the local cell and the caller's artifact lane.
        claims = jwt.decode(grant, options={"verify_signature": False})
        handles = claims.get("handles")
        if (
            claims.get("sub") != cell
            or claims.get("op") != operation
            or not isinstance(handles, list)
            or type(index) is not int
            or not 0 <= index < len(handles)
            or handles[index] != descriptor_digest(file)
        ):
            raise _failure()
    except (jwt.PyJWTError, TypeError, ValueError, UnicodeError) as error:
        raise _failure() from error
    return grant


def stage_cloud_artifact(
    file: Mapping[str, object],
    budget: artifacts.FetchBudget,
    *,
    index: int,
    lane: str | None,
    batch_deadline: float | None = None,
) -> artifacts.StagedArtifact:
    endpoint = broker_endpoint(os.environ.get("EXOMEM_CLOUD_ARTIFACT_BROKER_URL", ""))
    operation = {"source": "capture_source", "evidence": "preserve_artifacts"}.get(lane)
    if operation is None:
        raise _failure()
    cell = os.environ.get("EXOMEM_CLOUD_CELL_ID", "")
    grant = _request_grant(file, cell=cell, operation=operation, index=index)
    body = json.dumps(
        {
            "file": {
                key: file.get(key) for key in ("file_id", "download_url", "mime_type", "file_name")
            },
            "index": index,
            "cell": cell,
            "operation": operation,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    deadline = min(
        artifacts._monotonic() + artifacts._RETRIEVAL_DEADLINE_SECONDS,
        batch_deadline or float("inf"),
    )
    connection = _PrivateConnection(
        *endpoint, timeout=artifacts.remaining_retrieval_timeout(deadline)
    )
    response = None
    destination = None
    cancel = partial(artifacts._cancel_connection, connection)
    try:
        artifacts._bounded_retrieval_call(
            partial(
                connection.request,
                "POST",
                FETCH_PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {grant}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            ),
            deadline=deadline,
            cancel=cancel,
        )
        response = artifacts._bounded_retrieval_call(
            connection.getresponse, deadline=deadline, cancel=cancel
        )
        if response.status != 200:
            raise _failure()
        length = response.getheader("Content-Length")
        if not isinstance(length, str) or re.fullmatch(r"[0-9]{1,10}", length) is None:
            raise _failure()
        size = int(length)
        budget.validate_content_length(size)
        expected = response.getheader("X-Exomem-Artifact-SHA256")
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise _failure()
        declared_mime = response.getheader("Content-Type")
        if not isinstance(declared_mime, str) or not declared_mime:
            raise _failure()
        mime = transport_content_type(declared_mime)
        filename = decode_filename(response.getheader("X-Exomem-Artifact-Filename"))
        fd, raw_path = tempfile.mkstemp(prefix="exomem-artifact-")
        destination = Path(raw_path)
        digest, written = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as output:
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(artifacts.remaining_retrieval_timeout(deadline))
                block = artifacts._bounded_retrieval_call(
                    partial(response.read, artifacts._CHUNK_SIZE), deadline=deadline, cancel=cancel
                )
                artifacts.remaining_retrieval_timeout(deadline)
                if not block:
                    break
                written += len(block)
                budget.validate_content_length(written)
                if written > size:
                    raise _failure()
                digest.update(block)
                output.write(block)
        if written != size or digest.hexdigest() != expected:
            raise _failure()
        budget.consume(written)
        return artifacts.StagedArtifact(
            artifacts._file_id(file), destination, written, expected, mime, filename
        )
    except BaseException as error:
        if destination is not None:
            destination.unlink(missing_ok=True)
        cancel()
        if isinstance(error, (OSError, http.client.HTTPException, ValueError, UnicodeError)):
            raise _failure() from error
        raise
    finally:
        if response is not None:
            response.close()
        connection.close()
