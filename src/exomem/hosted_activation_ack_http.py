"""Bounded authenticated TLS client for hosted activation acknowledgement."""

from __future__ import annotations

import math
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from . import hosted_security
from .hosted_activation_ack_protocol import (
    PROTOCOL,
    ProtocolError,
    decode_message,
    encode_message,
    listener_dns_name,
)

TRUST_CA_PATH = Path("/run/exomem/activation-ack-trust/ca.pem")
PORT = 8443
MAX_EXCHANGE_SECONDS = 5.0
MAX_CONNECT_SECONDS = 0.5
MAX_HEADER_BYTES = 8 * 1024
MAX_ERROR_BYTES = 8 * 1024
MAX_BUNDLE_BYTES = 192 * 1024
_CURRENT_PATH = "/cell-runtime/v1/activation/current"
_ACK_PATH = "/cell-runtime/v1/activation/ack"
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,511}\Z")
_REQUEST_ID = re.compile(r"[0-9a-f]{64}\Z")
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_ERROR_STATUSES = {
    400: frozenset({"MALFORMED_REQUEST"}),
    401: frozenset({"AUTHENTICATION_FAILED"}),
    409: frozenset({"ACTIVATION_CONFLICT"}),
    429: frozenset({"ACK_CAPACITY_EXCEEDED"}),
    503: frozenset(
        {"ACK_UNAVAILABLE", "ACK_DEADLINE_EXCEEDED", "ACK_PENDING", "KEYRING_NOT_AVAILABLE"}
    ),
}


class ActivationAckHttpError(RuntimeError):
    """Content-free worker transport or protocol failure."""

    def __init__(self, code: str = "ACK_UNAVAILABLE", retry_after_ms: int = 0) -> None:
        self.code = code
        self.retry_after_ms = retry_after_ms
        super().__init__(f"{code}: hosted activation acknowledgement request failed")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ActivationAckHttpError("ACK_DEADLINE_EXCEEDED")
    return remaining


def _deadline(value: object) -> float:
    now = time.monotonic()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not now < value <= now + MAX_EXCHANGE_SECONDS
    ):
        raise ActivationAckHttpError("ACK_DEADLINE_EXCEEDED")
    return float(value)


def _read_headers(connection: ssl.SSLSocket, deadline: float) -> tuple[bytes, bytes]:
    buffered = bytearray()
    while True:
        boundary = buffered.find(b"\r\n\r\n")
        if boundary >= 0:
            end = boundary + 4
            if end > MAX_HEADER_BYTES:
                raise ActivationAckHttpError
            return bytes(buffered[:boundary]), bytes(buffered[end:])
        if len(buffered) >= MAX_HEADER_BYTES:
            raise ActivationAckHttpError
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(min(4096, MAX_HEADER_BYTES - len(buffered)))
        if not chunk:
            raise ActivationAckHttpError
        buffered.extend(chunk)


def _parse_headers(raw: bytes) -> tuple[int, dict[str, str]]:
    lines = raw.split(b"\r\n")
    if not lines:
        raise ActivationAckHttpError
    status_parts = lines[0].split(b" ", 2)
    if (
        len(status_parts) != 3
        or status_parts[0] != b"HTTP/1.1"
        or len(status_parts[1]) != 3
        or not status_parts[1].isdigit()
    ):
        raise ActivationAckHttpError
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[:1] in b" \t" or b":" not in line:
            raise ActivationAckHttpError
        name, raw_value = line.split(b":", 1)
        if _HEADER_NAME.fullmatch(name) is None:
            raise ActivationAckHttpError
        try:
            key = name.decode("ascii").lower()
            value = raw_value.strip(b" \t").decode("ascii")
        except UnicodeDecodeError:
            raise ActivationAckHttpError from None
        if key in headers or "\r" in value or "\n" in value:
            raise ActivationAckHttpError
        headers[key] = value
    return int(status_parts[1]), headers


def _content_length(headers: Mapping[str, str], maximum: int) -> int:
    if "transfer-encoding" in headers or set(headers).isdisjoint({"content-length"}):
        raise ActivationAckHttpError
    raw = headers["content-length"]
    if not raw.isascii() or not raw.isdigit() or str(int(raw)) != raw:
        raise ActivationAckHttpError
    length = int(raw)
    if not 1 <= length <= maximum:
        raise ActivationAckHttpError
    return length


def _read_body(connection: ssl.SSLSocket, initial: bytes, length: int, deadline: float) -> bytes:
    if len(initial) > length:
        raise ActivationAckHttpError
    body = bytearray(initial)
    while len(body) < length:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(min(16 * 1024, length - len(body)))
        if not chunk:
            raise ActivationAckHttpError
        body.extend(chunk)
    return bytes(body)


class _Resolution:
    def __init__(self) -> None:
        self.finished = threading.Event()
        self.value: object = None


class HostedActivationAckHttpClient:
    """One fixed-path HTTP/1.1 exchange per verified TLS connection."""

    def __init__(
        self,
        *,
        endpoint: tuple[str, int],
        server_hostname: str,
        ssl_context: ssl.SSLContext,
        cell_id: str,
        credential_loader: Callable[[], hosted_security.CredentialBundle] = (
            hosted_security.load_credential_bundle
        ),
    ) -> None:
        if (
            not isinstance(endpoint, tuple)
            or len(endpoint) != 2
            or not isinstance(endpoint[0], str)
            or not isinstance(endpoint[1], int)
            or not isinstance(server_hostname, str)
            or not isinstance(ssl_context, ssl.SSLContext)
            or not isinstance(cell_id, str)
            or _IDENTIFIER.fullmatch(cell_id) is None
            or not callable(credential_loader)
        ):
            raise ActivationAckHttpError
        self._endpoint = endpoint
        self._server_hostname = server_hostname
        self._context = ssl_context
        self._cell_id = cell_id
        self._credential_loader = credential_loader
        self._resolver_lock = threading.Lock()
        self._resolution: _Resolution | None = None

    @classmethod
    def for_platform_namespace(
        cls, namespace: str, *, cell_id: str
    ) -> HostedActivationAckHttpClient:
        if not isinstance(namespace, str) or _DNS_LABEL.fullmatch(namespace) is None:
            raise ActivationAckHttpError
        hostname = listener_dns_name(namespace)
        try:
            context = ssl.create_default_context(cafile=str(TRUST_CA_PATH))
        except OSError:
            raise ActivationAckHttpError from None
        return cls(
            endpoint=(hostname, PORT),
            server_hostname=hostname,
            ssl_context=context,
            cell_id=cell_id,
        )

    def current(self, request_id: str, *, deadline: float) -> dict[str, object]:
        return self._exchange("GET", _CURRENT_PATH, request_id, None, _deadline(deadline))

    def acknowledge(
        self,
        request_id: str,
        publication: Mapping[str, object],
        *,
        deadline: float,
    ) -> dict[str, object]:
        target = _deadline(deadline)
        request = {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "budget_ms": max(1, min(5000, int(_remaining(target) * 1000))),
            "publication": dict(publication),
        }
        try:
            body = encode_message(request, "httpAckRequest")
        except (ProtocolError, TypeError, ValueError):
            raise ActivationAckHttpError("MALFORMED_REQUEST") from None
        return self._exchange("POST", _ACK_PATH, request_id, body, target)

    def _credential(self) -> tuple[str, str]:
        try:
            bundle = self._credential_loader()
            version = sorted(bundle.credentials)[0]
            return version, bundle.credentials[version]
        except (
            hosted_security.HostedSecurityError,
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
        ):
            raise ActivationAckHttpError from None

    def _connect(self, deadline: float) -> ssl.SSLSocket:
        connect_deadline = min(deadline, time.monotonic() + MAX_CONNECT_SECONDS)
        plain: socket.socket | None = None
        try:
            for family, socktype, protocol, _, address in self._resolve(connect_deadline):
                candidate = socket.socket(family, socktype, protocol)
                try:
                    candidate.settimeout(_remaining(connect_deadline))
                    candidate.connect(address)
                    plain = candidate
                    break
                except OSError:
                    candidate.close()
            if plain is None:
                raise ActivationAckHttpError
            plain.settimeout(_remaining(connect_deadline))
            secured = self._context.wrap_socket(plain, server_hostname=self._server_hostname)
            plain = None
            _remaining(connect_deadline)
            return secured
        except (OSError, ssl.SSLError, ValueError):
            if plain is not None:
                plain.close()
            raise ActivationAckHttpError from None

    def _resolve(self, deadline: float) -> list[tuple]:
        with self._resolver_lock:
            state = self._resolution
            if state is None:
                state = _Resolution()
                self._resolution = state

                def lookup() -> None:
                    try:
                        value: object = socket.getaddrinfo(*self._endpoint, type=socket.SOCK_STREAM)
                    except Exception as error:  # noqa: BLE001 - thread must release its slot
                        value = error
                    with self._resolver_lock:
                        state.value = value
                        state.finished.set()
                        if self._resolution is state:
                            self._resolution = None

                threading.Thread(target=lookup, daemon=True).start()
        if not state.finished.wait(timeout=_remaining(deadline)):
            raise ActivationAckHttpError
        resolved = state.value
        if isinstance(resolved, Exception) or not isinstance(resolved, list) or not resolved:
            raise ActivationAckHttpError
        return resolved

    def _exchange(
        self,
        method: str,
        path: str,
        request_id: str,
        body: bytes | None,
        deadline: float,
    ) -> dict[str, object]:
        if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
            raise ActivationAckHttpError("MALFORMED_REQUEST")
        try:
            version, credential = self._credential()
            payload = b"" if body is None else body
            headers = (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: {self._server_hostname}:{self._endpoint[1]}\r\n"
                f"Authorization: Bearer {credential}\r\n"
                f"X-Exomem-Cell-Id: {self._cell_id}\r\n"
                f"X-Exomem-Credential-Version: {version}\r\n"
                f"X-Exomem-Activation-Protocol: {PROTOCOL}\r\n"
                f"X-Exomem-Request-Id: {request_id}\r\n"
                "Accept: application/json\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            if len(headers) > MAX_HEADER_BYTES or len(payload) > 8 * 1024:
                raise ActivationAckHttpError("MALFORMED_REQUEST")
            with self._connect(deadline) as connection:
                connection.settimeout(_remaining(deadline))
                connection.sendall(headers + payload)
                raw_headers, initial = _read_headers(connection, deadline)
                status, response_headers = _parse_headers(raw_headers)
                maximum = MAX_BUNDLE_BYTES if status == 200 else MAX_ERROR_BYTES
                length = _content_length(response_headers, maximum)
                response_body = _read_body(connection, initial, length, deadline)
            _remaining(deadline)
            if status == 200:
                response = decode_message(response_body, "httpBundleResponse")
                if response["request_id"] != request_id or response["cell_id"] != self._cell_id:
                    raise ActivationAckHttpError
                return response
            allowed = _ERROR_STATUSES.get(status)
            error = decode_message(response_body, "errorResponse")
            if allowed is None or error["code"] not in allowed or error["request_id"] != request_id:
                raise ActivationAckHttpError
            raise ActivationAckHttpError(error["code"], error["retry_after_ms"])
        except ActivationAckHttpError:
            raise
        except (OSError, ProtocolError, TypeError, UnicodeError, ValueError):
            raise ActivationAckHttpError from None
