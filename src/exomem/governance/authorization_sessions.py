"""Pure authorization-session credential grammar.

The parser is shared by capability verification and terminal scrubbing so an
accepted credential can never fall outside the scrubber's canonical language.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

AUTHORIZATION_SESSION_CREDENTIAL_BYTES = 70
_MAX_SIGNED_SQLITE_INTEGER = (1 << 63) - 1
_LOCATOR_DIGEST_DOMAIN = b"exomem.authorization-session.locator/v1"
_CREDENTIAL_VERIFIER_DOMAIN = b"exomem.authorization-session.verifier/v1"
_BASE64URL_ALPHABET = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


@dataclass(frozen=True, slots=True)
class AuthorizationSessionCredential:
    encoded: str = field(repr=False)
    locator: bytes
    secret: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthorizationSessionBinding:
    session_id: str
    principal_id: str
    issuer_family: str
    cell_id: str
    logical_vault_id: str
    keyring_id: str
    credential_generation: int
    expires_at: int

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "principal_id",
            "issuer_family",
            "cell_id",
            "logical_vault_id",
            "keyring_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
                raise ValueError(f"{name} must be non-empty bounded text")
        if (
            isinstance(self.credential_generation, bool)
            or not 1
            <= self.credential_generation
            <= _MAX_SIGNED_SQLITE_INTEGER
        ):
            raise ValueError("credential_generation must be a positive integer")
        if (
            isinstance(self.expires_at, bool)
            or not 1 <= self.expires_at <= _MAX_SIGNED_SQLITE_INTEGER
        ):
            raise ValueError("expires_at must be a positive integer")


AuthorizationSessionStatus = Literal["active", "closed"]


@dataclass(frozen=True, slots=True)
class AuthorizationSessionVerifierRecord:
    binding: AuthorizationSessionBinding
    verifier_key_id: str
    locator_digest: bytes = field(repr=False)
    verifier: bytes = field(repr=False)
    status: AuthorizationSessionStatus = "active"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.verifier_key_id, str)
            or not self.verifier_key_id
            or len(self.verifier_key_id.encode("utf-8")) > 512
        ):
            raise ValueError("verifier_key_id must be non-empty bounded text")
        if (
            not isinstance(self.locator_digest, bytes)
            or len(self.locator_digest) != hashlib.sha256().digest_size
        ):
            raise ValueError("locator_digest must be one SHA-256 digest")
        if (
            not isinstance(self.verifier, bytes)
            or len(self.verifier) != hashlib.sha256().digest_size
        ):
            raise ValueError("verifier must be one SHA-256 digest")
        if self.status not in {"active", "closed"}:
            raise ValueError("authorization session status is invalid")


@dataclass(frozen=True, slots=True)
class IssuedAuthorizationSessionCredential:
    bearer: str = field(repr=False)
    record: AuthorizationSessionVerifierRecord


def _frame(domain: bytes, fields: tuple[bytes, ...]) -> bytes:
    framed = bytearray(domain)
    framed.append(0)
    for value in fields:
        framed.extend(len(value).to_bytes(4, "big"))
        framed.extend(value)
    return bytes(framed)


def _binding_fields(binding: AuthorizationSessionBinding) -> tuple[bytes, ...]:
    return (
        binding.session_id.encode("utf-8"),
        binding.principal_id.encode("utf-8"),
        binding.issuer_family.encode("utf-8"),
        binding.cell_id.encode("utf-8"),
        binding.logical_vault_id.encode("utf-8"),
        binding.keyring_id.encode("utf-8"),
        binding.credential_generation.to_bytes(8, "big"),
        binding.expires_at.to_bytes(8, "big"),
    )


def _require_verifier_key(verifier_key: bytes) -> None:
    if not isinstance(verifier_key, bytes) or len(verifier_key) != 32:
        raise ValueError("authorization-session verifier key must contain exactly 256 bits")


def _locator_digest(verifier_key: bytes, locator: bytes) -> bytes:
    return hmac.new(
        verifier_key,
        _frame(_LOCATOR_DIGEST_DOMAIN, (locator,)),
        hashlib.sha256,
    ).digest()


def _credential_verifier(
    verifier_key: bytes,
    *,
    verifier_key_id: str,
    locator: bytes,
    secret: bytes,
    binding: AuthorizationSessionBinding,
) -> bytes:
    return hmac.new(
        verifier_key,
        _frame(
            _CREDENTIAL_VERIFIER_DOMAIN,
            (
                secret,
                locator,
                verifier_key_id.encode("utf-8"),
                *_binding_fields(binding),
            ),
        ),
        hashlib.sha256,
    ).digest()


def _encode_credential(locator: bytes, secret: bytes) -> str:
    if len(locator) != 16 or len(secret) != 32:
        raise ValueError("authorization-session credential parts have invalid lengths")
    return (
        "as1."
        + base64.urlsafe_b64encode(locator).rstrip(b"=").decode("ascii")
        + "."
        + base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
    )


def issue_credential(
    *,
    verifier_key: bytes,
    verifier_key_id: str,
    binding: AuthorizationSessionBinding,
) -> IssuedAuthorizationSessionCredential:
    """Issue one random credential and its bearer-free persisted verifier record."""

    _require_verifier_key(verifier_key)
    locator = secrets.token_bytes(16)
    secret = secrets.token_bytes(32)
    bearer = _encode_credential(locator, secret)
    record = AuthorizationSessionVerifierRecord(
        binding=binding,
        verifier_key_id=verifier_key_id,
        locator_digest=_locator_digest(verifier_key, locator),
        verifier=_credential_verifier(
            verifier_key,
            verifier_key_id=verifier_key_id,
            locator=locator,
            secret=secret,
            binding=binding,
        ),
    )
    return IssuedAuthorizationSessionCredential(bearer=bearer, record=record)


def verify_credential(
    value: object,
    *,
    record: AuthorizationSessionVerifierRecord,
    verifier_key: bytes,
    verifier_key_id: str,
    expected_binding: AuthorizationSessionBinding,
    now: int,
) -> bool:
    """Verify a parsed credential against its row and trusted request binding."""

    _require_verifier_key(verifier_key)
    if isinstance(now, bool) or not isinstance(now, int):
        return False
    parsed = parse_credential(value)
    if parsed is None:
        return False
    calculated_locator = _locator_digest(verifier_key, parsed.locator)
    calculated_verifier = _credential_verifier(
        verifier_key,
        verifier_key_id=verifier_key_id,
        locator=parsed.locator,
        secret=parsed.secret,
        binding=record.binding,
    )
    valid = hmac.compare_digest(calculated_locator, record.locator_digest)
    valid &= hmac.compare_digest(calculated_verifier, record.verifier)
    valid &= hmac.compare_digest(verifier_key_id, record.verifier_key_id)
    for actual, expected in zip(
        _binding_fields(record.binding),
        _binding_fields(expected_binding),
        strict=True,
    ):
        valid &= hmac.compare_digest(actual, expected)
    valid &= record.status == "active"
    valid &= now < record.binding.expires_at
    return bool(valid)


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
