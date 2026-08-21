"""Pure authorization-session credential grammar.

The parser is shared by capability verification and terminal scrubbing so an
accepted credential can never fall outside the scrubber's canonical language.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from dataclasses import dataclass

AUTHORIZATION_SESSION_CREDENTIAL_BYTES = 70
_BASE64URL_ALPHABET = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


@dataclass(frozen=True, slots=True)
class AuthorizationSessionCredential:
    encoded: str
    locator: bytes
    secret: bytes


def parse_credential(value: object) -> AuthorizationSessionCredential | None:
    """Parse only canonical ``as1`` credentials with their exact bounded parts."""

    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return None
    if (
        len(encoded) != AUTHORIZATION_SESSION_CREDENTIAL_BYTES
        or encoded[:4] != b"as1."
        or encoded[26:27] != b"."
    ):
        return None
    locator = encoded[4:26]
    secret = encoded[27:]
    if not all(byte in _BASE64URL_ALPHABET for byte in (*locator, *secret)):
        return None
    try:
        locator_bytes = base64.b64decode(locator + b"==", altchars=b"-_", validate=True)
        secret_bytes = base64.b64decode(secret + b"=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(locator_bytes) != 16 or len(secret_bytes) != 32:
        return None
    if (
        base64.urlsafe_b64encode(locator_bytes).rstrip(b"=") != locator
        or base64.urlsafe_b64encode(secret_bytes).rstrip(b"=") != secret
    ):
        return None
    return AuthorizationSessionCredential(value, locator_bytes, secret_bytes)


def iter_credential_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield every canonical credential span using the shared bounded parser."""

    search_from = 0
    while True:
        start = text.find("as1.", search_from)
        if start < 0:
            return
        end = start + AUTHORIZATION_SESSION_CREDENTIAL_BYTES
        if parse_credential(text[start:end]) is None:
            search_from = start + 1
            continue
        yield start, end
        search_from = end
