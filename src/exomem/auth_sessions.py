"""Durable, Exomem-owned MCP session records.

GitHub proves identity before this module is called.  Session validation is
deliberately local to Exomem and never retains or revalidates an upstream token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from key_value.aio.stores.filetree import FileTreeStore

from .remote_oauth_storage import RemoteOAuthStorage

_TOKEN_VERSION = "exo_s1"
_SCHEMA_VERSION = 1
_TOKEN_PATTERN = re.compile(
    rf"^{_TOKEN_VERSION}\.(?P<session_id>[A-Za-z0-9_-]{{16,64}})\."
    r"(?P<secret>[A-Za-z0-9_-]{43,128})$"
)
_GENERATION_KEY = "current"
_MAX_ISSUANCE_ATTEMPTS = 8


class SessionStoreUnavailable(RuntimeError):
    """The authoritative session store or its cipher could not be used."""


@dataclass(frozen=True)
class SessionKeys:
    hmac_key: bytes
    storage_key: bytes
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
        storage_key=storage_key,
        fingerprint=hashlib.sha256(fingerprint_key).hexdigest()[:16],
    )


@dataclass(frozen=True)
class ParsedSessionToken:
    session_id: str
    secret: str


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
        if self.schema_version != _SCHEMA_VERSION:
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
        self._fallback_lock = asyncio.Lock()

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
        self, key: str, value: Mapping[str, Any], *, collection: str
    ) -> bool:
        encrypted = self._encrypt(value)
        try:
            operation = getattr(self.raw, "put_if_absent", None)
            if operation is not None:
                return bool(await operation(key, encrypted, collection=collection))
            async with self._fallback_lock:
                if await self.raw.get(key, collection=collection) is not None:
                    return False
                await self.raw.put(key, encrypted, collection=collection)
                return True
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


_LOCAL_ATOMIC_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}


class _LocalFileBackend:
    """FileTreeStore with single-node atomic initialization and key listing."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
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
        lock_key = (str(self.directory), collection_name, key)
        lock = _LOCAL_ATOMIC_LOCKS.setdefault(lock_key, asyncio.Lock())
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
    ):
        if not issuer or not audience:
            raise ValueError("session issuer and audience are required")
        self.keys = derive_session_keys(signing_root)
        self.issuer = issuer
        self.audience = audience
        self.clock = clock
        self.codec = SessionTokenCodec(self.keys.hmac_key)
        self.sessions_collection = f"exomem-auth-sessions-v1-{self.keys.fingerprint}"
        self.generations_collection = f"exomem-auth-generations-v1-{self.keys.fingerprint}"
        self._storage = _EncryptedStorage(storage, self.keys.storage_key)

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
        return replacement.generation

    @staticmethod
    def _normalize_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(str(scope) for scope in scopes)
        if any(not scope for scope in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("session scopes must be non-empty and unique")
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

    async def validate(self, bearer: str) -> SessionRecord | None:
        parsed = self.codec.parse(bearer)
        if parsed is None:
            return None
        raw = await self._storage.get(parsed.session_id, collection=self.sessions_collection)
        if raw is None:
            return None
        try:
            record = SessionRecord.from_dict(raw)
        except ValueError:
            return None
        if not self.codec.verify(bearer, record.token_digest):
            return None
        if (
            record.session_id != parsed.session_id
            or record.status != "active"
            or record.issuer != self.issuer
            or record.audience != self.audience
        ):
            return None
        if record.generation != await self.current_generation():
            return None
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
        return tombstone

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
        if record.status == "revoked":
            return True
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
                records.append(SessionRecord.from_dict(raw))
            except ValueError as error:
                raise SessionStoreUnavailable("session record is corrupt") from error
        return sorted(records, key=lambda record: (record.issued_at, record.session_id))
