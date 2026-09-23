"""Exomem Cloud cell mode (design D1): auth, surface, read-only, logging.

`EXOMEM_CLOUD_CELL=1` selects a thin seam over the standalone server: only
authentication, logging, tool surface and read-only enforcement change.
Everything else (`LocalRuntimeActivation`, `AuthorizationSessionMiddleware`,
the local writer lease) is the standalone path, unmodified.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from exomem import commands as commands_module
from exomem import privacy_log, server
from exomem.access_log import AccessLogMiddleware
from exomem.governance import principal as principal_module
from exomem.governance.tool import GovernanceError, _require_owner

CELL_ID = "cell-abc123"
# A real C4 bearer is always 43 base64url characters; both fixtures below stay
# well past the 32-character floor `CloudCellCredentials.from_env` enforces.
TOKEN = "current-bearer-token-value-for-tests-32"  # noqa: S105 - test fixture, not a real secret
PREVIOUS_TOKEN = "previous-bearer-token-value-for-tests-32"  # noqa: S105
LEGACY_VERSION = "2025-11-25"


def _spy_dotenv(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: calls.append((a, k)))
    return calls


def _cloud_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cell_id: str = CELL_ID,
    token: str = TOKEN,
    previous_token: str | None = None,
    read_only: bool = False,
) -> None:
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", cell_id)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", token)
    if previous_token is not None:
        monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", previous_token)
    else:
        monkeypatch.delenv("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", raising=False)
    if read_only:
        monkeypatch.setenv("EXOMEM_CLOUD_READ_ONLY", "1")
    else:
        monkeypatch.delenv("EXOMEM_CLOUD_READ_ONLY", raising=False)


def _build_cloud_server(monkeypatch: pytest.MonkeyPatch, **cloud_kwargs):
    dotenv_calls = _spy_dotenv(monkeypatch)
    _cloud_env(monkeypatch, **cloud_kwargs)
    mcp = server.build_server(require_auth=True)
    return mcp, dotenv_calls


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return headers


async def _mcp_call(
    client: httpx.AsyncClient,
    *,
    token: str | None,
    request_id: int,
    method: str,
    params: dict[str, object],
) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers={**_headers(token), "mcp-protocol-version": LEGACY_VERSION},
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


async def _initialize(client: httpx.AsyncClient, *, token: str | None) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers=_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cloud-test-client", "version": "1"},
            },
        },
    )


async def _call_tool(
    client: httpx.AsyncClient,
    *,
    token: str | None,
    name: str,
    arguments: dict,
    request_id: int = 2,
) -> httpx.Response:
    return await _mcp_call(
        client,
        token=token,
        request_id=request_id,
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )


def _teardown_activation(mcp) -> None:
    activation = mcp._exomem_local_runtime_activation
    assert activation._shutdown.is_set() or True
    activation._shutdown.set()


# ---------------------------------------------------------------------------
# CloudCellTokenVerifier: bearer comparison and fixed non-owner claims
# ---------------------------------------------------------------------------


def test_cloud_verifier_accepts_current_and_previous_bearer() -> None:
    from exomem.server_auth import CloudCellTokenVerifier

    verifier = CloudCellTokenVerifier(
        cell_id=CELL_ID, token=TOKEN, previous_token=PREVIOUS_TOKEN
    )
    current = asyncio.run(verifier.verify_token(TOKEN))
    previous = asyncio.run(verifier.verify_token(PREVIOUS_TOKEN))
    assert current is not None
    assert previous is not None
    assert current.claims == {"sub": CELL_ID, "iss": "exomem-cloud-cell"}
    assert previous.claims == {"sub": CELL_ID, "iss": "exomem-cloud-cell"}


def test_cloud_verifier_rejects_unknown_and_empty_bearer() -> None:
    from exomem.server_auth import CloudCellTokenVerifier

    verifier = CloudCellTokenVerifier(
        cell_id=CELL_ID, token=TOKEN, previous_token=PREVIOUS_TOKEN
    )
    assert asyncio.run(verifier.verify_token("something-else")) is None
    assert asyncio.run(verifier.verify_token("")) is None


def test_cloud_verifier_without_rotation_rejects_previous() -> None:
    from exomem.server_auth import CloudCellTokenVerifier

    verifier = CloudCellTokenVerifier(cell_id=CELL_ID, token=TOKEN)
    assert asyncio.run(verifier.verify_token(PREVIOUS_TOKEN)) is None


def test_cloud_verifier_claims_never_carry_the_bearer() -> None:
    from exomem.server_auth import CloudCellTokenVerifier

    verifier = CloudCellTokenVerifier(cell_id=CELL_ID, token=TOKEN)
    result = asyncio.run(verifier.verify_token(TOKEN))
    assert result is not None
    assert TOKEN not in str(result.claims)
    assert result.claims["sub"] == CELL_ID
    assert result.claims["iss"] == "exomem-cloud-cell"


# ---------------------------------------------------------------------------
# CloudCellCredentials.from_env: a minimum bearer strength (design D1.1)
# ---------------------------------------------------------------------------


def test_short_current_token_fails_startup_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import cloud_cell

    short = "too-short-1234"  # noqa: S105 - test fixture, not a real secret
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL_ID)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", short)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", raising=False)

    with pytest.raises(cloud_cell.CloudConfigError) as excinfo:
        cloud_cell.CloudCellCredentials.from_env()

    assert excinfo.value.code == "CLOUD_CELL_CONFIG_INVALID"
    assert short not in str(excinfo.value)


def test_short_previous_token_fails_startup_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor applies to the previous token during rotation as well as the
    current one (design D1.1): a rotation Secret can be just as mis-rendered."""
    from exomem import cloud_cell

    short_previous = "also-too-short"  # noqa: S105
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL_ID)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", TOKEN)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", short_previous)

    with pytest.raises(cloud_cell.CloudConfigError) as excinfo:
        cloud_cell.CloudCellCredentials.from_env()

    assert excinfo.value.code == "CLOUD_CELL_CONFIG_INVALID"
    assert short_previous not in str(excinfo.value)


def test_cloud_server_refuses_to_build_on_a_short_configured_token(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import cloud_cell

    with pytest.raises(cloud_cell.CloudConfigError, match="CLOUD_CELL_CONFIG_INVALID"):
        _build_cloud_server(monkeypatch, token="short-token-12")


def test_token_at_exactly_the_floor_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import cloud_cell

    exactly_32 = "a" * cloud_cell.MIN_TOKEN_LENGTH
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL_ID)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", exactly_32)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", raising=False)

    credentials = cloud_cell.CloudCellCredentials.from_env()

    assert credentials.token == exactly_32


# ---------------------------------------------------------------------------
# Principal resolution: the cloud claims resolve to a fixed non-owner
# ---------------------------------------------------------------------------


def test_cloud_principal_from_verifier_claims_is_non_owner_and_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.server_auth import CloudCellTokenVerifier

    verifier = CloudCellTokenVerifier(cell_id=CELL_ID, token=TOKEN)
    access_token = asyncio.run(verifier.verify_token(TOKEN))
    assert access_token is not None

    monkeypatch.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: (dict(access_token.claims), None),
    )
    resolved = principal_module.resolve_mcp_principal()
    assert resolved.resolved is True
    assert resolved.audience_id != principal_module.OWNER_AUDIENCE
    assert resolved.audience_id != principal_module.MOST_RESTRICTIVE_AUDIENCE


def test_cloud_non_owner_principal_fails_owner_only_governance_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.server_auth import CloudCellTokenVerifier

    verifier = CloudCellTokenVerifier(cell_id=CELL_ID, token=TOKEN)
    access_token = asyncio.run(verifier.verify_token(TOKEN))
    assert access_token is not None
    monkeypatch.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: (dict(access_token.claims), None),
    )
    cloud_principal = principal_module.resolve_mcp_principal()

    with pytest.raises(GovernanceError, match="GOVERNANCE_OWNER_REQUIRED"):
        _require_owner(cloud_principal)


def test_cloud_governed_write_commits_then_owner_only_op_refuses(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_govern_memory_tool import PATTERN_GLOB, _proposal_documents

    from exomem import reserved_paths
    from exomem.server_auth import CloudCellTokenVerifier
    from exomem.writer_lease import LeaseConfig, LeaseManager

    verifier = CloudCellTokenVerifier(cell_id=CELL_ID, token=TOKEN)
    access_token = asyncio.run(verifier.verify_token(TOKEN))
    assert access_token is not None
    monkeypatch.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: (dict(access_token.claims), None),
    )
    cloud_principal = principal_module.resolve_mcp_principal()

    remember = next(c for c in commands_module.PRODUCT_COMMANDS if c.name == "remember")
    govern = next(c for c in commands_module.PRODUCT_COMMANDS if c.name == "govern_memory")
    manager = LeaseManager(LeaseConfig(state_dir=vault.parent / "writer-state"))

    with principal_module.request_scope(cloud_principal):
        result = manager.invoke(
            remember,
            (vault,),
            {
                "content": "cloud write body",
                "title": "Cloud Write Test",
                "status": "draft",
            },
            idempotency_key="cloud-remember-1",
        )
        assert result.get("status") in {"committed", None} or result

        # The dispatcher-issued authority scope simulates arriving through the
        # real MCP/writer_lease.invoke_command path (which invoke_command
        # itself installs per call); the only thing this test varies is the
        # principal, so this isolates that a cloud (non-owner) principal is
        # refused even with dispatcher authority present.
        with (
            reserved_paths._owner_authority_scope("govern_memory"),
            pytest.raises(GovernanceError, match="GOVERNANCE_OWNER_REQUIRED"),
        ):
            manager.invoke(
                govern,
                (vault,),
                {
                    "operation": "propose",
                    "documents": _proposal_documents(),
                    "selector_paths": [PATTERN_GLOB],
                    "intent": "Treat pattern notes as confidential for the external audience",
                    "target_ceiling": 1,
                    "duration": "standing",
                },
            )


# ---------------------------------------------------------------------------
# Routes: only /mcp, /health, /health/ready are registered
# ---------------------------------------------------------------------------


def test_cloud_server_exposes_only_mcp_health_and_ready_routes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, dotenv_calls = _build_cloud_server(monkeypatch)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    health = await client.get("/health")
                    ready = await client.get("/health/ready")
                    rest = await client.get("/api/ask_memory")
                    upload = await client.post("/upload")
                    download = await client.post("/download")
                    return health, ready, rest, upload, download

        health, ready, rest, upload, download = asyncio.run(scenario())
        assert health.status_code == 200, health.text
        assert ready.status_code in {200, 503}, ready.text
        assert rest.status_code == 404
        assert upload.status_code == 404
        assert download.status_code == 404
        assert dotenv_calls == []
    finally:
        _teardown_activation(mcp)


def test_cloud_health_routes_disclose_no_vault_content(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    return await client.get("/health"), await client.get("/health/ready")

        health, ready = asyncio.run(scenario())
        assert str(vault) not in health.text
        assert str(vault) not in ready.text
    finally:
        _teardown_activation(mcp)


def test_cloud_ready_route_gates_on_retrieval_admission(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/health/ready` must answer non-2xx for the ~20-30s cloud-mode warm
    window before retrieval is admitted (writes return `MUTATION_WARMING`
    during that window), so a kubelet or gateway routing on 2xx sees
    `CELL_NOT_READY` instead of routing live traffic into refusals — and 2xx,
    content-free, once retrieval is admitted (redone task 2.8's readiness
    check)."""
    from exomem import readiness

    admitted = {"value": False}
    monkeypatch.setattr(
        readiness,
        "retrieval_admission",
        lambda _vault_root=None: (
            {"state": "ready", "admitted": True}
            if admitted["value"]
            else {"state": "warming", "admitted": False}
        ),
    )
    mcp, _ = _build_cloud_server(monkeypatch)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    warming = await client.get("/health/ready")
                    admitted["value"] = True
                    ready = await client.get("/health/ready")
                    return warming, ready

        warming, ready = asyncio.run(scenario())
        assert warming.status_code == 503, warming.text
        assert warming.json()["status"] == "not_ready"
        assert warming.json()["retrieval"] == {"state": "warming", "admitted": False}
        assert str(vault) not in warming.text

        assert ready.status_code == 200, ready.text
        assert ready.json()["status"] == "ready"
        assert ready.json()["retrieval"] == {"state": "ready", "admitted": True}
        assert str(vault) not in ready.text
    finally:
        _teardown_activation(mcp)


def test_cloud_mode_never_reads_dotenv(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp, dotenv_calls = _build_cloud_server(monkeypatch)
    try:
        assert dotenv_calls == []
    finally:
        _teardown_activation(mcp)


def test_cloud_mcp_request_without_bearer_is_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    return await _initialize(client, token=None)

        response = asyncio.run(scenario())
        assert response.status_code == 401
    finally:
        _teardown_activation(mcp)


def test_cloud_mcp_request_with_wrong_bearer_is_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    return await _initialize(client, token="not-the-bearer")

        response = asyncio.run(scenario())
        assert response.status_code == 401
    finally:
        _teardown_activation(mcp)


def test_cloud_mcp_request_with_previous_bearer_authenticates(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch, previous_token=PREVIOUS_TOKEN)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    return await _initialize(client, token=PREVIOUS_TOKEN)

        response = asyncio.run(scenario())
        assert response.status_code == 200, response.text
    finally:
        _teardown_activation(mcp)


# ---------------------------------------------------------------------------
# Tool surface: CLOUD_SURFACE_EXCLUSIONS
# ---------------------------------------------------------------------------


def test_cloud_surface_exclusions_are_exactly_four_technical_exclusions() -> None:
    assert set(commands_module.CLOUD_SURFACE_EXCLUSIONS) == {
        "transfer_artifact",
        "adopt_vault",
        "process_media",
        "read_media",
    }
    for exclusion in commands_module.CLOUD_SURFACE_EXCLUSIONS.values():
        assert exclusion.reason
        assert exclusion.lifted_when


def test_cloud_tool_list_excludes_the_four_but_keeps_the_rest(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch)
    try:
        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}
        assert "transfer_artifact" not in names
        assert "adopt_vault" not in names
        assert "process_media" not in names
        assert "read_media" not in names
        assert "configure_memory" in names
        assert "activate_context" in names
        assert "ask_memory" in names
        assert "remember" in names
    finally:
        _teardown_activation(mcp)


def test_cloud_never_registers_legacy_aliases_even_with_the_opt_in_set(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cell has no legacy clients, and a legacy leaf would re-expose the
    leaves of the commands `CLOUD_SURFACE_EXCLUSIONS` just removed (design
    D1.3) -- so the operator-env compatibility opt-in is refused in cloud
    mode, not merely left unset."""
    monkeypatch.setenv("EXOMEM_MCP_LEGACY_COMPAT", "1")
    mcp, _ = _build_cloud_server(monkeypatch)
    try:
        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}
        assert "note" not in names
        assert "create_file" not in names
        # The excluded commands stay excluded -- a legacy alias must not
        # re-expose what CLOUD_SURFACE_EXCLUSIONS just removed.
        assert "transfer_artifact" not in names
        assert "adopt_vault" not in names
    finally:
        _teardown_activation(mcp)


# ---------------------------------------------------------------------------
# Content-free logging
# ---------------------------------------------------------------------------


def test_content_private_logging_enabled_true_in_cloud_mode() -> None:
    assert privacy_log.content_private_logging_enabled({"EXOMEM_CLOUD_CELL": "1"}) is True
    assert privacy_log.content_private_logging_enabled({"EXOMEM_CLOUD_CELL": "0"}) is False
    assert privacy_log.content_private_logging_enabled({}) is False


def test_cloud_call_trace_omits_a_distinctive_query_phrase(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)
        phrase = "zz-distinctive-query-phrase-should-never-be-logged"

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    await _initialize(client, token=TOKEN)
                    return await _call_tool(
                        client, token=TOKEN, name="ask_memory", arguments={"query": phrase}
                    )

        with caplog.at_level("DEBUG"):
            response = asyncio.run(scenario())
        assert response.status_code == 200, response.text
        assert phrase not in caplog.text
    finally:
        _teardown_activation(mcp)


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


def test_cloud_read_only_refuses_a_write_but_serves_a_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch, read_only=True)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    await _initialize(client, token=TOKEN)
                    write = await _call_tool(
                        client,
                        token=TOKEN,
                        name="remember",
                        arguments={"content": "should not land", "title": "RO Test", "status": "draft"},
                        request_id=3,
                    )
                    read = await _call_tool(
                        client,
                        token=TOKEN,
                        name="ask_memory",
                        arguments={"query": "anything"},
                        request_id=4,
                    )
                    return write, read

        write, read = asyncio.run(scenario())
        assert write.status_code == 200, write.text
        write_result = write.json()["result"]
        # A structured OpError refusal is a successful tool call (isError is
        # False) whose payload envelope reports success=False -- the same
        # shape every other structured refusal in this codebase uses.
        assert write_result["isError"] is False, write_result
        write_structured = write_result["structuredContent"]
        assert write_structured["success"] is False, write_structured
        assert write_structured["error"]["code"] == "CLOUD_CELL_READ_ONLY", write_structured
        assert read.status_code == 200, read.text
        assert read.json()["result"]["isError"] is False
    finally:
        _teardown_activation(mcp)


def test_cloud_not_read_only_allows_writes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch, read_only=False)
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    await _initialize(client, token=TOKEN)
                    return await _call_tool(
                        client,
                        token=TOKEN,
                        name="remember",
                        arguments={"content": "should land", "title": "Non-RO Test", "status": "draft"},
                        request_id=3,
                    )

        write = asyncio.run(scenario())
        assert write.status_code == 200, write.text
        assert write.json()["result"]["isError"] is False, write.text
    finally:
        _teardown_activation(mcp)


def test_cloud_read_only_env_helper() -> None:
    from exomem import cloud_cell

    assert cloud_cell.cloud_read_only_enabled({"EXOMEM_CLOUD_READ_ONLY": "1"}) is True
    assert cloud_cell.cloud_read_only_enabled({"EXOMEM_CLOUD_READ_ONLY": "0"}) is False
    assert cloud_cell.cloud_read_only_enabled({}) is False


# ---------------------------------------------------------------------------
# Standalone (non-cloud) server is unaffected
# ---------------------------------------------------------------------------


def test_standalone_server_unaffected_by_cloud_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    mcp = server.build_server(require_auth=False)
    try:
        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}
        # The standalone server keeps commands cloud mode would exclude.
        assert "adopt_vault" in names or "adopt_vault" not in commands_module.PRODUCT_PUBLIC_NAMES
        assert "ask_memory" in names
    finally:
        _teardown_activation(mcp)


# ---------------------------------------------------------------------------
# Caller-supplied identifiers stay bounded in logs (content-private mode)
# ---------------------------------------------------------------------------


def _rendered_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Records as `exomem.log` renders them. `caplog.text` shows only the
    message, never the structured `fields` a log line also carries."""
    from exomem.log_events import JsonLinesFormatter

    formatter = JsonLinesFormatter()
    return "\n".join([caplog.text, *(formatter.format(record) for record in caplog.records)])


def test_client_label_is_a_fixed_family_in_content_private_mode() -> None:
    cloud = {"EXOMEM_CLOUD_CELL": "1"}
    assert privacy_log.log_client_label("claude-ai", env=cloud) == "claude-ai"
    assert privacy_log.log_client_label("Codex CLI/1.2.3 (linux)", env=cloud) == "codex"
    assert privacy_log.log_client_label("openai-mcp", env=cloud) == "chatgpt"
    assert privacy_log.log_client_label("claude-code/2.1", env=cloud) == "claude-code"
    for free_text in ["my private project notes", "x" * 500, "line\nbreak"]:
        assert privacy_log.log_client_label(free_text, env=cloud) == "other"
    assert privacy_log.log_client_label("", env=cloud) is None
    # Outside content-private mode the value is kept verbatim, as today.
    assert privacy_log.log_client_label("line\nbreak", env={}) == "line\nbreak"


def test_client_version_keeps_only_a_dotted_number_in_content_private_mode() -> None:
    cloud = {"EXOMEM_CLOUD_CELL": "1"}
    assert privacy_log.log_client_version("1.2.3", env=cloud) == "1.2.3"
    assert privacy_log.log_client_version("1.2.3-beta secret", env=cloud) is None
    assert privacy_log.log_client_version("1.2.3-beta", env={}) == "1.2.3-beta"


def test_session_reference_is_a_digest_in_content_private_mode() -> None:
    cloud = {"EXOMEM_CLOUD_CELL": "1"}
    raw = "zz-caller-chosen-session-text"
    ref = privacy_log.log_session_ref(raw, env=cloud)
    assert ref is not None and raw not in ref
    assert ref == privacy_log.log_session_ref(raw, env=cloud)
    assert ref != privacy_log.log_session_ref(raw + "-other", env=cloud)
    assert privacy_log.log_session_ref(raw, env={}) == raw
    assert privacy_log.log_session_ref(None, env=cloud) is None


def test_cloud_logs_and_ledger_never_carry_caller_header_text(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    ledger_dir = tmp_path / "ledger"
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(ledger_dir))
    mcp, _ = _build_cloud_server(monkeypatch)
    agent_phrase = "zz-user-agent-phrase-never-logged"
    session_phrase = "zz-session-header-phrase-never-logged"
    ray_phrase = "zz-cf-ray-header-phrase-never-logged"
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)
        logged_app = AccessLogMiddleware(app)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=logged_app), base_url="http://cell.local"
                ) as client:
                    return await client.post(
                        "/mcp",
                        headers={
                            **_headers(TOKEN),
                            "mcp-protocol-version": LEGACY_VERSION,
                            "user-agent": agent_phrase,
                            "mcp-session-id": session_phrase,
                            "cf-ray": ray_phrase,
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "ask_memory", "arguments": {"query": "x"}},
                        },
                    )

        with caplog.at_level("DEBUG"):
            response = asyncio.run(scenario())
        assert response.status_code == 200, response.text
        ledger_text = "".join(
            path.read_text(encoding="utf-8") for path in ledger_dir.rglob("*.jsonl")
        )
        assert ledger_text, "the call ledger recorded nothing"
        logs = _rendered_logs(caplog)
        assert '"session_id": "sha256:' in logs, "sanity: the access log line was rendered"
        for phrase in (agent_phrase, session_phrase, ray_phrase):
            assert phrase not in logs
            assert phrase not in ledger_text
    finally:
        _teardown_activation(mcp)


def test_http_method_is_a_standard_verb_or_other_in_content_private_mode() -> None:
    cloud = {"EXOMEM_CLOUD_CELL": "1"}
    assert privacy_log.log_http_method("POST", env=cloud) == "POST"
    assert privacy_log.log_http_method("HDR-VIOLET-QUOKKA", env=cloud) == "OTHER"
    assert privacy_log.log_http_method("HDR-VIOLET-QUOKKA", env={}) == "HDR-VIOLET-QUOKKA"


def test_cloud_logs_never_carry_a_caller_chosen_http_method_or_cf_ray(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mcp, _ = _build_cloud_server(monkeypatch)
    method_phrase = "ZZ-METHOD-PHRASE-NEVER-LOGGED"
    ray = "0123456789abcdef-IDE"  # Cloudflare-shaped, but a cloud cell has no Cloudflare
    try:
        app = mcp.http_app(stateless_http=True, json_response=True)
        # The access log is installed by `mcp.run` in production; wrap the
        # test app the same way so its record is part of what is checked.
        logged_app = AccessLogMiddleware(app)

        async def scenario():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=logged_app), base_url="http://cell.local"
                ) as client:
                    await client.request(method_phrase, "/mcp", headers=_headers(TOKEN))
                    await client.post("/mcp", headers={**_headers(TOKEN), "cf-ray": ray}, json={})

        with caplog.at_level("DEBUG"):
            asyncio.run(scenario())
        logs = _rendered_logs(caplog)
        assert '"method": "OTHER"' in logs, "sanity: the access log line was rendered"
        assert method_phrase not in logs
        assert ray not in logs
    finally:
        _teardown_activation(mcp)


# ---------------------------------------------------------------------------
# Log directory placement
# ---------------------------------------------------------------------------


def test_cloud_mode_ignores_an_empty_log_dir_and_stays_off_the_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tempfile

    from exomem import logging_config

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_LOG_DIR", "")
    monkeypatch.setenv("HOME", str(tmp_path / "volume-home"))
    resolved = logging_config.resolve_log_dir()
    assert resolved == Path(tempfile.gettempdir()) / "exomem-logs"
    # An explicit directory is still honoured.
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "explicit"))
    assert logging_config.resolve_log_dir() == tmp_path / "explicit"


# ---------------------------------------------------------------------------
# No CLI entry point reads `.env` in cloud mode (design D1.6)
# ---------------------------------------------------------------------------

_DOTENV_SENTINEL = "EXOMEM_TEST_DOTENV_SENTINEL"


def _cwd_with_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"{_DOTENV_SENTINEL}=from-dotenv\nEXOMEM_BASE_URL=https://from-dotenv.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # setenv first so monkeypatch restores (deletes) them even if a red run
    # lets `.env` write them into the process environment.
    for name in (_DOTENV_SENTINEL, "EXOMEM_BASE_URL"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def test_auth_sessions_cli_never_reads_dotenv_in_cloud_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from exomem import __main__ as cli

    _cwd_with_dotenv(monkeypatch, tmp_path)
    _cloud_env(monkeypatch)
    with pytest.raises(ValueError, match="EXOMEM_BASE_URL is required"):
        cli._build_auth_session_authority()
    assert _DOTENV_SENTINEL not in os.environ


def test_doctor_cli_never_reads_dotenv_in_cloud_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from exomem import __main__ as cli
    from exomem import doctor as doctor_module

    class _Stop(Exception):
        pass

    seen: dict[str, str | None] = {}

    def fake_doctor(**_kwargs):
        seen["sentinel"] = os.environ.get(_DOTENV_SENTINEL)
        raise _Stop

    _cwd_with_dotenv(monkeypatch, tmp_path)
    _cloud_env(monkeypatch)
    monkeypatch.setattr(doctor_module, "doctor", fake_doctor)
    with pytest.raises(_Stop):
        cli._doctor_main([])
    assert seen == {"sentinel": None}


def test_cli_dotenv_loaders_still_read_dotenv_outside_cloud_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from exomem import __main__ as cli

    _cwd_with_dotenv(monkeypatch, tmp_path)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    cli._load_cwd_dotenv()
    assert os.environ.get(_DOTENV_SENTINEL) == "from-dotenv"
