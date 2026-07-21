from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from exomem import server_auth
from exomem.auth_sessions import (
    ACCESS_TOKEN_TTL_SECONDS,
    SessionAuthority,
    SessionIdentity,
    SessionStoreUnavailable,
)
from exomem.runtime_readiness import build_runtime_readiness


class AtomicStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str | None, str], dict[str, Any]] = {}
        self.fail_get = False
        self.get_error: Exception | None = None
        self._lock = asyncio.Lock()

    async def get(
        self, key: str, *, collection: str | None = None
    ) -> dict[str, Any] | None:
        if self.fail_get:
            raise TimeoutError("remote store timed out")
        if self.get_error is not None:
            raise self.get_error
        value = self.data.get((collection, key))
        return None if value is None else dict(value)

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
            storage_key = (collection, key)
            if storage_key in self.data:
                return False
            self.data[storage_key] = dict(value)
            return True

    async def list_keys(self, *, collection: str | None = None) -> list[str]:
        return sorted(key for stored_collection, key in self.data if stored_collection == collection)


def _stale_authority(
    tmp_path: Path,
    *,
    now: list[float],
    grace: float = 86_400.0,
) -> tuple[SessionAuthority, AtomicStore, Any, Any]:
    from exomem.session_validation_cache import (
        SessionStoreTelemetry,
        SessionValidationCache,
    )

    store = AtomicStore()
    cache = SessionValidationCache(
        tmp_path / "session-validations.sqlite",
        encryption_key=Fernet.generate_key(),
    )
    telemetry = SessionStoreTelemetry()
    authority = SessionAuthority(
        storage=store,
        signing_root="stable-signing-root",
        issuer="https://memory.example",
        audience="https://memory.example/mcp",
        clock=lambda: now[0],
        validation_cache=cache,
        stale_grace_seconds=grace,
        session_store_telemetry=telemetry,
    )
    return authority, store, cache, telemetry


async def _issue(authority: SessionAuthority) -> tuple[str, Any]:
    return await authority.issue(
        client_id="codex-client",
        scopes=("exomem:read",),
        identity=SessionIdentity(github_user_id=123456, github_login="Person"),
    )


async def _issue_offline(authority: SessionAuthority) -> tuple[str, Any, str]:
    return await authority.issue_offline(
        client_id="codex-client",
        scopes=("offline_access", "exomem:read"),
        identity=SessionIdentity(github_user_id=123456, github_login="Person"),
    )


@pytest.mark.anyio
async def test_store_outage_serves_recent_validation_and_marks_degraded(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [1_800_000_000.0]
    authority, store, _, telemetry = _stale_authority(tmp_path, now=now)
    bearer, issued = await _issue(authority)
    assert await authority.validate(bearer) == issued

    store.fail_get = True
    with caplog.at_level(logging.WARNING, logger="exomem.auth_sessions"):
        assert await authority.validate(bearer) == issued

    assert telemetry.snapshot() == {"state": "degraded", "stale_served_count": 1}
    assert "event=session_stale_served count=1" in caplog.text
    assert bearer not in caplog.text
    assert issued.github_login not in caplog.text


@pytest.mark.anyio
async def test_store_outage_without_prior_validation_fails_closed(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    authority, store, _, telemetry = _stale_authority(tmp_path, now=now)
    bearer, _ = await _issue(authority)
    store.fail_get = True

    with pytest.raises(SessionStoreUnavailable, match="unavailable"):
        await authority.validate(bearer)

    assert telemetry.snapshot() == {"state": "degraded", "stale_served_count": 0}


@pytest.mark.anyio
@pytest.mark.parametrize(("status_code", "serves_stale"), [(503, True), (401, False)])
async def test_only_remote_5xx_status_allows_stale_fallback(
    tmp_path: Path,
    status_code: int,
    serves_stale: bool,
) -> None:
    now = [1_800_000_000.0]
    authority, store, _, telemetry = _stale_authority(tmp_path, now=now)
    bearer, issued = await _issue(authority)
    assert await authority.validate(bearer) == issued
    request = httpx.Request("POST", "https://coordinator.example/v1/state/get")
    response = httpx.Response(status_code, request=request)
    store.get_error = httpx.HTTPStatusError(
        "remote status",
        request=request,
        response=response,
    )

    if serves_stale:
        assert await authority.validate(bearer) == issued
        assert telemetry.snapshot() == {"state": "degraded", "stale_served_count": 1}
    else:
        with pytest.raises(SessionStoreUnavailable):
            await authority.validate(bearer)
        assert telemetry.snapshot() == {"state": "ok", "stale_served_count": 0}


@pytest.mark.anyio
async def test_connection_error_allows_stale_but_unrelated_os_error_does_not(
    tmp_path: Path,
) -> None:
    now = [1_800_000_000.0]
    authority, store, _, telemetry = _stale_authority(tmp_path, now=now)
    bearer, issued = await _issue(authority)
    assert await authority.validate(bearer) == issued
    request = httpx.Request("POST", "https://coordinator.example/v1/state/get")
    store.get_error = httpx.ConnectError("connection refused", request=request)
    assert await authority.validate(bearer) == issued
    assert telemetry.snapshot() == {"state": "degraded", "stale_served_count": 1}

    store.get_error = OSError("unrelated local failure")
    with pytest.raises(SessionStoreUnavailable):
        await authority.validate(bearer)
    assert telemetry.snapshot() == {"state": "degraded", "stale_served_count": 1}


@pytest.mark.anyio
async def test_authoritative_revocation_clears_entry_and_prevents_later_stale_use(
    tmp_path: Path,
) -> None:
    now = [1_800_000_000.0]
    authority, store, cache, telemetry = _stale_authority(tmp_path, now=now)
    bearer, issued = await _issue(authority)
    assert await authority.validate(bearer) == issued
    assert cache.get(bearer) is not None

    assert await authority.revoke_bearer(bearer, reason="operator-revocation")
    assert cache.get(bearer) is None

    store.fail_get = True
    with pytest.raises(SessionStoreUnavailable):
        await authority.validate(bearer)
    assert telemetry.snapshot()["stale_served_count"] == 0


@pytest.mark.anyio
async def test_generation_replacement_and_session_tombstone_clear_stale_eligibility(
    tmp_path: Path,
) -> None:
    now = [1_800_000_000.0]
    authority, store, cache, _ = _stale_authority(tmp_path, now=now)
    first_bearer, first = await _issue(authority)
    second_bearer, second = await _issue(authority)
    assert await authority.validate(first_bearer) == first
    assert await authority.validate(second_bearer) == second

    await authority.replace_generation()
    assert cache.get(first_bearer) is None
    assert cache.get(second_bearer) is None

    third_bearer, third = await _issue(authority)
    assert await authority.validate(third_bearer) == third
    assert await authority.tombstone(third.session_id, reason="operator-revocation")
    assert cache.get(third_bearer) is None

    store.fail_get = True
    for bearer in (first_bearer, second_bearer, third_bearer):
        with pytest.raises(SessionStoreUnavailable):
            await authority.validate(bearer)


@pytest.mark.anyio
async def test_access_or_refresh_revocation_clears_every_cached_family_session(
    tmp_path: Path,
) -> None:
    now = [1_800_000_000.0]
    authority, store, cache, _ = _stale_authority(tmp_path, now=now)
    first_access, first_record, refresh = await _issue_offline(authority)
    assert await authority.validate(first_access) == first_record
    second_access, second_record, next_refresh = await authority.rotate_refresh(
        refresh,
        client_id="codex-client",
        scopes=("offline_access", "exomem:read"),
    )
    assert await authority.validate(second_access) == second_record

    assert await authority.revoke_bearer(first_access, reason="access-revocation")
    assert cache.get(first_access) is None
    assert cache.get(second_access) is None

    third_access, third_record, third_refresh = await _issue_offline(authority)
    assert await authority.validate(third_access) == third_record
    assert await authority.revoke_bearer(third_refresh, reason="refresh-revocation")
    assert cache.get(third_access) is None

    store.fail_get = True
    for bearer in (first_access, second_access, third_access):
        with pytest.raises(SessionStoreUnavailable):
            await authority.validate(bearer)
    assert next_refresh


@pytest.mark.anyio
async def test_authoritative_expiry_clears_cached_access_session(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    authority, store, cache, _ = _stale_authority(tmp_path, now=now)
    access, record, _ = await _issue_offline(authority)
    assert await authority.validate(access) == record

    now[0] += ACCESS_TOKEN_TTL_SECONDS
    assert await authority.validate(access) is None
    assert cache.get(access) is None

    store.fail_get = True
    with pytest.raises(SessionStoreUnavailable):
        await authority.validate(access)


@pytest.mark.anyio
async def test_failed_cache_invalidation_disables_stale_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_800_000_000.0]
    authority, store, cache, telemetry = _stale_authority(tmp_path, now=now)
    bearer, record = await _issue(authority)
    assert await authority.validate(bearer) == record
    connect = cache._connect

    def fail_connect() -> Any:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(cache, "_connect", fail_connect)
    assert await authority.tombstone(record.session_id, reason="operator-revocation")
    monkeypatch.setattr(cache, "_connect", connect)

    store.fail_get = True
    with pytest.raises(SessionStoreUnavailable):
        await authority.validate(bearer)
    assert telemetry.snapshot()["stale_served_count"] == 0


@pytest.mark.anyio
async def test_stale_cache_never_enables_issuance_refresh_or_revocation(
    tmp_path: Path,
) -> None:
    now = [1_800_000_000.0]
    authority, store, _, _ = _stale_authority(tmp_path, now=now)
    access, record, refresh = await _issue_offline(authority)
    assert await authority.validate(access) == record
    store.fail_get = True

    with pytest.raises(SessionStoreUnavailable):
        await _issue(authority)
    with pytest.raises(SessionStoreUnavailable):
        await authority.rotate_refresh(
            refresh,
            client_id="codex-client",
            scopes=("offline_access", "exomem:read"),
        )
    with pytest.raises(SessionStoreUnavailable):
        await authority.revoke_bearer(access, reason="offline-revocation")


@pytest.mark.anyio
async def test_expired_grace_and_zero_grace_preserve_fail_closed_behavior(
    tmp_path: Path,
) -> None:
    now = [1_800_000_000.0]
    authority, store, _, telemetry = _stale_authority(tmp_path / "expired", now=now, grace=60)
    bearer, issued = await _issue(authority)
    assert await authority.validate(bearer) == issued
    now[0] += 61
    store.fail_get = True
    with pytest.raises(SessionStoreUnavailable):
        await authority.validate(bearer)
    assert telemetry.snapshot() == {"state": "degraded", "stale_served_count": 0}

    now = [1_800_000_000.0]
    disabled, disabled_store, _, disabled_telemetry = _stale_authority(
        tmp_path / "disabled", now=now, grace=0
    )
    disabled_bearer, disabled_record = await _issue(disabled)
    assert await disabled.validate(disabled_bearer) == disabled_record
    disabled_store.fail_get = True
    with pytest.raises(SessionStoreUnavailable):
        await disabled.validate(disabled_bearer)
    assert disabled_telemetry.snapshot() == {"state": "ok", "stale_served_count": 0}


@pytest.mark.anyio
async def test_successful_remote_recovery_restores_ok_state(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    authority, store, _, telemetry = _stale_authority(tmp_path, now=now)
    bearer, issued = await _issue(authority)
    assert await authority.validate(bearer) == issued
    store.fail_get = True
    assert await authority.validate(bearer) == issued
    assert telemetry.snapshot()["state"] == "degraded"

    store.fail_get = False
    assert await authority.validate(bearer) == issued
    assert telemetry.snapshot() == {"state": "ok", "stale_served_count": 1}


def test_readiness_includes_session_store_telemetry() -> None:
    snapshot = build_runtime_readiness(
        coordination={
            "enabled": False,
            "role": "standalone",
            "replica_id": None,
            "coordinator_healthy": True,
        },
        release="1.2.3",
        mcp_tool_surface_sha256="a" * 64,
        session_store={"state": "degraded", "stale_served_count": 7},
    )

    assert snapshot["session_store"] == {"state": "degraded", "stale_served_count": 7}


def test_server_auth_wires_replica_local_cache_and_grace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    values = {
        "EXOMEM_JWT_SIGNING_KEY": "stable-signing-root",
        "EXOMEM_OAUTH_STORAGE_URL": "https://coordinator.example",
        "EXOMEM_OAUTH_STORAGE_NAMESPACE": "personal-main",
        "EXOMEM_OAUTH_STORAGE_TOKEN": "coordinator-secret",
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state_dir),
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "laptop",
        "EXOMEM_SESSION_STALE_GRACE_SECONDS": "123",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    authority = server_auth.build_session_authority(base_url="https://memory.example")

    expected_suffix = hashlib.sha256(b"personal-main\0laptop").hexdigest()[:20]
    assert authority._validation_cache.path == (
        state_dir / f"session-validations-{expected_suffix}.sqlite"
    ).resolve()
    assert authority._stale_grace_seconds == 123.0


def test_server_auth_uses_exact_fastmcp_storage_key_and_zero_grace_omits_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class RecordingCache:
        def __init__(self, path: Path, *, encryption_key: bytes) -> None:
            captured.update(path=path, encryption_key=encryption_key)

    values = {
        "EXOMEM_JWT_SIGNING_KEY": "stable-signing-root",
        "EXOMEM_OAUTH_STORAGE_URL": "https://coordinator.example",
        "EXOMEM_OAUTH_STORAGE_NAMESPACE": "personal-main",
        "EXOMEM_OAUTH_STORAGE_TOKEN": "coordinator-secret",
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path),
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "laptop",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(server_auth, "SessionValidationCache", RecordingCache)

    enabled = server_auth.build_session_authority(base_url="https://memory.example")
    assert captured["encryption_key"] == server_auth._oauth_storage_encryption_key(
        "stable-signing-root"
    )
    assert enabled._validation_cache is not None

    captured.clear()
    monkeypatch.setenv("EXOMEM_SESSION_STALE_GRACE_SECONDS", "0")
    disabled = server_auth.build_session_authority(base_url="https://memory.example")
    assert disabled._validation_cache is None
    assert disabled._stale_grace_seconds == 0
    assert captured == {}
