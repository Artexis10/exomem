from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from exomem import server_auth
from exomem.auth_sessions import SessionAuthority, SessionIdentity
from exomem.lease_coordinator import create_app
from exomem.remote_oauth_storage import RemoteOAuthStorage


def _set_oauth_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "GITHUB_CLIENT_ID": "github-client",
        "GITHUB_CLIENT_SECRET": "github-secret",
        "EXOMEM_GITHUB_USERNAME": "Person",
        "EXOMEM_GITHUB_USER_ID": "123456",
        "EXOMEM_JWT_SIGNING_KEY": "stable-signing-root",
    }
    values.update(overrides)
    for key, value in values.items():
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)


def test_stdio_auth_disabled_needs_no_oauth_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "EXOMEM_GITHUB_USERNAME",
        "EXOMEM_GITHUB_USER_ID",
        "EXOMEM_JWT_SIGNING_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    assert server_auth.build_oauth(require_auth=False, base_url="http://localhost") is None


@pytest.mark.parametrize(
    ("missing_key", "message"),
    [
        ("EXOMEM_JWT_SIGNING_KEY", "EXOMEM_JWT_SIGNING_KEY"),
        ("EXOMEM_GITHUB_USER_ID", "EXOMEM_GITHUB_USER_ID"),
    ],
)
def test_http_oauth_requires_explicit_trust_anchors(
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
    message: str,
) -> None:
    _set_oauth_env(monkeypatch)
    monkeypatch.delenv(missing_key)

    with pytest.raises(RuntimeError, match=message):
        server_auth.build_oauth(require_auth=True, base_url="https://memory.example")


@pytest.mark.parametrize("raw", ["0", "-1", "not-numeric"])
def test_http_oauth_rejects_invalid_immutable_user_id(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    _set_oauth_env(monkeypatch, EXOMEM_GITHUB_USER_ID=raw)

    with pytest.raises(RuntimeError, match="positive numeric"):
        server_auth.build_oauth(require_auth=True, base_url="https://memory.example")


def test_build_session_authority_uses_encrypted_local_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_oauth_env(monkeypatch)
    monkeypatch.delenv("EXOMEM_OAUTH_STORAGE_URL", raising=False)
    from fastmcp import settings

    monkeypatch.setattr(settings, "home", tmp_path)

    authority = server_auth.build_session_authority(base_url="https://memory.example/")

    assert isinstance(authority, SessionAuthority)
    assert authority.issuer == "https://memory.example"
    assert authority.audience == "https://memory.example/mcp"
    assert (tmp_path / "oauth-sessions").is_dir()


def test_build_session_authority_uses_authenticated_uncached_remote_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("EXOMEM_SESSION_STALE_GRACE_SECONDS", raising=False)
    _set_oauth_env(
        monkeypatch,
        EXOMEM_OAUTH_STORAGE_URL="https://coordinator.example",
        EXOMEM_OAUTH_STORAGE_NAMESPACE="vault",
        EXOMEM_OAUTH_STORAGE_TOKEN="coordinator-secret",
        EXOMEM_WRITER_LEASE_STATE_DIR=str(tmp_path),
    )

    authority = server_auth.build_session_authority(base_url="https://memory.example")
    raw_store = authority._storage.raw

    assert isinstance(raw_store, RemoteOAuthStorage)
    assert raw_store.token == "coordinator-secret"
    assert raw_store.cache_ttl == 0
    assert authority._stale_grace_seconds == 86_400.0


def test_shared_session_authority_refuses_missing_storage_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oauth_env(
        monkeypatch,
        EXOMEM_OAUTH_STORAGE_URL="https://coordinator.example",
        EXOMEM_OAUTH_STORAGE_NAMESPACE="vault",
        EXOMEM_OAUTH_STORAGE_TOKEN="",
    )

    with pytest.raises(RuntimeError, match="EXOMEM_OAUTH_STORAGE_TOKEN"):
        server_auth.build_session_authority(base_url="https://memory.example")


def test_ha_runtime_refuses_replica_local_session_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oauth_env(monkeypatch)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "https://coordinator.example")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "main")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "local")
    monkeypatch.delenv("EXOMEM_OAUTH_STORAGE_URL", raising=False)

    with pytest.raises(RuntimeError, match="EXOMEM_OAUTH_STORAGE_URL"):
        server_auth.build_session_authority(base_url="https://memory.example")


@pytest.mark.anyio
async def test_existing_local_session_survives_single_host_coordination_and_auth_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Enabling a local coordinator must not strand a session in another auth store."""
    from fastmcp import settings

    _set_oauth_env(monkeypatch)
    monkeypatch.delenv("EXOMEM_OAUTH_STORAGE_URL", raising=False)
    monkeypatch.setattr(settings, "home", tmp_path)
    original = server_auth.build_oauth(require_auth=True, base_url="https://memory.example")
    client = OAuthClientInformationFull(
        client_id="local-client", redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
    )
    await original.register_client(client)
    bearer, issued = await original._session_authority.issue(
        client_id="local-client", scopes=("exomem:read",),
        identity=SessionIdentity(github_user_id=123456, github_login="person"),
    )
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOPOLOGY", "single-host")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "http://127.0.0.1:8766")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "main")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "local")
    restarted = server_auth.build_oauth(require_auth=True, base_url="https://memory.example")
    assert (await restarted.get_client("local-client")).client_id == client.client_id
    assert await restarted._session_authority.validate(bearer) == issued
    assert await restarted._session_authority.tombstone(issued.session_id, reason="operator")
    assert await original._session_authority.validate(bearer) is None


@pytest.mark.parametrize("storage_token", ["", "shared-secret"])
def test_explicit_shared_auth_retains_validation_and_selection_in_single_host_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, storage_token: str,
) -> None:
    _set_oauth_env(monkeypatch, EXOMEM_WRITER_LEASE_TOPOLOGY="single-host",
                   EXOMEM_WRITER_LEASE_URL="http://127.0.0.1:8766",
                   EXOMEM_WRITER_LEASE_VAULT_ID="main", EXOMEM_WRITER_LEASE_REPLICA_ID="local",
                   EXOMEM_WRITER_LEASE_STATE_DIR=str(tmp_path),
                   EXOMEM_OAUTH_STORAGE_URL="https://coordinator.example",
                   EXOMEM_OAUTH_STORAGE_TOKEN=storage_token)
    if not storage_token:
        with pytest.raises(RuntimeError, match="EXOMEM_OAUTH_STORAGE_TOKEN"):
            server_auth.build_session_authority(base_url="https://memory.example")
    else:
        authority = server_auth.build_session_authority(base_url="https://memory.example")
        assert isinstance(authority._storage.raw, RemoteOAuthStorage)


def test_build_oauth_constructs_durable_proxy_and_cache_disabled_verifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_oauth_env(monkeypatch)
    from fastmcp import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    captured: dict[str, Any] = {}

    class RecordingProxy:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(server_auth, "ExomemSessionOAuthProxy", RecordingProxy)

    result = server_auth.build_oauth(
        require_auth=True,
        base_url="https://memory.example",
    )

    assert isinstance(result, RecordingProxy)
    assert isinstance(captured["session_authority"], SessionAuthority)
    assert captured["jwt_signing_key"] == "stable-signing-root"
    assert captured["upstream_revocation_endpoint"] is None
    assert captured["valid_scopes"] == [
        "offline_access",
        "exomem:read",
        "exomem:write",
    ]
    verifier = captured["token_verifier"]
    assert isinstance(verifier, server_auth.SingleUserGitHubVerifier)
    assert verifier._allowed_login == "person"
    assert verifier._allowed_user_id == 123456
    assert verifier._cache.enabled is False


@pytest.mark.anyio
async def test_coordinator_state_rejects_unauthenticated_and_accepts_configured_bearer(
    tmp_path: Path,
) -> None:
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="secret")
    transport = httpx.ASGITransport(app=app)
    denied = RemoteOAuthStorage(
        url="https://coordinator.example",
        namespace="main",
        token=None,
        cache_ttl=0,
        transport=transport,
    )
    configured = RemoteOAuthStorage(
        url="https://coordinator.example",
        namespace="main",
        token="secret",
        cache_ttl=0,
        transport=transport,
    )

    with pytest.raises(httpx.HTTPStatusError) as error:
        await denied.get("current", collection="auth")
    assert error.value.response.status_code == 401
    assert await configured.get("current", collection="auth") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "unset"),
        ("github:123456", "active"),
        ("github:654321", "mismatch"),
        ("github:0123456", "malformed"),
        ("Person", "malformed"),
    ],
)
def test_build_oauth_logs_the_remote_owner_binding_state_without_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
    expected: str,
) -> None:
    """A malformed or mismatched binding never stops startup; one content-free
    line says which of the four states the host is in."""
    _set_oauth_env(monkeypatch)
    if value is None:
        monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", value)
    from fastmcp import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    monkeypatch.setattr(server_auth, "ExomemSessionOAuthProxy", lambda **_: object())

    with caplog.at_level("INFO", logger=server_auth.log.name):
        assert (
            server_auth.build_oauth(require_auth=True, base_url="https://memory.example")
            is not None
        )

    lines = [r.getMessage() for r in caplog.records if "remote_owner_binding" in r.getMessage()]
    assert lines == [f"event=remote_owner_binding state={expected}"]
    for record in caplog.records:
        message = record.getMessage()
        assert "123456" not in message and "654321" not in message
        assert "person" not in message.casefold()
