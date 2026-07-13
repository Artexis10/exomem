from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from exomem.auth_sessions import (
    SessionAuthority,
    SessionIdentity,
    SessionStoreUnavailable,
    SessionTokenCodec,
    derive_session_keys,
)


class AtomicMemoryStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str | None, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str | None, str]] = []
        self.pause_after_session_put = False
        self.session_written = asyncio.Event()
        self.resume_session_put = asyncio.Event()
        self.fail_get = False
        self._lock = asyncio.Lock()

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        self.calls.append(("get", collection, key))
        if self.fail_get:
            raise OSError("store unavailable")
        value = self.data.get((collection, key))
        return dict(value) if value is not None else None

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        del ttl
        self.calls.append(("put", collection, key))
        self.data[(collection, key)] = dict(value)
        if self.pause_after_session_put and collection and "sessions" in collection:
            self.pause_after_session_put = False
            self.session_written.set()
            await self.resume_session_put.wait()

    async def put_if_absent(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> bool:
        del ttl
        async with self._lock:
            if (collection, key) in self.data:
                return False
            self.data[(collection, key)] = dict(value)
        if self.pause_after_session_put and collection and "sessions" in collection:
            self.pause_after_session_put = False
            self.session_written.set()
            await self.resume_session_put.wait()
        return True

    async def list_keys(self, *, collection: str | None = None) -> list[str]:
        return sorted(key for coll, key in self.data if coll == collection)


def _authority(
    store: AtomicMemoryStore,
    *,
    signing_root: str = "test-signing-root",
    issuer: str = "https://memory.example",
    audience: str = "https://memory.example/mcp",
) -> SessionAuthority:
    return SessionAuthority(
        storage=store,
        signing_root=signing_root,
        issuer=issuer,
        audience=audience,
        clock=lambda: 1_800_000_000.0,
    )


def _identity() -> SessionIdentity:
    return SessionIdentity(github_user_id=123456, github_login="Person")


def test_token_codec_issues_versioned_opaque_token_with_256_secret_bits() -> None:
    keys = derive_session_keys("explicit-root")
    codec = SessionTokenCodec(keys.hmac_key)

    bearer, session_id, digest = codec.issue()
    parsed = codec.parse(bearer)

    assert bearer.startswith("exo_s1.")
    assert parsed is not None and parsed.session_id == session_id
    secret = parsed.secret + "=" * (-len(parsed.secret) % 4)
    assert len(base64.urlsafe_b64decode(secret)) >= 32
    assert bearer not in digest
    assert codec.verify(bearer, digest)


def test_token_codec_rejects_malformed_and_uses_constant_time_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = derive_session_keys("explicit-root")
    codec = SessionTokenCodec(keys.hmac_key)
    bearer, _, digest = codec.issue()
    compared: list[tuple[str, str]] = []

    def recording_compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return left == right

    monkeypatch.setattr("exomem.auth_sessions.hmac.compare_digest", recording_compare)

    assert codec.parse("not-a-session") is None
    assert codec.parse("exo_s1.bad/identifier.secret") is None
    assert not codec.verify(bearer + "x", digest)
    assert compared and compared[-1][1] == digest


def test_key_derivation_is_purpose_separated_and_fingerprinted() -> None:
    first = derive_session_keys("explicit-root")
    same = derive_session_keys("explicit-root")
    rotated = derive_session_keys("rotated-root")

    assert first == same
    assert first.hmac_key != first.storage_key
    assert first.fingerprint != rotated.fingerprint
    assert "explicit-root" not in repr(first)


def test_identity_normalizes_login_and_rejects_invalid_values() -> None:
    assert SessionIdentity(github_user_id=7, github_login="  SomeUser  ").github_login == "someuser"
    with pytest.raises(ValueError, match="numeric"):
        SessionIdentity(github_user_id=0, github_login="person")
    with pytest.raises(ValueError, match="login"):
        SessionIdentity(github_user_id=7, github_login="   ")


@pytest.mark.anyio
async def test_issue_validate_list_and_tombstone_without_storing_bearer() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)

    bearer, issued = await authority.issue(
        client_id="codex",
        scopes=("exomem:read", "exomem:write"),
        identity=_identity(),
    )

    assert await authority.validate(bearer) == issued
    assert await authority.list_sessions() == [issued]
    assert issued.github_login == "person"
    assert issued.token_digest not in bearer
    assert bearer not in repr(raw.data)
    assert all("__encrypted_data__" in value for value in raw.data.values())

    assert await authority.tombstone(issued.session_id, reason="operator")
    assert await authority.validate(bearer) is None
    [revoked] = await authority.list_sessions()
    assert revoked.status == "revoked"
    assert revoked.revoked_at == 1_800_000_000.0
    assert revoked.revocation_reason == "operator"


@pytest.mark.anyio
async def test_validation_rejects_malformed_unknown_and_context_mismatch() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)
    bearer, _ = await authority.issue(
        client_id="codex", scopes=("exomem:read",), identity=_identity()
    )
    calls_before = len(raw.calls)

    assert await authority.validate("malformed") is None
    assert len(raw.calls) == calls_before
    unknown, _, _ = SessionTokenCodec(derive_session_keys("test-signing-root").hmac_key).issue()
    assert await authority.validate(unknown) is None
    assert await _authority(raw, issuer="https://other.example").validate(bearer) is None
    assert await _authority(raw, audience="https://other.example/mcp").validate(bearer) is None


@pytest.mark.anyio
async def test_signing_key_rotation_selects_fresh_namespace_without_decrypting_old_data() -> None:
    raw = AtomicMemoryStore()
    old = _authority(raw)
    bearer, issued = await old.issue(
        client_id="codex", scopes=("exomem:read",), identity=_identity()
    )
    old_collection = old.sessions_collection
    raw.data[(old_collection, issued.session_id)] = {"__encrypted_data__": "corrupt"}

    rotated = _authority(raw, signing_root="rotated-root")

    assert rotated.sessions_collection != old_collection
    assert await rotated.validate(bearer) is None
    assert not any(call[1] == old_collection for call in raw.calls[-2:])


@pytest.mark.anyio
async def test_store_and_cipher_failures_are_distinct_from_invalid_sessions() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)
    bearer, issued = await authority.issue(
        client_id="codex", scopes=("exomem:read",), identity=_identity()
    )

    raw.fail_get = True
    with pytest.raises(SessionStoreUnavailable, match="unavailable"):
        await authority.validate(bearer)
    raw.fail_get = False
    raw.data[(authority.sessions_collection, issued.session_id)] = {
        "__encrypted_data__": "not-fernet",
        "__encryption_version__": 1,
    }
    with pytest.raises(SessionStoreUnavailable, match="decrypt"):
        await authority.validate(bearer)


@pytest.mark.anyio
async def test_replace_generation_invalidates_every_existing_session() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)
    bearer, issued = await authority.issue(
        client_id="codex", scopes=("exomem:read",), identity=_identity()
    )

    replacement = await authority.replace_generation()

    assert replacement != issued.generation
    assert await authority.validate(bearer) is None


@pytest.mark.anyio
async def test_generation_initialization_is_atomic_across_authorities() -> None:
    raw = AtomicMemoryStore()
    first = _authority(raw)
    second = _authority(raw)

    (first_result, second_result) = await asyncio.gather(
        first.issue(client_id="a", scopes=("exomem:read",), identity=_identity()),
        second.issue(client_id="b", scopes=("exomem:read",), identity=_identity()),
    )

    assert first_result[1].generation == second_result[1].generation
    assert await first.validate(second_result[0]) == second_result[1]


@pytest.mark.anyio
async def test_issuance_retries_and_tombstones_when_revoke_all_wins_race() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)
    await authority.current_generation()
    raw.pause_after_session_put = True

    issue_task = asyncio.create_task(
        authority.issue(client_id="codex", scopes=("exomem:read",), identity=_identity())
    )
    await raw.session_written.wait()
    replacement = await authority.replace_generation()
    raw.resume_session_put.set()
    bearer, issued = await issue_task

    assert issued.generation == replacement
    assert await authority.validate(bearer) == issued
    sessions = await authority.list_sessions()
    assert [record.status for record in sessions].count("revoked") == 1
    assert [record.status for record in sessions].count("active") == 1


@pytest.mark.anyio
async def test_local_authority_encrypts_and_persists_sessions(tmp_path: Path) -> None:
    first = SessionAuthority.local(
        directory=tmp_path,
        signing_root="stable-root",
        issuer="https://memory.example",
        audience="https://memory.example/mcp",
        clock=lambda: 1_800_000_000.0,
    )
    bearer, issued = await first.issue(
        client_id="codex", scopes=("exomem:read",), identity=_identity()
    )
    second = SessionAuthority.local(
        directory=tmp_path,
        signing_root="stable-root",
        issuer="https://memory.example",
        audience="https://memory.example/mcp",
        clock=lambda: 1_800_000_000.0,
    )

    assert await second.validate(bearer) == issued
    assert await second.list_sessions() == [issued]
    disk_text = "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert bearer not in disk_text
    assert "person" not in disk_text
