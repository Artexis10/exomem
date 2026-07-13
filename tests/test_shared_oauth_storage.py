from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from key_value.aio.stores.memory import MemoryStore

from exomem.auth_sessions import SessionAuthority, SessionIdentity
from exomem.lease_coordinator import SQLiteStateStore, create_app
from exomem.remote_oauth_storage import ReadThroughMirrorStorage, RemoteOAuthStorage


@pytest.mark.anyio
async def test_remote_storage_round_trip_bulk_ttl_and_auth(tmp_path: Path) -> None:
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="secret")
    transport = httpx.ASGITransport(app=app)
    store = RemoteOAuthStorage(
        url="https://coordinator.example",
        namespace="main",
        token="secret",
        cache_ttl=0,
        transport=transport,
    )

    await store.put("one", {"ciphertext": "a"}, collection="tokens", ttl=60)
    assert await store.get("one", collection="tokens") == {"ciphertext": "a"}
    value, ttl = await store.ttl("one", collection="tokens")
    assert value == {"ciphertext": "a"}
    assert ttl is not None and 0 < ttl <= 60

    await store.put_many(
        ["two", "three"],
        [{"ciphertext": "b"}, {"ciphertext": "c"}],
        collection="tokens",
    )
    assert await store.get_many(["one", "two", "missing"], collection="tokens") == [
        {"ciphertext": "a"},
        {"ciphertext": "b"},
        None,
    ]
    assert await store.delete_many(["one", "two", "missing"], collection="tokens") == 2
    assert await store.put_if_absent(
        "generation", {"ciphertext": "first"}, collection="auth"
    )
    assert not await store.put_if_absent(
        "generation", {"ciphertext": "second"}, collection="auth"
    )
    assert await store.get("generation", collection="auth") == {"ciphertext": "first"}
    assert await store.list_keys(collection="tokens") == ["three"]

    denied = RemoteOAuthStorage(
        url="https://coordinator.example",
        namespace="main",
        cache_ttl=0,
        transport=transport,
    )
    with pytest.raises(httpx.HTTPStatusError) as error:
        await denied.get("three", collection="tokens")
    assert error.value.response.status_code == 401


@pytest.mark.anyio
async def test_remote_storage_caches_hot_token_records() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["user-agent"].startswith("Mozilla/5.0")
        assert "Exomem-Coordinator" in request.headers["user-agent"]
        return httpx.Response(200, json={"result": {"ciphertext": "hot"}})

    store = RemoteOAuthStorage(
        url="https://coordinator.example",
        namespace="main",
        cache_ttl=300,
        transport=httpx.MockTransport(handler),
    )
    assert await store.get("jti", collection="mcp-jti-mappings") == {"ciphertext": "hot"}
    assert await store.get("jti", collection="mcp-jti-mappings") == {"ciphertext": "hot"}
    assert calls == 1


def test_sqlite_state_store_expires_and_isolates_namespaces(tmp_path: Path) -> None:
    now = [100.0]
    store = SQLiteStateStore(tmp_path / "coordinator.sqlite", clock=lambda: now[0])
    store.put("a", "tokens", "same", {"value": "a"}, 5)
    store.put("b", "tokens", "same", {"value": "b"}, None)
    assert store.get("a", "tokens", "same")[0] == {"value": "a"}
    assert store.get("b", "tokens", "same")[0] == {"value": "b"}
    now[0] = 106.0
    assert store.get("a", "tokens", "same") == (None, None)
    assert store.get("b", "tokens", "same")[0] == {"value": "b"}


def test_sqlite_state_store_put_if_absent_and_key_enumeration(tmp_path: Path) -> None:
    now = [100.0]
    store = SQLiteStateStore(tmp_path / "coordinator.sqlite", clock=lambda: now[0])

    assert store.put_if_absent("main", "auth", "generation", {"ciphertext": "first"}, None)
    assert not store.put_if_absent(
        "main", "auth", "generation", {"ciphertext": "second"}, None
    )
    store.put("main", "auth", "expiring", {"ciphertext": "soon"}, 5)
    store.put("other", "auth", "hidden", {"ciphertext": "other"}, None)
    assert store.list_keys("main", "auth") == ["expiring", "generation"]

    now[0] = 106.0

    assert store.list_keys("main", "auth") == ["generation"]
    assert store.get("main", "auth", "generation")[0] == {"ciphertext": "first"}


@pytest.mark.anyio
async def test_read_through_migrates_existing_local_state_and_mirrors_writes() -> None:
    remote = MemoryStore()
    local = MemoryStore()
    store = ReadThroughMirrorStorage(primary=remote, fallback=local)
    await local.put("existing", {"ciphertext": "old"}, collection="tokens", ttl=60)

    assert await store.get("existing", collection="tokens") == {"ciphertext": "old"}
    assert await remote.get("existing", collection="tokens") == {"ciphertext": "old"}

    await store.put("new", {"ciphertext": "new"}, collection="tokens")
    assert await remote.get("new", collection="tokens") == {"ciphertext": "new"}
    assert await local.get("new", collection="tokens") == {"ciphertext": "new"}


@pytest.mark.anyio
async def test_remote_session_authority_is_cross_replica_and_revocation_safe(tmp_path: Path) -> None:
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="secret")
    transport = httpx.ASGITransport(app=app)
    kwargs = {
        "url": "https://coordinator.example",
        "namespace": "main",
        "storage_token": "secret",
        "signing_root": "stable-root",
        "issuer": "https://memory.example",
        "audience": "https://memory.example/mcp",
        "transport": transport,
    }
    first = SessionAuthority.remote(**kwargs)
    second = SessionAuthority.remote(**kwargs)
    bearer, issued = await first.issue(
        client_id="codex",
        scopes=("exomem:read",),
        identity=SessionIdentity(github_user_id=123, github_login="person"),
    )

    assert await second.validate(bearer) == issued
    assert await second.tombstone(issued.session_id, reason="operator")
    assert await first.validate(bearer) is None
    [listed] = await first.list_sessions()
    assert listed.status == "revoked"


def test_remote_session_authority_requires_storage_bearer() -> None:
    with pytest.raises(ValueError, match="storage token"):
        SessionAuthority.remote(
            url="https://coordinator.example",
            namespace="main",
            storage_token="",
            signing_root="stable-root",
            issuer="https://memory.example",
            audience="https://memory.example/mcp",
        )


def test_shared_storage_requires_stable_key_and_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem.server_auth import build_oauth

    base = {
        "GITHUB_CLIENT_ID": "client",
        "GITHUB_CLIENT_SECRET": "secret",
        "EXOMEM_GITHUB_USERNAME": "person",
        "EXOMEM_OAUTH_STORAGE_URL": "https://coordinator.example",
    }
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("EXOMEM_JWT_SIGNING_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EXOMEM_JWT_SIGNING_KEY"):
        build_oauth(require_auth=True, base_url="https://memory.example")

    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "stable-signing-key")
    with pytest.raises(RuntimeError, match="NAMESPACE"):
        build_oauth(require_auth=True, base_url="https://memory.example")
