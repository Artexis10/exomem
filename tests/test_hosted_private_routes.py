from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from exomem import cli_ops, hosted_runtime, schema, server, server_runtime
from exomem import hosted_gateway as gateway
from exomem.hosted_runtime import (
    HostedCellConfig,
    HostedCellLifecycle,
    HostedResourceLimits,
    provision_hosted_cell,
)
from exomem.server_hosted import register_hosted_routes

SENSITIVE_QUERY = "sensitive-query-sentinel-7f3c"
SENSITIVE_PATH = "Knowledge Base/private-path-sentinel-91d2.md"
DEFAULT_REQUEST_ID = "11111111-1111-4111-8111-111111111111"


def _principal(label: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(label.encode()).digest()).rstrip(b"=").decode()


DEFAULT_PRINCIPAL = _principal("principal-default")
SHARED_PRINCIPAL = _principal("principal-shared")


class _ASGIClient:
    """Synchronous facade around HTTPX's Python 3.14-safe ASGI transport."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def request_with_lifespan(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with self.app.router.lifespan_context(self.app):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)


@pytest.fixture(autouse=True)
def _python_314_inline_route_threadpool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid the current AnyIO worker-return deadlock in route contract tests."""

    async def inline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("exomem.server_hosted.run_in_threadpool", inline)


class IsolatedInvoker:
    def __init__(self) -> None:
        self.completed: dict[str, tuple[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        command,
        *injected,
        idempotency_key: str | None = None,
        implicit_idempotency_scope: str | None = None,
        **kwargs,
    ) -> Any:
        self.calls.append(
            {
                "command": command.name,
                "vault": injected[0],
                "idempotency_key": idempotency_key,
                "implicit_scope": implicit_idempotency_scope,
            }
        )
        digest = hashlib.sha256(repr((command.name, sorted(kwargs.items()))).encode()).hexdigest()
        if idempotency_key and idempotency_key in self.completed:
            previous_digest, result = self.completed[idempotency_key]
            if previous_digest != digest:
                raise cli_ops.OpError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "idempotency key was already used for different input",
                )
            return result
        result = command.leaf(*injected, **kwargs)
        if idempotency_key:
            self.completed[idempotency_key] = (digest, result)
        return result


def _cell(
    tmp_path: Path,
    *,
    cell_id: str,
    credential: str,
    invoker: Any | None = None,
    guard_events: list[str] | None = None,
) -> tuple[_ASGIClient, HostedCellConfig, HostedCellLifecycle, IsolatedInvoker]:
    vault_root = tmp_path / cell_id / "vault"
    from exomem.init import init_vault

    init_vault(vault_root)
    config = HostedCellConfig(
        cell_id=cell_id,
        vault_root=vault_root,
        state_root=tmp_path / cell_id / "state",
        log_root=tmp_path / cell_id / "logs",
        service_credential=credential,
        resource_limits=HostedResourceLimits(
            storage_bytes=1024 * 1024,
            upload_bytes=4096,
            worker_count=0,
        ),
    )
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    isolated = invoker or IsolatedInvoker()

    @contextmanager
    def mutation_guard(_vault_root: Path) -> Iterator[None]:
        if guard_events is not None:
            guard_events.append("guard-enter")
        try:
            yield
        finally:
            if guard_events is not None:
                guard_events.append("guard-exit")

    app = FastMCP(f"test-{cell_id}")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=schema.load_source_schema(vault_root),
        invoke_command_func=isolated,
        mutation_guard_factory=mutation_guard,
    )
    return _ASGIClient(app.http_app()), config, lifecycle, isolated


def _headers(
    config: HostedCellConfig,
    *,
    credential: str | None = None,
    cell_id: str | None = None,
    protocol: str | None = None,
    request_id: str = DEFAULT_REQUEST_ID,
    principal: str = DEFAULT_PRINCIPAL,
    idempotency_key: str | None = None,
    **extra: str,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {credential or config.service_credential}",
        gateway.CELL_HEADER: cell_id or config.cell_id,
        gateway.PROTOCOL_HEADER: protocol or config.protocol_version,
        gateway.REQUEST_HEADER: request_id,
        gateway.PRINCIPAL_HEADER: principal,
        **extra,
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _remember_body(sentinel: str) -> dict[str, str]:
    return {
        "note_type": "insight",
        "title": "Identical hosted title",
        "content": f"# Identical hosted title\n\n## Claim\n\n{sentinel}\n",
    }


def test_every_private_custom_route_manually_requires_service_auth(tmp_path: Path) -> None:
    client, config, _lifecycle, _invoker = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
    )
    valid = _headers(config)
    routes = [
        ("GET", "/private/exomem/v1/contract"),
        ("GET", "/private/exomem/v1/live"),
        ("GET", "/private/exomem/v1/ready"),
        ("POST", "/private/exomem/v1/lifecycle/quiesce"),
        ("POST", "/private/exomem/v1/lifecycle/resume"),
        ("POST", "/private/exomem/v1/lifecycle/seal"),
        ("POST", "/private/exomem/v1/command/ask_memory"),
        ("POST", "/private/exomem/v1/upload"),
        ("GET", "/private/exomem/v1/download"),
    ]

    for method, path in routes:
        response = client.request(method, path)
        assert response.status_code == 401, (path, response.text)
        assert response.json()["error"]["code"] == "HOSTED_UNAUTHORIZED"
        assert config.cell_id not in response.text
        assert config.service_credential not in response.text

    assert client.get("/private/exomem/v1/live", headers=valid).status_code == 200


def test_private_context_rejects_wrong_cell_protocol_and_selector_attacks(
    tmp_path: Path,
) -> None:
    client, config, _lifecycle, invoker = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
    )
    wrong_credential = _headers(config, credential="bravo-private-service-credential-0002")
    wrong_cell = _headers(config, cell_id="cell-bravo")
    wrong_protocol = _headers(config, protocol="999")

    assert client.get("/private/exomem/v1/ready", headers=wrong_credential).status_code == 401
    assert client.get("/private/exomem/v1/ready", headers=wrong_cell).status_code == 403
    assert client.get("/private/exomem/v1/ready", headers=wrong_protocol).status_code == 409

    selector_body = client.post(
        "/private/exomem/v1/command/ask_memory",
        headers=_headers(config),
        json={"query": "safe", "cell_id": "cell-bravo"},
    )
    nested_selector_body = client.post(
        "/private/exomem/v1/command/ask_memory",
        headers=_headers(config),
        json={"query": "safe", "options": [{"cell_id": "cell-bravo"}]},
    )
    selector_header = client.post(
        "/private/exomem/v1/command/ask_memory",
        headers=_headers(config, **{"X-Tenant-Id": "tenant-bravo"}),
        json={"query": "safe"},
    )
    selector_query = client.get(
        "/private/exomem/v1/live",
        headers=_headers(config),
        params={"tenant_id": "tenant-bravo"},
    )
    for response in (
        selector_body,
        nested_selector_body,
        selector_header,
        selector_query,
    ):
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "HOSTED_SELECTOR_REJECTED"
        assert "bravo" not in response.text
    assert invoker.calls == []


def test_private_context_rejects_duplicate_or_malformed_trusted_headers(
    tmp_path: Path,
) -> None:
    client, config, _lifecycle, invoker = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
    )
    valid = list(_headers(config).items())
    duplicate_request = client.get(
        "/private/exomem/v1/ready",
        headers=[
            *valid,
            (gateway.REQUEST_HEADER, "22222222-2222-4222-8222-222222222222"),
        ],
    )
    duplicate_auth = client.get(
        "/private/exomem/v1/ready",
        headers=[*valid, ("Authorization", f"Bearer {config.service_credential}")],
    )
    malformed_request = client.get(
        "/private/exomem/v1/ready",
        headers=_headers(config, request_id="request-with-selector-like-sentinel"),
    )
    malformed_principal = client.get(
        "/private/exomem/v1/ready",
        headers=_headers(config, principal="tenant-bravo:principal-sentinel"),
    )

    for response in (
        duplicate_request,
        duplicate_auth,
        malformed_request,
        malformed_principal,
    ):
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "HOSTED_CONTEXT_INVALID"
        assert "sentinel" not in response.text
    assert invoker.calls == []


def test_two_cells_keep_identical_paths_and_idempotency_keys_isolated(tmp_path: Path) -> None:
    alpha, alpha_config, alpha_lifecycle, alpha_invoker = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
    )
    bravo, bravo_config, _bravo_lifecycle, bravo_invoker = _cell(
        tmp_path,
        cell_id="cell-bravo",
        credential="bravo-private-service-credential-0002",
    )
    public_key = "same-public-idempotency-key"
    alpha_response = alpha.post(
        "/private/exomem/v1/command/remember",
        headers=_headers(
            alpha_config,
            principal=SHARED_PRINCIPAL,
            idempotency_key=public_key,
        ),
        json=_remember_body("ALPHA-ONLY-SENTINEL"),
    )
    bravo_response = bravo.post(
        "/private/exomem/v1/command/remember",
        headers=_headers(
            bravo_config,
            principal=SHARED_PRINCIPAL,
            idempotency_key=public_key,
        ),
        json=_remember_body("BRAVO-ONLY-SENTINEL"),
    )

    assert alpha_response.status_code == bravo_response.status_code == 200
    alpha_path = alpha_response.json()["data"]["path"]
    bravo_path = bravo_response.json()["data"]["path"]
    assert alpha_path == bravo_path
    assert "ALPHA-ONLY-SENTINEL" in (alpha_config.vault_root / alpha_path).read_text()
    assert "BRAVO-ONLY-SENTINEL" in (bravo_config.vault_root / bravo_path).read_text()
    assert alpha_invoker.calls[0]["idempotency_key"] != bravo_invoker.calls[0]["idempotency_key"]
    assert public_key not in alpha_invoker.calls[0]["idempotency_key"]

    replay = alpha.post(
        "/private/exomem/v1/command/remember",
        headers=_headers(
            alpha_config,
            principal=SHARED_PRINCIPAL,
            idempotency_key=public_key,
        ),
        json=_remember_body("ALPHA-ONLY-SENTINEL"),
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["path"] == alpha_path

    bravo_calls = len(bravo_invoker.calls)
    alpha_lifecycle.set_mutation_authority(
        False, reason_code="HOSTED_MUTATION_AUTHORITY_UNAVAILABLE"
    )
    unavailable = alpha.post(
        "/private/exomem/v1/command/remember",
        headers=_headers(
            alpha_config,
            principal=SHARED_PRINCIPAL,
            idempotency_key="unavailable-cell-key",
        ),
        json=_remember_body("MUST-NOT-FALL-BACK"),
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "HOSTED_MUTATION_NOT_ADMITTED"
    assert len(bravo_invoker.calls) == bravo_calls
    assert not list(bravo_config.vault_root.rglob("*MUST-NOT-FALL-BACK*"))


def test_lifecycle_routes_gate_reads_writes_and_sealing(tmp_path: Path) -> None:
    client, config, lifecycle, invoker = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
    )
    headers = _headers(config)

    quiesced = client.post(
        "/private/exomem/v1/lifecycle/quiesce",
        headers=headers,
        json={"timeout_seconds": 1},
    )
    assert quiesced.status_code == 200
    assert quiesced.json()["data"]["phase"] == "quiesced"

    blocked_write = client.post(
        "/private/exomem/v1/command/remember",
        headers=headers,
        json=_remember_body("must-not-write"),
    )
    assert blocked_write.status_code == 503
    assert blocked_write.json()["error"]["code"] == "HOSTED_MUTATION_NOT_ADMITTED"
    assert invoker.calls == []

    read_while_quiesced = client.post(
        "/private/exomem/v1/command/browse_memory",
        headers=headers,
        json={"path": "Knowledge Base"},
    )
    assert read_while_quiesced.status_code == 200

    assert (
        client.post("/private/exomem/v1/lifecycle/seal", headers=headers).json()["error"]["code"]
        == "HOSTED_ROUTING_NOT_STOPPED"
    )
    sealed = client.post(
        "/private/exomem/v1/lifecycle/seal",
        headers={**headers, gateway.ROUTING_STOPPED_HEADER: "true"},
    )
    assert sealed.status_code == 200
    assert sealed.json()["data"]["phase"] == "sealed"
    assert lifecycle.readiness().read_admitted is False
    assert client.post("/private/exomem/v1/lifecycle/resume", headers=headers).status_code == 503


def test_hosted_call_traces_and_errors_omit_query_path_and_arguments(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class SensitiveInvoker:
        def __init__(self) -> None:
            self.fail = False

        def __call__(self, _command, *_injected, **_kwargs):
            if self.fail:
                raise ValueError(f"NOT_FOUND: {SENSITIVE_PATH}")
            return []

    invoker = SensitiveInvoker()
    client, config, _lifecycle, _isolated = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
        invoker=invoker,
    )
    caplog.set_level(logging.INFO)
    headers = _headers(config)

    success = client.post(
        "/private/exomem/v1/command/ask_memory",
        headers=headers,
        json={"query": SENSITIVE_QUERY, "mode": "keyword"},
    )
    assert success.status_code == 200
    invoker.fail = True
    failure = client.post(
        "/private/exomem/v1/command/read_memory",
        headers=headers,
        json={"path": SENSITIVE_PATH},
    )
    assert failure.status_code == 404
    assert failure.json()["error"]["code"] == "NOT_FOUND"
    assert SENSITIVE_PATH not in failure.text
    assert SENSITIVE_QUERY not in caplog.text
    assert SENSITIVE_PATH not in caplog.text
    assert DEFAULT_PRINCIPAL not in caplog.text

    middleware = server.CallTraceMiddleware(hosted=True)
    message = {
        "params": {
            "name": "ask_memory",
            "arguments": {"query": SENSITIVE_QUERY, "path": SENSITIVE_PATH},
        }
    }

    async def next_call(_context):
        return {"ok": True}

    asyncio.run(middleware.on_call_tool(SimpleNamespace(message=message), next_call))
    assert SENSITIVE_QUERY not in caplog.text
    assert SENSITIVE_PATH not in caplog.text


def test_hosted_upload_holds_injected_mutation_guard_only_around_commit(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    client, config, _lifecycle, _invoker = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
        guard_events=events,
    )
    small_grant = gateway.mint_transfer_grant(
        config,
        tenant_scope="tenant-001",
        principal_scope=DEFAULT_PRINCIPAL,
        operation="upload",
        jti="upload-grant-small",
        max_bytes=8,
    )
    oversized = client.post(
        "/private/exomem/v1/upload",
        headers={
            **_headers(config),
            gateway.TRANSFER_GRANT_HEADER: small_grant,
        },
        files={"file": ("too-large.bin", b"123456789", "application/octet-stream")},
        data={"scope": "Case", "category": "Evidence"},
    )
    assert oversized.status_code == 413, oversized.text
    assert oversized.json()["error"]["code"] == "TOO_LARGE"
    assert events == []

    grant = gateway.mint_transfer_grant(
        config,
        tenant_scope="tenant-001",
        principal_scope=DEFAULT_PRINCIPAL,
        operation="upload",
        jti="upload-grant-001",
        max_bytes=128,
    )
    headers = {
        **_headers(config),
        gateway.TRANSFER_GRANT_HEADER: grant,
    }
    uploaded = client.post(
        "/private/exomem/v1/upload",
        headers=headers,
        files={"file": ("proof.bin", b"private evidence", "application/octet-stream")},
        data={"scope": "Case", "category": "Evidence"},
    )

    assert uploaded.status_code == 201, uploaded.text
    assert events == ["guard-enter", "guard-exit"]
    path = uploaded.json()["data"]["path"]
    assert (config.vault_root / path).read_bytes() == b"private evidence"

    replay = client.post(
        "/private/exomem/v1/upload",
        headers=headers,
        files={"file": ("proof.bin", b"changed", "application/octet-stream")},
        data={"scope": "Case", "category": "Evidence"},
    )
    assert replay.status_code == 409
    assert (config.vault_root / path).read_bytes() == b"private evidence"


def test_hosted_transfer_scope_expiry_cross_cell_and_download_isolation(tmp_path: Path) -> None:
    alpha, alpha_config, _alpha_lifecycle, _alpha_invoker = _cell(
        tmp_path,
        cell_id="cell-alpha",
        credential="alpha-private-service-credential-0001",
    )
    bravo, bravo_config, _bravo_lifecycle, _bravo_invoker = _cell(
        tmp_path,
        cell_id="cell-bravo",
        credential="bravo-private-service-credential-0002",
    )
    relative = "Knowledge Base/Notes/shared.md"
    for config, content in (
        (alpha_config, b"ALPHA-DOWNLOAD"),
        (bravo_config, b"BRAVO-DOWNLOAD"),
    ):
        target = config.vault_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    alpha_grant = gateway.mint_transfer_grant(
        alpha_config,
        tenant_scope="tenant-alpha",
        principal_scope=SHARED_PRINCIPAL,
        operation="download",
        jti="download-alpha",
        max_bytes=64,
    )
    bravo_grant = gateway.mint_transfer_grant(
        bravo_config,
        tenant_scope="tenant-bravo",
        principal_scope=SHARED_PRINCIPAL,
        operation="download",
        jti="download-bravo",
        max_bytes=64,
    )
    alpha_headers = {
        **_headers(
            alpha_config,
            principal=SHARED_PRINCIPAL,
        ),
        gateway.TRANSFER_GRANT_HEADER: alpha_grant,
    }
    bravo_headers = {
        **_headers(
            bravo_config,
            principal=SHARED_PRINCIPAL,
        ),
        gateway.TRANSFER_GRANT_HEADER: bravo_grant,
    }
    assert (
        alpha.get(
            "/private/exomem/v1/download", headers=alpha_headers, params={"path": relative}
        ).content
        == b"ALPHA-DOWNLOAD"
    )
    assert (
        bravo.get(
            "/private/exomem/v1/download", headers=bravo_headers, params={"path": relative}
        ).content
        == b"BRAVO-DOWNLOAD"
    )

    symlink = alpha_config.vault_root / "Knowledge Base" / "Notes" / "link.md"
    symlink.symlink_to("shared.md")
    symlink_response = alpha.get(
        "/private/exomem/v1/download",
        headers=alpha_headers,
        params={"path": "Knowledge Base/Notes/link.md"},
    )
    assert symlink_response.status_code == 400
    assert symlink_response.json()["error"]["code"] == "INVALID_PATH"
    assert "ALPHA-DOWNLOAD" not in symlink_response.text

    oversized_target = alpha_config.vault_root / "Knowledge Base" / "Notes" / "large.bin"
    oversized_target.write_bytes(b"x" * 65)
    oversized_download = alpha.get(
        "/private/exomem/v1/download",
        headers=alpha_headers,
        params={"path": "Knowledge Base/Notes/large.bin"},
    )
    assert oversized_download.status_code == 413
    assert oversized_download.json()["error"]["code"] == "HOSTED_TRANSFER_LIMIT_INVALID"

    cross_cell = bravo.get(
        "/private/exomem/v1/download",
        headers={**bravo_headers, gateway.TRANSFER_GRANT_HEADER: alpha_grant},
        params={"path": relative},
    )
    wrong_operation = alpha.post(
        "/private/exomem/v1/upload",
        headers=alpha_headers,
        files={"file": ("x.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
    )
    expired = gateway.mint_transfer_grant(
        alpha_config,
        tenant_scope="tenant-alpha",
        principal_scope=SHARED_PRINCIPAL,
        operation="download",
        jti="download-expired",
        max_bytes=64,
        now=1,
        ttl_seconds=1,
    )
    expired_response = alpha.get(
        "/private/exomem/v1/download",
        headers={**alpha_headers, gateway.TRANSFER_GRANT_HEADER: expired},
        params={"path": relative},
    )
    for response in (cross_cell, wrong_operation, expired_response):
        assert response.status_code in {401, 403}, response.text
        assert "ALPHA-DOWNLOAD" not in response.text
        assert "BRAVO-DOWNLOAD" not in response.text


def test_hosted_server_build_skips_personal_oauth_assets_rest_and_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": "cell-alpha",
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
        "EXOMEM_HOSTED_STATE_ROOT": str(tmp_path / "state"),
        "EXOMEM_LOG_DIR": str(tmp_path / "logs"),
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": "alpha-private-service-credential-0001",
    }
    config = HostedCellConfig.from_env(values)
    provision_hosted_cell(config)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    personal_ingress = {
        "EXOMEM_BASE_URL": "https://personal-ingress.invalid",
        "EXOMEM_CF_ACCESS_AUD": "cf-audience-sentinel",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.invalid",
        "EXOMEM_GITHUB_USERNAME": "github-user-sentinel",
        "EXOMEM_LARGE_UPLOAD_BASE_URL": "https://large-ingress.invalid",
        "EXOMEM_REST_API_KEY": "rest-key-sentinel",
        "EXOMEM_UPLOAD_TOKEN": "upload-token-sentinel",
        "EXOMEM_WRITER_LEASE_PREFERRED": "1",
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "foreign-replica-sentinel",
        "EXOMEM_WRITER_LEASE_TIMEOUT": "99",
        "EXOMEM_WRITER_LEASE_TOKEN": "foreign-writer-token-sentinel",
        "EXOMEM_WRITER_LEASE_TTL": "99",
        "EXOMEM_WRITER_LEASE_URL": "https://foreign-coordinator.invalid",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "foreign-vault-sentinel",
        "GITHUB_CLIENT_ID": "github-client-id-sentinel",
        "GITHUB_CLIENT_SECRET": "github-client-secret-sentinel",
    }
    for key, value in personal_ingress.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("exomem.writer_lease.start_server_lifecycle", lambda: None)
    monkeypatch.setattr(
        server_runtime,
        "probe_hosted_mutation_authority",
        lambda _vault_root: (True, "HOSTED_READY"),
    )
    monkeypatch.setattr(
        server,
        "build_oauth",
        lambda **_kwargs: pytest.fail("hosted build called consumer OAuth"),
    )
    monkeypatch.setattr(
        server,
        "register_asset_routes",
        lambda *_args, **_kwargs: pytest.fail("hosted build registered public assets"),
    )
    monkeypatch.setattr(
        server,
        "register_oauth_metadata_route",
        lambda *_args, **_kwargs: pytest.fail("hosted build registered OAuth metadata"),
    )
    monkeypatch.setattr(
        server,
        "register_transfer_routes",
        lambda *_args, **_kwargs: pytest.fail("hosted build registered personal transfer"),
    )
    monkeypatch.setattr(
        server,
        "register_rest_facade",
        lambda *_args, **_kwargs: pytest.fail("hosted build registered personal REST"),
    )

    app = server.build_server(require_auth=True)
    assert asyncio.run(app.list_tools()) == []
    assert all(key not in hosted_runtime.os.environ for key in personal_ingress)
    client = _ASGIClient(app.http_app())
    headers = _headers(config)

    assert client.get("/private/exomem/v1/live", headers=headers).status_code == 200
    contract = client.get("/private/exomem/v1/contract", headers=headers).json()
    contract_commands = {command["name"] for command in contract["commands"]}
    assert {"adopt_vault", "transfer_artifact"} <= contract_commands
    direct_transfer = client.post(
        "/private/exomem/v1/command/transfer_artifact",
        headers=headers,
        json={"operation": "upload"},
    )
    assert direct_transfer.status_code == 409
    assert direct_transfer.json()["error"]["code"] == "HOSTED_TRANSFER_INTERCEPT_REQUIRED"
    assert not any(value in direct_transfer.text for value in personal_ingress.values())
    import_path_sentinel = "/private/foreign-cell/import-sentinel"
    direct_import = client.post(
        "/private/exomem/v1/command/adopt_vault",
        headers=headers,
        json={"path": import_path_sentinel, "include_hidden": True},
    )
    assert direct_import.status_code == 409
    assert direct_import.json()["error"]["code"] == "HOSTED_IMPORT_INTERCEPT_REQUIRED"
    assert import_path_sentinel not in direct_import.text
    for path in (
        "/api/openapi.json",
        "/upload",
        "/download",
        "/studio/",
        "/favicon.svg",
        "/.well-known/oauth-authorization-server",
    ):
        assert client.get(path, headers=headers).status_code == 404, path
    assert client.post("/mcp").status_code == 401

    attempted_mcp_bypass = client.request_with_lifespan(
        "POST",
        "/mcp",
        headers={
            "Authorization": f"Bearer {config.service_credential}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "remember",
                "arguments": _remember_body("MCP-BYPASS-MUST-NOT-WRITE"),
            },
        },
    )
    assert attempted_mcp_bypass.status_code >= 400 or "error" in attempted_mcp_bypass.text
    assert "MCP-BYPASS-MUST-NOT-WRITE" not in "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in config.vault_root.rglob("*.md")
    )
