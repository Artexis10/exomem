"""Authenticated streaming envelope format for portable recovery archives."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"EXOMEM-RECOVERY\x00\x01"
_LENGTH = struct.Struct(">I")
_CHUNK_IDENTITY = struct.Struct(">QI")
_RECOVERY_ENVELOPE_AAD = b"exomem.database-recovery-envelope.v1"


class ArchiveAuthenticationError(RuntimeError):
    """Ciphertext or authenticated recovery identity did not verify."""


@dataclass(frozen=True, slots=True)
class RecoveryIdentity:
    tenant_id: str
    cell_id: str
    operation_id: str
    fence_generation: int
    archive_sha256: str
    manifest_sha256: str
    archive_size: int

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.cell_id, self.operation_id)):
            raise ValueError("recovery identity fields must be non-empty")
        if self.fence_generation < 1 or self.archive_size < 1:
            raise ValueError("recovery fence and size must be positive")
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in (self.archive_sha256, self.manifest_sha256)
        ):
            raise ValueError("recovery digests must be lowercase SHA-256")

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)

    def provider_metadata(self) -> dict[str, str]:
        return {
            "tenant-id": self.tenant_id,
            "cell-id": self.cell_id,
            "operation-id": self.operation_id,
            "fence-generation": str(self.fence_generation),
            "archive-sha256": self.archive_sha256,
            "manifest-sha256": self.manifest_sha256,
            "archive-size": str(self.archive_size),
        }

    def authenticated_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")


class DataKeyWrapper(Protocol):
    def wrap(self, data_key: bytes, *, identity: RecoveryIdentity) -> str: ...

    def unwrap(self, wrapped_data_key: str, *, identity: RecoveryIdentity) -> bytes: ...


class AesGcmKeyWrapper:
    """Root-key adapter; wrapped keys are stored only in the external ledger."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 key wrapper requires exactly 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_secret(cls, secret: bytes | str) -> AesGcmKeyWrapper:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        return cls(hashlib.sha256(raw).digest())

    def wrap(self, data_key: bytes, *, identity: RecoveryIdentity) -> str:
        if len(data_key) != 32:
            raise ValueError("archive data key must be AES-256")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, data_key, identity.authenticated_bytes())
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def unwrap(self, wrapped_data_key: str, *, identity: RecoveryIdentity) -> bytes:
        try:
            raw = base64.b64decode(wrapped_data_key.encode("ascii"), altchars=b"-_", validate=True)
            key = self._cipher.decrypt(raw[:12], raw[12:], identity.authenticated_bytes())
        except (InvalidTag, ValueError, UnicodeEncodeError) as error:
            raise ArchiveAuthenticationError("wrapped archive key did not authenticate") from error
        if len(key) != 32:
            raise ArchiveAuthenticationError("wrapped archive key has invalid size")
        return key


@dataclass(frozen=True, slots=True)
class DatabaseRecoveryEnvelope:
    """Self-contained encrypted sidecar required to recover after ledger loss."""

    object_key: str
    object_version_id: str
    identity: RecoveryIdentity
    wrapped_data_key: str
    ciphertext_sha256: str
    ciphertext_size: int
    metadata_sha256: str
    created_at: datetime
    object_lock_until: datetime

    def __post_init__(self) -> None:
        if not self.object_key or not self.object_version_id or not self.wrapped_data_key:
            raise ValueError("database recovery envelope is incomplete")
        if self.ciphertext_size < 1:
            raise ValueError("database recovery ciphertext size must be positive")
        for value in (self.ciphertext_sha256, self.metadata_sha256):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("database recovery envelope digest is invalid")
        for value in (self.created_at, self.object_lock_until):
            if value.tzinfo is None:
                raise ValueError("database recovery envelope timestamps must be timezone-aware")

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "objectKey": self.object_key,
            "objectVersionId": self.object_version_id,
            "identity": self.identity.as_dict(),
            "wrappedDataKey": self.wrapped_data_key,
            "ciphertextSha256": self.ciphertext_sha256,
            "ciphertextSize": self.ciphertext_size,
            "metadataSha256": self.metadata_sha256,
            "createdAt": self.created_at.astimezone(UTC).isoformat(),
            "objectLockUntil": self.object_lock_until.astimezone(UTC).isoformat(),
        }


class RecoveryEnvelopeCodec(Protocol):
    def seal(self, envelope: DatabaseRecoveryEnvelope) -> bytes: ...

    def open(self, sealed: bytes) -> DatabaseRecoveryEnvelope: ...


class AesGcmRecoveryEnvelopeCodec:
    """Encrypt and authenticate the recovery sidecar under the offline root escrow."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 recovery envelope key requires exactly 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_secret(cls, secret: bytes | str) -> AesGcmRecoveryEnvelopeCodec:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        return cls(hashlib.sha256(raw).digest())

    def seal(self, envelope: DatabaseRecoveryEnvelope) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(
            envelope.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return nonce + self._cipher.encrypt(nonce, plaintext, _RECOVERY_ENVELOPE_AAD)

    def open(self, sealed: bytes) -> DatabaseRecoveryEnvelope:
        try:
            if len(sealed) < 12 + 16:
                raise ValueError("sealed recovery envelope is truncated")
            plaintext = self._cipher.decrypt(sealed[:12], sealed[12:], _RECOVERY_ENVELOPE_AAD)
            value = json.loads(plaintext.decode("ascii"))
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError("recovery envelope version is invalid")
            identity_value = value["identity"]
            if not isinstance(identity_value, dict):
                raise ValueError("recovery envelope identity is invalid")
            identity = RecoveryIdentity(
                tenant_id=str(identity_value["tenant_id"]),
                cell_id=str(identity_value["cell_id"]),
                operation_id=str(identity_value["operation_id"]),
                fence_generation=int(identity_value["fence_generation"]),
                archive_sha256=str(identity_value["archive_sha256"]),
                manifest_sha256=str(identity_value["manifest_sha256"]),
                archive_size=int(identity_value["archive_size"]),
            )
            return DatabaseRecoveryEnvelope(
                object_key=str(value["objectKey"]),
                object_version_id=str(value["objectVersionId"]),
                identity=identity,
                wrapped_data_key=str(value["wrappedDataKey"]),
                ciphertext_sha256=str(value["ciphertextSha256"]),
                ciphertext_size=int(value["ciphertextSize"]),
                metadata_sha256=str(value["metadataSha256"]),
                created_at=datetime.fromisoformat(str(value["createdAt"])),
                object_lock_until=datetime.fromisoformat(str(value["objectLockUntil"])),
            )
        except (
            InvalidTag,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise ArchiveAuthenticationError(
                "database recovery envelope did not authenticate"
            ) from error


@dataclass(frozen=True, slots=True)
class ArchiveEncryptionReceipt:
    encryption_scheme: str
    wrapped_data_key: str
    ciphertext_sha256: str
    ciphertext_size: int
    authenticated_metadata: dict[str, str]


class ChunkedArchiveCipher:
    """Bounded-memory AES-256-GCM file encryption with atomic decryption."""

    def __init__(self, *, chunk_size: int = 1024 * 1024) -> None:
        if not 16 * 1024 <= chunk_size <= 16 * 1024 * 1024:
            raise ValueError("archive chunk size must be between 16 KiB and 16 MiB")
        self._chunk_size = chunk_size

    def encrypt(
        self,
        source: Path,
        destination: Path,
        *,
        identity: RecoveryIdentity,
        key_wrapper: DataKeyWrapper,
    ) -> ArchiveEncryptionReceipt:
        if source.stat().st_size != identity.archive_size:
            raise ArchiveAuthenticationError("archive size differs from authenticated identity")
        data_key = os.urandom(32)
        cipher = AESGCM(data_key)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
        digest = hashlib.sha256()
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                self._write(writer, digest, _MAGIC)
                index = 0
                while plaintext := reader.read(self._chunk_size):
                    nonce = os.urandom(12)
                    aad = identity.authenticated_bytes() + _CHUNK_IDENTITY.pack(
                        index, len(plaintext)
                    )
                    ciphertext = cipher.encrypt(nonce, plaintext, aad)
                    self._write(writer, digest, _LENGTH.pack(len(plaintext)))
                    self._write(writer, digest, nonce)
                    self._write(writer, digest, ciphertext)
                    index += 1
                self._write(writer, digest, _LENGTH.pack(0))
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination)
            destination.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return ArchiveEncryptionReceipt(
            encryption_scheme="envelope-aes-256-gcm",
            wrapped_data_key=key_wrapper.wrap(data_key, identity=identity),
            ciphertext_sha256=digest.hexdigest(),
            ciphertext_size=destination.stat().st_size,
            authenticated_metadata=identity.provider_metadata(),
        )

    def decrypt(
        self,
        source: Path,
        destination: Path,
        *,
        identity: RecoveryIdentity,
        wrapped_data_key: str,
        key_wrapper: DataKeyWrapper,
    ) -> None:
        data_key = key_wrapper.unwrap(wrapped_data_key, identity=identity)
        cipher = AESGCM(data_key)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
        plaintext_digest = hashlib.sha256()
        plaintext_size = 0
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                if self._read_exact(reader, len(_MAGIC)) != _MAGIC:
                    raise ArchiveAuthenticationError("archive encryption header is invalid")
                index = 0
                while True:
                    raw_length = self._read_exact(reader, _LENGTH.size)
                    (length,) = _LENGTH.unpack(raw_length)
                    if length == 0:
                        if reader.read(1):
                            raise ArchiveAuthenticationError("archive has trailing ciphertext")
                        break
                    if length > self._chunk_size:
                        raise ArchiveAuthenticationError("archive chunk exceeds declared bound")
                    nonce = self._read_exact(reader, 12)
                    ciphertext = self._read_exact(reader, length + 16)
                    aad = identity.authenticated_bytes() + _CHUNK_IDENTITY.pack(index, length)
                    try:
                        plaintext = cipher.decrypt(nonce, ciphertext, aad)
                    except InvalidTag as error:
                        raise ArchiveAuthenticationError(
                            "archive ciphertext did not authenticate"
                        ) from error
                    writer.write(plaintext)
                    plaintext_digest.update(plaintext)
                    plaintext_size += len(plaintext)
                    index += 1
                writer.flush()
                os.fsync(writer.fileno())
            if plaintext_size != identity.archive_size:
                raise ArchiveAuthenticationError("decrypted archive size is invalid")
            if plaintext_digest.hexdigest() != identity.archive_sha256:
                raise ArchiveAuthenticationError("decrypted archive digest is invalid")
            os.replace(temporary, destination)
            destination.chmod(0o600)
        except (EOFError, struct.error) as error:
            raise ArchiveAuthenticationError("archive ciphertext is truncated") from error
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write(writer, digest: hashlib._Hash, value: bytes) -> None:  # type: ignore[name-defined]
        writer.write(value)
        digest.update(value)

    @staticmethod
    def _read_exact(reader, size: int) -> bytes:
        value = reader.read(size)
        if len(value) != size:
            raise EOFError("unexpected end of encrypted archive")
        return value
