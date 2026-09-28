"""Worker half of authenticated local ingress, driven through the real manager door.

Every request here goes the way production sends it: through the manager's
`ServiceIngress` (the public listener) or its `LocalListener` (the loopback
listener), which forwards to a real FastMCP worker app in process. The tests
are the threat scenarios of `add-authenticated-local-ingress`: the audience
is the ingress, a forged stamp never turns a public request local, an old
manager's unstripped stamp is refused, and a local token resolves to the
`owner-local` principal and nothing else. All ids, hosts and tokens are
synthetic.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from key_value.aio.stores.memory import MemoryStore
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from exomem import local_ingress, server
from exomem.auth_sessions import SessionAuthority, SessionIdentity, SessionStoreUnavailable
from exomem.governance import principal as principal_module
from exomem.governance.authorization_transport import AuthorizationSessionMiddleware
from exomem.service_ingress import (
    INGRESS_KEY_ENV,
    INGRESS_PROOF_HEADER,
    INGRESS_STAMP_HEADER,
    LocalListener,
    ServiceIngress,
)
from exomem.session_oauth import ExomemSessionOAuthProxy

ISSUER = "https://memory.example"
AUDIENCE = f"{ISSUER}/mcp"
LOCAL_BASE = "http://127.0.0.1:8764"
KEY = "manager-proof-for-tests"
ROOT = "local-ingress-signing-root"
OWNER_ID = 4242
PROTOCOL_VERSION = "2026-07-28"
TOOL_NAME = "ask_memory"
STAMP = INGRESS_STAMP_HEADER.decode()
PROOF = INGRESS_PROOF_HEADER.decode()


class _NeverExternalVerifier:
    required_scopes: list[str] = []

    async def verify_token(self, token: str) -> None:
        raise AssertionError("durable Exomem sessions must not call the provider")


def _probe_app(vault: Path, *, auth: Any, middleware: list | None = None) -> Any:
    mcp = server.ExomemFastMCP("local-ingress-probe", auth=auth)
    mcp.add_middleware(AuthorizationSessionMiddleware(vault))

    @mcp.tool(name=TOOL_NAME)
    async def principal_probe(marker: str = "") -> dict[str, object]:
        principal = principal_module.effective_principal()
        grant = local_ingress.current_grant()
        return {
            "audience_id": principal.audience_id,
            "principal_kind": principal.principal_kind,
            "remote_owner": principal.remote_owner,
            "local_owner": principal.local_owner,
            "issuer_family": principal.issuer_family,
            "grant_client": grant.client_id if grant is not None else None,
        }

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Any) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok"})

    return mcp.http_app(stateless_http=True, json_response=True, middleware=middleware or [])


@dataclass
class _Harness:
    worker: Any
    public_authority: SessionAuthority
    local_authority: SessionAuthority


def _authorities(tmp_path: Path) -> tuple[SessionAuthority, SessionAuthority]:
    directory = tmp_path / "oauth-sessions"
    public = SessionAuthority.local(
        directory=directory, signing_root=ROOT, issuer=ISSUER, audience=AUDIENCE
    )
    local = SessionAuthority.local(
        directory=directory,
        signing_root=ROOT,
        issuer=local_ingress.LOCAL_ISSUER,
        audience=local_ingress.LOCAL_AUDIENCE,
    )
    return public, local


def _oauth(public: SessionAuthority, local: SessionAuthority | None) -> ExomemSessionOAuthProxy:
    return ExomemSessionOAuthProxy(
        session_authority=public,
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id="github-client",
        upstream_client_secret="github-secret",
        upstream_revocation_endpoint=None,
        token_verifier=_NeverExternalVerifier(),
        base_url=ISSUER,
        client_storage=MemoryStore(),
        jwt_signing_key="local-ingress-jwt-root",
        require_authorization_consent=False,
        local_verifier=(
            local_ingress.LocalCredentialVerifier(local) if local is not None else None
        ),
    )


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A worker whose manager gave it the proof, as every managed spawn does."""
    monkeypatch.setenv(INGRESS_KEY_ENV, KEY)
    monkeypatch.setenv("EXOMEM_BASE_URL", ISSUER)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", str(OWNER_ID))
    return monkeypatch


@pytest.fixture
def oauth_worker(vault: Path, tmp_path: Path, managed: pytest.MonkeyPatch) -> _Harness:
    public, local = _authorities(tmp_path)
    return _Harness(_probe_app(vault, auth=_oauth(public, local)), public, local)


@pytest.fixture
def loopback_worker(vault: Path, tmp_path: Path, managed: pytest.MonkeyPatch) -> _Harness:
    """A no-auth managed worker: the gate sits at the front of its own list."""
    public, local = _authorities(tmp_path)
    gate = Middleware(
        local_ingress.LocalIngressMiddleware,
        verifier=local_ingress.LocalCredentialVerifier(local),
    )
    return _Harness(_probe_app(vault, auth=None, middleware=[gate]), public, local)


@dataclass
class _Doors:
    local: httpx.AsyncClient
    public: httpx.AsyncClient
    worker: httpx.AsyncClient


@asynccontextmanager
async def _doors(harness: _Harness) -> AsyncIterator[_Doors]:
    """The manager's two listeners in front of the worker, plus the worker's
    own socket as an older, non-stripping manager would reach it."""
    app = harness.worker
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as upstream:
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(upstream)
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=LocalListener(ingress)),
                    base_url=LOCAL_BASE,
                ) as local,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=ingress), base_url=ISSUER
                ) as public,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=LOCAL_BASE
                ) as worker,
            ):
                yield _Doors(local=local, public=public, worker=worker)
            await ingress.aclose()


async def _issue_local(harness: _Harness, client_id: str = "home") -> tuple[str, str]:
    token, record = await harness.local_authority.issue(
        client_id=client_id,
        scopes=list(local_ingress.LOCAL_SCOPES),
        identity=SessionIdentity(github_user_id=OWNER_ID, github_login="example-owner"),
    )
    return token, record.session_id


async def _issue_public(harness: _Harness) -> str:
    token, _record = await harness.public_authority.issue(
        client_id="remote-client",
        scopes=["exomem:read", "exomem:write"],
        identity=SessionIdentity(github_user_id=OWNER_ID, github_login="example-owner"),
    )
    return token


def _mcp_body(request_id: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "local-client", "version": "1"},
            },
            "name": TOOL_NAME,
            "arguments": {"marker": str(request_id)},
        },
    }


async def _post_mcp(
    client: httpx.AsyncClient,
    bearer: str | None,
    request_id: int = 1,
    extra: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "mcp-protocol-version": PROTOCOL_VERSION,
        "mcp-method": "tools/call",
        "mcp-name": TOOL_NAME,
        **(extra or {}),
    }
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    return await client.post("/mcp", headers=headers, json=_mcp_body(request_id))


def _seen(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]


def _remote_scope() -> str:
    return "principal:" + hashlib.sha256(f"{ISSUER}\0{OWNER_ID}".encode()).hexdigest()


# ---- R6: the owner-local principal ---------------------------------------------


@pytest.mark.anyio
async def test_a_local_token_over_the_local_listener_is_the_owner_labelled_local(
    oauth_worker: _Harness,
) -> None:
    token, _ = await _issue_local(oauth_worker)
    async with _doors(oauth_worker) as doors:
        seen = _seen(await _post_mcp(doors.local, token))

    assert seen == {
        "audience_id": principal_module.OWNER_AUDIENCE,
        "principal_kind": "owner-local",
        "remote_owner": False,
        "local_owner": True,
        "issuer_family": "mcp-local",
        "grant_client": "home",
    }
    assert local_ingress.current_grant() is None


@pytest.mark.anyio
async def test_a_bound_remote_owner_binding_never_relabels_a_local_token(
    oauth_worker: _Harness, managed: pytest.MonkeyPatch
) -> None:
    managed.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{OWNER_ID}")
    token, _ = await _issue_local(oauth_worker)
    async with _doors(oauth_worker) as doors:
        seen = _seen(await _post_mcp(doors.local, token))

    assert seen["principal_kind"] == "owner-local"
    assert seen["remote_owner"] is False


@pytest.mark.anyio
async def test_a_no_auth_managed_worker_resolves_the_same_owner_local(
    loopback_worker: _Harness,
) -> None:
    token, _ = await _issue_local(loopback_worker)
    async with _doors(loopback_worker) as doors:
        local = _seen(await _post_mcp(doors.local, token))
        missing = await _post_mcp(doors.local, None, 2)
        # Unstamped, the loopback worker is exactly today's open local server:
        # the same token is just a bearer there, never the local owner.
        unchanged = _seen(await _post_mcp(doors.public, token, 3))

    assert local["principal_kind"] == "owner-local"
    assert local["issuer_family"] == "mcp-local"
    assert missing.status_code == 401
    assert unchanged["principal_kind"] == "principal"
    assert unchanged["local_owner"] is False
    assert unchanged["grant_client"] is None


def test_claims_copied_from_a_local_token_are_not_the_local_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp.server import dependencies as fastmcp_dependencies

    copied = local_ingress.LocalIngressAccessToken(
        token="exo_s1.forged",
        client_id="home",
        scopes=list(local_ingress.LOCAL_SCOPES),
        claims={"sub": "local:abc", "iss": local_ingress.LOCAL_ISSUER, "ingress": "local"},
    )
    monkeypatch.setattr(fastmcp_dependencies, "get_access_token", lambda: copied)
    # The right type but no live grant: provenance fails.
    resolved = principal_module.resolve_mcp_principal()
    assert resolved.principal_kind == "principal"
    assert resolved.audience_id != principal_module.OWNER_AUDIENCE


# ---- R4: the audience is the ingress --------------------------------------------


@pytest.mark.anyio
async def test_an_oauth_session_on_the_local_listener_is_refused_without_oauth_metadata(
    oauth_worker: _Harness,
) -> None:
    public_token = await _issue_public(oauth_worker)
    async with _doors(oauth_worker) as doors:
        refused = await _post_mcp(doors.local, public_token)

    assert refused.status_code == 401
    assert "resource_metadata" not in refused.headers.get("www-authenticate", "")
    assert "invalid_token" in refused.headers["www-authenticate"]


@pytest.mark.anyio
@pytest.mark.parametrize("bearer", [None, "the-owner-rest-key", "the-static-upload-token", ""])
async def test_static_owner_credentials_and_no_credential_are_refused_on_the_local_listener(
    oauth_worker: _Harness, bearer: str | None
) -> None:
    async with _doors(oauth_worker) as doors:
        refused = await _post_mcp(doors.local, bearer)
        api = await doors.local.post(
            "/api/ask_memory",
            headers={"authorization": f"Bearer {bearer}"} if bearer else {},
            json={"query": "x"},
        )

    assert refused.status_code == 401
    assert api.status_code == 401
    for response in (refused, api):
        assert "resource_metadata" not in response.headers.get("www-authenticate", "")


@pytest.mark.anyio
async def test_a_local_token_via_the_public_listener_gets_the_ordinary_401(
    oauth_worker: _Harness,
) -> None:
    token, _ = await _issue_local(oauth_worker)
    async with _doors(oauth_worker) as doors:
        refused = await _post_mcp(doors.public, token)

    assert refused.status_code == 401
    # The public path's own challenge, byte for byte what it was.
    assert "resource_metadata" in refused.headers["www-authenticate"]


@pytest.mark.anyio
async def test_the_public_path_is_unchanged_for_an_oauth_session(oauth_worker: _Harness) -> None:
    public_token = await _issue_public(oauth_worker)
    async with _doors(oauth_worker) as doors:
        seen = _seen(await _post_mcp(doors.public, public_token))

    assert seen["audience_id"] == _remote_scope()
    assert seen["principal_kind"] == "principal"
    assert seen["local_owner"] is False
    assert seen["grant_client"] is None


# ---- R2: a forged stamp never turns a request local ----------------------------


@pytest.mark.anyio
async def test_a_forged_stamp_through_the_public_listener_is_stripped_even_with_the_key(
    oauth_worker: _Harness,
) -> None:
    token, _ = await _issue_local(oauth_worker)
    async with _doors(oauth_worker) as doors:
        refused = await _post_mcp(doors.public, token, extra={STAMP: "local", PROOF: KEY})

    assert refused.status_code == 401
    assert "resource_metadata" in refused.headers["www-authenticate"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stamp",
    [
        {STAMP: "local"},
        {STAMP: "local", PROOF: "guessed"},
        {PROOF: KEY},
        {STAMP: "public", PROOF: KEY},
        {STAMP: "local", PROOF: KEY[:-1]},
    ],
)
async def test_an_unproven_stamp_reaching_the_worker_is_refused(
    oauth_worker: _Harness, stamp: dict[str, str]
) -> None:
    """What a manager that predates stripping would forward from a remote caller."""
    token, _ = await _issue_local(oauth_worker)
    async with _doors(oauth_worker) as doors:
        refused = await _post_mcp(doors.worker, token, extra=stamp)
        proven = await _post_mcp(doors.worker, token, 2, extra={STAMP: "local", PROOF: KEY})

    assert refused.status_code == 403
    assert _seen(proven)["principal_kind"] == "owner-local"


@pytest.mark.anyio
async def test_a_worker_its_manager_gave_no_proof_refuses_every_stamp(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(INGRESS_KEY_ENV, raising=False)
    public, local = _authorities(tmp_path)
    harness = _Harness(_probe_app(vault, auth=_oauth(public, local)), public, local)
    token, _ = await _issue_local(harness)
    async with _doors(harness) as doors:
        refused = await _post_mcp(doors.worker, token, extra={STAMP: "local", PROOF: KEY})
        empty = await _post_mcp(doors.worker, token, 2, extra={STAMP: "local", PROOF: ""})

    assert refused.status_code == 403
    assert empty.status_code == 403


@pytest.mark.anyio
async def test_the_worker_reapplies_the_door_rules_to_a_proven_stamp(
    oauth_worker: _Harness,
) -> None:
    token, _ = await _issue_local(oauth_worker)
    proven = {STAMP: "local", PROOF: KEY}
    async with _doors(oauth_worker) as doors:
        transit = await _post_mcp(doors.worker, token, extra={**proven, "cf-ray": "x"})
        origin = await _post_mcp(doors.worker, token, 2, extra={**proven, "origin": "null"})
        control = await doors.worker.post(
            "/control/promote", headers={**proven, "authorization": f"Bearer {token}"}
        )

    assert transit.status_code == 403
    assert origin.status_code == 403
    assert control.status_code == 404


# ---- R5: revocation reaches the local door -------------------------------------


@pytest.mark.anyio
async def test_revoking_a_local_session_or_all_sessions_closes_the_local_door(
    oauth_worker: _Harness,
) -> None:
    first, first_id = await _issue_local(oauth_worker, "home")
    second, _ = await _issue_local(oauth_worker, "codex")
    public_token = await _issue_public(oauth_worker)
    async with _doors(oauth_worker) as doors:
        assert (await _post_mcp(doors.local, first)).status_code == 200
        await oauth_worker.public_authority.tombstone(first_id, reason="operator-revocation")
        assert (await _post_mcp(doors.local, first, 2)).status_code == 401
        assert (await _post_mcp(doors.local, second, 3)).status_code == 200
        await oauth_worker.public_authority.replace_generation()
        assert (await _post_mcp(doors.local, second, 4)).status_code == 401
        assert (await _post_mcp(doors.public, public_token, 5)).status_code == 401


# ---- R3: health stays open; the store's outage is retryable ----------------------


@pytest.mark.anyio
async def test_health_needs_no_token_on_the_local_listener(oauth_worker: _Harness) -> None:
    async with _doors(oauth_worker) as doors:
        response = await doors.local.get("/health")
    assert response.status_code == 200


@pytest.mark.anyio
async def test_a_session_store_outage_is_a_retryable_503_not_an_open_door(
    vault: Path, tmp_path: Path, managed: pytest.MonkeyPatch
) -> None:
    class Down:
        async def validate(self, bearer: str) -> None:
            raise SessionStoreUnavailable("store down")

    public, local = _authorities(tmp_path)
    harness = _Harness(_probe_app(vault, auth=_oauth(public, Down())), public, local)
    token, _ = await _issue_local(harness)
    async with _doors(harness) as doors:
        response = await _post_mcp(doors.local, token)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


@pytest.mark.anyio
async def test_under_a_grant_the_proxy_accepts_only_that_exact_bearer(
    oauth_worker: _Harness,
) -> None:
    token, _ = await _issue_local(oauth_worker)
    public_token = await _issue_public(oauth_worker)
    proxy = _oauth(oauth_worker.public_authority, oauth_worker.local_authority)
    verifier = local_ingress.LocalCredentialVerifier(oauth_worker.local_authority)
    grant = await verifier.verify(token)
    assert grant is not None
    assert await verifier.verify(public_token) is None

    bound = local_ingress._GRANT.set(grant)
    try:
        assert await proxy.load_access_token(token) is grant.access_token
        assert await proxy.load_access_token(public_token) is None
    finally:
        local_ingress._GRANT.reset(bound)
    # Off local ingress the proxy validates exactly as before.
    public = await proxy.load_access_token(public_token)
    assert public is not None and public.claims["aud"] == AUDIENCE
    assert await proxy.load_access_token(token) is None


def test_a_worker_without_a_supervisor_socket_arms_no_gate(monkeypatch) -> None:
    class Fake:
        auth = None

    assert [m.cls for m in server.http_middleware(Fake())] == [
        server.EdgeIngressMiddleware,
        server.AccessLogMiddleware,
        server.PrimeMcpSSEMiddleware,
    ]
    armed = server.http_middleware(Fake(), worker_socket=Path("/run/worker.sock"))
    assert armed[0].cls is local_ingress.LocalIngressMiddleware
    Fake.auth = object()
    # With an auth provider the gate lives in the provider's own middleware.
    assert local_ingress.LocalIngressMiddleware not in [
        m.cls for m in server.http_middleware(Fake(), worker_socket=Path("/run/worker.sock"))
    ]


# ---- R7 and R8: REST, /upload and logs through the real server -------------------


REST_KEY = "the-owner-rest-key"
UPLOAD_TOKEN = "the-static-upload-token"


@dataclass
class _RealWorker:
    app: Any
    local_token: str
    public_token: str


@pytest.fixture
def real_env(vault: Path, tmp_path: Path, managed: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    from fastmcp import settings

    from exomem import server_transfer

    managed.setattr(server, "load_dotenv", lambda *a, **k: None)
    managed.setattr(settings, "home", tmp_path / "fastmcp-home")
    for name in ("EXOMEM_OAUTH_STORAGE_URL", "EXOMEM_WRITER_LEASE_URL"):
        managed.delenv(name, raising=False)
    for name, value in {
        "GITHUB_CLIENT_ID": "fixture-client",
        "GITHUB_CLIENT_SECRET": "fixture-secret",
        "EXOMEM_GITHUB_USERNAME": "example-owner",
        "EXOMEM_JWT_SIGNING_KEY": ROOT,
        "EXOMEM_REST_API_KEY": REST_KEY,
        "EXOMEM_UPLOAD_TOKEN": UPLOAD_TOKEN,
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "writer-state"),
    }.items():
        managed.setenv(name, value)

    async def inline_threadpool(function, *args, **kwargs):
        return function(*args, **kwargs)

    managed.setattr(server_transfer, "run_in_threadpool", inline_threadpool)
    return managed


def _real_app(tmp_path: Path) -> Any:
    """The shipped server as a supervisor-owned worker builds it."""
    worker_socket = tmp_path / "worker.sock"
    mcp = server.build_server(require_auth=True, worker_socket=worker_socket)
    return mcp.http_app(
        middleware=server.http_middleware(mcp, worker_socket=worker_socket),
        stateless_http=True,
    )


@pytest.fixture
def real_worker(tmp_path: Path, real_env: pytest.MonkeyPatch) -> _RealWorker:
    import asyncio

    from exomem import server_auth

    app = _real_app(tmp_path)
    identity = SessionIdentity(github_user_id=OWNER_ID, github_login="example-owner")

    async def issue() -> tuple[str, str]:
        local, _ = await server_auth.build_local_session_authority().issue(
            client_id="home", scopes=list(local_ingress.LOCAL_SCOPES), identity=identity
        )
        public, _ = await server_auth.build_session_authority(base_url=ISSUER).issue(
            client_id="remote-client", scopes=["exomem:read"], identity=identity
        )
        return local, public

    local_token, public_token = asyncio.run(issue())
    return _RealWorker(app=app, local_token=local_token, public_token=public_token)


@asynccontextmanager
async def _real_doors(worker: _RealWorker) -> AsyncIterator[_Doors]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker.app), base_url="http://worker"
    ) as upstream:
        ingress = ServiceIngress(ingress_key=KEY)
        ingress.resume(upstream)
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=LocalListener(ingress)), base_url=LOCAL_BASE
            ) as local,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=ingress), base_url=ISSUER) as public,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=worker.app), base_url=LOCAL_BASE
            ) as direct,
        ):
            yield _Doors(local=local, public=public, worker=direct)
        await ingress.aclose()


def _capture_rest_principal(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from exomem import writer_lease

    kinds: list[str] = []

    def invoke(command: Any, *args: Any, **kwargs: Any) -> dict[str, str]:
        kinds.append(principal_module.effective_principal().principal_kind)
        return {"ok": "yes"}

    monkeypatch.setattr(writer_lease, "invoke_command", invoke)
    return kinds


@pytest.mark.anyio
async def test_rest_accepts_the_local_token_only_on_local_ingress_as_owner_local(
    real_worker: _RealWorker, managed: pytest.MonkeyPatch
) -> None:
    kinds = _capture_rest_principal(managed)
    local_auth = {"authorization": f"Bearer {real_worker.local_token}"}
    async with _real_doors(real_worker) as doors:
        local = await doors.local.post("/api/ask_memory", headers=local_auth, json={"query": "x"})
        key_on_local = await doors.local.post(
            "/api/ask_memory", headers={"authorization": f"Bearer {REST_KEY}"}, json={"query": "x"}
        )
        local_on_public = await doors.public.post(
            "/api/ask_memory", headers=local_auth, json={"query": "x"}
        )
        key_on_public = await doors.public.post(
            "/api/ask_memory", headers={"authorization": f"Bearer {REST_KEY}"}, json={"query": "x"}
        )

    assert local.status_code == 200, local.text
    assert key_on_local.status_code == 401
    assert local_on_public.status_code == 401
    assert key_on_public.status_code == 200, key_on_public.text
    assert kinds == ["owner-local", "owner"]


@pytest.mark.anyio
async def test_rest_on_local_ingress_does_not_need_the_rest_key_configured(
    real_worker: _RealWorker, managed: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    managed.delenv("EXOMEM_REST_API_KEY")
    # The facade reads the key when it registers its routes; rebuild without it.
    real_worker.app = _real_app(tmp_path)
    kinds = _capture_rest_principal(managed)
    async with _real_doors(real_worker) as doors:
        local = await doors.local.post(
            "/api/ask_memory",
            headers={"authorization": f"Bearer {real_worker.local_token}"},
            json={"query": "x"},
        )
        public = await doors.public.post(
            "/api/ask_memory", headers={"authorization": f"Bearer {REST_KEY}"}, json={"query": "x"}
        )
    assert local.status_code == 200, local.text
    assert public.status_code == 503
    assert kinds == ["owner-local"]


@pytest.mark.anyio
async def test_upload_over_local_ingress_preserves_the_bytes_and_nothing_else_opens_it(
    real_worker: _RealWorker, vault: Path
) -> None:
    payload = b"\x89PNG local bytes"

    def form() -> dict[str, Any]:
        return {
            "files": {"file": ("shot.png", payload, "image/png")},
            "data": {"scope": "Local", "category": "Attachments"},
        }

    async with _real_doors(real_worker) as doors:
        local = await doors.local.post(
            "/upload", headers={"authorization": f"Bearer {real_worker.local_token}"}, **form()
        )
        static_on_local = await doors.local.post(
            "/upload", headers={"authorization": f"Bearer {UPLOAD_TOKEN}"}, **form()
        )
        local_on_public = await doors.public.post(
            "/upload", headers={"authorization": f"Bearer {real_worker.local_token}"}, **form()
        )
        download = await doors.local.get(
            "/download",
            params={"path": "x"},
            headers={"authorization": f"Bearer {real_worker.local_token}"},
        )

    assert local.status_code == 201, local.text
    handle = local.json()
    assert (vault / handle["path"]).read_bytes() == payload
    assert handle["hash"] == hashlib.sha256(payload).hexdigest()
    assert static_on_local.status_code == 401
    assert local_on_public.status_code == 401
    assert download.status_code == 404


@pytest.mark.anyio
async def test_local_requests_are_logged_with_ingress_and_client_and_nothing_secret(
    real_worker: _RealWorker, managed: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _capture_rest_principal(managed)
    managed.delenv("EXOMEM_DISABLE_ACCESS_LOG", raising=False)
    caplog.set_level(logging.DEBUG)
    async with _real_doors(real_worker) as doors:
        await doors.local.post(
            "/api/ask_memory",
            headers={"authorization": f"Bearer {real_worker.local_token}"},
            json={"query": "a private question"},
        )
        await doors.local.post(
            "/api/ask_memory",
            headers={"authorization": f"Bearer {real_worker.public_token}"},
            json={"query": "x"},
        )
        await doors.public.post(
            "/api/ask_memory", headers={"authorization": f"Bearer {REST_KEY}"}, json={"query": "x"}
        )

    access = [r for r in caplog.records if r.name == "exomem.access"]
    local_rows = [r for r in access if r.fields.get("ingress") == "local"]
    assert len(local_rows) == 1
    assert local_rows[0].fields["client_id"] == "home"
    # Every other row keeps the public record's shape: no ingress field at all.
    others = [r for r in access if r not in local_rows]
    assert others and all("ingress" not in r.fields for r in others)
    assert any("event=local_ingress_refused" in r.getMessage() for r in caplog.records)
    everything = "\n".join(
        f"{r.getMessage()} {getattr(r, 'fields', '')} {getattr(r, 'content', '')}"
        for r in caplog.records
    )
    for secret in (real_worker.local_token, real_worker.public_token, KEY, REST_KEY):
        assert secret not in everything
    assert "a private question" not in everything


@pytest.mark.anyio
async def test_owner_static_credentials_through_the_tunnel_are_counted_not_refused(
    real_worker: _RealWorker, managed: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _capture_rest_principal(managed)
    caplog.set_level(logging.INFO, logger="exomem.local_ingress")
    tunnel = {"cf-ray": "8f00000000000000-AMS"}
    async with _real_doors(real_worker) as doors:
        rest = await doors.public.post(
            "/api/ask_memory",
            headers={**tunnel, "authorization": f"Bearer {REST_KEY}"},
            json={"query": "x"},
        )
        loopback = await doors.public.post(
            "/api/ask_memory", headers={"authorization": f"Bearer {REST_KEY}"}, json={"query": "x"}
        )
        upload = await doors.public.post(
            "/upload",
            headers={**tunnel, "authorization": f"Bearer {UPLOAD_TOKEN}"},
            files={"file": ("a.txt", b"bytes", "text/plain")},
            data={"scope": "Local", "category": "Tunnel"},
        )

    assert rest.status_code == 200
    assert loopback.status_code == 200
    assert upload.status_code == 201, upload.text
    counted = [r.getMessage() for r in caplog.records if "owner_credential_transit" in r.getMessage()]
    assert counted == [
        "event=owner_credential_transit credential=rest_api_key transit=cloudflare",
        "event=owner_credential_transit credential=upload_token transit=cloudflare",
    ]
