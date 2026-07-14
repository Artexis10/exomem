from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from exomem import hosted_gateway as gateway
from exomem import hosted_transfer, hosted_transfer_routes
from exomem.hosted_runtime import (
    HostedCellConfig,
    HostedCellLifecycle,
    HostedConfigError,
    HostedLifecycleError,
    HostedResourceLimits,
)
from exomem.server_hosted import register_hosted_routes

ORIGIN = "https://substratesystems.io"
TRANSFER_HOST = "transfer.substratesystems.io"
KID = "credential-7"
CREDENTIAL = "transfer-signing-credential-with-at-least-thirty-two-bytes"
PRINCIPAL = base64.urlsafe_b64encode(hashlib.sha256(b"principal").digest()).rstrip(b"=").decode()
JTI = "11111111-1111-4111-8111-111111111111"
NOW = int(time.time())


class Replay(RuntimeError):
    code = "HOSTED_JTI_REPLAY"


class SecurityUnavailable(RuntimeError):
    code = "HOSTED_SECURITY_UNAVAILABLE"


class FakeSecurityAuthority:
    def __init__(self) -> None:
        self.accepted = {KID: CREDENTIAL}
        self.consumed: set[str] = set()
        self.consume_calls: list[dict[str, Any]] = []
        self.unavailable = False

    def verify_transfer_signature(
        self, kid: str, ascii_payload: bytes, signature: bytes
    ) -> bool:
        if self.unavailable:
            raise SecurityUnavailable
        credential = self.accepted.get(kid)
        if credential is None:
            return False
        expected = hmac.new(
            credential.encode("utf-8"), ascii_payload, hashlib.sha256
        ).digest()
        return hmac.compare_digest(signature, expected)

    def consume_transfer_jti(self, **values: Any) -> None:
        if self.unavailable:
            raise SecurityUnavailable
        self.consume_calls.append(values)
        jti = str(values["jti"])
        if jti in self.consumed:
            raise Replay
        self.consumed.add(jti)


def _upload_target(data: bytes = b"alpha") -> dict[str, Any]:
    metadata = {
        "category": "documents",
        "content_type": "text/plain",
        "description": None,
        "filename": "alpha.txt",
        "scope": "research",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    return {
        "kind": "upload-v1",
        "metadata": metadata,
        "metadata_sha256": hashlib.sha256(hosted_transfer.canonical_json(metadata)).hexdigest(),
    }


def _grant(
    *,
    operation: str = "upload",
    target: dict[str, Any] | None = None,
    jti: str = JTI,
    origin: str = ORIGIN,
    kid: str = KID,
    issued_at: int = NOW,
    not_before: int = NOW,
    expires_at: int = NOW + 300,
) -> str:
    if target is None:
        target = _upload_target() if operation == "upload" else {
            "kind": "download-v1",
            "path": "Knowledge Base/alpha.txt",
        }
    return hosted_transfer.mint_transfer_grant_v2(
        signing_credential=CREDENTIAL,
        kid=kid,
        origin=origin,
        operation=operation,
        cell_id="cell-alpha",
        principal_scope=PRINCIPAL,
        jti=jti,
        max_bytes=1024,
        target=target,
        issued_at=issued_at,
        not_before=not_before,
        expires_at=expires_at,
    )


def _resign_claims(claims: dict[str, Any], credential: str = CREDENTIAL) -> str:
    raw = hosted_transfer.canonical_json(claims)
    payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        credential.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{payload}.{encoded_signature}"


def _claims(token: str) -> dict[str, Any]:
    payload = token.split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _config(tmp_path: Path) -> HostedCellConfig:
    return HostedCellConfig(
        cell_id="cell-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential="private-service-credential-with-thirty-two-bytes",
        enforce_transfer_v1_compatibility=False,
        transfer_browser_origin=ORIGIN,
        transfer_host=TRANSFER_HOST,
        resource_limits=HostedResourceLimits(
            storage_bytes=1024 * 1024,
            upload_bytes=90 * 1024 * 1024,
            worker_count=0,
        ),
    )


def _app(
    tmp_path: Path,
    security: FakeSecurityAuthority,
    *,
    preserve_stream_func: Any | None = None,
    config_value: HostedCellConfig | None = None,
    runtime_temp_authority: Any | None = None,
) -> tuple[Any, HostedCellConfig, HostedCellLifecycle]:
    from exomem.init import init_vault
    from exomem.schema import load_source_schema

    config = config_value or _config(tmp_path)
    init_vault(config.vault_root)
    config.state_root.mkdir(parents=True, exist_ok=True)
    config.log_root.mkdir(parents=True, exist_ok=True)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    app = FastMCP("transfer-v2")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=load_source_schema(config.vault_root),
        transfer_security_authority=security,
        preserve_stream_func=preserve_stream_func,
        runtime_temp_authority=runtime_temp_authority,
    )
    return app.http_app(), config, lifecycle


async def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=f"https://{TRANSFER_HOST}",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_checked_in_transfer_contract_is_complete_golden() -> None:
    path = (
        Path(__file__).parents[1]
        / "openspec/changes/complete-hosted-runtime-deployment-contract/contracts"
        / "hosted-transfer-v2.json"
    )
    raw = path.read_bytes()

    assert hashlib.sha256(raw).hexdigest() == (
        "d886f491aa9ac47b926e16b77a8862b415625e9dc52df54a692ace10eaaeb93e"
    )
    assert hosted_transfer.load_transfer_contract(path) == json.loads(raw)


def test_implemented_public_error_catalog_exactly_matches_normative_artifact() -> None:
    path = (
        Path(__file__).parents[1]
        / "openspec/changes/complete-hosted-runtime-deployment-contract/contracts"
        / "hosted-transfer-v2.json"
    )
    catalog = hosted_transfer.load_transfer_contract(path)["responses"]["error"]["catalog"]
    implemented = {
        code: {
            "status": values[0],
            "message": values[1],
            "retryable": values[2],
            "requires_new_grant": values[3],
        }
        for code, values in hosted_transfer_routes._ERROR_CATALOG.items()
    }
    assert implemented == {
        code: {
            "status": values["status"],
            "message": values["message"],
            "retryable": values["retryable"],
            "requires_new_grant": values["requires_new_grant"],
        }
        for code, values in catalog.items()
    }


def test_grant_v2_has_exact_canonical_claims_and_uses_versioned_authority() -> None:
    token = _grant()
    claims = _claims(token)

    assert list(sorted(claims)) == sorted(
        [
            "v",
            "aud",
            "kid",
            "origin",
            "op",
            "method",
            "cell",
            "principal",
            "iat",
            "nbf",
            "exp",
            "jti",
            "limits",
            "target",
        ]
    )
    assert claims == {
        "aud": "exomem-hosted-transfer",
        "cell": "cell-alpha",
        "exp": NOW + 300,
        "iat": NOW,
        "jti": JTI,
        "kid": KID,
        "limits": {"max_bytes": 1024},
        "method": "PUT",
        "nbf": NOW,
        "op": "upload",
        "origin": ORIGIN,
        "principal": PRINCIPAL,
        "target": _upload_target(),
        "v": 2,
    }

    authority = FakeSecurityAuthority()
    grant = hosted_transfer.verify_transfer_grant_v2(
        token,
        security_authority=authority,
        expected_origin=ORIGIN,
        expected_operation="upload",
        expected_method="PUT",
        expected_cell_id="cell-alpha",
        upload_limit_bytes=90 * 1024 * 1024,
        storage_limit_bytes=1024 * 1024,
        now=NOW,
    )
    assert grant.credential_version == KID
    assert grant.jti == JTI
    assert grant.upload_metadata == _upload_target()["metadata"]

    authority.accepted.clear()  # models a finalized old credential version
    with pytest.raises(hosted_transfer.TransferGrantRejected):
        hosted_transfer.verify_transfer_grant_v2(
            token,
            security_authority=authority,
            expected_origin=ORIGIN,
            expected_operation="upload",
            expected_method="PUT",
            expected_cell_id="cell-alpha",
            upload_limit_bytes=90 * 1024 * 1024,
            storage_limit_bytes=1024 * 1024,
            now=NOW,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda claims: claims.update(v=1),
        lambda claims: claims.update(extra=True),
        lambda claims: claims.update(origin="https://evil.example"),
        lambda claims: claims.update(method="GET"),
        lambda claims: claims.update(cell="cell-bravo"),
        lambda claims: claims.update(principal="a" * 43),
        lambda claims: claims.update(jti="not-a-uuid"),
        lambda claims: claims["limits"].update(max_bytes=90 * 1024 * 1024 + 1),
        lambda claims: claims["target"]["metadata"].update(filename="../alpha.txt"),
        lambda claims: claims["target"]["metadata"].update(filename="bad\u0085name.txt"),
        lambda claims: claims["target"]["metadata"].update(content_type="not-a-media-type"),
        lambda claims: claims["target"]["metadata"].update(size=1025),
    ],
)
def test_grant_v2_rejects_altered_claim_bindings_before_jti(mutate: Any) -> None:
    claims = _claims(_grant())
    mutate(claims)
    # Keep the metadata digest valid where a nested metadata field was changed;
    # the altered semantic binding must still reject independently.
    if isinstance(claims.get("target"), dict) and isinstance(
        claims["target"].get("metadata"), dict
    ):
        claims["target"]["metadata_sha256"] = hashlib.sha256(
            hosted_transfer.canonical_json(claims["target"]["metadata"])
        ).hexdigest()
    authority = FakeSecurityAuthority()
    with pytest.raises(hosted_transfer.TransferGrantRejected):
        hosted_transfer.verify_transfer_grant_v2(
            _resign_claims(claims),
            security_authority=authority,
            expected_origin=ORIGIN,
            expected_operation="upload",
            expected_method="PUT",
            expected_cell_id="cell-alpha",
            upload_limit_bytes=90 * 1024 * 1024,
            storage_limit_bytes=1024 * 1024,
            now=NOW,
        )


def test_grant_v2_rejects_oversized_noncanonical_and_duplicate_json() -> None:
    authority = FakeSecurityAuthority()
    verify = lambda token: hosted_transfer.verify_transfer_grant_v2(  # noqa: E731
        token,
        security_authority=authority,
        expected_origin=ORIGIN,
        expected_operation="upload",
        expected_method="PUT",
        expected_cell_id="cell-alpha",
        upload_limit_bytes=90 * 1024 * 1024,
        storage_limit_bytes=1024 * 1024,
        now=NOW,
    )
    with pytest.raises(hosted_transfer.TransferGrantRejected):
        verify("a" * 8193)

    claims = _claims(_grant())
    noncanonical = json.dumps(claims, ensure_ascii=False).encode("utf-8")
    payload = base64.urlsafe_b64encode(noncanonical).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        CREDENTIAL.encode(), payload.encode("ascii"), hashlib.sha256
    ).digest()
    with pytest.raises(hosted_transfer.TransferGrantRejected):
        verify(f"{payload}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}")

    canonical = hosted_transfer.canonical_json(claims).decode("utf-8")
    duplicate = canonical[:-1] + ',"v":2}'
    payload = base64.urlsafe_b64encode(duplicate.encode()).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        CREDENTIAL.encode(), payload.encode("ascii"), hashlib.sha256
    ).digest()
    with pytest.raises(hosted_transfer.TransferGrantRejected):
        verify(f"{payload}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}")


@pytest.mark.parametrize(
    ("issued_at", "not_before", "expires_at", "valid"),
    [
        (NOW, NOW + 30, NOW + 31, True),
        (NOW, NOW + 31, NOW + 32, False),
        (NOW + 31, NOW + 31, NOW + 32, False),
        (NOW, NOW, NOW, False),
        (NOW, NOW, NOW + 901, False),
    ],
)
def test_grant_v2_enforces_exact_time_equations(
    issued_at: int,
    not_before: int,
    expires_at: int,
    valid: bool,
) -> None:
    authority = FakeSecurityAuthority()
    try:
        token = _grant(
            issued_at=issued_at,
            not_before=not_before,
            expires_at=expires_at,
        )
    except hosted_transfer.TransferGrantRejected:
        assert valid is False
        return
    call = lambda: hosted_transfer.verify_transfer_grant_v2(  # noqa: E731
        token,
        security_authority=authority,
        expected_origin=ORIGIN,
        expected_operation="upload",
        expected_method="PUT",
        expected_cell_id="cell-alpha",
        upload_limit_bytes=90 * 1024 * 1024,
        storage_limit_bytes=1024 * 1024,
        now=NOW,
    )
    if valid:
        assert call().expires_at == expires_at
    else:
        with pytest.raises(hosted_transfer.TransferGrantRejected):
            call()


def test_public_upload_consumes_before_body_and_returns_exact_envelope(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    events: list[str] = []

    class Result:
        pass

    def preserve(*_args: Any, **kwargs: Any) -> Result:
        events.append("commit")
        assert stat.S_IMODE(os.fstat(kwargs["stream"].fileno()).st_mode) == 0o600
        assert kwargs["stream"].read() == b"alpha"
        return Result()

    app, _config_value, lifecycle = _app(
        tmp_path,
        security,
        preserve_stream_func=preserve,
    )
    response = asyncio.run(
        _request(
            app,
            "PUT",
            "/public/exomem/v2/transfers/upload",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "text/plain",
                "X-Exomem-Transfer-Grant": _grant(),
            },
            content=b"alpha",
        )
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "success": True,
        "data": {
            "operation": "upload",
            "bytes": 5,
            "sha256": hashlib.sha256(b"alpha").hexdigest(),
            "committed": True,
        },
    }
    assert len(security.consume_calls) == 1
    consumed = dict(security.consume_calls[0])
    consumed_at = consumed.pop("consumed_at")
    assert consumed == {
        "cell_id": "cell-alpha",
        "schema_version": 2,
        "kid": KID,
        "jti": JTI,
        "expires_at": NOW + 300,
    }
    assert NOW <= consumed_at < NOW + 300
    assert events == ["commit"]
    assert stat.S_IMODE((tmp_path / "state/tmp/transfers-v2").stat().st_mode) == 0o700
    assert lifecycle.snapshot().active_transfers == 0
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["vary"] == "Origin"
    assert response.headers["cache-control"] == "private, no-store"


def test_public_chunked_upload_without_content_length_succeeds(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    committed: list[bytes] = []

    def preserve(*_args: Any, **kwargs: Any) -> object:
        committed.append(kwargs["stream"].read())
        return object()

    app, _config_value, _lifecycle = _app(
        tmp_path,
        security,
        preserve_stream_func=preserve,
    )

    async def chunks() -> Any:
        yield b"al"
        yield b"pha"

    response = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "text/plain",
                hosted_transfer.TRANSFER_GRANT_HEADER: _grant(),
            },
            content=chunks(),
        )
    )
    assert response.status_code == 201, response.text
    assert committed == [b"alpha"]
    assert security.consumed == {JTI}


def test_public_preflight_is_route_specific_bodyless_and_non_consuming(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, _config_value, _lifecycle = _app(tmp_path, security)

    upload = asyncio.run(
        _request(
            app,
            "OPTIONS",
            "/public/exomem/v2/transfers/upload",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "Content-Type, X-Exomem-Transfer-Grant",
            },
        )
    )
    assert upload.status_code == 204, upload.text
    assert upload.content == b""
    assert upload.headers["access-control-allow-methods"] == "PUT"
    assert upload.headers["access-control-allow-headers"] == (
        "Content-Type, X-Exomem-Transfer-Grant"
    )
    assert upload.headers["access-control-max-age"] == "300"
    assert "access-control-allow-credentials" not in upload.headers
    assert security.consume_calls == []

    hostile = asyncio.run(
        _request(
            app,
            "OPTIONS",
            "/public/exomem/v2/transfers/download",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Exomem-Transfer-Grant",
            },
        )
    )
    assert hostile.status_code == 403
    assert "access-control-allow-origin" not in hostile.headers
    assert security.consume_calls == []

    overbroad = asyncio.run(
        _request(
            app,
            "OPTIONS",
            "/public/exomem/v2/transfers/upload",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": (
                    "Content-Type, X-Exomem-Transfer-Grant, Authorization"
                ),
            },
        )
    )
    assert overbroad.status_code == 400
    assert "access-control-allow-origin" not in overbroad.headers
    assert security.consume_calls == []


def test_v2_temp_startup_removes_only_recognized_stale_uploads(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = config.state_root / "tmp" / "transfers-v2"
    root.mkdir(parents=True)
    stale = root / "upload-11111111-1111-4111-8111-111111111111.tmp"
    stale.write_bytes(b"partial")
    hosted_transfer_routes.cleanup_hosted_transfer_temp(config.state_root)
    assert list(root.iterdir()) == []

    (root / "unknown-user-file").write_bytes(b"must-not-delete")
    with pytest.raises(RuntimeError):
        hosted_transfer_routes.cleanup_hosted_transfer_temp(config.state_root)
    assert (root / "unknown-user-file").read_bytes() == b"must-not-delete"


def test_route_registration_never_cleans_stale_transfer_temp(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = config.state_root / "tmp" / "transfers-v2"
    root.mkdir(parents=True)
    stale = root / "upload-11111111-1111-4111-8111-111111111111.tmp"
    stale.write_bytes(b"partial")

    with pytest.raises(RuntimeError):
        hosted_transfer_routes._prepare_temp_root(config.state_root)

    assert stale.read_bytes() == b"partial"


def test_public_preconsumption_failure_preserves_jti_but_chunked_mismatch_burns_it(
    tmp_path: Path,
) -> None:
    security = FakeSecurityAuthority()
    app, _config_value, _lifecycle = _app(tmp_path, security)

    invalid = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "application/octet-stream",
                hosted_transfer.TRANSFER_GRANT_HEADER: _grant(),
            },
            content=b"alpha",
        )
    )
    assert invalid.status_code == 400
    assert invalid.json() == {
        "success": False,
        "error": {
            "code": "TRANSFER_REQUEST_INVALID",
            "message": "transfer request is invalid",
            "retryable": False,
            "requires_new_grant": False,
        },
    }
    assert security.consume_calls == []

    async def short_body() -> Any:
        yield b"alph"

    consumed = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "text/plain",
                hosted_transfer.TRANSFER_GRANT_HEADER: _grant(),
            },
            content=short_body(),
        )
    )
    assert consumed.status_code == 422
    assert consumed.json()["error"] == {
        "code": "TRANSFER_INTEGRITY_FAILED",
        "message": "transfer integrity verification failed",
        "retryable": False,
        "requires_new_grant": True,
    }
    assert security.consumed == {JTI}
    assert list((tmp_path / "state/tmp/transfers-v2").iterdir()) == []

    replay = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "text/plain",
                hosted_transfer.TRANSFER_GRANT_HEADER: _grant(),
            },
            content=short_body(),
        )
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "TRANSFER_GRANT_REJECTED"


def test_security_authority_unavailable_is_retryable_and_does_not_consume(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    security.unavailable = True
    app, _config_value, _lifecycle = _app(tmp_path, security)
    response = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "text/plain",
                hosted_transfer.TRANSFER_GRANT_HEADER: _grant(),
            },
            content=b"alpha",
        )
    )
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "TRANSFER_SECURITY_UNAVAILABLE",
        "message": "transfer security state is unavailable",
        "retryable": True,
        "requires_new_grant": False,
    }
    assert security.consume_calls == []


def test_closed_lifecycle_rejects_before_jti_consumption(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, _config_value, lifecycle = _app(tmp_path, security)
    lifecycle.quiesce(timeout=1)
    response = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "text/plain",
                hosted_transfer.TRANSFER_GRANT_HEADER: _grant(),
            },
            content=b"alpha",
        )
    )
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "TRANSFER_ADMISSION_CLOSED",
        "message": "transfer admission is closed",
        "retryable": True,
        "requires_new_grant": False,
    }
    assert security.consume_calls == []


def test_public_request_rejects_host_query_bearer_cookie_range_and_duplicate_grant(
    tmp_path: Path,
) -> None:
    security = FakeSecurityAuthority()
    app, _config_value, _lifecycle = _app(tmp_path, security)
    base_headers = {
        "Origin": ORIGIN,
        "Content-Type": "text/plain",
        hosted_transfer.TRANSFER_GRANT_HEADER: _grant(),
    }
    requests = [
        (hosted_transfer.TRANSFER_UPLOAD_PATH + "?path=alpha", base_headers),
        (hosted_transfer.TRANSFER_UPLOAD_PATH, {**base_headers, "Authorization": "Bearer x"}),
        (hosted_transfer.TRANSFER_UPLOAD_PATH, {**base_headers, "Cookie": "x=y"}),
        (hosted_transfer.TRANSFER_UPLOAD_PATH, {**base_headers, "Range": "bytes=0-1"}),
        (
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            {**base_headers, gateway.CELL_HEADER: "cell-alpha"},
        ),
    ]
    for path, headers in requests:
        response = asyncio.run(_request(app, "PUT", path, headers=headers, content=b"alpha"))
        assert response.status_code == 400, (path, response.text)
        assert response.json()["error"]["code"] == "TRANSFER_REQUEST_INVALID"
    assert security.consume_calls == []

    wrong_host = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers={**base_headers, "Host": "evil.example"},
            content=b"alpha",
        )
    )
    assert wrong_host.status_code == 400
    assert security.consume_calls == []

    duplicate_grant = asyncio.run(
        _request(
            app,
            "PUT",
            hosted_transfer.TRANSFER_UPLOAD_PATH,
            headers=[
                ("Origin", ORIGIN),
                ("Content-Type", "text/plain"),
                (hosted_transfer.TRANSFER_GRANT_HEADER, _grant()),
                (hosted_transfer.TRANSFER_GRANT_HEADER, _grant()),
            ],
            content=b"alpha",
        )
    )
    assert duplicate_grant.status_code == 401
    assert duplicate_grant.json()["error"]["code"] == "TRANSFER_GRANT_REJECTED"
    assert security.consume_calls == []


def test_public_download_streams_exact_target_with_bounded_rfc8187_headers(
    tmp_path: Path,
) -> None:
    security = FakeSecurityAuthority()
    app, config, lifecycle = _app(tmp_path, security)
    target = config.vault_root / "Knowledge Base" / "résumé $.txt"
    target.write_bytes(b"download-body")
    grant = _grant(
        operation="download",
        target={"kind": "download-v1", "path": "Knowledge Base/résumé $.txt"},
    )

    response = asyncio.run(
        _request(
            app,
            "GET",
            hosted_transfer.TRANSFER_DOWNLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                hosted_transfer.TRANSFER_GRANT_HEADER: grant,
            },
        )
    )
    assert response.status_code == 200, response.text
    assert response.content == b"download-body"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-length"] == str(len(b"download-body"))
    assert response.headers["content-disposition"] == (
        'attachment; filename="exomem-download"; '
        "filename*=UTF-8''r%C3%A9sum%C3%A9%20$.txt"
    )
    assert response.headers["access-control-expose-headers"] == (
        "Content-Disposition, Content-Length, Content-Type"
    )
    assert security.consumed == {JTI}
    assert lifecycle.snapshot().active_transfers == 0


def test_missing_download_is_existence_neutral_and_burns_grant(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    app, _config_value, _lifecycle = _app(tmp_path, security)
    grant = _grant(
        operation="download",
        target={"kind": "download-v1", "path": "Knowledge Base/private-sentinel.txt"},
    )
    response = asyncio.run(
        _request(
            app,
            "GET",
            hosted_transfer.TRANSFER_DOWNLOAD_PATH,
            headers={
                "Origin": ORIGIN,
                hosted_transfer.TRANSFER_GRANT_HEADER: grant,
            },
        )
    )
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "error": {
            "code": "TRANSFER_TARGET_UNAVAILABLE",
            "message": "transfer target is unavailable",
            "retryable": False,
            "requires_new_grant": True,
        },
    }
    assert "private-sentinel" not in response.text
    assert security.consumed == {JTI}


def test_hosted_transfer_defaults_and_private_v1_deadline_are_fail_closed(
    tmp_path: Path,
) -> None:
    values = {
        "EXOMEM_HOSTED_CELL_ID": "cell-alpha",
        "EXOMEM_VAULT_PATH": str((tmp_path / "vault").resolve()),
        "EXOMEM_HOSTED_STATE_ROOT": str((tmp_path / "state").resolve()),
        "EXOMEM_LOG_DIR": str((tmp_path / "logs").resolve()),
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": "private-service-credential-with-thirty-two-bytes",
        "EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN": ORIGIN,
        "EXOMEM_HOSTED_TRANSFER_HOST": TRANSFER_HOST,
    }
    config = HostedCellConfig.from_env(values)
    assert config.resource_limits.upload_bytes == 90 * 1024 * 1024
    assert config.private_v1_transfer_enabled(now=NOW) is False
    environment: dict[str, str] = {}
    config.apply_process_environment(environment)
    assert environment["TMPDIR"] == str(config.state_root / "tmp" / "runtime")

    build = "2026-07-14T00:00:00Z"
    valid = {
        **values,
        "EXOMEM_RELEASE_BUILD_TIME": build,
        "EXOMEM_HOSTED_TRANSFER_V1_COMPAT_UNTIL": "2026-07-21T00:00:00Z",
    }
    config = HostedCellConfig.from_env(valid)
    assert config.private_v1_transfer_enabled(now=1784591999) is True
    assert config.private_v1_transfer_enabled(now=1784592000) is False

    for deadline in (
        "2026-07-21T00:00:01Z",
        "2026-07-20T23:59:59+00:00",
        "not-a-time",
        "",
    ):
        invalid = {
            **values,
            "EXOMEM_RELEASE_BUILD_TIME": build,
            "EXOMEM_HOSTED_TRANSFER_V1_COMPAT_UNTIL": deadline,
        }
        assert HostedCellConfig.from_env(invalid).private_v1_transfer_enabled(now=NOW) is False

    for invalid_transfer_config in (
        {**values, "EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN": "https://substratesystems.io/"},
        {**values, "EXOMEM_HOSTED_TRANSFER_BROWSER_ORIGIN": "http://substratesystems.io"},
        {**values, "EXOMEM_HOSTED_TRANSFER_HOST": "Transfer.substratesystems.io"},
        {key: value for key, value in values.items() if key != "EXOMEM_HOSTED_TRANSFER_HOST"},
    ):
        with pytest.raises(HostedConfigError) as error:
            HostedCellConfig.from_env(invalid_transfer_config)
        assert getattr(error.value, "code", "") == "HOSTED_TRANSFER_CONFIG_INVALID"


def test_direct_hosted_config_is_also_private_v1_default_off(tmp_path: Path) -> None:
    config = HostedCellConfig(
        cell_id="cell-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential="private-service-credential-with-thirty-two-bytes",
    )

    assert config.private_v1_transfer_enabled(now=NOW) is False


def test_private_v1_routes_authenticate_then_fail_closed_without_deadline(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    config = replace(
        _config(tmp_path),
        enforce_transfer_v1_compatibility=True,
        transfer_v1_compat_until=None,
        signed_release_build_time=None,
    )
    app, config, _lifecycle = _app(tmp_path, security, config_value=config)
    unauthorized = asyncio.run(_request(app, "POST", "/private/exomem/v1/upload"))
    assert unauthorized.status_code == 401

    authorized = asyncio.run(
        _request(
            app,
            "POST",
            "/private/exomem/v1/upload",
            headers={
                "Authorization": f"Bearer {config.service_credential}",
                gateway.CELL_HEADER: config.cell_id,
                gateway.PROTOCOL_HEADER: config.protocol_version,
                gateway.REQUEST_HEADER: "22222222-2222-4222-8222-222222222222",
                gateway.PRINCIPAL_HEADER: PRINCIPAL,
            },
        )
    )
    assert authorized.status_code == 404
    assert authorized.json()["error"]["code"] == "HOSTED_TRANSFER_V1_DISABLED"


def test_private_v1_compatibility_caps_entire_multipart_request_at_four_mib(
    tmp_path: Path,
) -> None:
    security = FakeSecurityAuthority()
    config = replace(
        _config(tmp_path),
        resource_limits=HostedResourceLimits(
            storage_bytes=8 * 1024 * 1024,
            upload_bytes=8 * 1024 * 1024,
            worker_count=0,
        ),
    )
    app, config, _lifecycle = _app(tmp_path, security, config_value=config)
    grant = gateway.mint_transfer_grant(
        config,
        tenant_scope="tenant-alpha",
        principal_scope=PRINCIPAL,
        operation="upload",
        jti="legacy-upload-cap",
        max_bytes=5 * 1024 * 1024,
    )
    response = asyncio.run(
        _request(
            app,
            "POST",
            "/private/exomem/v1/upload",
            headers={
                "Authorization": f"Bearer {config.service_credential}",
                gateway.CELL_HEADER: config.cell_id,
                gateway.PROTOCOL_HEADER: config.protocol_version,
                gateway.REQUEST_HEADER: "33333333-3333-4333-8333-333333333333",
                gateway.PRINCIPAL_HEADER: PRINCIPAL,
                gateway.TRANSFER_GRANT_HEADER: grant,
                "Idempotency-Key": "legacy-upload-cap",
            },
            files={"file": ("large.bin", b"x" * (4 * 1024 * 1024), "application/octet-stream")},
            data={"scope": "research", "category": "documents"},
        )
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "TOO_LARGE"


def test_private_v1_upload_reserves_the_shared_runtime_temp_quota(tmp_path: Path) -> None:
    security = FakeSecurityAuthority()
    config = replace(
        _config(tmp_path),
        resource_limits=HostedResourceLimits(
            storage_bytes=8 * 1024 * 1024,
            upload_bytes=8 * 1024 * 1024,
            worker_count=0,
        ),
    )
    runtime_root = config.state_root / "tmp" / "runtime"
    runtime_root.mkdir(parents=True)
    existing = runtime_root / "existing-worker.tmp"
    existing.write_bytes(b"x" * (13 * 1024 * 1024))
    existing.chmod(0o600)
    app, config, _lifecycle = _app(tmp_path, security, config_value=config)
    grant = gateway.mint_transfer_grant(
        config,
        tenant_scope="tenant-alpha",
        principal_scope=PRINCIPAL,
        operation="upload",
        jti="legacy-runtime-quota",
        max_bytes=4 * 1024 * 1024,
    )

    response = asyncio.run(
        _request(
            app,
            "POST",
            "/private/exomem/v1/upload",
            headers={
                "Authorization": f"Bearer {config.service_credential}",
                gateway.CELL_HEADER: config.cell_id,
                gateway.PROTOCOL_HEADER: config.protocol_version,
                gateway.REQUEST_HEADER: "44444444-4444-4444-8444-444444444444",
                gateway.PRINCIPAL_HEADER: PRINCIPAL,
                gateway.TRANSFER_GRANT_HEADER: grant,
                "Idempotency-Key": "legacy-runtime-quota",
            },
            files={
                "file": (
                    "large.bin",
                    b"y" * (3 * 1024 * 1024),
                    "application/octet-stream",
                )
            },
            data={"scope": "research", "category": "documents"},
        )
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "HOSTED_TRANSFER_UNAVAILABLE"
    assert existing.stat().st_size == 13 * 1024 * 1024


def test_private_v1_allows_only_one_active_upload(tmp_path: Path) -> None:
    admitted = threading.Event()
    release = threading.Event()

    class BlockingReservation:
        def __enter__(self) -> Path:
            admitted.set()
            assert release.wait(timeout=5)
            return tmp_path

        def __exit__(self, *_args: object) -> None:
            return None

    class BlockingRuntimeTempAuthority:
        def reserve(self, _maximum_bytes: int) -> BlockingReservation:
            return BlockingReservation()

    security = FakeSecurityAuthority()
    app, config, _lifecycle = _app(
        tmp_path,
        security,
        runtime_temp_authority=BlockingRuntimeTempAuthority(),
    )

    def upload(jti: str, request_id: str) -> httpx.Response:
        grant = gateway.mint_transfer_grant(
            config,
            tenant_scope="tenant-alpha",
            principal_scope=PRINCIPAL,
            operation="upload",
            jti=jti,
            max_bytes=1024,
        )
        return asyncio.run(
            _request(
                app,
                "POST",
                "/private/exomem/v1/upload",
                headers={
                    "Authorization": f"Bearer {config.service_credential}",
                    gateway.CELL_HEADER: config.cell_id,
                    gateway.PROTOCOL_HEADER: config.protocol_version,
                    gateway.REQUEST_HEADER: request_id,
                    gateway.PRINCIPAL_HEADER: PRINCIPAL,
                    gateway.TRANSFER_GRANT_HEADER: grant,
                    "Idempotency-Key": jti,
                },
                files={"file": ("small.bin", b"small", "application/octet-stream")},
                data={"scope": "research", "category": "documents"},
            )
        )

    first_result: list[httpx.Response] = []
    first = threading.Thread(
        target=lambda: first_result.append(
            upload("legacy-first", "55555555-5555-4555-8555-555555555555")
        )
    )
    first.start()
    assert admitted.wait(timeout=5)
    second = upload("legacy-second", "66666666-6666-4666-8666-666666666666")
    release.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert first_result[0].status_code == 201
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "HOSTED_TRANSFER_UNAVAILABLE"


def test_quiesce_drains_admitted_transfer_and_closes_new_transfer_admission(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.state_root.mkdir(parents=True)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    transfer = lifecycle.admit_public_transfer()
    transfer.__enter__()
    completed = threading.Event()
    errors: list[BaseException] = []

    def quiesce() -> None:
        try:
            lifecycle.quiesce(timeout=2)
            completed.set()
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - assertion evidence
            errors.append(exc)

    thread = threading.Thread(target=quiesce, daemon=True)
    thread.start()
    deadline = time.monotonic() + 1
    while lifecycle.readiness().phase != "quiescing":
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert completed.is_set() is False
    with pytest.raises(HostedLifecycleError) as closed:
        with lifecycle.admit_public_transfer():
            pass
    assert getattr(closed.value, "code", "") == "HOSTED_TRANSFER_NOT_ADMITTED"

    transfer.__exit__(None, None, None)
    thread.join(1)
    assert completed.is_set() is True
    assert errors == []
