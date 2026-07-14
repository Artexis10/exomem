"""Envelope codec boundary for material that must survive worker restarts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EnvelopeCodec(Protocol):
    def encrypt_json(self, value: dict[str, Any], *, purpose: str) -> str: ...

    def decrypt_json(self, value: str, *, purpose: str) -> dict[str, Any]: ...


class AesGcmEnvelopeCodec:
    """Small injected AES-256-GCM codec with purpose-bound ciphertext."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._cipher = AESGCM(key)

    @classmethod
    def from_secret(cls, secret: str) -> AesGcmEnvelopeCodec:
        return cls(hashlib.sha256(secret.encode("utf-8")).digest())

    def encrypt_json(self, value: dict[str, Any], *, purpose: str) -> str:
        nonce = os.urandom(12)
        plaintext = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        ciphertext = self._cipher.encrypt(nonce, plaintext, purpose.encode("utf-8"))
        return "v1." + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt_json(self, value: str, *, purpose: str) -> dict[str, Any]:
        if not value.startswith("v1."):
            raise ValueError("unsupported ciphertext version")
        raw = base64.urlsafe_b64decode(value[3:].encode("ascii"))
        plaintext = self._cipher.decrypt(raw[:12], raw[12:], purpose.encode("utf-8"))
        decoded = json.loads(plaintext)
        if not isinstance(decoded, dict):
            raise ValueError("encrypted payload must be an object")
        return decoded
