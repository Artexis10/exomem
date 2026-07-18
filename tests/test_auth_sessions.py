from __future__ import annotations

import asyncio
import base64
import multiprocessing
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from exomem.auth_sessions import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_RETRY_GRACE_SECONDS,
    InvalidRefreshToken,
    SessionAuthority,
    SessionIdentity,
    SessionStoreUnavailable,
    SessionTokenCodec,
    _InterprocessFileLock,
    derive_session_keys,
)


class AtomicMemoryStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str | None, str], dict[str, Any]] = {}
        self.expiries: dict[tuple[str | None, str], float] = {}
        self.calls: list[tuple[str, str | None, str]] = []
        self.clock = time.time
        self.pause_after_session_put = False
        self.session_written = asyncio.Event()
        self.resume_session_put = asyncio.Event()
        self.fail_get = False
        self._lock = asyncio.Lock()

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        self.calls.append(("get", collection, key))
        if self.fail_get:
            raise OSError("store unavailable")
        storage_key = (collection, key)
        expires_at = self.expiries.get(storage_key)
        if expires_at is not None and self.clock() >= expires_at:
            self.data.pop(storage_key, None)
            self.expiries.pop(storage_key, None)
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
        self.calls.append(("put", collection, key))
        self.data[(collection, key)] = dict(value)
        if ttl is None:
            self.expiries.pop((collection, key), None)
        else:
            self.expiries[(collection, key)] = self.clock() + ttl
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
        async with self._lock:
            storage_key = (collection, key)
            expires_at = self.expiries.get(storage_key)
            if expires_at is not None and self.clock() >= expires_at:
                self.data.pop(storage_key, None)
                self.expiries.pop(storage_key, None)
            if (collection, key) in self.data:
                return False
            self.data[(collection, key)] = dict(value)
            if ttl is not None:
                self.expiries[(collection, key)] = self.clock() + ttl
        if self.pause_after_session_put and collection and "sessions" in collection:
            self.pause_after_session_put = False
            self.session_written.set()
            await self.resume_session_put.wait()
        return True

    async def list_keys(self, *, collection: str | None = None) -> list[str]:
        return sorted(key for coll, key in self.data if coll == collection)


class NonAtomicMemoryStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str | None, str], dict[str, Any]] = {}

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        return self.data.get((collection, key))

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        del ttl
        self.data[(collection, key)] = dict(value)

    async def list_keys(self, *, collection: str | None = None) -> list[str]:
        return sorted(key for coll, key in self.data if coll == collection)


def _multiprocess_generation_worker(
    directory: str,
    start: Any,
    barrier: Any,
    results: Any,
) -> None:
    async def run() -> None:
        authority = SessionAuthority.local(
            directory=Path(directory),
            signing_root="stable-root",
            issuer="https://memory.example",
            audience="https://memory.example/mcp",
        )
        backend = authority._storage.raw
        original_get = backend.get
        original_put = backend.store.put
        first_generation_read = True

        async def synchronized_get(
            key: str, *, collection: str | None = None
        ) -> dict[str, Any] | None:
            nonlocal first_generation_read
            value = await original_get(key, collection=collection)
            if first_generation_read and key == "current":
                first_generation_read = False
                await asyncio.to_thread(barrier.wait)
            return value

        async def delayed_put(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0.2)
            await original_put(*args, **kwargs)

        backend.get = synchronized_get
        backend.store.put = delayed_put
        await asyncio.to_thread(start.wait)
        results.put(await authority.current_generation())

    asyncio.run(run())


def _multiprocess_lock_holder(path: str, ready: Any, release: Any) -> None:
    async def run() -> None:
        async with _InterprocessFileLock(Path(path), timeout=2):
            ready.set()
            await asyncio.to_thread(release.wait)

    asyncio.run(run())


def _authority(
    store: AtomicMemoryStore,
    *,
    signing_root: str = "test-signing-root",
    issuer: str = "https://memory.example",
    audience: str = "https://memory.example/mcp",
    clock: Any = None,
) -> SessionAuthority:
    return SessionAuthority(
        storage=store,
        signing_root=signing_root,
        issuer=issuer,
        audience=audience,
        clock=clock or (lambda: 1_800_000_000.0),
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


def test_key_and_parsed_token_repr_do_not_expose_secret_material() -> None:
    keys = derive_session_keys("explicit-root")
    codec = SessionTokenCodec(keys.hmac_key)
    bearer, _, _ = codec.issue()
    parsed = codec.parse(bearer)

    assert parsed is not None
    assert "hmac_key=" not in repr(keys)
    assert "storage_key=" not in repr(keys)
    assert parsed.secret not in repr(parsed)


def test_session_authority_exposes_only_nonsecret_key_fingerprint() -> None:
    authority = _authority(AtomicMemoryStore())

    assert not hasattr(authority, "keys")
    assert authority.fingerprint == derive_session_keys("test-signing-root").fingerprint


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
async def test_literal_pre_0242_session_mapping_still_validates_lists_and_revokes() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)
    generation = await authority.current_generation()
    bearer, session_id, digest = authority.codec.issue()
    legacy_mapping = {
        "schema_version": 1,
        "session_id": session_id,
        "token_digest": digest,
        "client_id": "codex",
        "scopes": ["exomem:read"],
        "issuer": "https://memory.example",
        "audience": "https://memory.example/mcp",
        "github_user_id": 123456,
        "github_login": "person",
        "issued_at": 1_800_000_000.0,
        "generation": generation,
        "status": "active",
        "revoked_at": None,
        "revocation_reason": None,
    }
    assert "expires_at" not in legacy_mapping and "family_id" not in legacy_mapping
    await authority._storage.put(
        session_id,
        legacy_mapping,
        collection=authority.sessions_collection,
    )

    validated = await authority.validate(bearer)
    assert validated is not None and validated.schema_version == 1
    assert validated.expires_at is None and validated.family_id is None
    assert await authority.list_sessions() == [validated]
    assert await authority.tombstone(session_id, reason="operator")
    assert await authority.validate(bearer) is None


@pytest.mark.anyio
async def test_offline_issue_creates_one_hour_access_and_unstored_refresh_token() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)

    access, record, refresh = await authority.issue_offline(
        client_id="chatgpt",
        scopes=("offline_access", "exomem:read"),
        identity=_identity(),
    )

    assert access.startswith("exo_a2.")
    assert refresh.startswith("exo_r2.")
    assert record.schema_version == 2
    assert record.expires_at == record.issued_at + ACCESS_TOKEN_TTL_SECONDS
    assert record.family_id
    assert await authority.validate(access) == record
    grant = await authority.validate_refresh(refresh, client_id="chatgpt")
    assert grant is not None
    assert grant.scopes == ("offline_access", "exomem:read")
    assert access not in repr(raw.data)
    assert refresh not in repr(raw.data)
    assert all("__encrypted_data__" in value for value in raw.data.values())


@pytest.mark.anyio
async def test_refresh_rotation_is_cross_authority_idempotent_then_revokes_on_replay() -> None:
    raw = AtomicMemoryStore()
    now = [1_800_000_000.0]
    raw.clock = lambda: now[0]
    first = _authority(raw, clock=lambda: now[0])
    second = _authority(raw, clock=lambda: now[0])
    access, _, refresh = await first.issue_offline(
        client_id="chatgpt",
        scopes=("offline_access", "exomem:read"),
        identity=_identity(),
    )

    rotated_a, rotated_record_a, next_refresh_a = await first.rotate_refresh(
        refresh,
        client_id="chatgpt",
        scopes=("offline_access", "exomem:read"),
    )
    now[0] += REFRESH_RETRY_GRACE_SECONDS - 1
    rotated_b, rotated_record_b, next_refresh_b = await second.rotate_refresh(
        refresh,
        client_id="chatgpt",
        scopes=("offline_access", "exomem:read"),
    )

    assert next_refresh_a == next_refresh_b
    assert rotated_a != rotated_b
    assert await first.validate(rotated_a) == rotated_record_a
    assert await second.validate(rotated_b) == rotated_record_b

    now[0] += 2
    with pytest.raises(InvalidRefreshToken, match="reuse"):
        await second.rotate_refresh(
            refresh,
            client_id="chatgpt",
            scopes=("offline_access", "exomem:read"),
        )

    assert await first.validate(access) is None
    assert await first.validate(rotated_a) is None
    assert await first.validate_refresh(next_refresh_a, client_id="chatgpt") is None


@pytest.mark.anyio
async def test_refresh_grace_uses_store_time_not_replica_clocks() -> None:
    raw = AtomicMemoryStore()
    store_now = [1_800_000_000.0]
    raw.clock = lambda: store_now[0]
    ahead = _authority(raw, clock=lambda: store_now[0] + 3600)
    behind = _authority(raw, clock=lambda: store_now[0] - 3600)
    _, _, refresh = await ahead.issue_offline(
        client_id="chatgpt",
        scopes=("offline_access",),
        identity=_identity(),
    )

    first = await ahead.rotate_refresh(
        refresh,
        client_id="chatgpt",
        scopes=("offline_access",),
    )
    concurrent = await behind.rotate_refresh(
        refresh,
        client_id="chatgpt",
        scopes=("offline_access",),
    )
    assert first[2] == concurrent[2]

    store_now[0] += REFRESH_RETRY_GRACE_SECONDS + 1
    with pytest.raises(InvalidRefreshToken, match="reuse"):
        await behind.rotate_refresh(
            refresh,
            client_id="chatgpt",
            scopes=("offline_access",),
        )
    assert await ahead.validate(first[0]) is None


@pytest.mark.anyio
async def test_late_replay_wins_race_with_current_refresh_issuance() -> None:
    raw = AtomicMemoryStore()
    now = [1_800_000_000.0]
    raw.clock = lambda: now[0]
    first = _authority(raw, clock=lambda: now[0])
    second = _authority(raw, clock=lambda: now[0])
    _, _, old_refresh = await first.issue_offline(
        client_id="chatgpt",
        scopes=("offline_access",),
        identity=_identity(),
    )
    prior_access, _, current_refresh = await first.rotate_refresh(
        old_refresh,
        client_id="chatgpt",
        scopes=("offline_access",),
    )
    now[0] += REFRESH_RETRY_GRACE_SECONDS + 1
    raw.pause_after_session_put = True

    current_task = asyncio.create_task(
        second.rotate_refresh(
            current_refresh,
            client_id="chatgpt",
            scopes=("offline_access",),
        )
    )
    await raw.session_written.wait()
    with pytest.raises(InvalidRefreshToken, match="reuse"):
        await first.rotate_refresh(
            old_refresh,
            client_id="chatgpt",
            scopes=("offline_access",),
        )
    raw.resume_session_put.set()

    with pytest.raises(InvalidRefreshToken, match="inactive"):
        await current_task
    assert await first.validate(prior_access) is None
    assert await first.validate_refresh(current_refresh, client_id="chatgpt") is None
    family_records = [
        record for record in await first.list_sessions() if record.family_id is not None
    ]
    assert any(record.status == "revoked" for record in family_records)


@pytest.mark.anyio
async def test_offline_access_expiry_revocation_and_generation_fail_closed() -> None:
    raw = AtomicMemoryStore()
    now = [1_800_000_000.0]
    authority = _authority(raw, clock=lambda: now[0])
    access, record, refresh = await authority.issue_offline(
        client_id="chatgpt",
        scopes=("offline_access",),
        identity=_identity(),
    )

    now[0] = record.expires_at
    assert await authority.validate(access) is None
    assert await authority.validate_refresh(refresh, client_id="other") is None
    assert await authority.validate_refresh(refresh, client_id="chatgpt") is not None

    assert await authority.revoke_bearer(access, reason="oauth-client-revocation")
    assert await authority.validate_refresh(refresh, client_id="chatgpt") is None

    _, tombstone_record, tombstone_refresh = await authority.issue_offline(
        client_id="chatgpt",
        scopes=("offline_access",),
        identity=_identity(),
    )
    assert await authority.tombstone(tombstone_record.session_id, reason="operator")
    assert await authority.validate_refresh(tombstone_refresh, client_id="chatgpt") is None

    _, _, another_refresh = await authority.issue_offline(
        client_id="chatgpt",
        scopes=("offline_access",),
        identity=_identity(),
    )
    await authority.replace_generation()
    assert await authority.validate_refresh(another_refresh, client_id="chatgpt") is None


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
async def test_generation_initialization_refuses_non_atomic_storage() -> None:
    authority = SessionAuthority(
        storage=NonAtomicMemoryStore(),
        signing_root="stable-root",
        issuer="https://memory.example",
        audience="https://memory.example/mcp",
    )

    with pytest.raises(SessionStoreUnavailable, match="atomic put-if-absent"):
        await authority.current_generation()


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


def test_local_generation_initialization_is_atomic_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_generation_worker,
            args=(str(tmp_path), start, barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
    try:
        assert [process.exitcode for process in processes] == [0, 0]
        generations = [results.get(timeout=2) for _ in processes]
        assert generations[0] == generations[1]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


@pytest.mark.anyio
async def test_live_interprocess_lock_is_never_reclaimed_by_elapsed_time(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    lock_path = tmp_path / "authority.lock"
    holder = context.Process(
        target=_multiprocess_lock_holder,
        args=(str(lock_path), ready, release),
    )
    holder.start()
    try:
        assert await asyncio.to_thread(ready.wait, 15)
        with pytest.raises(SessionStoreUnavailable, match="timed out"):
            async with _InterprocessFileLock(lock_path, timeout=0.1):
                pytest.fail("a live interprocess lock was stolen")
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
    assert holder.exitcode == 0

    async with _InterprocessFileLock(lock_path, timeout=1):
        pass


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


@pytest.mark.anyio
async def test_swapped_ciphertext_fails_closed_for_validation_and_tombstone() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)
    bearer, first = await authority.issue(
        client_id="first", scopes=("exomem:read",), identity=_identity()
    )
    _, second = await authority.issue(
        client_id="second", scopes=("exomem:read",), identity=_identity()
    )
    raw.data[(authority.sessions_collection, first.session_id)] = raw.data[
        (authority.sessions_collection, second.session_id)
    ]

    with pytest.raises(SessionStoreUnavailable, match="storage key"):
        await authority.validate(bearer)
    with pytest.raises(SessionStoreUnavailable, match="storage key"):
        await authority.tombstone(first.session_id, reason="operator")


@pytest.mark.anyio
async def test_swapped_ciphertext_fails_closed_during_session_listing() -> None:
    raw = AtomicMemoryStore()
    authority = _authority(raw)
    _, first = await authority.issue(
        client_id="first", scopes=("exomem:read",), identity=_identity()
    )
    _, second = await authority.issue(
        client_id="second", scopes=("exomem:read",), identity=_identity()
    )
    first_key = (authority.sessions_collection, first.session_id)
    second_key = (authority.sessions_collection, second.session_id)
    raw.data[first_key], raw.data[second_key] = raw.data[second_key], raw.data[first_key]

    with pytest.raises(SessionStoreUnavailable, match="storage key"):
        await authority.list_sessions()
