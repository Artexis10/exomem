"""The one-shot authorization-session credential carrier and its CLI reader.

The CLI reads a carrier on every vault command, so this module imports no MCP,
HTTP or session-store machinery: those cost about a quarter of a second at CLI
start-up. The server's transport (`authorization_transport`) imports the carrier from here.
"""

from __future__ import annotations

import os

from . import authorization_sessions
from .authorization_request import ABSENT_CREDENTIAL, INVALID_CREDENTIAL


class CredentialCarrier:
    """One-shot, redacted storage for a bounded canonical bearer."""

    __slots__ = ("_payload", "_state")

    def __init__(self, payload: bytes | None, *, state: str) -> None:
        self._payload = bytearray(payload) if payload is not None else None
        self._state = state

    @classmethod
    def absent(cls) -> CredentialCarrier:
        return cls(None, state="absent")

    @classmethod
    def invalid(cls) -> CredentialCarrier:
        return cls(None, state="invalid")

    @classmethod
    def from_value(cls, value: object) -> CredentialCarrier:
        if not isinstance(value, str):
            return cls.invalid()
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            return cls.invalid()
        if (
            len(encoded) != authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES
            or authorization_sessions.parse_credential(value) is None
        ):
            return cls.invalid()
        return cls(encoded, state="present")

    def consume(self) -> object:
        state = self._state
        self._state = "consumed"
        if state == "absent":
            return ABSENT_CREDENTIAL
        if state != "present" or self._payload is None:
            return INVALID_CREDENTIAL
        try:
            return self._payload.decode("ascii")
        finally:
            for index in range(len(self._payload)):
                self._payload[index] = 0
            self._payload = None

    @property
    def is_absent(self) -> bool:
        """Whether no credential was presented at the protected boundary."""

        return self._state == "absent"

    @property
    def is_invalid(self) -> bool:
        """Whether a credential was presented but failed canonical parsing."""

        return self._state == "invalid"

    def discard(self) -> None:
        """Destroy any retained bytes without materializing an immutable string."""

        if self._payload is not None:
            for index in range(len(self._payload)):
                self._payload[index] = 0
            self._payload = None
        self._state = "consumed"

    def __repr__(self) -> str:
        return f"CredentialCarrier(state={self._state!r})"


def read_cli_authorization_fd(fd_spec: str) -> CredentialCarrier:
    """Read at most one canonical bearer from a caller-owned descriptor."""

    if fd_spec == "-":
        descriptor = 0
    elif isinstance(fd_spec, str) and fd_spec.isascii() and fd_spec.isdecimal():
        descriptor = int(fd_spec)
    else:
        return CredentialCarrier.invalid()
    if descriptor < 0:
        return CredentialCarrier.invalid()

    limit = authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES + 1
    payload = bytearray()
    try:
        while len(payload) < limit:
            chunk = os.read(descriptor, limit - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES:
            return CredentialCarrier.invalid()
        try:
            decoded: object = payload.decode("ascii")
        except UnicodeDecodeError:
            decoded = INVALID_CREDENTIAL
        return (
            CredentialCarrier.invalid()
            if decoded is INVALID_CREDENTIAL
            else CredentialCarrier.from_value(decoded)
        )
    except OSError:
        return CredentialCarrier.invalid()
    finally:
        for index in range(len(payload)):
            payload[index] = 0
