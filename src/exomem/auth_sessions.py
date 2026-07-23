"""Durable, Exomem-owned MCP session records.

GitHub proves identity before this module is called.  Session validation is
deliberately local to Exomem and never retains or revalidates an upstream token.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, Literal

if os.name == "nt":  # pragma: no cover - exercised on Windows deployments
    import msvcrt
else:  # pragma: no cover - branch selection is platform-specific
    import fcntl

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from key_value.aio.stores.filetree import FileTreeStore

from .remote_oauth_storage import RemoteOAuthStorage
from .session_validation_cache import (
    SessionStoreTelemetry,
    SessionValidationCache,
)
from .session_validation_cache import (
    session_store_telemetry as _default_session_store_telemetry,
)

logger = logging.getLogger(__name__)

_TOKEN_VERSION = "exo_s1"
_ACCESS_TOKEN_VERSION = "exo_a2"
_REFRESH_TOKEN_VERSION = "exo_r2"
_SCHEMA_VERSION = 1
_ACCESS_SCHEMA_VERSION = 2
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_RETRY_GRACE_SECONDS = 30
_TOKEN_PATTERN = re.compile(
    rf"^{_TOKEN_VERSION}\.(?P<session_id>[A-Za-z0-9_-]{{16,64}})\."
    r"(?P<secret>[A-Za-z0-9_-]{43,128})$"
)
_ACCESS_TOKEN_PATTERN = re.compile(
    rf"^{_ACCESS_TOKEN_VERSION}\.(?P<session_id>[A-Za-z0-9_-]{{16,64}})\."
    r"(?P<secret>[A-Za-z0-9_-]{43,128})$"
)
_REFRESH_TOKEN_PATTERN = re.compile(
    rf"^{_REFRESH_TOKEN_VERSION}\.(?P<family_id>[A-Za-z0-9_-]{{16,64}})\."
    r"(?P<sequence>0|[1-9][0-9]{0,19})\.(?P<proof>[A-Za-z0-9_-]{43})$"
)
_GENERATION_KEY = "current"
_MAX_ISSUANCE_ATTEMPTS = 8


class SessionStoreUnavailable(RuntimeError):
    """The authoritative session store or its cipher could not be used."""


class InvalidRefreshToken(ValueError):
    """A refresh token is malformed, inactive, mismatched, or replayed."""


@dataclass(frozen=True)
class SessionKeys:
    hmac_key: bytes = field(repr=False)
    access_hmac_key: bytes = field(repr=False)
    refresh_hmac_key: bytes = field(repr=False)
    storage_key: bytes = field(repr=False)
    fingerprint: str


def _derive(root: bytes, *, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(root)


def derive_session_keys(signing_root: str) -> SessionKeys:
    """Derive purpose-separated proof and storage keys from an explicit root."""
    if not signing_root or not signing_root.strip():
        raise ValueError("an explicit signing root is required")
    material = signing_root.encode("utf-8")
    hmac_key = _derive(
        material,
        salt=b"exomem-session-proof-v1",
        info=b"opaque MCP bearer HMAC",
    )
    access_hmac_key = _derive(
        material,
        salt=b"exomem-access-proof-v2",
        info=b"opaque OAuth access bearer HMAC",
    )
    refresh_hmac_key = _derive(
        material,
        salt=b"exomem-refresh-proof-v2",
        info=b"deterministic OAuth refresh rotation HMAC",
    )
    storage_key = _derive(
        material,
        salt=b"exomem-session-storage-v1",
        info=b"session record Fernet encryption",
    )
    fingerprint_key = _derive(
        material,
        salt=b"exomem-session-namespace-v1",
        info=b"non-secret root namespace fingerprint",
    )
    return SessionKeys(
        hmac_key=hmac_key,
        access_hmac_key=access_hmac_key,
        refresh_hmac_key=refresh_hmac_key,
        storage_key=storage_key,
        fingerprint=hashlib.sha256(fingerprint_key).hexdigest()[:16],
    )


@dataclass(frozen=True)
class ParsedSessionToken:
    session_id: str
    secret: str = field(repr=False)


class SessionTokenCodec:
    """Issue and verify versioned opaque session bearers."""

    def __init__(self, hmac_key: bytes):
        if len(hmac_key) < 32:
            raise ValueError("session HMAC key must contain at least 256 bits")
        self._hmac_key = hmac_key

    def issue(self) -> tuple[str, str, str]:
        session_id = secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32)
        bearer = f"{_TOKEN_VERSION}.{session_id}.{secret}"
        return bearer, session_id, self.digest(bearer)

    @staticmethod
    def parse(bearer: str) -> ParsedSessionToken | None:
        if not isinstance(bearer, str):
            return None
        match = _TOKEN_PATTERN.fullmatch(bearer)
        if match is None:
            return None
        return ParsedSessionToken(
            session_id=match.group("session_id"),
            secret=match.group("secret"),
        )

    def digest(self, bearer: str) -> str:
        return hmac.new(self._hmac_key, bearer.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, bearer: str, expected_digest: str) -> bool:
        calculated = self.digest(bearer)
        return hmac.compare_digest(calculated, expected_digest)


class AccessTokenCodec(SessionTokenCodec):
    """Issue and verify expiring v2 OAuth access tokens."""

    def issue(self) -> tuple[str, str, str]:
        session_id = secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32)
        bearer = f"{_ACCESS_TOKEN_VERSION}.{session_id}.{secret}"
        return bearer, session_id, self.digest(bearer)

    @staticmethod
    def parse(bearer: str) -> ParsedSessionToken | None:
        if not isinstance(bearer, str):
            return None
        match = _ACCESS_TOKEN_PATTERN.fullmatch(bearer)
        if match is None:
            return None
        return ParsedSessionToken(
            session_id=match.group("session_id"),
            secret=match.group("secret"),
        )


@dataclass(frozen=True)
class ParsedRefreshToken:
    family_id: str
    sequence: int
    proof: str = field(repr=False)


class RefreshTokenCodec:
    """Create deterministic, self-authenticating refresh descendants."""

    def __init__(self, hmac_key: bytes):
        if len(hmac_key) < 32:
            raise ValueError("refresh HMAC key must contain at least 256 bits")
        self._hmac_key = hmac_key

    def issue(self) -> tuple[str, str]:
        family_id = secrets.token_urlsafe(18)
        return self.token_for(family_id, 0), family_id

    def token_for(self, family_id: str, sequence: int) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", family_id) or sequence < 0:
            raise ValueError("invalid refresh family or sequence")
        prefix = f"{_REFRESH_TOKEN_VERSION}.{family_id}.{sequence}"
        proof = base64.urlsafe_b64encode(
            hmac.new(self._hmac_key, prefix.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        return f"{prefix}.{proof}"

    def parse(self, bearer: str) -> ParsedRefreshToken | None:
        if not isinstance(bearer, str):
            return None
        match = _REFRESH_TOKEN_PATTERN.fullmatch(bearer)
        if match is None:
            return None
        family_id = match.group("family_id")
        sequence = int(match.group("sequence"))
        expected = self.token_for(family_id, sequence).rsplit(".", 1)[1]
        proof = match.group("proof")
        if not hmac.compare_digest(proof, expected):
            return None
        return ParsedRefreshToken(
            family_id=family_id,
            sequence=sequence,
            proof=proof,
        )


@dataclass(frozen=True)
class SessionIdentity:
    github_user_id: int
    github_login: str

    def __post_init__(self) -> None:
        if isinstance(self.github_user_id, bool) or self.github_user_id <= 0:
            raise ValueError("GitHub identity requires a positive numeric user ID")
        login = self.github_login.strip().casefold()
        if not login:
            raise ValueError("GitHub identity requires a login")
        object.__setattr__(self, "github_login", login)


SessionStatus = Literal["active", "revoked"]


@dataclass(frozen=True)
class SessionRecord:
    schema_version: int
    session_id: str
    token_digest: str
    client_id: str
    scopes: tuple[str, ...]
    issuer: str
    audience: str
    github_user_id: int
    github_login: str
    issued_at: float
    generation: str
    status: SessionStatus
    expires_at: float | None = None
    family_id: str | None = None
    revoked_at: float | None = None
    revocation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["scopes"] = list(self.scopes)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SessionRecord:
        try:
            record = cls(
                schema_version=int(value["schema_version"]),
                session_id=str(value["session_id"]),
                token_digest=str(value["token_digest"]),
                client_id=str(value["client_id"]),
                scopes=tuple(str(scope) for scope in value["scopes"]),
                issuer=str(value["issuer"]),
                audience=str(value["audience"]),
                github_user_id=int(value["github_user_id"]),
                github_login=str(value["github_login"]),
                issued_at=float(value["issued_at"]),
                generation=str(value["generation"]),
                status=str(value["status"]),  # type: ignore[arg-type]
                expires_at=(
                    None if value.get("expires_at") is None else float(value["expires_at"])
                ),
                family_id=(
                    None if value.get("family_id") is None else str(value["family_id"])
                ),
                revoked_at=(
                    None if value.get("revoked_at") is None else float(value["revoked_at"])
                ),
                revocation_reason=(
                    None
                    if value.get("revocation_reason") is None
                    else str(value["revocation_reason"])
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid session record") from error
        if not record.structurally_valid():
            raise ValueError("invalid session record")
        return record

    def structurally_valid(self) -> bool:
        if self.schema_version not in {_SCHEMA_VERSION, _ACCESS_SCHEMA_VERSION}:
            return False
        if SessionTokenCodec.parse(f"{_TOKEN_VERSION}.{self.session_id}.{'a' * 43}") is None:
            return False
        if not re.fullmatch(r"[0-9a-f]{64}", self.token_digest):
            return False
        if not self.client_id or not self.issuer or not self.audience or not self.generation:
            return False
        if any(not scope for scope in self.scopes) or len(set(self.scopes)) != len(self.scopes):
            return False
        if self.github_user_id <= 0 or self.github_login != self.github_login.strip().casefold():
            return False
        if not self.github_login or not math.isfinite(self.issued_at) or self.issued_at <= 0:
            return False
        if self.schema_version == _SCHEMA_VERSION:
            if self.expires_at is not None or self.family_id is not None:
                return False
        elif (
            self.expires_at is None
            or not math.isfinite(self.expires_at)
            or self.expires_at <= self.issued_at
            or self.family_id is None
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", self.family_id)
        ):
            return False
        if self.status == "active":
            return self.revoked_at is None and self.revocation_reason is None
        if self.status == "revoked":
            return (
                self.revoked_at is not None
                and math.isfinite(self.revoked_at)
                and bool(self.revocation_reason)
            )
        return False


@dataclass(frozen=True)
class RefreshFamilyRecord:
    schema_version: int
    family_id: str
    client_id: str
    scopes: tuple[str, ...]
    issuer: str
    audience: str
    github_user_id: int
    github_login: str
    created_at: float
    generation: str
    status: SessionStatus
    revoked_at: float | None = None
    revocation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["scopes"] = list(self.scopes)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RefreshFamilyRecord:
        try:
            record = cls(
                schema_version=int(value["schema_version"]),
                family_id=str(value["family_id"]),
                client_id=str(value["client_id"]),
                scopes=tuple(str(scope) for scope in value["scopes"]),
                issuer=str(value["issuer"]),
                audience=str(value["audience"]),
                github_user_id=int(value["github_user_id"]),
                github_login=str(value["github_login"]),
                created_at=float(value["created_at"]),
                generation=str(value["generation"]),
                status=str(value["status"]),  # type: ignore[arg-type]
                revoked_at=(
                    None if value.get("revoked_at") is None else float(value["revoked_at"])
                ),
                revocation_reason=(
                    None
                    if value.get("revocation_reason") is None
                    else str(value["revocation_reason"])
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid refresh family record") from error
        if not record.structurally_valid():
            raise ValueError("invalid refresh family record")
        return record

    def structurally_valid(self) -> bool:
        if self.schema_version != _ACCESS_SCHEMA_VERSION:
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", self.family_id):
            return False
        if not self.client_id or not self.issuer or not self.audience or not self.generation:
            return False
        if any(not scope for scope in self.scopes) or len(set(self.scopes)) != len(self.scopes):
            return False
        if self.github_user_id <= 0 or self.github_login != self.github_login.strip().casefold():
            return False
        if not self.github_login or not math.isfinite(self.created_at) or self.created_at <= 0:
            return False
        if self.status == "active":
            return self.revoked_at is None and self.revocation_reason is None
        if self.status == "revoked":
            return (
                self.revoked_at is not None
                and math.isfinite(self.revoked_at)
                and bool(self.revocation_reason)
            )
        return False


@dataclass(frozen=True)
class RefreshRedemptionRecord:
    schema_version: int
    family_id: str
    sequence: int
    claim_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RefreshRedemptionRecord:
        try:
            record = cls(
                schema_version=int(value["schema_version"]),
                family_id=str(value["family_id"]),
                sequence=int(value["sequence"]),
                claim_id=str(value["claim_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid refresh redemption record") from error
        if (
            record.schema_version != _ACCESS_SCHEMA_VERSION
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", record.family_id)
            or record.sequence < 0
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", record.claim_id)
        ):
            raise ValueError("invalid refresh redemption record")
        return record


@dataclass(frozen=True)
class RefreshGrant:
    family_id: str
    sequence: int
    client_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class GenerationRecord:
    schema_version: int
    generation: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GenerationRecord:
        try:
            record = cls(
                schema_version=int(value["schema_version"]),
                generation=str(value["generation"]),
                created_at=float(value["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid generation record") from error
        if (
            record.schema_version != _SCHEMA_VERSION
            or not record.generation
            or not math.isfinite(record.created_at)
            or record.created_at <= 0
        ):
            raise ValueError("invalid generation record")
        return record


class _EncryptedStorage:
    def __init__(self, storage: Any, storage_key: bytes):
        self.raw = storage
        self._fernet = Fernet(base64.urlsafe_b64encode(storage_key))

    @staticmethod
    def _unavailable(action: str, error: Exception) -> SessionStoreUnavailable:
        return SessionStoreUnavailable(f"session store {action} unavailable: {error}")

    def _encrypt(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            payload = json.dumps(dict(value), separators=(",", ":"), sort_keys=True).encode()
            ciphertext = self._fernet.encrypt(payload).decode("ascii")
        except Exception as error:  # pragma: no cover - defensive cipher boundary
            raise SessionStoreUnavailable("session record encryption failed") from error
        return {"__encrypted_data__": ciphertext, "__encryption_version__": 1}

    def _decrypt(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if value.get("__encryption_version__") != 1:
                raise ValueError("unsupported encryption version")
            ciphertext = value["__encrypted_data__"]
            if not isinstance(ciphertext, str):
                raise TypeError("ciphertext must be text")
            decoded = self._fernet.decrypt(ciphertext.encode("ascii"))
            result = json.loads(decoded)
            if not isinstance(result, dict):
                raise TypeError("decrypted record must be an object")
            return result
        except (InvalidToken, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SessionStoreUnavailable("session record decrypt failed") from error

    async def get(self, key: str, *, collection: str) -> dict[str, Any] | None:
        try:
            value = await self.raw.get(key, collection=collection)
        except Exception as error:
            raise self._unavailable("read", error) from error
        return None if value is None else self._decrypt(value)

    async def put(self, key: str, value: Mapping[str, Any], *, collection: str) -> None:
        encrypted = self._encrypt(value)
        try:
            await self.raw.put(key, encrypted, collection=collection)
        except Exception as error:
            raise self._unavailable("write", error) from error

    async def put_if_absent(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str,
        ttl: float | None = None,
    ) -> bool:
        encrypted = self._encrypt(value)
        try:
            operation = getattr(self.raw, "put_if_absent", None)
            if operation is None:
                raise SessionStoreUnavailable(
                    "session store lacks atomic put-if-absent support"
                )
            return bool(
                await operation(
                    key,
                    encrypted,
                    collection=collection,
                    ttl=ttl,
                )
            )
        except Exception as error:
            if isinstance(error, SessionStoreUnavailable):
                raise
            raise self._unavailable("atomic write", error) from error

    async def list_keys(self, *, collection: str) -> list[str]:
        operation = getattr(self.raw, "list_keys", None)
        if operation is None:
            operation = getattr(self.raw, "keys", None)
        if operation is None:
            raise SessionStoreUnavailable("session store does not support key enumeration")
        try:
            keys = await operation(collection=collection)
            return [str(key) for key in keys]
        except Exception as error:
            raise self._unavailable("enumeration", error) from error


class _InterprocessFileLock:
    """Portable advisory lock automatically released when its process exits."""

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 10.0,
    ):
        self.path = path
        self.timeout = timeout
        self._handle: BinaryIO | None = None

    def _try_acquire(self) -> bool:
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows deployments
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        self._handle = handle
        return True

    async def __aenter__(self) -> _InterprocessFileLock:
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_acquire():
                return self
            if time.monotonic() >= deadline:
                raise SessionStoreUnavailable(
                    "timed out acquiring the local session-store lock"
                ) from None
            await asyncio.sleep(0.01)

    async def __aexit__(self, *_: object) -> None:
        if self._handle is None:
            return
        if os.name == "nt":  # pragma: no cover - exercised on Windows deployments
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class _LocalFileBackend:
    """FileTreeStore with single-node atomic initialization and key listing."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_directory = self.directory / ".exomem-session-locks"
        self._lock_directory.mkdir(parents=True, exist_ok=True)
        self.store = FileTreeStore(data_directory=self.directory)

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        return await self.store.get(key, collection=collection)

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        await self.store.put(key, value, collection=collection, ttl=ttl)

    async def put_if_absent(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> bool:
        collection_name = collection or "default"
        lock_name = hashlib.sha256(f"{collection_name}\0{key}".encode()).hexdigest()
        lock = _InterprocessFileLock(self._lock_directory / f"{lock_name}.lock")
        async with lock:
            if await self.store.get(key, collection=collection) is not None:
                return False
            await self.store.put(key, value, collection=collection, ttl=ttl)
            return True

    async def list_keys(self, *, collection: str | None = None) -> list[str]:
        collection_name = collection or "default"
        directory = self.directory / collection_name
        if not directory.exists():
            return []
        return sorted(path.stem for path in directory.glob("*.json") if path.is_file())


class SessionAuthority:
    """Authoritative encrypted store for durable Exomem MCP sessions."""

    def __init__(
        self,
        *,
        storage: Any,
        signing_root: str,
        issuer: str,
        audience: str,
        clock: Callable[[], float] = time.time,
        validation_cache: SessionValidationCache | None = None,
        stale_grace_seconds: float = 0.0,
        session_store_telemetry: SessionStoreTelemetry | None = None,
    ):
        if not issuer or not audience:
            raise ValueError("session issuer and audience are required")
        if not math.isfinite(stale_grace_seconds) or stale_grace_seconds < 0:
            raise ValueError("session stale grace must be a non-negative finite number")
        keys = derive_session_keys(signing_root)
        self.fingerprint = keys.fingerprint
        self.issuer = issuer
        self.audience = audience
        self.clock = clock
        self.codec = SessionTokenCodec(keys.hmac_key)
        self.access_codec = AccessTokenCodec(keys.access_hmac_key)
        self.refresh_codec = RefreshTokenCodec(keys.refresh_hmac_key)
        self.sessions_collection = f"exomem-auth-sessions-v1-{keys.fingerprint}"
        self.generations_collection = f"exomem-auth-generations-v1-{keys.fingerprint}"
        self.refresh_families_collection = (
            f"exomem-auth-refresh-families-v2-{keys.fingerprint}"
        )
        self.refresh_redemptions_collection = (
            f"exomem-auth-refresh-redemptions-v2-{keys.fingerprint}"
        )
        self.refresh_grace_collection = (
            f"exomem-auth-refresh-grace-v2-{keys.fingerprint}"
        )
        self._storage = _EncryptedStorage(storage, keys.storage_key)
        self._validation_cache = validation_cache
        self._stale_grace_seconds = float(stale_grace_seconds)
        self._session_store_telemetry = (
            session_store_telemetry or _default_session_store_telemetry
        )

    @classmethod
    def local(
        cls,
        *,
        directory: Path,
        signing_root: str,
        issuer: str,
        audience: str,
        clock: Callable[[], float] = time.time,
    ) -> SessionAuthority:
        return cls(
            storage=_LocalFileBackend(directory),
            signing_root=signing_root,
            issuer=issuer,
            audience=audience,
            clock=clock,
        )

    @classmethod
    def remote(
        cls,
        *,
        url: str,
        namespace: str,
        storage_token: str,
        signing_root: str,
        issuer: str,
        audience: str,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        validation_cache: SessionValidationCache | None = None,
        stale_grace_seconds: float = 0.0,
        session_store_telemetry: SessionStoreTelemetry | None = None,
    ) -> SessionAuthority:
        if not storage_token or not storage_token.strip():
            raise ValueError("a non-empty OAuth storage token is required for HA sessions")
        storage = RemoteOAuthStorage(
            url=url,
            namespace=namespace,
            token=storage_token.strip(),
            timeout=timeout,
            cache_ttl=0,
            transport=transport,
        )
        return cls(
            storage=storage,
            signing_root=signing_root,
            issuer=issuer,
            audience=audience,
            clock=clock,
            validation_cache=validation_cache,
            stale_grace_seconds=stale_grace_seconds,
            session_store_telemetry=session_store_telemetry,
        )

    def _new_generation(self) -> GenerationRecord:
        return GenerationRecord(
            schema_version=_SCHEMA_VERSION,
            generation=secrets.token_urlsafe(24),
            created_at=float(self.clock()),
        )

    async def current_generation(self) -> str:
        raw = await self._storage.get(
            _GENERATION_KEY, collection=self.generations_collection
        )
        if raw is not None:
            try:
                return GenerationRecord.from_dict(raw).generation
            except ValueError as error:
                raise SessionStoreUnavailable("session generation record is corrupt") from error

        proposed = self._new_generation()
        created = await self._storage.put_if_absent(
            _GENERATION_KEY,
            proposed.to_dict(),
            collection=self.generations_collection,
        )
        if created:
            return proposed.generation
        raw = await self._storage.get(
            _GENERATION_KEY, collection=self.generations_collection
        )
        if raw is None:
            raise SessionStoreUnavailable("session generation disappeared during initialization")
        try:
            return GenerationRecord.from_dict(raw).generation
        except ValueError as error:
            raise SessionStoreUnavailable("session generation record is corrupt") from error

    async def replace_generation(self) -> str:
        replacement = self._new_generation()
        await self._storage.put(
            _GENERATION_KEY,
            replacement.to_dict(),
            collection=self.generations_collection,
        )
        if self._validation_cache is not None:
            self._validation_cache.clear()
        return replacement.generation

    @staticmethod
    def _normalize_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(scope) for scope in scopes))
        if any(not scope for scope in normalized):
            raise ValueError("session scopes must be non-empty")
        return normalized

    async def issue(
        self,
        *,
        client_id: str,
        scopes: Sequence[str],
        identity: SessionIdentity,
    ) -> tuple[str, SessionRecord]:
        if not client_id:
            raise ValueError("session client ID is required")
        normalized_scopes = self._normalize_scopes(scopes)
        if not isinstance(identity, SessionIdentity):
            raise TypeError("session identity is required")

        for _ in range(_MAX_ISSUANCE_ATTEMPTS):
            generation = await self.current_generation()
            bearer, session_id, digest = self.codec.issue()
            record = SessionRecord(
                schema_version=_SCHEMA_VERSION,
                session_id=session_id,
                token_digest=digest,
                client_id=client_id,
                scopes=normalized_scopes,
                issuer=self.issuer,
                audience=self.audience,
                github_user_id=identity.github_user_id,
                github_login=identity.github_login,
                issued_at=float(self.clock()),
                generation=generation,
                status="active",
            )
            created = await self._storage.put_if_absent(
                session_id,
                record.to_dict(),
                collection=self.sessions_collection,
            )
            if not created:  # astronomically unlikely random session-ID collision
                continue
            observed_generation = await self.current_generation()
            if observed_generation == generation:
                return bearer, record
            await self._write_tombstone(record, reason="generation-changed-during-issuance")
        raise SessionStoreUnavailable("could not issue a generation-stable session")

    async def _load_family(self, family_id: str) -> RefreshFamilyRecord | None:
        raw = await self._storage.get(
            family_id,
            collection=self.refresh_families_collection,
        )
        if raw is None:
            return None
        try:
            family = RefreshFamilyRecord.from_dict(raw)
        except ValueError as error:
            raise SessionStoreUnavailable("refresh family record is corrupt") from error
        if family.family_id != family_id:
            raise SessionStoreUnavailable(
                "refresh family record does not match its authoritative storage key"
            )
        return family

    async def _family_is_active(self, family: RefreshFamilyRecord) -> bool:
        return (
            family.status == "active"
            and family.issuer == self.issuer
            and family.audience == self.audience
            and family.generation == await self.current_generation()
        )

    async def _issue_access_for_family(
        self,
        family: RefreshFamilyRecord,
        *,
        scopes: Sequence[str],
    ) -> tuple[str, SessionRecord]:
        normalized_scopes = self._normalize_scopes(scopes)
        if any(scope not in family.scopes for scope in normalized_scopes):
            raise InvalidRefreshToken("refresh scopes exceed the family grant")
        for _ in range(_MAX_ISSUANCE_ATTEMPTS):
            issued_at = float(self.clock())
            bearer, session_id, digest = self.access_codec.issue()
            record = SessionRecord(
                schema_version=_ACCESS_SCHEMA_VERSION,
                session_id=session_id,
                token_digest=digest,
                client_id=family.client_id,
                scopes=normalized_scopes,
                issuer=family.issuer,
                audience=family.audience,
                github_user_id=family.github_user_id,
                github_login=family.github_login,
                issued_at=issued_at,
                generation=family.generation,
                status="active",
                expires_at=issued_at + ACCESS_TOKEN_TTL_SECONDS,
                family_id=family.family_id,
            )
            created = await self._storage.put_if_absent(
                session_id,
                record.to_dict(),
                collection=self.sessions_collection,
            )
            if not created:
                continue
            current_family = await self._load_family(family.family_id)
            if current_family is not None and await self._family_is_active(current_family):
                return bearer, record
            await self._write_tombstone(record, reason="refresh-family-inactive")
            raise InvalidRefreshToken("refresh family is inactive")
        raise SessionStoreUnavailable("could not issue an OAuth access token")

    async def issue_offline(
        self,
        *,
        client_id: str,
        scopes: Sequence[str],
        identity: SessionIdentity,
    ) -> tuple[str, SessionRecord, str]:
        """Issue a one-hour access token and durable refresh family."""
        if not client_id:
            raise ValueError("session client ID is required")
        normalized_scopes = self._normalize_scopes(scopes)
        if "offline_access" not in normalized_scopes:
            raise ValueError("offline issuance requires the offline_access scope")
        if not isinstance(identity, SessionIdentity):
            raise TypeError("session identity is required")

        for _ in range(_MAX_ISSUANCE_ATTEMPTS):
            generation = await self.current_generation()
            refresh_token, family_id = self.refresh_codec.issue()
            family = RefreshFamilyRecord(
                schema_version=_ACCESS_SCHEMA_VERSION,
                family_id=family_id,
                client_id=client_id,
                scopes=normalized_scopes,
                issuer=self.issuer,
                audience=self.audience,
                github_user_id=identity.github_user_id,
                github_login=identity.github_login,
                created_at=float(self.clock()),
                generation=generation,
                status="active",
            )
            created = await self._storage.put_if_absent(
                family_id,
                family.to_dict(),
                collection=self.refresh_families_collection,
            )
            if not created:
                continue
            if generation != await self.current_generation():
                await self._revoke_family(family_id, reason="generation-changed-during-issuance")
                continue
            try:
                access_token, access_record = await self._issue_access_for_family(
                    family,
                    scopes=normalized_scopes,
                )
            except InvalidRefreshToken:
                continue
            return access_token, access_record, refresh_token
        raise SessionStoreUnavailable("could not issue a generation-stable refresh family")

    async def validate_refresh(
        self,
        bearer: str,
        *,
        client_id: str,
    ) -> RefreshGrant | None:
        parsed = self.refresh_codec.parse(bearer)
        if parsed is None:
            return None
        family = await self._load_family(parsed.family_id)
        if (
            family is None
            or family.client_id != client_id
            or not await self._family_is_active(family)
        ):
            return None
        return RefreshGrant(
            family_id=family.family_id,
            sequence=parsed.sequence,
            client_id=family.client_id,
            scopes=family.scopes,
        )

    async def rotate_refresh(
        self,
        bearer: str,
        *,
        client_id: str,
        scopes: Sequence[str],
    ) -> tuple[str, SessionRecord, str]:
        grant = await self.validate_refresh(bearer, client_id=client_id)
        if grant is None:
            raise InvalidRefreshToken("refresh token is invalid or inactive")
        normalized_scopes = self._normalize_scopes(scopes)
        if any(scope not in grant.scopes for scope in normalized_scopes):
            raise InvalidRefreshToken("refresh scopes exceed the family grant")

        receipt_key = f"{grant.family_id}.{grant.sequence}"

        def parse_redemption(
            raw: Mapping[str, Any],
            *,
            label: str,
        ) -> RefreshRedemptionRecord:
            try:
                record = RefreshRedemptionRecord.from_dict(raw)
            except ValueError as error:
                raise SessionStoreUnavailable(f"refresh {label} record is corrupt") from error
            if record.family_id != grant.family_id or record.sequence != grant.sequence:
                raise SessionStoreUnavailable(
                    f"refresh {label} does not match its authoritative storage key"
                )
            return record

        raw_receipt = await self._storage.get(
            receipt_key,
            collection=self.refresh_redemptions_collection,
        )
        if raw_receipt is None:
            proposed = RefreshRedemptionRecord(
                schema_version=_ACCESS_SCHEMA_VERSION,
                family_id=grant.family_id,
                sequence=grant.sequence,
                claim_id=secrets.token_urlsafe(18),
            )
            grace_created = await self._storage.put_if_absent(
                receipt_key,
                proposed.to_dict(),
                collection=self.refresh_grace_collection,
                ttl=REFRESH_RETRY_GRACE_SECONDS,
            )
            if grace_created:
                grace = proposed
            else:
                raw_grace = await self._storage.get(
                    receipt_key,
                    collection=self.refresh_grace_collection,
                )
                if raw_grace is None:
                    raise SessionStoreUnavailable(
                        "refresh grace disappeared during an atomic claim"
                    )
                grace = parse_redemption(raw_grace, label="grace")
            receipt_created = await self._storage.put_if_absent(
                receipt_key,
                grace.to_dict(),
                collection=self.refresh_redemptions_collection,
            )
            if receipt_created:
                receipt = grace
            else:
                raw_receipt = await self._storage.get(
                    receipt_key,
                    collection=self.refresh_redemptions_collection,
                )
                if raw_receipt is None:
                    raise SessionStoreUnavailable(
                        "refresh redemption disappeared after an atomic claim"
                    )
                receipt = parse_redemption(raw_receipt, label="redemption")
        else:
            receipt = parse_redemption(raw_receipt, label="redemption")

        raw_grace = await self._storage.get(
            receipt_key,
            collection=self.refresh_grace_collection,
        )
        grace = (
            None
            if raw_grace is None
            else parse_redemption(raw_grace, label="grace")
        )
        if grace is None or grace.claim_id != receipt.claim_id:
            await self._revoke_family(
                grant.family_id,
                reason="refresh-token-reuse",
            )
            raise InvalidRefreshToken("refresh token reuse detected")

        family = await self._load_family(grant.family_id)
        if family is None or not await self._family_is_active(family):
            raise InvalidRefreshToken("refresh family is inactive")
        access_token, access_record = await self._issue_access_for_family(
            family,
            scopes=normalized_scopes,
        )
        next_refresh = self.refresh_codec.token_for(
            grant.family_id,
            grant.sequence + 1,
        )
        return access_token, access_record, next_refresh

    async def _load_session_for_bearer(self, bearer: str) -> SessionRecord | None:
        parsed = self.codec.parse(bearer)
        codec: SessionTokenCodec = self.codec
        expected_schema = _SCHEMA_VERSION
        if parsed is None:
            parsed = self.access_codec.parse(bearer)
            codec = self.access_codec
            expected_schema = _ACCESS_SCHEMA_VERSION
        if parsed is None:
            return None
        raw = await self._storage.get(parsed.session_id, collection=self.sessions_collection)
        if raw is None:
            return None
        try:
            record = SessionRecord.from_dict(raw)
        except ValueError:
            return None
        if record.session_id != parsed.session_id:
            raise SessionStoreUnavailable(
                "session record does not match its authoritative storage key"
            )
        if record.schema_version != expected_schema or not codec.verify(
            bearer, record.token_digest
        ):
            return None
        return record

    async def _validate_authoritatively(self, bearer: str) -> SessionRecord | None:
        record = await self._load_session_for_bearer(bearer)
        if record is None:
            return None
        if (
            record.status != "active"
            or record.issuer != self.issuer
            or record.audience != self.audience
        ):
            return None
        if record.generation != await self.current_generation():
            return None
        if record.schema_version == _ACCESS_SCHEMA_VERSION:
            if record.expires_at is None or float(self.clock()) >= record.expires_at:
                return None
            if record.family_id is None:
                return None
            family = await self._load_family(record.family_id)
            if family is None or not await self._family_is_active(family):
                return None
        return record

    @staticmethod
    def _remote_failure_allows_stale(error: SessionStoreUnavailable) -> bool:
        cause: BaseException | None = error
        while cause is not None:
            if isinstance(cause, httpx.HTTPStatusError):
                return cause.response.status_code >= 500
            if isinstance(
                cause,
                (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    ConnectionError,
                    TimeoutError,
                ),
            ):
                return True
            cause = cause.__cause__
        return False

    def _cached_record_is_eligible(self, bearer: str, record: SessionRecord) -> bool:
        if (
            record.status != "active"
            or record.issuer != self.issuer
            or record.audience != self.audience
        ):
            return False
        codec: SessionTokenCodec = self.codec
        if record.schema_version == _ACCESS_SCHEMA_VERSION:
            codec = self.access_codec
            if record.expires_at is None or float(self.clock()) >= record.expires_at:
                return False
            if record.family_id is None:
                return False
        return codec.verify(bearer, record.token_digest)

    async def validate(self, bearer: str) -> SessionRecord | None:
        if self._validation_cache is None:
            return await self._validate_authoritatively(bearer)
        bearer_is_parseable = (
            self.codec.parse(bearer) is not None
            or self.access_codec.parse(bearer) is not None
        )
        try:
            record = await self._validate_authoritatively(bearer)
        except SessionStoreUnavailable as error:
            if not self._remote_failure_allows_stale(error):
                raise
            stale_active = self._stale_grace_seconds > 0
            self._session_store_telemetry.remote_unavailable(stale_active=stale_active)
            if not stale_active:
                raise
            cached = self._validation_cache.get(bearer)
            if cached is None:
                raise
            age = float(self.clock()) - cached.validated_at
            if age < 0 or age > self._stale_grace_seconds:
                raise
            try:
                record = SessionRecord.from_dict(cached.claims)
            except ValueError:
                raise error from None
            if not self._cached_record_is_eligible(bearer, record):
                self._validation_cache.delete(bearer)
                raise error
            count = self._session_store_telemetry.stale_served()
            logger.warning("event=session_stale_served count=%d", count)
            return record

        if bearer_is_parseable:
            self._session_store_telemetry.remote_ok()
        if record is None:
            self._validation_cache.delete(bearer)
        else:
            try:
                self._validation_cache.upsert(
                    bearer,
                    record.to_dict(),
                    validated_at=float(self.clock()),
                )
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("event=session_validation_cache_write_failed")
        return record

    async def _write_tombstone(self, record: SessionRecord, *, reason: str) -> SessionRecord:
        tombstone = replace(
            record,
            status="revoked",
            revoked_at=float(self.clock()),
            revocation_reason=reason,
        )
        await self._storage.put(
            record.session_id,
            tombstone.to_dict(),
            collection=self.sessions_collection,
        )
        if self._validation_cache is not None:
            self._validation_cache.delete_session(record.session_id)
        return tombstone

    async def _revoke_family(self, family_id: str, *, reason: str) -> bool:
        if not family_id or not reason:
            raise ValueError("refresh family ID and revocation reason are required")
        family = await self._load_family(family_id)
        if family is None:
            return False
        if family.status == "revoked":
            if self._validation_cache is not None:
                self._validation_cache.delete_family(family_id)
            return True
        tombstone = replace(
            family,
            status="revoked",
            revoked_at=float(self.clock()),
            revocation_reason=reason,
        )
        await self._storage.put(
            family.family_id,
            tombstone.to_dict(),
            collection=self.refresh_families_collection,
        )
        if self._validation_cache is not None:
            self._validation_cache.delete_family(family_id)
        return True

    async def revoke_bearer(self, bearer: str, *, reason: str) -> bool:
        """Revoke a legacy session or an entire v2 access/refresh family."""
        if not reason:
            raise ValueError("revocation reason is required")
        parsed_refresh = self.refresh_codec.parse(bearer)
        if parsed_refresh is not None:
            revoked = await self._revoke_family(parsed_refresh.family_id, reason=reason)
            if self._validation_cache is not None:
                self._validation_cache.delete_family(parsed_refresh.family_id)
            return revoked
        record = await self._load_session_for_bearer(bearer)
        if record is None:
            if self._validation_cache is not None:
                self._validation_cache.delete(bearer)
            return False
        if record.family_id is not None:
            await self._revoke_family(record.family_id, reason=reason)
        if record.status != "revoked":
            await self._write_tombstone(record, reason=reason)
        if self._validation_cache is not None:
            self._validation_cache.delete(bearer)
        return True

    async def tombstone(self, session_id: str, *, reason: str) -> bool:
        if not session_id or not reason:
            raise ValueError("session ID and revocation reason are required")
        raw = await self._storage.get(session_id, collection=self.sessions_collection)
        if raw is None:
            return False
        try:
            record = SessionRecord.from_dict(raw)
        except ValueError as error:
            raise SessionStoreUnavailable("session record is corrupt") from error
        if record.session_id != session_id:
            raise SessionStoreUnavailable(
                "session record does not match its authoritative storage key"
            )
        if record.status == "revoked":
            if self._validation_cache is not None:
                self._validation_cache.delete_session(session_id)
            return True
        if record.family_id is not None:
            await self._revoke_family(record.family_id, reason=reason)
        await self._write_tombstone(record, reason=reason)
        return True

    async def list_sessions(self) -> list[SessionRecord]:
        keys = await self._storage.list_keys(collection=self.sessions_collection)
        records: list[SessionRecord] = []
        for key in keys:
            raw = await self._storage.get(key, collection=self.sessions_collection)
            if raw is None:
                continue
            try:
                record = SessionRecord.from_dict(raw)
            except ValueError as error:
                raise SessionStoreUnavailable("session record is corrupt") from error
            if record.session_id != key:
                raise SessionStoreUnavailable(
                    "session record does not match its authoritative storage key"
                )
            records.append(record)
        return sorted(records, key=lambda record: (record.issued_at, record.session_id))
