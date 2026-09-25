"""D7 secret derivation: the C4 cell bearer and envelope-encrypted keys.

Pure functions only. Write-once persistence (`WHERE ... IS NULL`) and the B2
application-key lifecycle live in db.py and storage/, which call these.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BEARER_PREFIX = "exomem-cloud-cell-token-v1:"
_NONCE_LEN = 12


def derive_cell_bearer(key: bytes, cell_id: str) -> str:
    """C4: base64url_nopad(HMAC-SHA256(key, "exomem-cloud-cell-token-v1:" + cell_id))."""

    message = f"{BEARER_PREFIX}{cell_id}".encode("ascii")
    mac = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def bearer_matches(presented: str, *, key: bytes, cell_id: str) -> bool:
    """Constant-time comparison against the bearer derived for this cell_id."""

    expected = derive_cell_bearer(key, cell_id)
    return hmac.compare_digest(presented, expected)


def generate_backup_data_key() -> bytes:
    """A fresh random 32-byte per-cell restic/backup data key."""

    return os.urandom(32)


def _aad(*, cell_id: str, column: str, key_version: int) -> bytes:
    # D7: binds a wrapped blob to its cell, column and key version, so it
    # cannot be moved to another cell, column or version and still decrypt.
    return f"{cell_id}:{column}:{key_version}".encode("ascii")


def wrap_secret(master_key: bytes, plaintext: bytes, *, cell_id: str, column: str, key_version: int) -> bytes:
    """Envelope-encrypt `plaintext` with AES-GCM under `master_key`.

    The returned bytes are `nonce || ciphertext_with_tag`; the master-key
    version is stored alongside in a separate column, not in this blob.
    """

    aesgcm = AESGCM(master_key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext, _aad(cell_id=cell_id, column=column, key_version=key_version))
    return nonce + ciphertext


def unwrap_secret(master_key: bytes, wrapped: bytes, *, cell_id: str, column: str, key_version: int) -> bytes:
    aesgcm = AESGCM(master_key)
    nonce, ciphertext = wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:]
    return aesgcm.decrypt(nonce, ciphertext, _aad(cell_id=cell_id, column=column, key_version=key_version))
