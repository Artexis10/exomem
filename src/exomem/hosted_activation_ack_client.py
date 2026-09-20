"""Bounded same-UID client for the hosted custody publisher's Unix socket."""

from __future__ import annotations

import math
import os
import secrets
import socket
import stat
import struct
import time
from collections.abc import Mapping
from pathlib import Path

from .hosted_activation_ack_protocol import PROTOCOL, ProtocolError, decode_message, encode_message

SOCKET_PATH = Path("/run/exomem/activation-ack/ack.sock")
PROTOCOL_ENV = "EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL"
SOCKET_ENV = "EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET"
MAX_EXCHANGE_SECONDS = 5.0


class ActivationAcknowledgementUnavailable(RuntimeError):
    """Content-free failure; callers retain any already committed mutation."""

    code = "ACK_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("hosted activation acknowledgement is unavailable")


def is_hosted_activation_ack_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Recognize only the complete, fixed deployment capability."""
    values = os.environ if environ is None else environ
    if PROTOCOL_ENV not in values and SOCKET_ENV not in values:
        if "EXOMEM_HOSTED_CELL_ID" in values:
            raise ActivationAcknowledgementUnavailable
        return False
    if values.get(PROTOCOL_ENV) != PROTOCOL or values.get(SOCKET_ENV) != str(SOCKET_PATH):
        raise ActivationAcknowledgementUnavailable
    return True


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ActivationAcknowledgementUnavailable
    return remaining


def _read_exact(connection: socket.socket, count: int, deadline: float) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(remaining)
        if not chunk:
            raise ActivationAcknowledgementUnavailable
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class ActivationAcknowledgementClient:
    """One bounded exchange per connection, with a kernel-authenticated peer.

    Production uses the fixed socket path. The constructor permits an explicit
    private socket path for isolated transport tests, never a request argument.
    """

    def __init__(self, socket_path: Path = SOCKET_PATH) -> None:
        self._path = Path(socket_path)

    def check(self, *, timeout_seconds: float = MAX_EXCHANGE_SECONDS) -> dict[str, object]:
        return self._exchange("check", None, timeout_seconds=timeout_seconds)

    def acknowledge(
        self, publication: Mapping[str, object], *, timeout_seconds: float = MAX_EXCHANGE_SECONDS
    ) -> dict[str, object]:
        return self._exchange("ack", dict(publication), timeout_seconds=timeout_seconds)

    def _exchange(
        self, operation: str, publication: dict[str, object] | None, *, timeout_seconds: float
    ) -> dict[str, object]:
        try:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= MAX_EXCHANGE_SECONDS
                or not hasattr(socket, "SO_PEERCRED")
            ):
                raise ActivationAcknowledgementUnavailable
            deadline = time.monotonic() + timeout_seconds
            info = os.lstat(self._path)
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ActivationAcknowledgementUnavailable
            request_id = secrets.token_hex(32)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(min(0.5, _remaining(deadline)))
                connection.connect(str(self._path))
                _, peer_uid, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if peer_uid != os.geteuid():
                    raise ActivationAcknowledgementUnavailable
                request = {
                    "protocol": PROTOCOL,
                    "request_id": request_id,
                    "operation": operation,
                    "budget_ms": max(1, min(5000, int(_remaining(deadline) * 1000))),
                    "publication": publication,
                }
                encoded = encode_message(
                    request, "udsCheckRequest" if operation == "check" else "udsAckRequest"
                )
                connection.settimeout(_remaining(deadline))
                connection.sendall(struct.pack("!I", len(encoded)) + encoded)
                size = struct.unpack("!I", _read_exact(connection, 4, deadline))[0]
                if not 1 <= size <= 8192:
                    raise ActivationAcknowledgementUnavailable
                response = decode_message(_read_exact(connection, size, deadline), "udsResponse")
                _remaining(deadline)
            status = "ready" if operation == "check" else "acknowledged"
            code = "ACK_READY" if operation == "check" else "ACKNOWLEDGED"
            if (
                response["request_id"] != request_id
                or response["status"] != status
                or response["code"] != code
                or response["bundle_revision"] is None
                or response["activation"] is None
                or response["retry_after_ms"] != 0
                or (operation == "ack" and response["activation"] != publication["successor"])
            ):
                raise ActivationAcknowledgementUnavailable
            return response
        except ActivationAcknowledgementUnavailable:
            raise
        except (OSError, ProtocolError, TypeError, ValueError, OverflowError):
            raise ActivationAcknowledgementUnavailable from None
